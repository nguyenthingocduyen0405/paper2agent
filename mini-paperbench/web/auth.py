"""로그인: 사용자 계정, 비밀번호(scrypt), 세션 쿠키, API 토큰.

- 비밀번호는 hashlib.scrypt로 바꿔 저장한다(원문은 어디에도 남기지 않는다).
- 세션: 로그인하면 임의 토큰을 쿠키(pa_session, HttpOnly, SameSite=Lax)로 주고, DB에는 그 SHA-256만 둔다.
- 첫 관리자: ADMIN_USERNAME, ADMIN_PASSWORD(Secret web-secrets)로 처음 한 번 만든다. 이후 비밀번호는 화면에서 바꾼다.
- 서비스 토큰: SERVICE_TOKEN이 있으면 Authorization: Bearer <토큰>으로 API를 쓸 수 있다(Slack 도우미).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from sqlalchemy import delete, func, select, update

from web import db

COOKIE = "pa_session"
SESSION_SEC = 7 * 24 * 3600
USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,32}")
MIN_PASSWORD = 8
ROLES = ("user", "admin")
DEFAULT_DAILY_LIMIT = 3
SERVICE_USER = "service"
_N, _R, _P = 2 ** 14, 8, 1
LOCK_AFTER, LOCK_SEC = 5, 60
IP_LOCK_AFTER = 15
_MAX_TRACKED_KEYS = 4096
_failures: dict[str, list[float]] = {}
_ip_failures: dict[str, list[float]] = {}
_fail_lock = threading.Lock()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(want) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # 없는 사용자도 같은 시간이 걸리게


def _public(row) -> dict:
    return {"id": row.id, "username": row.username, "role": row.role, "daily_job_limit": row.daily_job_limit,
            "disabled": bool(row.disabled), "created_at": row.created_at}


def check_new_user(username: str, password: str, role: str) -> list[str]:
    errors = []
    if not USERNAME_RE.fullmatch(username or ""):
        errors.append("아이디는 영문, 숫자, 점(.), 밑줄(_), 하이픈(-)으로 3~32자로 적어 주세요.")
    if len(password or "") < MIN_PASSWORD:
        errors.append(f"비밀번호는 {MIN_PASSWORD}자 이상으로 정해 주세요.")
    if role not in ROLES:
        errors.append("역할을 골라 주세요.")
    return errors


def create_user(username: str, password: str, role: str = "user", daily_job_limit: int | None = DEFAULT_DAILY_LIMIT) -> dict:
    with db.engine().begin() as conn:
        res = conn.execute(db.users.insert().values(
            username=username, password_hash=hash_password(password), role=role,
            daily_job_limit=daily_job_limit, disabled=False, created_at=time.time()))
        uid = res.inserted_primary_key[0]
        return _public(conn.execute(select(db.users).where(db.users.c.id == uid)).one())


def get_user(user_id: int) -> dict | None:
    with db.engine().connect() as conn:
        row = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
    return _public(row) if row else None


def find_user(username: str) -> dict | None:
    with db.engine().connect() as conn:
        row = conn.execute(select(db.users).where(db.users.c.username == username)).first()
    return _public(row) if row else None


def list_users() -> list[dict]:
    with db.engine().connect() as conn:
        rows = conn.execute(select(db.users).where(db.users.c.role != "service").order_by(db.users.c.id)).all()
    return [_public(r) for r in rows]


def update_user(user_id: int, **values) -> None:
    if "password" in values:
        values["password_hash"] = hash_password(values.pop("password"))
    with db.engine().begin() as conn:
        conn.execute(update(db.users).where(db.users.c.id == user_id).values(**values))
        if values.get("disabled") or "password_hash" in values:
            conn.execute(delete(db.sessions).where(db.sessions.c.user_id == user_id))  # 다른 기기의 로그인도 끊는다


def end_user_sessions(user_id: int) -> int:
    """이 사용자의 모든 로그인을 끊는다."""
    with db.engine().begin() as conn:
        return conn.execute(delete(db.sessions).where(db.sessions.c.user_id == user_id)).rowcount or 0


def active_admins() -> int:
    with db.engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(db.users)
                            .where(db.users.c.role == "admin", db.users.c.disabled.is_(False))).scalar() or 0


def _prune_bucket(bucket: dict[str, list[float]], key: str, now: float) -> list[float]:
    recent = [t for t in bucket.get(key, []) if now - t < LOCK_SEC]
    if recent:
        bucket[key] = recent
    else:
        bucket.pop(key, None)
    if len(bucket) > _MAX_TRACKED_KEYS:
        stale = [k for k, ts in bucket.items() if not any(now - t < LOCK_SEC for t in ts)]
        for k in stale:
            bucket.pop(k, None)
    return recent


def _locked(username: str, client_ip: str = "") -> bool:
    now = time.time()
    with _fail_lock:
        if len(_prune_bucket(_failures, username, now)) >= LOCK_AFTER:
            return True
        if client_ip and len(_prune_bucket(_ip_failures, client_ip, now)) >= IP_LOCK_AFTER:
            return True
        return False


def _note_failure(username: str, client_ip: str = "") -> None:
    now = time.time()
    with _fail_lock:
        _prune_bucket(_failures, username, now)
        _failures.setdefault(username, []).append(now)
        if client_ip:
            _prune_bucket(_ip_failures, client_ip, now)
            _ip_failures.setdefault(client_ip, []).append(now)


def authenticate(username: str, password: str, client_ip: str = "") -> tuple[dict | None, str]:
    """(사용자, 오류 문구). 같은 아이디 또는 같은 IP에서 연달아 틀리면 잠시 막는다."""
    username = (username or "").strip()
    client_ip = (client_ip or "").strip()
    if _locked(username, client_ip):
        return None, "비밀번호를 여러 번 틀렸습니다. 1분 뒤에 다시 시도해 주세요."
    with db.engine().connect() as conn:
        row = conn.execute(select(db.users).where(db.users.c.username == username)).first()
    if row is None or row.role == "service":
        verify_password(password or "", _DUMMY_HASH)
        _note_failure(username, client_ip)
        return None, "아이디 또는 비밀번호가 맞지 않습니다."
    if not verify_password(password or "", row.password_hash):
        _note_failure(username, client_ip)
        return None, "아이디 또는 비밀번호가 맞지 않습니다."
    if row.disabled:
        return None, "사용이 중지된 계정입니다. 관리자에게 문의해 주세요."
    with _fail_lock:
        _failures.pop(username, None)
    return _public(row), ""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.engine().begin() as conn:
        conn.execute(delete(db.sessions).where(db.sessions.c.expires_at < now))
        conn.execute(db.sessions.insert().values(token_hash=_token_hash(token), user_id=user_id,
                                                 created_at=now, expires_at=now + SESSION_SEC))
    return token


def end_session(token: str | None) -> None:
    if token:
        with db.engine().begin() as conn:
            conn.execute(delete(db.sessions).where(db.sessions.c.token_hash == _token_hash(token)))


def user_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    q = (select(db.users).join(db.sessions, db.sessions.c.user_id == db.users.c.id)
         .where(db.sessions.c.token_hash == _token_hash(token), db.sessions.c.expires_at > time.time()))
    with db.engine().connect() as conn:
        row = conn.execute(q).first()
    if row is None or row.disabled:
        return None
    return _public(row)


def user_for_bearer(header: str | None) -> dict | None:
    want = os.environ.get("SERVICE_TOKEN", "")
    if not want or not header or not header.startswith("Bearer "):
        return None
    if not hmac.compare_digest(header[7:].strip().encode(), want.encode()):
        return None
    return find_user(SERVICE_USER)


def ensure_accounts() -> dict | None:
    """첫 관리자와 서비스 계정을 만든다(이미 있으면 그대로 둔다). 관리자를 돌려준다."""
    admin = None
    name, password = os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
    if name and password:
        admin = find_user(name) or create_user(name, password, "admin", None)
    if os.environ.get("SERVICE_TOKEN") and find_user(SERVICE_USER) is None:
        with db.engine().begin() as conn:
            conn.execute(db.users.insert().values(
                username=SERVICE_USER, password_hash="!", role="service", daily_job_limit=None,
                disabled=False, created_at=time.time()))
    if admin is None:
        with db.engine().connect() as conn:
            row = conn.execute(select(db.users).where(db.users.c.role == "admin").order_by(db.users.c.id)).first()
        admin = _public(row) if row else None
    return admin


def jobs_today(user_id: int, since: float) -> int:
    with db.engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(db.jobs)
                            .where(db.jobs.c.user_id == user_id, db.jobs.c.created_at >= since)).scalar() or 0
