"""Authentication, PBKDF2 password hashing, brute-force protection, and signed sessions."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

ADMIN_USER = "admin-zpc"
# 管理员初始密码的 PBKDF2-HMAC-SHA256 (200,000 次迭代) 加盐哈希，源码不留存明文密码
ADMIN_HASH = None

PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 24 * 3600  # 会话有效期 24 小时
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 连续失败 5 次锁定 15 分钟

BASE_DIR = Path(os.environ.get("WORKBENCH_DATA_DIR", Path(__file__).resolve().parent)).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = BASE_DIR / ".auth_users.json"
SECRET_FILE = BASE_DIR / ".auth_secret"

_LOCK = threading.Lock()
_FAILED_ATTEMPTS: dict[str, list[float]] = {}  # ip -> [timestamps]
_LOCKED_UNTIL: dict[str, float] = {}  # ip -> unlock_timestamp


def _get_secret() -> bytes:
    env_secret = os.environ.get("WORKBENCH_SECRET")
    if env_secret:
        if len(env_secret) < 32:
            raise RuntimeError("WORKBENCH_SECRET 至少需要 32 个字符")
        return hashlib.sha256(env_secret.encode("utf-8")).digest()
    if os.environ.get("WORKBENCH_CLOUD") == "1":
        raise RuntimeError("云端必须设置 WORKBENCH_SECRET")
    with _LOCK:
        if SECRET_FILE.exists():
            try:
                raw = SECRET_FILE.read_bytes()
                if len(raw) >= 32:
                    return raw[:32]
            except OSError:
                pass
        key = secrets.token_bytes(32)
        try:
            SECRET_FILE.write_bytes(key)
        except OSError:
            pass
        return key


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def _admin_hash():
    """Initialize a unique administrator password on first launch."""
    admin_password = os.environ.get("WORKBENCH_ADMIN_PASSWORD")
    if admin_password:
        if len(admin_password) < 16:
            raise RuntimeError("WORKBENCH_ADMIN_PASSWORD 至少需要 16 个字符")
        return hash_password(admin_password)
    if os.environ.get("WORKBENCH_CLOUD") == "1":
        raise RuntimeError("云端必须设置 WORKBENCH_ADMIN_PASSWORD")
    admin_file = BASE_DIR / ".admin_hash"
    with _LOCK:
        if not admin_file.exists():
            password = secrets.token_urlsafe(18)
            admin_file.write_text(hash_password(password), encoding="utf-8")
            (BASE_DIR / "首次登录密码.txt").write_text(
                f"账号: {ADMIN_USER}\n初始密码: {password}\n请妥善保管并在记录后删除此文件。\n", encoding="utf-8")
        return admin_file.read_text(encoding="utf-8").strip()


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, hash_hex = stored_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk, expected)


def _load_users() -> dict[str, dict]:
    if not USERS_FILE.exists():
        return {}
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_users(users: dict[str, dict]) -> None:
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)


def check_rate_limit(client_ip: str) -> int:
    """Return remaining lockout seconds if IP is locked, else 0."""
    now = time.monotonic()
    with _LOCK:
        unlock_at = _LOCKED_UNTIL.get(client_ip, 0)
        if unlock_at > now:
            return int(unlock_at - now) + 1
        if client_ip in _LOCKED_UNTIL:
            _LOCKED_UNTIL.pop(client_ip, None)
            _FAILED_ATTEMPTS.pop(client_ip, None)
        return 0


def _record_failure(client_ip: str) -> int:
    """Record a failed attempt and return remaining attempts before lockout."""
    now = time.monotonic()
    with _LOCK:
        history = [t for t in _FAILED_ATTEMPTS.get(client_ip, []) if now - t < LOCKOUT_SECONDS]
        history.append(now)
        _FAILED_ATTEMPTS[client_ip] = history
        if len(history) >= MAX_LOGIN_ATTEMPTS:
            _LOCKED_UNTIL[client_ip] = now + LOCKOUT_SECONDS
            return 0
        return MAX_LOGIN_ATTEMPTS - len(history)


def _clear_failures(client_ip: str) -> None:
    with _LOCK:
        _FAILED_ATTEMPTS.pop(client_ip, None)
        _LOCKED_UNTIL.pop(client_ip, None)


def authenticate(username: str, password: str, client_ip: str) -> tuple[bool, str | None, str]:
    """Verify credentials with brute-force protection. Returns (ok, role, error_message)."""
    locked_sec = check_rate_limit(client_ip)
    if locked_sec > 0:
        mins = (locked_sec + 59) // 60
        return False, None, f"尝试次数过多，请 {mins} 分钟后再试"

    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False, None, "请输入账号和密码"

    admin_hash = _admin_hash()
    if hmac.compare_digest(username, ADMIN_USER):
        if verify_password(password, admin_hash):
            _clear_failures(client_ip)
            return True, "admin", ""
        rem = _record_failure(client_ip)
        msg = "账号或密码错误" if rem > 0 else "连续失败次数过多，该 IP 已被锁定 15 分钟"
        return False, None, msg

    with _LOCK:
        users = _load_users()
    user_info = users.get(username)
    if user_info and verify_password(password, user_info.get("password_hash", "")):
        _clear_failures(client_ip)
        return True, "user", ""

    # 即使用户名不存在，也执行一次空哈希以平衡响应时间，防止用户名枚举
    verify_password(password, admin_hash)
    rem = _record_failure(client_ip)
    msg = "账号或密码错误" if rem > 0 else "连续失败次数过多，该 IP 已被锁定 15 分钟"
    return False, None, msg


def create_session_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + SESSION_TTL,
        "nonce": secrets.token_hex(8),
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    sig = hmac.new(_get_secret(), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_session_token(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        raw, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(_get_secret(), raw.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
            return None
        username = payload.get("sub")
        role = payload.get("role")
        if username == ADMIN_USER and role == "admin":
            return payload
        with _LOCK:
            users = _load_users()
        if username in users and role == "user":
            return payload
        return None
    except Exception:
        return None


def list_users() -> list[dict]:
    with _LOCK:
        users = _load_users()
    result = [{"username": ADMIN_USER, "role": "admin", "created_at": "系统内置"}]
    for uname, info in users.items():
        result.append({
            "username": uname,
            "role": "user",
            "created_at": info.get("created_at", ""),
        })
    return result


def add_user(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    if not username or len(username) < 2 or len(username) > 32:
        return False, "用户名长度需在 2~32 个字符之间"
    if any(c in username for c in (" ", "\t", "\n", "\r", ":", "/", "\\")):
        return False, "用户名不能包含空格或特殊斜杠字符"
    if username == ADMIN_USER:
        return False, "不能覆盖最高权限管理员账号"
    if not password or len(password) < 12 or len(password) > 64:
        return False, "密码长度需在 12~64 个字符之间"

    with _LOCK:
        users = _load_users()
        users[username] = {
            "password_hash": hash_password(password),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_users(users)
    return True, "账号添加成功"


def delete_user(username: str) -> tuple[bool, str]:
    username = (username or "").strip()
    if username == ADMIN_USER:
        return False, "最高权限管理员账号不可删除"
    with _LOCK:
        users = _load_users()
        if username not in users:
            return False, "未找到该账号"
        users.pop(username)
        _save_users(users)
    return True, "账号已移除"
