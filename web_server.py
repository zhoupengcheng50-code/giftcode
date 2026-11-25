import asyncio
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from urllib.request import urlopen
from uuid import uuid4
import webbrowser

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from auth_manager import (
    add_user as auth_add_user,
    authenticate as auth_authenticate,
    create_session_token,
    delete_user as auth_delete_user,
    list_users as auth_list_users,
    verify_session_token,
)
from auto_gift_code import GiftCodeAutomator, batch_suffix, is_batch_date
from credentials import load as load_credentials
from unified_parser import TIERS, parse_any_file, parse_text_rows, parse_tiers, reward_name

WORK_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app):
    if "--open" in sys.argv:
        browser_timer = threading.Timer(1, webbrowser.open, args=("http://127.0.0.1:8888",))
        browser_timer.daemon = True
        browser_timer.start()
    yield


app = FastAPI(title="越南礼包码自动化工作台", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
LOCK = threading.Lock()
STATUS = {"running": False, "mode": "", "progress": 0, "tier": "", "action": "", "logs": [], "error": None}
PREVIEW = None
RESULT = None


@app.middleware("http")
async def local_request(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        if origin:
            from urllib.parse import urlparse
            origin_host = urlparse(origin).netloc.lower().split(':')[0]
            req_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).lower().split(':')[0]
            if origin_host != req_host and origin_host not in ("127.0.0.1", "localhost", "0.0.0.0"):
                return JSONResponse({"detail": "非法请求来源"}, status_code=403)

    path = request.url.path
    public_paths = ("/login", "/api/auth/login", "/api/auth/logout")
    if path not in public_paths:
        token = request.cookies.get("wb_session")
        user = verify_session_token(token)
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "未授权访问，请先登录"}, status_code=401)
            return RedirectResponse(url="/login", status_code=302)
        request.state.user = user
        if path.startswith("/api/users") and user.get("role") != "admin":
            return JSONResponse({"detail": "仅最高权限管理员可管理账号"}, status_code=403)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/auth/login")
async def api_login(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求格式无效")
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    ok, role, msg = auth_authenticate(username, password, _client_ip(request))
    if not ok:
        return JSONResponse({"detail": msg}, status_code=401)
    token = create_session_token(username, role)
    resp = JSONResponse({"ok": True, "username": username, "role": role})
    is_https = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    resp.set_cookie(
        key="wb_session",
        value=token,
        max_age=24 * 3600,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("wb_session", path="/")
    return resp


@app.get("/api/auth/me")
def api_me(request: Request):
    user = getattr(request.state, "user", {})
    return {"username": user.get("sub", ""), "role": user.get("role", "user")}


@app.get("/api/users")
def api_list_users():
    return {"users": auth_list_users()}


@app.post("/api/users")
async def api_add_user(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求格式无效")
    ok, msg = auth_add_user(str(payload.get("username", "")), str(payload.get("password", "")))
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "users": auth_list_users()}


@app.delete("/api/users/{username}")
def api_delete_user(username: str):
    ok, msg = auth_delete_user(username)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "users": auth_list_users()}


@app.get("/api/config")
def config():
    try:
        saved = bool(load_credentials()[0])
    except OSError:
        saved = False
    return {"credentials_saved": saved}


def invalid(exc):
    return HTTPException(400, str(exc))


UPLOAD_TIMEZONE = timezone(timedelta(hours=8))
FILENAME_DATE = re.compile(
    r"(?<![0-9])(?:(?P<compact>20[0-9]{6})|"
    r"(?:(?P<year>20[0-9]{2})[年._/-])?(?P<month>[0-9]{1,2})[月._/-](?P<day>[0-9]{1,2})日?)(?![0-9])"
)


def filename_batch_date(filename, upload_day):
    for match in FILENAME_DATE.finditer(Path(filename).stem):
        if match["compact"]:
            if is_batch_date(match["compact"]):
                return datetime.strptime(match["compact"], "%Y%m%d").date()
            continue
        years = [int(match["year"])] if match["year"] else range(upload_day.year - 1, upload_day.year + 2)
        dates = []
        for year in years:
            try:
                dates.append(date(year, int(match["month"]), int(match["day"])))
            except ValueError:
                continue
        if dates:
            return min(dates, key=lambda value: abs(value - upload_day))
    return None


def upload_defaults(filename, uploaded_at):
    named_date = filename_batch_date(filename, uploaded_at.date())
    batch = named_date or uploaded_at.date()
    year, month = (uploaded_at.year + 1, 1) if uploaded_at.month == 12 else (uploaded_at.year, uploaded_at.month + 1)
    day = min(uploaded_at.day, monthrange(year, month)[1])
    expires = uploaded_at.replace(year=year, month=month, day=day)
    return {
        "activity_name": f"越南{batch.year}网页支付累充礼包码{batch:%Y%m%d}",
        "batch_date": f"{batch:%Y%m%d}",
        "expire_time": expires.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_from_filename": named_date is not None,
    }


@app.post("/api/upload_docx")
async def upload(file: UploadFile = File(...)):
    uploaded_at = datetime.now(UPLOAD_TIMEZONE)
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".docx", ".xlsx", ".xls", ".csv", ".txt"}:
        raise HTTPException(400, "请选择支持的奖励文档")
    data = await file.read(10_000_001)
    if len(data) > 10_000_000:
        raise HTTPException(413, "文件不能超过 10 MB")
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("upload" + suffix)
            path.write_bytes(data)
            rewards = await asyncio.to_thread(parse_any_file, path)
        return {"rewards": rewards, "defaults": upload_defaults(filename, uploaded_at)}
    except Exception as exc:
        raise invalid(exc) from exc


@app.post("/api/parse_text")
def parse_text(data: dict):
    if not isinstance(data.get("text"), str):
        raise HTTPException(400, "请输入档位与奖励")
    try:
        return {"rewards": parse_tiers(parse_text_rows(data["text"]))}
    except ValueError as exc:
        raise invalid(exc) from exc


def validate_job(data):
    name = data.get("activity_name")
    expiry = data.get("expire_time")
    count = data.get("count")
    if not isinstance(name, str) or not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f]{1,100}', name.strip()) or name.strip().endswith("."):
        raise ValueError("请填写有效的本次活动名称")
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name.strip()):
        raise ValueError("活动名称不能作为 Windows 文件名")
    named_batch = batch_suffix(name.strip())
    supplied_batch = data.get("batch_date", "")
    if not isinstance(supplied_batch, str):
        raise ValueError("本期批次日期必须为 YYYYMMDD")
    batch = supplied_batch.strip() or named_batch
    if not is_batch_date(batch):
        raise ValueError("请填写本期批次日期（YYYYMMDD），或把日期放在活动名称末尾")
    if named_batch and named_batch != batch:
        raise ValueError("活动名称末尾的日期与本期批次日期不一致")
    if not isinstance(expiry, str):
        raise ValueError("请填写礼包码失效时间")
    expiry = expiry.strip()
    try:
        parsed_expiry = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("失效时间格式应为 YYYY-MM-DD HH:MM:SS") from exc
    if parsed_expiry.isoformat(sep=" ") != expiry:
        raise ValueError("失效时间格式应为 YYYY-MM-DD HH:MM:SS")
    if type(count) is not int or not 1 <= count <= 65535:
        raise ValueError("单档生成数量必须是 1–65535 的整数")
    rewards = data.get("rewards")
    if not isinstance(rewards, dict) or set(rewards) != set(TIERS):
        raise ValueError("请上传包含八档奖励的活动文档")
    for tier, items in rewards.items():
        if not isinstance(items, dict) or not items:
            raise ValueError(f"{tier} 档奖励为空")
        for name_in_doc, quantity in items.items():
            if not isinstance(name_in_doc, str) or not reward_name(name_in_doc) or type(quantity) is not int or quantity <= 0:
                raise ValueError(f"{tier} 档道具名称或数量无效：{name_in_doc}")
        names = {reward_name(item) for item in items}
        if len(names) != len(items):
            raise ValueError(f"{tier} 档含重复的道具名称")
    job = {"activity_name": name.strip(), "expire_time": expiry, "count": count,
           "rewards": {tier: rewards[tier] for tier in TIERS}}
    if batch != named_batch:
        job["batch_date"] = batch
    job_key = hashlib.sha256(json.dumps(job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    job["batch_date"] = batch
    job["directory"] = WORK_DIR / "jobs" / job_key
    user, password = data.get("username", ""), data.get("password", "")
    if not isinstance(user, str) or not isinstance(password, str) or bool(user.strip()) != bool(password):
        raise ValueError("首次登录时请同时填写后台账号和密码")
    if user.strip():
        job["credentials"] = user.strip(), password
    return job


def resume_progress(job):
    directory = job["directory"]
    if (directory / "progress.json").exists():
        candidates = [directory]
    else:
        candidates = []
        for path in directory.parent.glob("*/progress.json"):
            manifest = path.parent / "preview.json"
            if not manifest.exists() or (path.parent / "completed.json").exists():
                continue
            plan = json.loads(manifest.read_text(encoding="utf-8"))
            tiers = plan.get("tiers", {})
            same_batch = plan.get("batch_date") == job["batch_date"] and tiers.keys() == job["rewards"].keys()
            if same_batch and all(
                item["target"] == f"{job['activity_name']}-{tier}档"
                and {row["name"]: row["expected"] for row in item["items"] if row["expected"] is not None}
                == job["rewards"][tier]
                for tier, item in tiers.items()
            ):
                candidates.append(path.parent)
    if not candidates:
        return directory
    if len(candidates) != 1:
        raise RuntimeError("找到多个同名未完成任务，无法确认应恢复哪一个。")
    progress_dir = candidates[0]
    state = json.loads((progress_dir / "progress.json").read_text(encoding="utf-8"))
    if any(tier.get("submitted") for tier in state.values()):
        binding = progress_dir / "execution.json"
        previous = json.loads(binding.read_text(encoding="utf-8")) if binding.exists() else {}
        if previous.get("job", progress_dir.name) != directory.name:
            message = "此活动已提交过发码，恢复时须沿用原生成数量和失效时间。"
            if previous:
                message += f" 原每档数量 {previous['count']}，原失效时间 {previous['expire_time']}。"
            raise RuntimeError(message)
    return progress_dir


def start_worker(mode, job):
    global PREVIEW, RESULT
    def log(message, level="INFO"):
        with LOCK:
            STATUS["logs"].append({"text": message, "level": level})

    def progress(percent, tier, action):
        with LOCK:
            STATUS.update(progress=percent, tier=tier, action=action)

    try:
        job["directory"].mkdir(parents=True, exist_ok=True)
        manifest = job["directory"] / "preview.json"
        job["progress_directory"] = resume_progress(job)
        progress_dir = job["progress_directory"]
        if (progress_dir / "progress.json").exists():
            if mode == "preview":
                job["previous_plan"] = json.loads((progress_dir / "preview.json").read_text(encoding="utf-8"))
            if progress_dir != job["directory"]:
                log("找到同批次未完成任务，将继续核对已复制的活动")
        if mode == "execute":
            execution = progress_dir / "execution.json"
            temporary = execution.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "job": job["directory"].name, "count": job["count"], "expire_time": job["expire_time"],
            }), encoding="utf-8")
            temporary.replace(execution)
        value = GiftCodeAutomator(job, WORK_DIR, log, progress).run(mode)
        if mode == "preview":
            temporary = manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(manifest)
            with LOCK:
                PREVIEW = {"token": uuid4().hex, "path": job["directory"], "value": value}
        else:
            with LOCK:
                RESULT = Path(value)
        progress(100, "", "往期配置已核对" if mode == "preview" else "礼包码已全部核验并导出")
    except Exception as exc:
        log(str(exc), "ERROR")
        with LOCK:
            STATUS.update(error=str(exc), action="已停止")
    finally:
        with LOCK:
            STATUS["running"] = False


def enqueue(mode, data):
    global PREVIEW, RESULT
    try:
        job = validate_job(data)
    except ValueError as exc:
        raise invalid(exc) from exc
    with LOCK:
        if STATUS["running"]:
            raise HTTPException(409, "已有任务正在执行")
        if mode == "execute":
            if not PREVIEW or PREVIEW["token"] != data.get("preview_token") or PREVIEW["path"] != job["directory"]:
                raise HTTPException(409, "活动参数或奖励已变化，请重新核对往期")
            job["plan"] = PREVIEW["value"]
            if any(row["status"] == "review" for tier in job["plan"]["tiers"].values() for row in tier["items"]):
                raise HTTPException(409, "预览中仍有待核对的奖励")
        else:
            PREVIEW = None
        RESULT = None
        STATUS.update(running=True, mode=mode, progress=0, tier="", action="启动可见浏览器", logs=[], error=None)
    threading.Thread(target=start_worker, args=(mode, job), daemon=True).start()
    return {"status": "started"}


@app.post("/api/preview")
def preview(data: dict):
    return enqueue("preview", data)


@app.post("/api/start")
def start(data: dict):
    return enqueue("execute", data)


@app.get("/api/status")
def status():
    with LOCK:
        preview = {"token": PREVIEW["token"], **PREVIEW["value"]} if PREVIEW else None
        return {**STATUS, "logs": STATUS["logs"][:], "preview": preview, "download": RESULT is not None}


@app.get("/api/download/xls")
def download():
    with LOCK:
        result = RESULT
    if result is None or not result.is_file():
        raise HTTPException(404, "本次任务还没有可用文件")
    return FileResponse(result, filename=result.name, media_type="application/vnd.ms-excel")


WORKBENCH_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>越南后台GMS礼包码自动配置工作台</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --success: #16a34a;
      --warning: #d97706;
      --danger: #dc2626;
      --bg: #0f172a;
      --card-bg: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 12px;
      -webkit-text-size-adjust: 100%;
    }
    @media (min-width: 768px) {
      body { padding: 24px; }
    }
    .container { max-width: 1200px; margin: 0 auto; width: 100%; }
    header {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 20px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    @media (min-width: 640px) {
      header {
        flex-direction: row;
        justify-content: space-between;
        align-items: center;
      }
    }
    .title-box h1 {
      font-size: 19px;
      font-weight: 700;
      color: #38bdf8;
      display: flex;
      align-items: center;
      gap: 8px;
      word-break: break-word;
    }
    @media (min-width: 768px) {
      .title-box h1 { font-size: 24px; gap: 10px; }
    }
    .title-box p { color: var(--text-muted); font-size: 13px; margin-top: 4px; }
    .badge {
      display: inline-block;
      padding: 5px 12px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
    }
    .badge-idle { background: #334155; color: #94a3b8; }
    .badge-running { background: #1e3a8a; color: #60a5fa; animation: pulse 2s infinite; }
    .badge-success { background: #14532d; color: #4ade80; }
    .badge-error { background: #7f1d1d; color: #f87171; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    
    .grid { display: grid; grid-template-columns: 1fr; gap: 16px; margin-bottom: 20px; }
    @media (min-width: 900px) { .grid { grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; } }
    
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }
    @media (min-width: 768px) {
      .card { padding: 20px; }
    }
    .card-title {
      font-size: 15px;
      font-weight: 600;
      margin-bottom: 14px;
      color: #e2e8f0;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    @media (min-width: 768px) {
      .card-title { font-size: 16px; margin-bottom: 16px; }
    }
    
    .dropzone {
      border: 2px dashed #475569;
      border-radius: 8px;
      padding: 20px 12px;
      text-align: center;
      cursor: pointer;
      background: #0f172a;
      transition: all 0.2s;
    }
    @media (min-width: 768px) {
      .dropzone { padding: 26px; }
    }
    .dropzone:hover { border-color: #38bdf8; background: #1e293b; }
    .dropzone-icon { font-size: 28px; margin-bottom: 6px; }
    @media (min-width: 768px) { .dropzone-icon { font-size: 32px; } }
    
    .paste-toggle {
      margin-top: 10px;
      font-size: 13px;
      color: #38bdf8;
      cursor: pointer;
      text-align: center;
      text-decoration: underline;
      padding: 4px;
    }
    .paste-area {
      display: none;
      margin-top: 12px;
    }
    textarea {
      width: 100%;
      height: 110px;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      color: var(--text);
      font-size: 15px;
      font-family: monospace;
      outline: none;
    }
    
    .form-row { display: grid; grid-template-columns: 1fr; gap: 10px; margin-bottom: 10px; }
    @media (min-width: 600px) {
      .form-row { grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
    }
    .form-group { margin-bottom: 8px; }
    @media (min-width: 600px) { .form-group { margin-bottom: 12px; } }
    label { display: block; font-size: 13px; font-weight: 500; color: var(--text-muted); margin-bottom: 6px; }
    input[type="text"], input[type="password"], input[type="number"] {
      width: 100%;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }
    input:focus, textarea:focus { border-color: #38bdf8; }
    .switch-label { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; }
    
    .tiers-container { margin-top: 24px; }
    .tiers-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 16px;
    }
    .tier-card {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
    }
    .tier-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 10px;
    }
    .tier-name { font-weight: 700; font-size: 15px; color: #38bdf8; }
    .item-chip {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #1e293b;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 12px;
      margin-bottom: 6px;
      border: 1px solid #334155;
      transition: all 0.2s;
    }
    .item-qty { font-weight: 700; color: #fbbf24; }

    /* 和上传配置一致的绿色标注样式 */
    .item-chip-matched {
      background: rgba(16, 185, 129, 0.1) !important;
      border: 1px solid rgba(16, 185, 129, 0.5) !important;
    }
    .tag-match-ok {
      color: #34d399 !important;
      font-size: 11px;
      font-weight: 600;
      margin-left: 6px;
    }
    .tier-badge-ok {
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 4px;
      background: rgba(16, 185, 129, 0.2);
      color: #34d399;
      border: 1px solid #10b981;
      font-weight: 600;
    }

    /* 和上传配置不一致的红色警示样式 */
    .item-chip-mismatch {
      background: rgba(239, 68, 68, 0.22) !important;
      border: 1px solid #ef4444 !important;
      box-shadow: 0 0 10px rgba(239, 68, 68, 0.35) !important;
    }
    .item-chip-mismatch .item-name {
      color: #fca5a5 !important;
      font-weight: 700 !important;
    }
    .tag-match-err {
      color: #f87171 !important;
      font-size: 11px;
      font-weight: 700;
      margin-left: 6px;
    }
    .tier-badge-err {
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 4px;
      background: rgba(239, 68, 68, 0.3);
      color: #f87171;
      border: 1px solid #ef4444;
      font-weight: 700;
    }

    /* 尚未执行核验时的上传配置默认样式 */
    .tier-badge-pending {
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 4px;
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      border: 1px solid #0284c7;
      font-weight: 600;
    }
    .tag-pending {
      color: #94a3b8;
      font-size: 11px;
      margin-left: 4px;
    }
    
    .action-bar {
      margin: 20px 0;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    @media (min-width: 640px) {
      .action-bar {
        flex-direction: row;
        align-items: center;
        flex-wrap: wrap;
      }
    }
    .btn {
      padding: 12px 20px;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      border: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 44px;
      transition: all 0.2s;
      width: 100%;
    }
    @media (min-width: 640px) {
      .btn { width: auto; }
    }
    .btn-primary { background: var(--primary); color: white; }
    .btn-primary:hover:not(:disabled) {
      background: var(--primary-hover);
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.4);
    }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-success { background: var(--success); color: white; text-decoration: none; }
    .btn-success:hover { background: #15803d; box-shadow: 0 4px 12px rgba(22, 163, 74, 0.4); }
    
    .progress-box { margin: 16px 0; display: none; }
    .progress-track {
      background: #0f172a;
      height: 10px;
      border-radius: 5px;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    .progress-fill {
      background: linear-gradient(90deg, #38bdf8, #2563eb);
      height: 100%;
      width: 0%;
      transition: width 0.3s;
    }
    .progress-text {
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      margin-top: 6px;
      color: var(--text-muted);
    }
    
    .console-card { margin-top: 24px; }
    .terminal {
      background: #000;
      border-radius: 8px;
      padding: 16px;
      font-family: Consolas, Monaco, "Courier New", monospace;
      font-size: 13px;
      height: 260px;
      overflow-y: auto;
      border: 1px solid #1e293b;
    }
    .log-line { margin-bottom: 4px; line-height: 1.4; }
    .log-time { color: #64748b; }
    .log-INFO { color: #93c5fd; }
    .log-SUCCESS { color: #4ade80; font-weight: bold; }
    .log-WARN { color: #fde047; }
    .log-ERROR { color: #f87171; font-weight: bold; }
    
    .download-card {
      background: linear-gradient(135deg, #064e3b 0%, #0f172a 100%);
      border: 1px solid #059669;
      border-radius: 12px;
      padding: 20px;
      margin-top: 24px;
      display: none;
    }
    .download-card h3 { color: #34d399; font-size: 18px; margin-bottom: 8px; }
    .header-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .btn-sm { padding: 6px 12px; font-size: 12px; border-radius: 6px; min-height: 32px; width: auto; }
    .btn-outline { background: #1e293b; border: 1px solid #475569; color: #38bdf8; }
    .btn-outline:hover { background: #334155; border-color: #38bdf8; }
    .btn-danger-outline { background: rgba(220,38,38,0.12); border: 1px solid #ef4444; color: #fca5a5; }
    .btn-danger-outline:hover { background: rgba(220,38,38,0.25); }
    .modal-overlay {
      position: fixed; inset: 0; background: rgba(0,0,0,0.72); backdrop-filter: blur(4px);
      display: none; align-items: center; justify-content: center; z-index: 999; padding: 16px;
    }
    .modal-card {
      background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px;
      max-width: 480px; width: 100%; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5);
    }
    .user-list { margin-top: 14px; max-height: 240px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
    .user-item {
      display: flex; justify-content: space-between; align-items: center; padding: 8px 12px;
      background: #0f172a; border: 1px solid #334155; border-radius: 6px; font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-box">
        <h1>🎮 越南后台GMS礼包码自动配置工作台</h1>
        <p>支持文档直接解析（.docx/.xlsx/.txt/直接粘贴）、多档位自动配置与合并 .xls 表格直出</p>
      </div>
      <div class="header-actions">
        <span id="current-user-badge" class="badge badge-idle">校验中...</span>
        <button id="manage-users-btn" class="btn btn-sm btn-outline" style="display:none;" onclick="openUsersModal()">👥 账号管理</button>
        <button class="btn btn-sm btn-danger-outline" onclick="logout()">🚪 退出</button>
        <span id="global-badge" class="badge badge-idle">服务在线</span>
      </div>
    </header>

    <div class="grid">
      <div class="card">
        <div class="card-title">📄 1. 上传配置文档 (.docx / .xlsx / .txt)</div>
        <div class="dropzone" id="dropzone" onclick="document.getElementById('file-input').click()">
          <div class="dropzone-icon">📁</div>
          <div style="font-weight: 600; font-size: 15px;">点击或拖拽上传奖励文档 (.docx / .xlsx / .txt)</div>
          <div style="color: #64748b; font-size: 12px; margin-top: 4px;">无需配图，仅需【挡位】与【奖励】两列即可识别 8 大档位</div>
          <input type="file" id="file-input" accept=".docx,.xlsx,.xls,.txt,.csv" style="display: none;" onchange="handleFileSelect(event)">
        </div>
        <div class="paste-toggle" onclick="togglePasteArea()">或点击此处直接粘贴表格文本 📝</div>
        
        <div class="paste-area" id="paste-area">
          <textarea id="paste-text" placeholder="直接粘贴微信/Excel里的文字表格，如：\n| 挡位 | 奖励 |\n| 100.000 | 仙玉 x300 上清精元丹 x3... |"></textarea>
          <button class="btn btn-primary" style="margin-top: 8px; padding: 6px 14px; font-size: 13px;" onclick="handleTextParse()">解析粘贴内容</button>
        </div>
        
        <div id="upload-status" style="margin-top: 10px; font-size: 13px; color: #38bdf8; display: none;"></div>
      </div>

      <div class="card">
        <div class="card-title">⚙️ 2. 活动批次与生成参数</div>
        <div class="form-row">
          <div class="form-group">
            <label>本次活动命名</label>
            <input type="text" id="activity-name" placeholder="越南2026网页支付累充礼包码20261125">
          </div>
          <div class="form-group">
            <label for="batch-date">本期批次日期</label>
            <input type="text" id="batch-date" inputmode="numeric" maxlength="8" placeholder="YYYYMMDD，名称已有日期可留空">
          </div>
          <div class="form-group">
            <label>礼包码失效时间（GMS 时间）</label>
            <input type="text" id="expire-time" placeholder="YYYY-MM-DD HH:MM:SS">
          </div>
          <div class="form-group">
            <label>单档生成数量</label>
            <input type="number" id="code-count" min="1" max="65535" value="100">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>后台账号</label>
            <input type="text" id="username" placeholder="已保存时可留空">
          </div>
          <div class="form-group">
            <label>后台密码</label>
            <input type="password" id="password" placeholder="已保存时可留空" autocomplete="off">
          </div>
        </div>
        <div style="margin-top: 6px;">
          <label class="switch-label">
            <input type="checkbox" id="show-browser" checked disabled>
            <span>执行时显示浏览器窗口</span>
          </label>
        </div>
      </div>
    </div>

    <div class="card tiers-container">
      <div class="card-title" style="justify-content: space-between;">
        <span>🎁 3. 各档位奖励配置预览 (8大档位)</span>
        <span style="font-size: 12px; color: var(--text-muted);" id="tier-stat-badge">请上传活动文档</span>
      </div>
      <div class="tiers-grid" id="tiers-grid"></div>
    </div>

    <div class="action-bar">
      <button class="btn btn-primary" id="preview-btn" onclick="runPreview()">核对往期配置</button>
      <button class="btn btn-primary" id="start-btn" onclick="startAutomation()" disabled>
        <span>🚀 一键生成礼包码配置</span>
      </button>
      <div id="status-text" style="font-size: 14px; color: #94a3b8;">上传文档后核对往期配置</div>
    </div>

    <div class="progress-box" id="progress-box">
      <div class="progress-track">
        <div class="progress-fill" id="progress-fill"></div>
      </div>
      <div class="progress-text">
        <span id="current-action-text">正在初始化...</span>
        <span id="progress-percent-text">0%</span>
      </div>
    </div>

    <div class="download-card" id="download-card">
      <h3>🎉 礼包码全部生成并整合完毕！</h3>
      <p style="font-size: 14px; color: #a7f3d0; margin-bottom: 14px;">
        所有 8 个档位（100~10000）的激活码已成功合并为包含 8 个独立页签的Excel 97–2003 (.xls) 表格。
      </p>
      <div style="display: flex; gap: 12px;">
        <a href="/api/download/xls" class="btn btn-success" download style="text-decoration:none; font-size:16px; padding:12px 28px;">
          <span>📥 下载整合激活码表格 (.xls)</span>
        </a>
      </div>
    </div>

    <div class="card console-card">
      <div class="card-title">🖥️ 实时操作执行日志</div>
      <div class="terminal" id="terminal">
        <div class="log-line"><span class="log-time">[00:00:00]</span> <span class="log-INFO">工作台启动就绪，等待执行指令...</span></div>
      </div>
    </div>
  </div>

  <script>
    let rewards = null;
    let preview = null;
    let running = false;
    let timer;
    let uploadNumber = 0;
    const get = id => document.getElementById(id);
    const formIds = ['activity-name', 'batch-date', 'expire-time', 'code-count', 'username', 'password'];

    function togglePasteArea() {
      const area = get('paste-area');
      area.style.display = area.style.display === 'block' ? 'none' : 'block';
    }

    function element(tag, text, className) {
      const node = document.createElement(tag);
      node.textContent = text;
      if (className) node.className = className;
      return node;
    }

    async function call(url, payload, form = false) {
      const response = await fetch(url, payload === undefined ? {} : {
        method: 'POST',
        headers: form ? {} : {'Content-Type': 'application/json'},
        body: form ? payload : JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '请求失败');
      return data;
    }

    function needsRewardReview() {
      return preview && Object.values(preview.tiers).some(tier => tier.items.some(row => row.status === 'review'));
    }

    function setControls() {
      get('preview-btn').disabled = running || !rewards;
      get('start-btn').disabled = running || !preview || !rewards || needsRewardReview();
      formIds.forEach(id => { get(id).disabled = running; });
      const upload = document.querySelector('input[type=file]');
      upload.disabled = running;
      get('paste-text').disabled = running;
    }

    function render() {
      const grid = get('tiers-grid');
      grid.replaceChildren();
      if (!rewards) {
        get('tier-stat-badge').textContent = '请上传活动文档';
        return;
      }
      const batch = preview ? '本期 ' + preview.batch_date + ' · 上一期 ' + preview.baseline : '已识别 8 档，等待核对往期';
      get('tier-stat-badge').textContent = batch + (needsRewardReview() ? ' · 有奖励待核对' : '');
      Object.entries(rewards).forEach(([tier, items]) => {
        const card = element('div', '', 'tier-card');
        const data = preview?.tiers[tier];
        card.append(element('div', '累充 ' + tier, 'tier-title'));
        if (data) {
          card.append(element('div', data.target));
          card.append(element('small', '往期活动 ID ' + data.source.id));
        }
        const rows = data?.items ?? Object.entries(items).map(([name, expected]) => ({name, expected}));
        for (const row of rows) {
          let message = row.name + ' ×' + (row.expected ?? row.actual);
          if (data) {
            if (row.status === 'same') message += ' · 一致';
            else if (row.status === 'kept') message += ' · 后台已有，保留';
            else if (row.status === 'review') message += ' · 待核对：' + row.reason;
            else if (row.status === 'quantity') message += ' · 往期 ×' + row.actual + ' → 本次 ×' + row.expected;
          }
          const line = element('div', message, 'reward-item');
          if (data) line.style.color = row.status === 'kept' ? '#94a3b8' : row.matched ? '#6ee7b7' : '#fca5a5';
          card.append(line);
        }
        grid.append(card);
      });
    }

    function clearPreview() {
      preview = null;
      get('download-card').style.display = 'none';
      render();
      setControls();
    }

    async function parse(url, body, form) {
      if (running) return;
      const requestNumber = ++uploadNumber;
      rewards = null;
      clearPreview();
      const status = get('upload-status');
      status.style.display = 'block';
      status.textContent = '读取奖励文档…';
      try {
        const parsed = await call(url, body, form);
        if (requestNumber !== uploadNumber) return;
        rewards = parsed.rewards;
        status.textContent = '已识别 8 档奖励';
        if (parsed.defaults) {
          get('activity-name').value = parsed.defaults.activity_name;
          get('batch-date').value = parsed.defaults.batch_date;
          get('expire-time').value = parsed.defaults.expire_time;
          if (!parsed.defaults.batch_from_filename) status.textContent += ' · 文件名无有效日期，批次已按上传日期填写';
        }
      } catch (error) {
        if (requestNumber !== uploadNumber) return;
        status.textContent = error.message;
      }
      render();
      setControls();
    }

    function handleFileSelect(event) {
      const file = event.target.files[0];
      if (!file) return;
      if (event.target instanceof HTMLInputElement) event.target.value = '';
      const body = new FormData();
      body.append('file', file);
      return parse('/api/upload_docx', body, true);
    }

    function handleTextParse() {
      return parse('/api/parse_text', {text: get('paste-text').value}, false);
    }

    async function run(mode) {
      if (running || !rewards) return;
      const payload = {
        activity_name: get('activity-name').value,
        batch_date: get('batch-date').value,
        expire_time: get('expire-time').value,
        count: get('code-count').valueAsNumber,
        username: get('username').value,
        password: get('password').value,
        rewards,
        preview_token: preview?.token
      };
      if (mode === 'preview') clearPreview();
      running = true;
      setControls();
      get('status-text').textContent = '正在启动浏览器';
      get('download-card').style.display = 'none';
      try {
        await call(mode === 'preview' ? '/api/preview' : '/api/start', payload);
        await poll();
      } catch (error) {
        running = false;
        setControls();
        get('status-text').textContent = error.message;
      }
    }

    function runPreview() { return run('preview'); }
    function startAutomation() { return run('execute'); }

    async function poll() {
      clearTimeout(timer);
      try {
        const status = await call('/api/status');
        const terminal = get('terminal');
        terminal.replaceChildren(...status.logs.map(log => element('div', log.text, 'log-' + log.level)));
        terminal.scrollTop = terminal.scrollHeight;
        get('progress-box').style.display = 'block';
        get('progress-fill').style.width = status.progress + '%';
        get('progress-percent-text').textContent = status.progress + '%';
        get('current-action-text').textContent = [status.tier, status.action].filter(Boolean).join(' · ');
        get('status-text').textContent = status.error ?? status.action;
        if (status.running) {
          timer = setTimeout(poll, 1000);
          return;
        }
        running = false;
        if (status.mode === 'preview' && !status.error) {
          preview = status.preview;
          get('username').value = '';
          get('password').value = '';
          const config = await call('/api/config');
          if (config.credentials_saved) get('username').placeholder = '账号已保存，可留空';
        }
        get('download-card').style.display = status.download ? 'block' : 'none';
        render();
        setControls();
      } catch (error) {
        get('status-text').textContent = error.message;
        timer = setTimeout(poll, 1500);
      }
    }

    formIds.forEach(id => get(id).addEventListener('input', clearPreview));
    const zone = get('dropzone');
    if (zone) {
      zone.addEventListener('dragover', event => event.preventDefault());
      zone.addEventListener('drop', event => {
        event.preventDefault();
        if (event.dataTransfer.files.length) handleFileSelect({target: event.dataTransfer});
      });
    }
    setControls();
    call('/api/config').then(status => {
      if (status.credentials_saved) get('username').placeholder = '账号已保存，可留空';
    }).catch(error => { get('status-text').textContent = error.message; });

    call('/api/auth/me').then(me => {
      const badge = get('current-user-badge');
      badge.textContent = (me.role === 'admin' ? '👑 管理员: ' : '👤 ') + me.username;
      if (me.role === 'admin') {
        badge.className = 'badge badge-success';
        get('manage-users-btn').style.display = 'inline-flex';
      }
    }).catch(() => { location.href = '/login'; });

    async function logout() {
      await fetch('/api/auth/logout', { method: 'POST' });
      location.href = '/login';
    }

    function openUsersModal() {
      get('users-modal').style.display = 'flex';
      get('user-msg').textContent = '';
      loadUsers();
    }

    function closeUsersModal() {
      get('users-modal').style.display = 'none';
    }

    async function loadUsers() {
      const data = await call('/api/users');
      const box = get('user-list');
      box.replaceChildren();
      (data.users || []).forEach(u => {
        const item = document.createElement('div');
        item.className = 'user-item';
        const info = document.createElement('div');
        const strong = document.createElement('strong');
        strong.style.color = u.role === 'admin' ? '#4ade80' : '#f8fafc';
        strong.textContent = u.username;
        const roleSpan = document.createElement('span');
        roleSpan.style.cssText = 'color:#64748b;font-size:12px;margin-left:6px;';
        roleSpan.textContent = u.role === 'admin' ? '(最高管理员)' : '(授权用户)';
        info.append(strong, roleSpan);
        item.appendChild(info);
        if (u.role !== 'admin') {
          const delBtn = document.createElement('button');
          delBtn.className = 'btn btn-sm btn-danger-outline';
          delBtn.textContent = '移除';
          delBtn.onclick = async () => {
            if (!confirm(`确认吊销账号「${u.username}」的登录权限？`)) return;
            await fetch(`/api/users/${encodeURIComponent(u.username)}`, { method: 'DELETE' });
            loadUsers();
          };
          item.appendChild(delBtn);
        }
        box.appendChild(item);
      });
    }

    async function addAuthorizedUser() {
      const uname = get('new-auth-user').value.trim();
      const upass = get('new-auth-pass').value;
      const msgEl = get('user-msg');
      msgEl.textContent = '';
      try {
        await call('/api/users', { username: uname, password: upass });
        get('new-auth-user').value = '';
        get('new-auth-pass').value = '';
        msgEl.style.color = '#4ade80';
        msgEl.textContent = '✅ 授权账号添加成功';
        loadUsers();
      } catch (e) {
        msgEl.style.color = '#f87171';
        msgEl.textContent = e.message;
      }
    }
  </script>

  <div class="modal-overlay" id="users-modal" onclick="if(event.target===this) closeUsersModal()">
    <div class="modal-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <h3 style="color:#38bdf8;font-size:17px;">👥 授权账号权限管理</h3>
        <button class="btn btn-sm btn-outline" onclick="closeUsersModal()">关闭</button>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>新授权账号</label>
          <input type="text" id="new-auth-user" placeholder="输入用户名(2~32位)">
        </div>
        <div class="form-group">
          <label>登录密码</label>
          <input type="password" id="new-auth-pass" placeholder="设置密码(至少4位)">
        </div>
      </div>
      <button class="btn btn-primary" style="width:100%;" onclick="addAuthorizedUser()">➕ 添加并授权该账号</button>
      <div id="user-msg" style="font-size:13px;margin-top:8px;"></div>
      <div style="margin-top:16px;font-size:13px;color:#94a3b8;font-weight:600;">当前已授权账号列表：</div>
      <div class="user-list" id="user-list"></div>
    </div>
  </div>
</body>
</html>
"""

LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>安全登录 - 越南后台GMS礼包码配置工作台</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #0f172a;
      color: #f8fafc;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .login-card {
      width: 100%;
      max-width: 400px;
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 14px;
      padding: 28px 24px;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .login-header { text-align: center; margin-bottom: 24px; }
    .login-header h1 { font-size: 20px; color: #38bdf8; margin-bottom: 6px; }
    .login-header p { font-size: 13px; color: #94a3b8; }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 13px; color: #cbd5e1; margin-bottom: 6px; font-weight: 500; }
    input {
      width: 100%;
      padding: 11px 12px;
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      color: #f8fafc;
      font-size: 15px;
      outline: none;
    }
    input:focus { border-color: #38bdf8; }
    .btn {
      width: 100%;
      padding: 12px;
      background: #2563eb;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      min-height: 44px;
      transition: background 0.2s;
    }
    .btn:hover:not(:disabled) { background: #1d4ed8; }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; }
    .err-msg {
      margin-top: 14px;
      padding: 10px 12px;
      background: rgba(220, 38, 38, 0.15);
      border: 1px solid #ef4444;
      border-radius: 8px;
      color: #fca5a5;
      font-size: 13px;
      display: none;
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="login-card">
    <div class="login-header">
      <h1>🔐 工作台安全校验</h1>
      <p>仅限管理员及已授权人员登录访问</p>
    </div>
    <form id="login-form" onsubmit="handleLogin(event)">
      <div class="form-group">
        <label for="u">账号</label>
        <input id="u" type="text" autocomplete="username" required placeholder="请输入授权账号">
      </div>
      <div class="form-group">
        <label for="p">密码</label>
        <input id="p" type="password" autocomplete="current-password" required placeholder="请输入登录密码">
      </div>
      <button type="submit" class="btn" id="submit-btn">验证并进入工作台</button>
      <div class="err-msg" id="err-msg"></div>
    </form>
  </div>
  <script>
    async function handleLogin(e) {
      e.preventDefault();
      const btn = document.getElementById('submit-btn');
      const err = document.getElementById('err-msg');
      err.style.display = 'none';
      btn.disabled = true;
      btn.textContent = '正在校验...';
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            username: document.getElementById('u').value.trim(),
            password: document.getElementById('p').value
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || '登录失败');
        location.href = '/';
      } catch (ex) {
        err.textContent = ex.message;
        err.style.display = 'block';
        btn.disabled = false;
        btn.textContent = '验证并进入工作台';
      }
    }
  </script>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if verify_session_token(request.cookies.get("wb_session")):
        return RedirectResponse(url="/", status_code=302)
    return LOGIN_HTML


@app.get("/", response_class=HTMLResponse)
def index():
    return WORKBENCH_HTML


def open_running_workbench():
    url = "http://127.0.0.1:8888"
    try:
        with urlopen(f"{url}/login", timeout=1.5) as response:
            if response.status != 200:
                return False
    except OSError:
        return False
    webbrowser.open(url)
    return True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8888))
    if "--open" not in sys.argv or not open_running_workbench():
        uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
