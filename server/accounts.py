"""账号与登录。

- 管理员（id=1，默认用户名 root / 密码 123456）可以建账号、暂停、重置密码、删除（连数据一起删）；其他账号只能改自己的密码、设自己的提醒邮箱。
- 密码只存加盐哈希（scrypt），谁都看不到原文，管理员也一样；管理员重置的是「临时密码」，对方登录后必须马上改成自己的。
- 管理员可以给每个普通账号设有效期（到某一天为止，含当天；不设 = 永久）：过了那天登录不了、已登录的立刻下线，数据保留，续期后恢复；管理员账号没有有效期。
- 一个账号同一时间只有一处登录：新的登录会把旧的踢下线（旧的下一次请求收到「已在别处登录」）。
- 登录有防猜密码：同一用户名 + 同一 IP 连续输错 5 次、或同一 IP 累计输错 30 次，锁 15 分钟。
- 数据：data/accounts.db（账号、会话、登录记录）；管理员的个人库仍是 data/portfolio.db，其他账号在 data/users/<id>/portfolio.db（见 db.portfolio_path）。
- 会话 = 一个只用于本站的 Cookie（HttpOnly、SameSite=Lax；HTTPS 下加 Secure），30 天没用才过期；库里只存它的哈希。
- 环境变量 STOCK_AUTH=off 只在后端监听本机（127.0.0.1）时有效，给测试 / 本机预览用；监听局域网或公网时无效。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, settings

DEFAULT_ADMIN = ("root", "123456")
COOKIE = "sa_session"
SESSION_DAYS = 30
MAX_FAILS = 5                  # 同一用户名 + 同一 IP
IP_MAX_FAILS = 30              # 同一 IP（不管用户名）
LOCK_SECONDS = 15 * 60
MIN_PW, MAX_PW = 6, 128
ONLINE_SECONDS = 10 * 60
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-一-龥]{2,32}$")
_lock = threading.RLock()

PUBLIC_API = ("/api/ping", "/api/auth/login", "/api/auth/logout")
MUST_CHANGE_OK = ("/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/ping")
ADMIN_ONLY = ("/api/accounts", "/api/settings", "/api/notify", "/api/upgrade", "/api/datasources", "/api/data/health", "/api/storage", "/api/backups", "/api/restart")
USER_WRITE = ("/api/watchlist", "/api/kline/list", "/api/account", "/api/positions", "/api/trades", "/api/alerts", "/api/auth", "/api/me",
              "/api/analysis")         # 最后这条是只读的「筛选」查询（用 POST 只是因为条件多），不改任何数据

MESSAGES = {
    "none": "请先登录", "expired": "登录已过期，请重新登录", "kicked": "这个账号已在另一台设备登录，你已被下线",
    "paused": "账号已被暂停，请联系管理员", "reset": "密码已被管理员重置，请用新密码登录", "logout": "已退出登录", "deleted": "账号不存在", "account_expired": "账号已到期，请联系管理员续期",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, ukey TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active',
  must_change INTEGER NOT NULL DEFAULT 0, pw_default INTEGER NOT NULL DEFAULT 0,
  notify_to TEXT NOT NULL DEFAULT '', created_at TEXT, last_login_at TEXT, last_login_ip TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL, last_seen REAL, ip TEXT, ua TEXT, revoked TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS login_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, username TEXT, ip TEXT, ok INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS login_fail(k TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fail_k ON login_fail(k, ts);
"""


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---- 密码 ---------------------------------------------------------------------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    try:
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        kind = "scrypt"
    except (AttributeError, ValueError):                              # 少数 Python 构建没有 scrypt：退回 PBKDF2
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000, 32)
        kind = "pbkdf2"
    return f"{kind}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        kind, s, d = stored.split("$")
        salt, want = base64.b64decode(s), base64.b64decode(d)
        if kind == "scrypt":
            got = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        elif kind == "pbkdf2":
            got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000, 32)
        else:
            return False
        return hmac.compare_digest(got, want)
    except (ValueError, AttributeError):
        return False


_DUMMY = None


def _dummy_verify(pw: str) -> None:
    """用户名不存在时也算一遍哈希，让「用户名不存在」和「密码错」耗时一样。"""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("dummy")
    verify_password(pw, _DUMMY)


def validate_password(pw) -> str:
    if not isinstance(pw, str) or len(pw) < MIN_PW:
        raise AuthError(400, "weak", f"密码至少 {MIN_PW} 位")
    if len(pw) > MAX_PW:
        raise AuthError(400, "weak", f"密码最长 {MAX_PW} 位")
    return pw


def validate_username(name) -> str:
    name = (name or "").strip() if isinstance(name, str) else ""
    if not USERNAME_RE.match(name):
        raise AuthError(400, "bad_name", "用户名要 2~32 个字符，只能用中文、字母、数字、下划线和减号")
    return name


# ---- 数据库 -------------------------------------------------------------------

def _path() -> Path:
    return settings.data_dir() / "accounts.db"


SCHEMA_VERSION = 2


def _conn():
    """打开账号库。建表 / 迁移 / 建管理员只在库里的 user_version 低于 SCHEMA_VERSION 时做一次（旧版本建的库 user_version 是 0，会被补齐；
    账号库被删掉重建也是 0，会重新建）。这里每个 API 请求都会走一遍，所以平时只读一个数字。"""
    conn = db._connect(_path())
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        if "expires_at" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:       # 旧版本建的账号库：补上「有效期」列
            conn.execute("ALTER TABLE users ADD COLUMN expires_at TEXT")
        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():        # 第一次：建管理员（id 固定为 1：它的个人库就是原来的 data/portfolio.db）
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("INSERT INTO users(id,username,ukey,pw_hash,role,status,must_change,pw_default,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (settings.ADMIN_ID, DEFAULT_ADMIN[0], DEFAULT_ADMIN[0], hash_password(DEFAULT_ADMIN[1]), "admin", "active", 0, 1, now))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


def _expiry(r) -> tuple[str | None, bool, int | None]:
    """(有效期到哪天, 是否已过期, 还剩几天)。有效期含当天：到期日当天还能用，第二天起不能。管理员没有有效期。"""
    exp = None if r["role"] == "admin" else r["expires_at"]
    if not exp:
        return None, False, None
    left = (date.fromisoformat(exp) - date.today()).days
    return exp, left < 0, left


def _pub(r) -> dict:
    exp, expired, left = _expiry(r)
    return {"id": r["id"], "username": r["username"], "role": r["role"], "status": r["status"], "must_change": bool(r["must_change"]),
            "default_password": bool(r["pw_default"]), "notify_to": r["notify_to"] or "", "created_at": r["created_at"],
            "last_login_at": r["last_login_at"], "expires_at": exp, "expired": expired, "days_left": left}


def auth_required() -> bool:
    """要不要登录。只有设了 STOCK_AUTH=off 并且后端只监听本机时才不要（测试 / 本机预览）。"""
    if os.environ.get("STOCK_AUTH") == "off" and settings.cfg()["server"]["host"] in LOCAL_HOSTS:
        return False
    return True


def _is_loopback(host: str | None) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def client_ip(request) -> str:
    """客户端 IP。只有请求直接来自本机（反向代理 Caddy 就在本机）时才相信 X-Forwarded-For，取代理追加的最后一个。"""
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if _is_loopback(peer) and xff:
        return xff.split(",")[-1].strip()[:64] or peer
    return peer or "?"


def is_direct_local(request) -> bool:
    """请求是不是「本机直接打开」的：来自本机，且没有经过反向代理（没有 X-Forwarded-* 头）。"""
    peer = request.client.host if request.client else ""
    return _is_loopback(peer) and not request.headers.get("x-forwarded-for") and not request.headers.get("x-forwarded-proto")


def needs_change(user: dict, request=None) -> bool:
    """必须先改密码：管理员重置 / 新建的账号（must_change）；或者管理员还在用默认密码、而且不是在本机直接打开（局域网 / 公网 / 反向代理）。"""
    if user.get("must_change"):
        return True
    return bool(user.get("default_password")) and not (request is not None and is_direct_local(request))


def _revoke(conn, user_id: int, reason: str, keep: str | None = None) -> None:
    conn.execute("UPDATE sessions SET revoked=? WHERE user_id=? AND revoked IS NULL AND token_hash IS NOT ?", (reason, user_id, keep))


def _log(conn, username: str, ip: str, ok: bool, note: str = "") -> None:
    conn.execute("INSERT INTO login_log(ts,username,ip,ok,note) VALUES(?,?,?,?,?)", (datetime.now().isoformat(timespec="seconds"), username[:40], ip, 1 if ok else 0, note[:80]))
    conn.execute("DELETE FROM login_log WHERE id < (SELECT MAX(id) FROM login_log) - 500")


def _lock_left(conn, key: str, limit: int, now: float) -> int:
    n, first = conn.execute("SELECT COUNT(*), MIN(ts) FROM login_fail WHERE k=? AND ts>?", (key, now - LOCK_SECONDS)).fetchone()
    return int(first + LOCK_SECONDS - now) + 1 if n >= limit and first else 0


# ---- 登录 / 会话 --------------------------------------------------------------

def login(username: str, password: str, ip: str, ua: str = "") -> dict:
    """成功返回 {token, user, kicked}；失败抛 AuthError。
    分三段：① 锁内：查有没有被锁定、取出这个用户；② 锁外：算密码哈希（scrypt 要几十毫秒，而每个 API 请求的 authenticate 也要这把锁，
    放在锁里的话，一个刷登录的脚本就能让所有人的请求排队）；③ 锁内：记失败 / 建会话。两段之间密码被改了就按「密码错」处理。"""
    uname = (username or "").strip() if isinstance(username, str) else ""
    password = password if isinstance(password, str) else ""
    ukey = uname.lower()
    now = time.time()
    ku, ki = f"u:{ukey}|{ip}", f"ip:{ip}"
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM login_fail WHERE ts<?", (now - 86400,))
            left = max(_lock_left(conn, ku, MAX_FAILS, now), _lock_left(conn, ki, IP_MAX_FAILS, now))
            if left:
                _log(conn, uname, ip, False, "被锁定")
                conn.commit()
                raise AuthError(429, "locked", f"输错次数太多，请 {max(1, (left + 59) // 60)} 分钟后再试")
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE ukey=?", (ukey,)).fetchone() if ukey else None
            stored = row["pw_hash"] if row else None
        finally:
            conn.close()
    ok = bool(row) and verify_password(password, stored)                      # 不在锁里
    if not row:
        _dummy_verify(password)
    with _lock:
        conn = _conn()
        try:
            if row:
                row = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
                ok = ok and bool(row) and row["pw_hash"] == stored            # 这几十毫秒里密码被改了 / 账号被删了：不放行
            if not ok:
                conn.execute("INSERT INTO login_fail(k,ts) VALUES(?,?)", (ku, now))
                conn.execute("INSERT INTO login_fail(k,ts) VALUES(?,?)", (ki, now))
                _log(conn, uname, ip, False, "密码错" if row else "没有这个用户")
                conn.commit()
                raise AuthError(401, "bad_login", "用户名或密码不对")
            if row["status"] != "active":
                _log(conn, uname, ip, False, "账号已暂停")
                conn.commit()
                raise AuthError(403, "paused", MESSAGES["paused"])
            if _expiry(row)[1]:
                _log(conn, uname, ip, False, "账号已到期")
                conn.commit()
                raise AuthError(403, "account_expired", MESSAGES["account_expired"])
            kicked = bool(conn.execute("SELECT 1 FROM sessions WHERE user_id=? AND revoked IS NULL AND last_seen>?", (row["id"], now - SESSION_DAYS * 86400)).fetchone())
            _revoke(conn, row["id"], "kicked")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,last_seen,ip,ua,revoked) VALUES(?,?,?,?,?,?,NULL)",
                         (_h(token), row["id"], now, now, ip, (ua or "")[:200]))
            conn.execute("DELETE FROM sessions WHERE (revoked IS NOT NULL AND last_seen<?) OR last_seen<?", (now - 7 * 86400, now - 2 * SESSION_DAYS * 86400))
            conn.execute("DELETE FROM login_fail WHERE k=?", (ku,))
            conn.execute("UPDATE users SET last_login_at=?, last_login_ip=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), ip, row["id"]))
            _log(conn, uname, ip, True, "顶掉了另一处登录" if kicked else "")
            conn.commit()
            return {"token": token, "user": _pub(conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()), "kicked": kicked}
        finally:
            conn.close()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(token: str | None) -> tuple[dict | None, str]:
    """用 Cookie 里的会话找人。返回 (账号, 'ok')；找不到返回 (None, 原因代码)。"""
    if not token:
        return None, "none"
    now = time.time()
    with _lock:
        conn = _conn()
        try:
            s = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
            if not s:
                return None, "expired"
            if s["revoked"]:
                return None, s["revoked"]
            if now - (s["last_seen"] or 0) > SESSION_DAYS * 86400:
                return None, "expired"
            u = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
            if not u:
                return None, "deleted"
            if u["status"] != "active":
                return None, "paused"
            if _expiry(u)[1]:
                return None, "account_expired"
            pub = _pub(u)
            if now - (s["last_seen"] or 0) > 120:
                conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, s["token_hash"]))
                conn.commit()
                pub["_touched"] = True                                   # 中间件据此重发 Cookie：浏览器里的 30 天从「最近一次用」算起，而不是从登录那天算起
            return pub, "ok"
        finally:
            conn.close()


def logout(token: str | None) -> None:
    if not token:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE sessions SET revoked='logout' WHERE token_hash=? AND revoked IS NULL", (_h(token),))
            conn.commit()
        finally:
            conn.close()


def access_error(user: dict, method: str, path: str) -> str | None:
    """这个账号能不能调这个接口。管理员什么都能；其他账号只能读、以及改自己的自选 / 持仓 / 账户 / 密码 / 提醒邮箱。"""
    if user.get("role") == "admin":
        return None
    if _under(path, ADMIN_ONLY):
        return "这个操作只有管理员可以做"
    if method in ("GET", "HEAD", "OPTIONS") or _under(path, USER_WRITE):
        return None
    return "这个操作只有管理员可以做"


def _under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


# ---- 自己改密码 / 提醒邮箱 -----------------------------------------------------

def change_password(user_id: int, old: str, new: str) -> None:
    validate_password(new)
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or not verify_password(old if isinstance(old, str) else "", row["pw_hash"]):
                raise AuthError(400, "bad_old", "旧密码不对")
            if new == old:
                raise AuthError(400, "same", "新密码不能和旧密码一样")
            conn.execute("UPDATE users SET pw_hash=?, must_change=0, pw_default=0 WHERE id=?", (hash_password(new), user_id))
            conn.commit()
        finally:
            conn.close()


def set_notify_to(user_id: int, email: str) -> str:
    from . import notify
    email = (email or "").strip()
    if email and not all(notify.EMAIL_RE.match(x) for x in re.split(r"[,;\s]+", email) if x):
        raise AuthError(400, "bad_email", "邮箱格式不对（多个用逗号分隔）")
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE users SET notify_to=? WHERE id=?", (email, user_id))
            conn.commit()
        finally:
            conn.close()
    return email


def get_user(user_id: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _pub(r) if r else None
    finally:
        conn.close()


def notify_users() -> list[dict]:
    """要单独发提醒邮件的账号：不是管理员、没被暂停、填了收件邮箱（管理员的提醒走「设置 → 邮件提醒」）。"""
    conn = _conn()
    try:
        return [_pub(r) for r in conn.execute("SELECT * FROM users WHERE id<>? AND status='active' AND notify_to<>'' ORDER BY id", (settings.ADMIN_ID,)) if not _expiry(r)[1]]
    finally:
        conn.close()


# ---- 管理员：账号管理 ----------------------------------------------------------

def list_users() -> list[dict]:
    now = time.time()
    conn = _conn()
    try:
        out = []
        for r in conn.execute("SELECT * FROM users ORDER BY id"):
            seen = conn.execute("SELECT MAX(last_seen) FROM sessions WHERE user_id=? AND revoked IS NULL", (r["id"],)).fetchone()[0]
            out.append({**_pub(r), "online": bool(seen and now - seen < ONLINE_SECONDS), "last_login_ip": r["last_login_ip"]})
        return out
    finally:
        conn.close()


def login_log(limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute("SELECT ts,username,ip,ok,note FROM login_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))]
    finally:
        conn.close()


def _check_date(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise AuthError(400, "bad_date", "有效期要写成 年-月-日，比如 2026-12-31")


def create_user(username: str, password: str, notify_to: str = "", expires_at: str | None = None) -> dict:
    name = validate_username(username)
    validate_password(password)
    exp = _check_date(expires_at)
    with _lock:
        conn = _conn()
        try:
            if conn.execute("SELECT 1 FROM users WHERE ukey=?", (name.lower(),)).fetchone():
                raise AuthError(409, "exists", "已经有这个用户名了")
            cur = conn.execute("INSERT INTO users(username,ukey,pw_hash,role,status,must_change,pw_default,notify_to,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (name, name.lower(), hash_password(password), "user", "active", 1, 0, "", datetime.now().isoformat(timespec="seconds"), exp))
            conn.commit()
            uid = cur.lastrowid
        finally:
            conn.close()
    if notify_to:
        set_notify_to(uid, notify_to)
    return get_user(uid)


def _target(conn, user_id: int, allow_admin: bool = False, what: str = "暂停或删除"):
    r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not r:
        raise AuthError(404, "no_user", "没有这个账号")
    if r["role"] == "admin" and not allow_admin:
        raise AuthError(400, "is_admin", f"管理员账号不能{what}")
    return r


def set_password(user_id: int, password: str) -> None:
    """管理员给某个账号设（临时）密码：对方被踢下线，下次登录必须改成自己的。管理员自己的密码走「修改密码」。"""
    validate_password(password)
    with _lock:
        conn = _conn()
        try:
            r = _target(conn, user_id, what="用这里重置密码（自己的密码用「修改密码」）")
            conn.execute("UPDATE users SET pw_hash=?, must_change=1, pw_default=0 WHERE id=?", (hash_password(password), r["id"]))
            _revoke(conn, r["id"], "reset")
            conn.commit()
        finally:
            conn.close()


def set_expiry(user_id: int, expires_at: str | None = None, extend_days: int | None = None) -> dict:
    """设有效期：expires_at = 具体日期（含当天；None / 空 = 永久）；extend_days = 在「今天和现在的到期日里较晚的那天」上再加 N 天（续期用；原来是永久的从今天算起）。"""
    with _lock:
        conn = _conn()
        try:
            r = _target(conn, user_id, what="设置有效期（管理员没有有效期）")
            if extend_days is not None:
                n = int(extend_days)
                if not 1 <= n <= 3660:
                    raise AuthError(400, "bad_days", "续期天数要在 1~3660 之间")
                cur = r["expires_at"]
                base = max(date.today(), date.fromisoformat(cur)) if cur else date.today()
                new = (base + timedelta(days=n)).isoformat()
            else:
                new = _check_date(expires_at)
            conn.execute("UPDATE users SET expires_at=? WHERE id=?", (new, r["id"]))
            conn.commit()
        finally:
            conn.close()
    return get_user(user_id)


def set_status(user_id: int, status: str) -> None:
    if status not in ("active", "paused"):
        raise AuthError(400, "bad_status", "状态只能是 active / paused")
    with _lock:
        conn = _conn()
        try:
            r = _target(conn, user_id)
            conn.execute("UPDATE users SET status=? WHERE id=?", (status, r["id"]))
            if status == "paused":
                _revoke(conn, r["id"], "paused")
            conn.commit()
        finally:
            conn.close()


def rename_user(user_id: int, username: str) -> dict:
    name = validate_username(username)
    with _lock:
        conn = _conn()
        try:
            r = _target(conn, user_id, allow_admin=True)
            dup = conn.execute("SELECT id FROM users WHERE ukey=? AND id<>?", (name.lower(), r["id"])).fetchone()
            if dup:
                raise AuthError(409, "exists", "已经有这个用户名了")
            conn.execute("UPDATE users SET username=?, ukey=? WHERE id=?", (name, name.lower(), r["id"]))
            conn.commit()
        finally:
            conn.close()
    return get_user(user_id)


def delete_user(user_id: int) -> dict:
    """删除账号并删除它的全部数据（自选 / 持仓 / 交易 / 账户 / 提醒）。管理员账号不能删。"""
    with _lock:
        conn = _conn()
        try:
            r = _target(conn, user_id)
            conn.execute("DELETE FROM sessions WHERE user_id=?", (r["id"],))
            conn.execute("DELETE FROM users WHERE id=?", (r["id"],))
            conn.commit()
        finally:
            conn.close()
    udir = settings.data_dir() / "users" / str(int(user_id))
    removed = udir.exists()
    shutil.rmtree(udir, ignore_errors=True)
    return {"username": r["username"], "data_removed": removed}


def user_dirs() -> list[Path]:
    """所有非管理员账号的个人库文件（备份用）。"""
    base = settings.data_dir() / "users"
    return sorted(base.glob("*/portfolio.db")) if base.exists() else []
