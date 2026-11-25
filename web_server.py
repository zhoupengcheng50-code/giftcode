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
    public_paths = ("/login", "/api/auth/login", "/api/auth/logout", "/assets/zpc-logo.png")
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
    response.headers.setdefault("Cache-Control", "no-store")
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


WORKBENCH_THEME_CSS = r"""
    :root {
      color-scheme: dark;
      --bg: #081222; --top: #0a1425; --panel: #0f1d32; --well: #0a1628;
      --line: #263b56; --text: #ecf3ff; --muted: #a6b9d3; --accent: #84ccff;
      --blue: #2860da; --green: #77e6be; --amber: #ffce82; --red: #ff9fae;
      --radius: 15px;
    }
    :root[data-view="review"] {
      --bg: #061424; --top: #091b2e; --panel: #0c2138; --well: #091a2e;
      --line: #23415e; --muted: #abc2db; --accent: #77e5f7; --blue: #12657d;
      --radius: 11px;
    }
    body {
      margin: 0; min-height: 100vh; color: var(--text);
      font: 14px/1.55 "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif;
      background: radial-gradient(ellipse at 63% 0%, #17315c 0%, transparent 45%), var(--bg);
    }
    [data-view="review"] body { background: radial-gradient(ellipse at 86% 0%, #12395b, transparent 48%), var(--bg); }
    body { isolation: isolate; --flow-rgb: 100 164 253; --flow-bright: 112 220 255; }
    [data-view="review"] body { --flow-rgb: 72 183 230; --flow-bright: 122 242 245; }
    body::before, body::after {
      content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    }
    body::before {
      background:
        radial-gradient(ellipse 118% 92% at 3% 93%, transparent 75.8%, rgb(var(--flow-bright) / .36) 76%, transparent 76.2%),
        repeating-radial-gradient(ellipse 118% 92% at 3% 93%, transparent 0 4.7%, rgb(var(--flow-rgb) / .23) 4.82%, transparent 4.98%),
        radial-gradient(ellipse at 100% 28%, rgb(var(--flow-rgb) / .12), transparent 65%);
      mask-image: radial-gradient(ellipse 75% 70% at 86% 12%, #000 12%, #000c 48%, transparent 100%);
    }
    body::after {
      background:
        radial-gradient(ellipse 112% 85% at 93% 5%, transparent 79.7%, rgb(var(--flow-bright) / .3) 79.9%, transparent 80.1%),
        repeating-radial-gradient(ellipse 112% 85% at 93% 5%, transparent 0 5.3%, rgb(var(--flow-rgb) / .19) 5.42%, transparent 5.58%),
        radial-gradient(ellipse at 0 85%, rgb(var(--flow-rgb) / .11), transparent 62%);
      mask-image: radial-gradient(ellipse 73% 70% at 9% 85%, #000 13%, #000b 50%, transparent 100%);
    }
    .login-page::before, .login-page::after { opacity: .84; }
    @media (max-width: 600px) {
      body::before, body::after { opacity: .68; }
      .login-page::before, .login-page::after { opacity: .58; }
    }

    .brand { display: flex; align-items: center; gap: 12px; white-space: nowrap; }
    .brand strong, .brand small { display: block; }
    .brand strong { font-size: 16px; }
    .brand-logo {
      width: 96px; height: 38px; flex: none; object-fit: cover; object-position: center 52%;
      border: 1px solid var(--line); border-radius: 8px; background: #03061a;
    }
"""


WORKBENCH_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>越南后台GMS礼包码自动配置工作台</title>
  <style>
""" + WORKBENCH_THEME_CSS + r"""    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    button, input, textarea { font: inherit; }
    button { cursor: pointer; }
    a { color: inherit; }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: clamp(27px, 4vw, 38px); line-height: 1.2; letter-spacing: -.035em; }
    h2 { font-size: 17px; font-weight: 650; }
    h3 { font-size: 15px; }
    small { font-size: 12px; color: var(--muted); }
    .topbar { background: var(--top); border-bottom: 1px solid var(--line); }
    .topbar-inner, .container { width: min(1400px, calc(100% - 64px)); margin-inline: auto; }
    .topbar-inner { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-block: 10px; }
    .header-actions, .view-switch, .page-nav, .section-head, .import-meta, .action-bar { display: flex; align-items: center; }
    .header-actions { gap: 9px; flex-wrap: wrap; }
    .view-switch { gap: 3px; padding: 3px; border: 1px solid var(--line); background: var(--well); border-radius: 9px; }
    .view-switch button { border: 0; background: transparent; color: var(--muted); border-radius: 6px; padding: 5px 9px; white-space: nowrap; font-size: 12px; }
    .view-switch button[aria-pressed="true"] { background: #233e6b; color: #edf5ff; font-weight: 650; }
    [data-view="review"] .view-switch button[aria-pressed="true"] { background: #174e63; }
    .badge { padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; white-space: nowrap; color: var(--muted); font-size: 12px; }
    .service-badge { color: var(--green); }
    .service-badge::before { content: ""; display: inline-block; vertical-align: 1px; width: 6px; height: 6px; margin-right: 6px; background: var(--green); border-radius: 50%; }
    .container { padding-block: 28px 48px; }
    .page-heading { display: flex; justify-content: space-between; align-items: end; gap: 14px; margin-bottom: 23px; }
    .page-title { display: flex; align-items: center; gap: 14px; }
    .page-title .brand-logo { width: clamp(76px, 10vw, 124px); height: auto; aspect-ratio: 5 / 2; }
    .page-heading p, .section-head p { color: var(--muted); margin-top: 4px; }
    .page-nav { gap: 15px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
    .page-nav a { text-decoration: none; }
    .page-nav a:hover { color: var(--accent); text-decoration: underline; }
    .workbench, .prep, .workspace { display: grid; gap: 16px; min-width: 0; align-content: start; }
    .prep { grid-template-columns: minmax(0, 1fr) minmax(0, 1.65fr); }
    [data-view="review"] .workbench { grid-template-columns: minmax(265px, .34fr) minmax(0, 1fr); align-items: start; }
    [data-view="review"] .prep { grid-template-columns: minmax(0, 1fr); }
    .card { min-width: 0; padding: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
    .section-head { justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
    .section-head p { font-size: 12px; }
    .dropzone {
      display: grid; justify-items: center; align-content: center; gap: 4px; min-height: 124px; width: 100%; padding: 12px;
      border: 1px dashed #4a76a5; border-radius: 10px; color: var(--text);
      background: radial-gradient(ellipse at top, #162e50, transparent 80%), var(--well);
    }
    [data-view="review"] .dropzone { background: radial-gradient(ellipse at top, #133c54, transparent 80%), var(--well); }
    .dropzone:hover, .dropzone.is-dragging { border-color: var(--accent); }
    .upload-icon, .empty-icon { display: grid; place-items: center; width: 34px; height: 34px; margin-bottom: 2px; border: 1px solid var(--line); border-radius: 8px; background: var(--well); color: var(--accent); font-size: 21px; }
    .file-types { color: var(--muted); font-size: 11px; letter-spacing: .055em; }
    .import-meta { justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; margin-top: 9px; color: var(--muted); font-size: 12px; }
    #upload-status[data-state="error"] { color: var(--red); }
    #upload-status[data-state="ready"] { color: var(--green); }
    .paste-toggle { background: none; border: 0; color: var(--accent); padding: 4px 0; text-align: left; }
    .paste-toggle:hover { text-decoration: underline; }
    .paste-area { margin-top: 10px; }
    .paste-area textarea { min-height: 95px; resize: vertical; display: block; margin-bottom: 8px; }
    .settings-grid, .form-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; }
    .settings-grid { grid-template-columns: minmax(0, 2fr) minmax(0, .9fr) minmax(0, 1.4fr) minmax(0, .75fr); }
    [data-view="review"] .settings-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    [data-view="review"] :is(.activity-field, .expire-field) { grid-column: 1 / -1; }
    [data-view="review"] .expire-field { grid-row: 3; }
    .form-group { min-width: 0; }
    label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; }
    input:not([type="checkbox"], [type="file"]), textarea { width: 100%; min-height: 36px; border: 1px solid var(--line); border-radius: 7px; padding: 6px 9px; background: var(--well); color: var(--text); }
    input::placeholder, textarea::placeholder { color: #839bb8; opacity: 1; }
    input[type="number"], #batch-date, #expire-time { font-variant-numeric: tabular-nums; }
    .access-settings { margin-top: 9px; border-top: 1px solid var(--line); }
    summary { cursor: pointer; color: var(--muted); padding: 8px 0; font-size: 13px; }
    .access-settings summary { color: var(--accent); }
    .switch-label { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
    .empty-state { display: grid; justify-items: center; gap: 5px; padding: 26px 10px; text-align: center; color: var(--muted); border: 1px dashed var(--line); border-radius: 10px; background: var(--well); }
    .empty-state strong { color: var(--text); }
    .empty-state p { font-size: 12px; }
    .review-totals { display: flex; gap: 6px; flex-wrap: wrap; }
    .chip { display: inline-block; width: max-content; max-width: 100%; padding: 1px 6px; border: 1px solid var(--line); border-radius: 5px; color: var(--muted); font-size: 11px; line-height: 1.7; }
    .chip[data-status="same"] { color: var(--green); border-color: #26584f; background: #102d30; }
    .chip[data-status="quantity"] { color: var(--amber); border-color: #665035; background: #30271f; }
    .chip[data-status="review"] { color: var(--red); border-color: #704452; background: #362431; }
    .tiers-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .tier-card { min-width: 0; padding: 11px; border-radius: 9px; background: var(--well); border: 1px solid var(--line); }
    .tier-card-head, .tier-choice { display: flex; align-items: center; justify-content: space-between; gap: 6px; flex-wrap: wrap; }
    .tier-title { font-size: 15px; font-weight: 700; color: var(--accent); font-variant-numeric: tabular-nums; }
    .tier-target { overflow-wrap: anywhere; color: var(--muted); font-size: 12px; margin: 3px 0 1px; }
    .tier-source { display: block; color: var(--muted); font-size: 11px; }
    .card-rewards { margin-top: 7px; }
    .reward-item { border-top: 1px solid var(--line); padding: 4px 0; font-size: 12px; }
    .reward-main { display: flex; justify-content: space-between; gap: 5px; }
    .reward-main span:first-child { overflow-wrap: anywhere; }
    .reward-main span:last-child { white-space: nowrap; color: var(--muted); font-variant-numeric: tabular-nums; }
    .reward-note { display: block; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .reward-note[data-status="quantity"] { color: var(--amber); }
    .reward-note[data-status="review"] { color: var(--red); }
    .review-layout { display: none; }
    [data-view="review"] .tiers-grid { display: none; }
    [data-view="review"] .review-layout { display: grid; grid-template-columns: 148px minmax(0, 1fr); gap: 12px; align-items: start; }
    .tier-picker { display: grid; gap: 5px; }
    .tier-choice { min-width: 0; min-height: 37px; text-align: left; padding: 6px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--well); color: var(--text); }
    .tier-choice:hover, .tier-choice[aria-pressed="true"] { border-color: #539caf; background: #113a4d; }
    .tier-choice strong { font-size: 13px; font-variant-numeric: tabular-nums; }
    .tier-detail { min-width: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--well); overflow: hidden; }
    .tier-detail-head { padding: 11px 12px; border-bottom: 1px solid var(--line); }
    .table-scroll { overflow-x: auto; }
    .reward-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; }
    .reward-table th, .reward-table td { padding: 7px 9px; border-bottom: 1px solid var(--line); vertical-align: top; }
    .reward-table th { background: #112942; color: var(--muted); font-weight: 550; }
    .reward-table tbody tr:last-child td { border-bottom: 0; }
    .reward-table th:nth-child(2), .reward-table th:nth-child(3), .reward-table td:nth-child(2), .reward-table td:nth-child(3) { white-space: nowrap; font-variant-numeric: tabular-nums; }
    .reward-table td:first-child { overflow-wrap: anywhere; }
    .execution-card .section-head { margin-bottom: 0; }
    .action-bar { gap: 7px; flex-wrap: wrap; }
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 6px 12px; border: 1px solid transparent; border-radius: 7px; color: var(--text); font-weight: 650; text-decoration: none; text-align: center; }
    .btn-sm { min-height: 30px; padding: 4px 8px; font-size: 12px; }
    .btn:disabled { opacity: .5; cursor: not-allowed; }
    .btn-primary { background: var(--blue); border-color: #6794ef; }
    [data-view="review"] .btn-primary { border-color: #55b7cb; }
    .btn-outline, .btn-danger-outline { background: var(--well); border-color: var(--line); }
    .btn-danger-outline { color: var(--muted); }
    .btn-success { background: #16403d; border-color: #367261; color: #aeffdf; }
    .btn:hover:not(:disabled) { filter: brightness(1.2); }
    .progress-box, .download-card { display: none; margin-top: 12px; }
    .progress-text { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    .progress-track { height: 6px; background: var(--well); border-radius: 6px; overflow: hidden; }
    .progress-fill { width: 0; height: 100%; background: linear-gradient(90deg, #4180f4, var(--accent)); }
    .download-inner { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; padding: 10px; border: 1px solid #30665c; border-radius: 8px; background: #102d30; }
    .download-card h3 { color: var(--green); }
    .download-card p { color: var(--muted); font-size: 12px; }
    .console-card { border-top: 1px solid var(--line); margin-top: 12px; }
    .terminal { border: 1px solid var(--line); border-radius: 8px; background: var(--well); padding: 9px; height: 170px; overflow: auto; overflow-wrap: anywhere; font: 12px/1.55 Consolas, "Microsoft YaHei UI", monospace; }
    .log-INFO { color: #b1cff7; }
    .log-SUCCESS { color: var(--green); }
    .log-WARN { color: var(--amber); }
    .log-ERROR { color: var(--red); }
    .modal-overlay { position: fixed; inset: 0; z-index: 10; display: none; align-items: center; justify-content: center; background: #030915b8; padding: 16px; }
    .modal-card { width: min(100%, 480px); max-height: 90vh; overflow: auto; padding: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
    .user-list { display: grid; gap: 6px; max-height: 240px; overflow: auto; margin-top: 9px; }
    .user-item { display: flex; align-items: center; justify-content: space-between; padding: 7px; border: 1px solid var(--line); border-radius: 7px; background: var(--well); }
    @media (max-width: 1120px) {
      .settings-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .tiers-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 880px) {
      [data-view="review"] .workbench { grid-template-columns: minmax(0, 1fr); }
      [data-view="review"] .prep { grid-template-columns: minmax(0, 1fr) minmax(0, 1.65fr); }
      .topbar-inner, .container { width: calc(100% - 32px); }
      .page-heading { align-items: start; flex-direction: column; }
    }
    @media (max-width: 620px) {
      .topbar-inner { align-items: start; flex-direction: column; }
      .prep, [data-view="review"] .prep { grid-template-columns: minmax(0, 1fr); }
      .container { padding-top: 20px; }
      .card { padding: 12px; }
      .action-bar .btn { flex: 1; }
      .action-bar { width: 100%; }
      [data-view="review"] .review-layout { grid-template-columns: minmax(0, 1fr); }
      .tier-picker { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .tier-choice { display: grid; justify-items: start; }
    }
    @media (max-width: 390px) {
      .tiers-grid { grid-template-columns: minmax(0, 1fr); }
      .tier-picker { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand"><div><strong>GMS 工作台</strong><small>越南 · 礼包码配置</small></div></div>
      <div class="header-actions">
        <div class="view-switch" role="group" aria-label="工作台布局">
          <button type="button" data-view="flow" aria-pressed="true">流程总览</button>
          <button type="button" data-view="review" aria-pressed="false">档位核对</button>
        </div>
        <span id="global-badge" class="badge service-badge">服务在线</span>
        <span id="current-user-badge" class="badge badge-idle">校验中…</span>
        <button id="manage-users-btn" class="btn btn-sm btn-outline" style="display:none" onclick="openUsersModal()">账号管理</button>
        <button class="btn btn-sm btn-danger-outline" onclick="logout()">退出</button>
      </div>
    </div>
  </header>
  <main class="container">
    <div class="page-heading">
      <div class="page-title">
        <img class="brand-logo" src="/assets/zpc-logo.png" alt="ZPC" width="96" height="38">
        <div><h1>礼包码自动配置</h1><p>导入奖励文档、核对往期档位，生成并导出礼包码。</p></div>
      </div>
      <nav class="page-nav" aria-label="页面区块"><a href="#configuration">导入与参数</a><a href="#rewards">奖励预览</a><a href="#execution">执行与导出</a></nav>
    </div>

    <div class="workbench">
      <section class="prep" id="configuration" aria-label="导入与参数">
        <section class="card import-card">
          <div class="section-head"><h2>导入奖励文档</h2></div>
          <button type="button" class="dropzone" id="dropzone" onclick="document.getElementById('file-input').click()">
            <span class="upload-icon" aria-hidden="true">↑</span>
            <strong>选择文件或拖放到此处</strong>
            <span class="file-types">DOCX · XLSX · XLS · TXT · CSV</span>
          </button>
          <input type="file" id="file-input" accept=".docx,.xlsx,.xls,.txt,.csv" hidden onchange="handleFileSelect(event)">
          <div class="import-meta">
            <button class="paste-toggle" type="button" aria-controls="paste-area" aria-expanded="false" onclick="togglePasteArea()">粘贴表格文本</button>
            <span id="upload-status" role="status">支持「档位」「奖励」两列</span>
          </div>
          <div class="paste-area" id="paste-area" hidden>
            <label for="paste-text">表格文本</label>
            <textarea id="paste-text" placeholder="粘贴来自微信或 Excel 的档位和奖励两列"></textarea>
            <button id="parse-btn" class="btn btn-outline btn-sm" onclick="handleTextParse()">解析粘贴内容</button>
          </div>
        </section>

        <section class="card settings-card">
          <div class="section-head"><h2>活动批次与生成参数</h2></div>
          <div class="settings-grid">
            <div class="form-group activity-field">
              <label for="activity-name">本次活动命名</label>
              <input type="text" id="activity-name" placeholder="越南2026网页支付累充礼包码20261125">
            </div>
            <div class="form-group">
              <label for="batch-date">本期批次日期</label>
              <input type="text" id="batch-date" inputmode="numeric" maxlength="8" placeholder="YYYYMMDD" title="活动名称已有日期时可留空">
            </div>
            <div class="form-group expire-field">
              <label for="expire-time">失效时间（GMS 时间）</label>
              <input type="text" id="expire-time" placeholder="YYYY-MM-DD HH:MM:SS">
            </div>
            <div class="form-group">
              <label for="code-count">单档生成数量</label>
              <input type="number" id="code-count" min="1" max="65535" value="100">
            </div>
          </div>
          <details class="access-settings">
            <summary>后台账号与运行选项</summary>
            <div class="form-row">
              <div class="form-group"><label for="username">后台账号</label><input type="text" id="username" placeholder="已保存时可留空"></div>
              <div class="form-group"><label for="password">后台密码</label><input type="password" id="password" placeholder="已保存时可留空" autocomplete="off"></div>
            </div>
            <label class="switch-label"><input type="checkbox" id="show-browser" checked disabled>执行时显示浏览器窗口</label>
          </details>
        </section>
      </section>

      <div class="workspace">
        <section class="card tiers-container" id="rewards">
          <div class="section-head review-head">
            <div><h2>奖励配置预览</h2><p id="tier-stat-badge">导入文档后显示档位与奖励</p></div>
            <div class="review-totals" id="review-totals" hidden></div>
          </div>
          <div class="empty-state" id="empty-state"><span class="empty-icon" aria-hidden="true">▦</span><strong>等待导入奖励配置</strong><p>上传文档或粘贴表格文本后，即可查看各档奖励。</p></div>
          <div class="tiers-grid" id="tiers-grid" hidden></div>
          <div class="review-layout" id="review-layout" hidden>
            <div class="tier-picker" id="tier-picker" role="group" aria-label="选择累充档位"></div>
            <div class="tier-detail" id="tier-detail"></div>
          </div>
        </section>

        <section class="card execution-card" id="execution">
          <div class="section-head">
            <div><h2>执行与导出</h2><p id="status-text" role="status">上传文档后核对往期配置</p></div>
            <div class="action-bar">
              <button class="btn btn-outline" id="preview-btn" onclick="runPreview()" disabled>核对往期配置</button>
              <button class="btn btn-primary" id="start-btn" onclick="startAutomation()" disabled>生成礼包码 <span aria-hidden="true">→</span></button>
            </div>
          </div>
          <div class="progress-box" id="progress-box">
            <div class="progress-text"><span id="current-action-text">正在初始化…</span><span id="progress-percent-text">0%</span></div>
            <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
          </div>
          <div class="download-card" id="download-card">
            <div class="download-inner"><div><h3>礼包码生成完成</h3><p>已合并为 8 个独立档位页签的 Excel 文件。</p></div>
            <a href="/api/download/xls" class="btn btn-success" download>下载激活码表格 (.xls)</a></div>
          </div>
          <details class="console-card" id="console-card">
            <summary>实时执行日志</summary>
            <div class="terminal" id="terminal" tabindex="0" aria-label="执行日志">
              <div class="log-INFO">工作台已就绪，等待执行指令。</div>
            </div>
          </details>
        </section>
      </div>
    </div>
  </main>

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
      area.hidden = !area.hidden;
      document.querySelector('.paste-toggle').setAttribute('aria-expanded', !area.hidden);
      if (!area.hidden) get('paste-text').focus();
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
      get('file-input').disabled = running;
      get('dropzone').disabled = running;
      get('parse-btn').disabled = running;
      get('paste-text').disabled = running;
    }

    const rewardLabels = {same: '一致', kept: '后台保留', review: '待核对', quantity: '数量调整'};
    const viewButtons = document.querySelectorAll('.view-switch button');
    let selectedTier;

    function setView(view) {
      document.documentElement.dataset.view = view;
      viewButtons.forEach(button => button.setAttribute('aria-pressed', button.dataset.view === view));
    }

    setView(localStorage.getItem('gms-workbench-view') === 'review' ? 'review' : 'flow');
    viewButtons.forEach(button => button.addEventListener('click', () => {
      setView(button.dataset.view);
      localStorage.setItem('gms-workbench-view', button.dataset.view);
    }));

    function rewardChip(label, status) {
      const chip = element('span', label, 'chip');
      if (status) chip.dataset.status = status;
      return chip;
    }

    function tierSummary(rows) {
      const pending = rows.filter(row => row.status === 'review').length;
      const changes = rows.filter(row => row.status === 'quantity').length;
      if (pending) return rewardChip(pending + ' 项待核对', 'review');
      if (changes) return rewardChip(changes + ' 项调整', 'quantity');
      if (!rows.every(row => row.status === 'same')) return rewardChip('已核对');
      return rewardChip('全部一致', 'same');
    }

    function tierHeading(tier, data, compact) {
      const heading = element('div', '', compact ? 'tier-card-head' : 'tier-detail-head');
      heading.append(element('h3', '累充 ' + tier, 'tier-title'));
      if (!compact && data) {
        heading.append(element('p', data.target, 'tier-target'),
          element('small', '往期活动 ID ' + data.source.id, 'tier-source'));
      }
      return heading;
    }

    function tierCard({tier, data, rows}) {
      const card = element('article', '', 'tier-card');
      const head = tierHeading(tier, data, true);
      if (data) head.append(tierSummary(rows));
      card.append(head);
      if (data) card.append(element('p', data.target, 'tier-target'),
        element('small', '往期活动 ID ' + data.source.id, 'tier-source'));
      const list = element('div', '', 'card-rewards');
      for (const row of rows) {
        const line = element('div', '', 'reward-item');
        const main = element('div', '', 'reward-main');
        main.append(element('span', row.name), element('span', '×' + (row.expected ?? row.actual)));
        line.append(main);
        if (row.status && row.status !== 'same') {
          let note = rewardLabels[row.status];
          if (row.status === 'quantity') note = '往期 ×' + row.actual + ' → 本期 ×' + row.expected;
          if (row.status === 'review') note += '：' + row.reason;
          const label = element('small', note, 'reward-note');
          label.dataset.status = row.status;
          line.append(label);
        }
        list.append(line);
      }
      card.append(list);
      return card;
    }

    function selectTier({tier, data, rows}) {
      selectedTier = tier;
      const detail = get('tier-detail');
      detail.replaceChildren(tierHeading(tier, data, false));
      get('tier-picker').querySelectorAll('button').forEach(button => {
        button.setAttribute('aria-pressed', button.dataset.tier === tier);
      });
      const table = element('table', '', 'reward-table');
      table.setAttribute('aria-label', '累充 ' + tier + ' 奖励明细');
      const header = table.createTHead().insertRow();
      for (const label of ['奖励', '上一期', '本期', '核对结果']) {
        const cell = element('th', label);
        cell.scope = 'col';
        header.append(cell);
      }
      const tbody = table.createTBody();
      const quantity = value => value == null ? '—' : '×' + value;
      for (const row of rows) {
        const tr = tbody.insertRow();
        tr.insertCell().textContent = row.name;
        tr.insertCell().textContent = quantity(row.actual);
        tr.insertCell().textContent = quantity(row.status === 'kept' ? row.actual : row.expected);
        const status = tr.insertCell();
        status.append(rewardChip(rewardLabels[row.status] ?? '未核对', row.status));
        if (row.reason) status.append(element('small', row.reason, 'reward-note'));
      }
      const scroller = element('div', '', 'table-scroll');
      scroller.append(table);
      detail.append(scroller);
    }

    function render() {
      const grid = get('tiers-grid');
      const picker = get('tier-picker');
      const totals = get('review-totals');
      grid.replaceChildren();
      picker.replaceChildren();
      totals.replaceChildren();
      grid.hidden = get('review-layout').hidden = !rewards;
      get('empty-state').hidden = Boolean(rewards);
      totals.hidden = !preview || !rewards;
      if (!rewards) {
        selectedTier = undefined;
        get('tier-detail').replaceChildren();
        get('tier-stat-badge').textContent = '导入文档后显示档位与奖励';
        return;
      }
      const tiers = Object.entries(rewards).map(([tier, items]) => {
        const data = preview?.tiers[tier];
        const rows = data?.items ?? Object.entries(items).map(([name, expected]) => ({name, expected}));
        return {tier, data, rows};
      });
      const batch = preview ? '本期 ' + preview.batch_date + ' · 上一期 ' + preview.baseline : '已识别 ' + tiers.length + ' 档，等待核对往期';
      get('tier-stat-badge').textContent = batch;
      if (preview) {
        const rows = tiers.flatMap(tier => tier.rows);
        for (const [status, label] of Object.entries(rewardLabels)) {
          const count = rows.filter(row => row.status === status).length;
          if (count) totals.append(rewardChip(label + ' ' + count, status));
        }
      }
      for (const tier of tiers) {
        grid.append(tierCard(tier));
        const button = element('button', '', 'tier-choice');
        button.type = 'button';
        button.dataset.tier = tier.tier;
        button.setAttribute('aria-label', '累充 ' + tier.tier);
        button.append(element('strong', tier.tier));
        if (tier.data) button.append(tierSummary(tier.rows));
        button.addEventListener('click', () => selectTier(tier));
        picker.append(button);
      }
      const active = tiers.find(tier => tier.tier === selectedTier)
        ?? tiers.find(tier => tier.rows.some(row => row.status === 'review'))
        ?? tiers.find(tier => tier.rows.some(row => row.status === 'quantity'))
        ?? tiers[0];
      if (active) selectTier(active);
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
      status.dataset.state = 'loading';
      status.textContent = '读取奖励文档…';
      try {
        const parsed = await call(url, body, form);
        if (requestNumber !== uploadNumber) return;
        rewards = parsed.rewards;
        status.dataset.state = 'ready';
        status.textContent = '已识别 ' + Object.keys(rewards).length + ' 档奖励';
        if (parsed.defaults) {
          get('activity-name').value = parsed.defaults.activity_name;
          get('batch-date').value = parsed.defaults.batch_date;
          get('expire-time').value = parsed.defaults.expire_time;
          if (!parsed.defaults.batch_from_filename) status.textContent += ' · 文件名无有效日期，批次已按上传日期填写';
        }
      } catch (error) {
        if (requestNumber !== uploadNumber) return;
        status.dataset.state = 'error';
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
      get('console-card').open = true;
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
      zone.addEventListener('dragover', event => {
        event.preventDefault();
        if (!running) zone.classList.add('is-dragging');
      });
      zone.addEventListener('dragleave', () => zone.classList.remove('is-dragging'));
      zone.addEventListener('drop', event => {
        event.preventDefault();
        zone.classList.remove('is-dragging');
        if (event.dataTransfer.files.length) handleFileSelect({target: event.dataTransfer});
      });
    }
    setControls();
    call('/api/config').then(status => {
      if (status.credentials_saved) get('username').placeholder = '账号已保存，可留空';
      else document.querySelector('.access-settings').open = true;
    }).catch(error => { get('status-text').textContent = error.message; });

    call('/api/auth/me').then(me => {
      const badge = get('current-user-badge');
      badge.textContent = (me.role === 'admin' ? '管理员 · ' : '') + me.username;
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
        <h3 style="color:#38bdf8;font-size:17px;">授权账号管理</h3>
        <button class="btn btn-sm btn-outline" onclick="closeUsersModal()">关闭</button>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label for="new-auth-user">新授权账号</label>
          <input type="text" id="new-auth-user" placeholder="输入用户名(2~32位)">
        </div>
        <div class="form-group">
          <label for="new-auth-pass">登录密码</label>
          <input type="password" id="new-auth-pass" placeholder="设置密码(至少4位)">
        </div>
      </div>
      <button class="btn btn-primary" style="width:100%;" onclick="addAuthorizedUser()">添加授权账号</button>
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
  <title>登录 · GMS 工作台</title>
  <script>
    try {
      if (localStorage.getItem('gms-workbench-view') === 'review') {
        document.documentElement.dataset.view = 'review';
      }
    } catch {
      // Use the default theme when browser storage is unavailable.
    }
  </script>
  <style>
""" + WORKBENCH_THEME_CSS + r"""    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body { min-height: 100vh; min-height: 100svh; display: flex; flex-direction: column; }
    button, input { font: inherit; }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
    .topbar { background: var(--top); border-bottom: 1px solid var(--line); }
    .topbar-inner { width: min(1400px, calc(100% - 64px)); min-height: 72px; margin-inline: auto; display: flex; align-items: center; }
    .brand small { font-size: 12px; color: var(--muted); }
    .login-main { flex: 1; display: grid; place-items: center; padding: 32px 16px; }
    .login-card {
      width: min(100%, 416px); padding: clamp(20px, 6vw, 32px);
      border: 1px solid var(--line); border-radius: var(--radius);
      background: var(--panel); box-shadow: 0 18px 48px #02071155;
    }
    .login-heading { margin-bottom: 23px; text-align: center; }
    .login-title-row { display: flex; align-items: center; justify-content: center; gap: 9px; }
    .login-title-row .brand-logo { width: 76px; height: 32px; }
    h1 { margin: 0; font-size: clamp(23px, 5vw, 29px); line-height: 1.25; }
    .login-heading p { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
    .login-form { display: grid; gap: 15px; }
    label { display: block; margin-bottom: 5px; color: var(--muted); font-size: 13px; }
    input {
      display: block; width: 100%; min-height: 44px; padding: 8px 11px;
      border: 1px solid var(--line); border-radius: 7px;
      background: var(--well); color: var(--text); font-size: 16px;
    }
    input::placeholder { color: #839bb8; opacity: 1; }
    input:-webkit-autofill {
      -webkit-box-shadow: 0 0 0 100px var(--well) inset;
      -webkit-text-fill-color: var(--text);
      caret-color: var(--text);
    }
    .login-button {
      min-height: 44px; padding: 8px 12px; display: flex; align-items: center; justify-content: center; gap: 8px;
      border: 1px solid #6794ef; border-radius: 7px;
      background: var(--blue); color: var(--text); font-weight: 650; cursor: pointer;
    }
    [data-view="review"] .login-button { border-color: #55b7cb; }
    .login-button:hover:not(:disabled) { filter: brightness(1.2); }
    .login-button:disabled { cursor: wait; opacity: .7; }
    .err-msg {
      margin: 0; padding: 8px 10px; border: 1px solid #704452; border-radius: 7px;
      background: #362431; color: var(--red); font-size: 13px; overflow-wrap: anywhere;
    }
    @media (max-width: 880px) {
      .topbar-inner { width: calc(100% - 32px); }
    }
  </style>
</head>
<body class="login-page">
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand"><div><strong>GMS 工作台</strong><small>越南 · 礼包码配置</small></div></div>
    </div>
  </header>
  <main class="login-main">
    <section class="login-card" aria-labelledby="login-title">
      <div class="login-heading">
        <div class="login-title-row">
          <img class="brand-logo" src="/assets/zpc-logo.png" alt="ZPC" width="96" height="38">
          <h1 id="login-title">登录工作台</h1>
        </div>
        <p>仅限管理员及已授权人员访问</p>
      </div>
      <form id="login-form" class="login-form" method="post" onsubmit="handleLogin(event)">
        <div>
          <label for="u">账号</label>
          <input id="u" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" required placeholder="请输入授权账号">
        </div>
        <div>
          <label for="p">密码</label>
          <input id="p" name="password" type="password" autocomplete="current-password" required placeholder="请输入登录密码">
        </div>
        <button type="submit" class="login-button" id="submit-btn"><span id="submit-label">进入工作台</span><span aria-hidden="true">→</span></button>
        <div id="err-msg" class="err-msg" role="alert" hidden></div>
      </form>
    </section>
  </main>
  <script>
    async function handleLogin(event) {
      event.preventDefault();
      const button = document.getElementById('submit-btn');
      if (button.disabled) return;
      const label = document.getElementById('submit-label');
      const error = document.getElementById('err-msg');
      error.hidden = true;
      button.disabled = true;
      label.textContent = '正在验证…';
      try {
        const response = await fetch('/api/auth/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            username: document.getElementById('u').value.trim(),
            password: document.getElementById('p').value
          })
        });
        const data = await response.json().catch(() => null);
        if (response.ok && data?.ok === true) {
          location.href = '/';
          return;
        }
        error.textContent = typeof data?.detail === 'string' ? data.detail : '暂时无法登录，请稍后重试。';
      } catch {
        error.textContent = '无法连接工作台，请检查网络后重试。';
      }
      error.hidden = false;
      button.disabled = false;
      label.textContent = '进入工作台';
    }
  </script>
</body>
</html>

"""


@app.get("/assets/zpc-logo.png", include_in_schema=False)
def brand_logo():
    return FileResponse(
        WORK_DIR / "assets" / "zpc-logo.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


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
