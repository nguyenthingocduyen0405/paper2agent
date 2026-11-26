"""PaperAgent 웹: 로그인한 사용자가 논문을 올려 작업을 만들고, 진행 상황과 결과를 본다.

화면: 로그인(/login), 작업 목록(/), 새 작업(/new), 작업 상세(/jobs/<id>), 파일 보기, 논문별 결과 비교(/papers/<id>),
      통계(/stats), 내 계정(/account), 사용자 관리(/admin/users, 관리자)
작업 진행(Job 생성, 워커 배정, 집계, S3 보관)은 orchestrator.tick()이 백그라운드에서 TICK_SEC마다 맡는다.
저장: 작업·사용자·결과는 DB(web/db.py, EC2에서는 RDS PostgreSQL), 파일은 /data와 S3(web/blob.py).
실행(쿠버네티스 안, mini-paperbench 폴더 기준): uvicorn web.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import DBAPIError, OperationalError

from web import admin, auth, blob, db, gpu, orchestrator, records, stats, store, views

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("web")
TICK_SEC = float(os.environ.get("TICK_SEC", "5"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"   # 늘 https로만 열 때 1


def _secure_cookie(request: Request) -> bool:
    """https로 들어온 요청이면 쿠키에 Secure를 붙인다(Cloudflare 임시 주소는 X-Forwarded-Proto: https로 넘어온다).
    SSH 터널(http://localhost)로 들어오면 붙이지 않는다."""
    return COOKIE_SECURE or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"         or '"scheme":"https"' in request.headers.get("cf-visitor", "").replace(" ", "")
MAX_UPLOAD = 20 * 1024 * 1024
MIN_PAPER_CHARS = 2000  # agent/prepare.py와 같은 값: 이보다 짧은 글은 논문 본문으로 보지 않는다
TEXT_EXT = (".md", ".markdown", ".txt")
MAX_VIEW = 1024 * 1024
HERE = Path(__file__).resolve().parent
PUBLIC_PATHS = ("/login", "/healthz")
_ready = asyncio.Event()


def init_db() -> None:
    """테이블, 첫 관리자, 예전 파일 기록(job.json) 옮기기. DB가 아직 안 켜졌으면 예외."""
    db.engine()
    admin = auth.ensure_accounts()
    for job_id in store.import_legacy(admin["id"] if admin else None):
        job = store.load(job_id)
        records.backfill(job)
        store.save(job)
        log.info("job %s imported from job.json", job_id)


async def _orchestrate() -> None:
    while not _ready.is_set():
        try:
            await asyncio.to_thread(init_db)
            _ready.set()
            log.info("database ready (%s)", db.url().split("@")[-1])
        except Exception as e:
            db.reset()
            log.warning("database not ready yet: %s", str(e).splitlines()[0][:200])
            await asyncio.sleep(10)
    while True:
        try:
            await asyncio.to_thread(orchestrator.tick)
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(TICK_SEC)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_orchestrate())
    yield
    task.cancel()


app = FastAPI(title="PaperAgent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(fmt_time=views.fmt_time, status_label=views.status_label, fmt_duration=views.fmt_duration,
                             fmt_bytes=views.fmt_bytes, fmt_tokens=views.fmt_tokens)


def _wait_page(status: int = 503) -> HTMLResponse:
    return HTMLResponse("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=15>"
                        "<title>PaperAgent</title><body style='font-family:sans-serif;padding:40px'>"
                        "<h2>데이터베이스를 켜는 중입니다</h2><p>몇 분 걸릴 수 있습니다. 이 화면은 15초마다 다시 시도합니다.</p>",
                        status_code=status)


def _same_origin(request: Request) -> bool:
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return False
    https = _secure_cookie(request)
    origin = request.headers.get("origin")
    if origin is not None:
        if not origin or origin == "null":
            return False
        u = urlsplit(origin)
        return u.netloc.lower() == host and (not https or u.scheme == "https")
    referer = request.headers.get("referer")
    if referer is not None:
        u = urlsplit(referer)
        return bool(u.netloc) and u.netloc.lower() == host and (not https or u.scheme == "https")
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site is not None:
        return sec_fetch_site.strip().lower() in ("same-origin", "none")
    # 비브라우저 API 클라이언트(Authorization: Bearer, 세션 쿠키 없음)만 헤더 없이 허용한다
    has_bearer = (request.headers.get("authorization") or "").startswith("Bearer ")
    has_cookie = bool(getattr(request, "cookies", None) and request.cookies.get(auth.COOKIE))
    return has_bearer and not has_cookie


_TRUSTED_PROXY_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _TRUSTED_PROXY_NETS)
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer and _is_trusted_proxy(peer):
        cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf_ip:
            return cf_ip
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # 신뢰할 수 있는 프록시(Caddy/cloudflared)가 맨 뒤에 붙인 직전 홉을 사용한다(클라이언트 위조 방지)
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            if hops:
                return hops[-1]
    return peer


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path == "/healthz":
        return await call_next(request)
    if not _ready.is_set():
        return _wait_page()
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        return PlainTextResponse("다른 사이트에서 보낸 요청은 받지 않습니다.", status_code=403)
    try:
        if path in PUBLIC_PATHS:
            return await call_next(request)
        user = await asyncio.to_thread(auth.user_for_session, request.cookies.get(auth.COOKIE))
        if user is None:
            user = await asyncio.to_thread(auth.user_for_bearer, request.headers.get("authorization"))
        if user is None:
            if path.startswith("/api/") or request.headers.get("authorization"):
                return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        request.state.user = user
        return await call_next(request)
    except (OperationalError, DBAPIError):
        log.exception("database error")
        return _wait_page()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """모든 응답에 브라우저 보안 헤더를 붙인다. HSTS는 https로 들어온 요청에만 붙인다(학교 서버는 http)."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if _secure_cookie(request):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


def _render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = dict(ctx or {})
    ctx.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def _user(request: Request) -> dict:
    return request.state.user


def _require_admin(request: Request) -> dict:
    user = _user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "관리자만 볼 수 있습니다")
    return user


def _job_or_404(request: Request, job_id: str) -> dict:
    job = store.load(job_id)
    if job is None or not store.can_see(_user(request), job):
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    records.ensure_local(job)
    return job


def _submission_dir(job: dict) -> Path:
    return Path(job["submission_dir"]).resolve()


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# ---------- 로그인, 계정 ----------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return _render(request, "login.html", {"next": _safe_next(next), "error": "", "username": ""})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    user, error = auth.authenticate(username, password, _client_ip(request))
    if user is None:
        log.warning("login failed: user=%r ip=%s", username[:64], _client_ip(request))
        return _render(request, "login.html", {"next": _safe_next(next), "error": error, "username": username}, 400)
    token = auth.new_session(user["id"])
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_SEC, httponly=True, samesite="lax",
                    secure=_secure_cookie(request), path="/")
    log.info("login: %s", user["username"])
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/account", response_class=HTMLResponse)
def account_form(request: Request):
    return _render(request, "account.html", {"errors": [], "saved": False})


@app.post("/account")
def account_save(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form("")):
    user = _user(request)
    errors = []
    if user["role"] == "service":
        errors.append("서비스 계정은 비밀번호가 없습니다.")
    elif auth.authenticate(user["username"], current, _client_ip(request))[0] is None:
        errors.append("지금 비밀번호가 맞지 않습니다.")
    if len(new) < auth.MIN_PASSWORD:
        errors.append(f"새 비밀번호는 {auth.MIN_PASSWORD}자 이상으로 정해 주세요.")
    elif new != confirm:
        errors.append("새 비밀번호 두 칸이 서로 다릅니다.")
    if errors:
        return _render(request, "account.html", {"errors": errors, "saved": False}, 400)
    auth.update_user(user["id"], password=new)  # 다른 로그인은 모두 끊긴다
    token = auth.new_session(user["id"])
    resp = _render(request, "account.html", {"errors": [], "saved": True})
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_SEC, httponly=True, samesite="lax",
                    secure=_secure_cookie(request), path="/")
    return resp


def _parse_limit(text: str) -> tuple[int | None, str]:
    text = (text or "").strip()
    if not text:
        return None, ""
    if not text.isdigit():
        return None, "하루 작업 수는 숫자로 적어 주세요(비우면 제한 없음)."
    return int(text), ""


def _users_page(request: Request, errors: list[str] | None = None, form: dict | None = None, status: int = 200):
    return _render(request, "admin_users.html", {
        "users": auth.list_users(), "usage": admin.user_usage(_kst_midnight()), "errors": errors or [],
        "form": form or {}, "default_limit": auth.DEFAULT_DAILY_LIMIT, "notice": ""}, status)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    _require_admin(request)
    return _users_page(request)


def _target_user(user_id: int) -> dict:
    target = auth.get_user(user_id)
    if target is None or target["role"] == "service":
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    return target


def _user_page(request: Request, target: dict, errors: list[str] | None = None, status: int = 200):
    usage = admin.user_usage(_kst_midnight()).get(target["id"], {})
    recent = store.list_jobs(None, target["id"])[:10]
    return _render(request, "admin_user.html", {"target": target, "usage": usage, "recent": recent,
                                                 "errors": errors or []}, status)


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user(request: Request, user_id: int):
    _require_admin(request)
    return _user_page(request, _target_user(user_id))


@app.post("/admin/users/{user_id}/logout")
def admin_logout_user(request: Request, user_id: int):
    me = _require_admin(request)
    target = _target_user(user_id)
    n = auth.end_user_sessions(user_id)
    log.info("sessions of %s ended by %s (%d)", target["username"], me["username"], n)
    if target["id"] == me["id"]:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(f"/admin/users/{user_id}?loggedout=1", status_code=303)


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(request: Request, user_id: int):
    me = _require_admin(request)
    target = _target_user(user_id)
    if target["id"] == me["id"]:
        return _user_page(request, target, ["자기 계정은 지울 수 없습니다."], status=400)
    if target["role"] == "admin" and not target["disabled"] and auth.active_admins() <= 1:
        return _user_page(request, target, ["마지막 관리자 계정은 지울 수 없습니다."], status=400)
    admin.delete_user_rows(user_id)
    log.warning("user %s deleted by %s", target["username"], me["username"])
    return RedirectResponse("/admin/users?deleted=" + quote(target["username"]), status_code=303)


@app.post("/admin/users")
def admin_create_user(request: Request, username: str = Form(""), password: str = Form(""), role: str = Form("user"),
                      daily_job_limit: str = Form("")):
    _require_admin(request)
    username = username.strip()
    errors = auth.check_new_user(username, password, role)
    limit, err = _parse_limit(daily_job_limit)
    if err:
        errors.append(err)
    if not errors and auth.find_user(username):
        errors.append("이미 있는 아이디입니다.")
    if errors:
        return _users_page(request, errors, {"username": username, "role": role, "daily_job_limit": daily_job_limit}, 400)
    auth.create_user(username, password, role, limit)
    log.info("user created: %s (%s) by %s", username, role, _user(request)["username"])
    return RedirectResponse("/admin/users?created=" + quote(username), status_code=303)


@app.post("/admin/users/{user_id}")
def admin_update_user(request: Request, user_id: int, daily_job_limit: str = Form(""), new_password: str = Form(""),
                      disabled: str = Form(""), role: str = Form("")):
    me = _require_admin(request)
    target = _target_user(user_id)
    limit, err = _parse_limit(daily_job_limit)
    errors = [err] if err else []
    role = role or target["role"]
    if role not in auth.ROLES:
        errors.append("역할을 다시 골라 주세요.")
    if new_password and len(new_password) < auth.MIN_PASSWORD:
        errors.append(f"비밀번호는 {auth.MIN_PASSWORD}자 이상으로 정해 주세요.")
    if target["id"] == me["id"] and disabled:
        errors.append("자기 계정은 사용 중지할 수 없습니다.")
    if target["id"] == me["id"] and role != target["role"]:
        errors.append("자기 계정의 역할은 바꿀 수 없습니다. 다른 관리자에게 부탁해 주세요.")
    losing_admin = target["role"] == "admin" and not target["disabled"] and (role != "admin" or disabled)
    if losing_admin and auth.active_admins() <= 1:
        errors.append("마지막 관리자의 역할을 바꾸거나 사용 중지할 수 없습니다.")
    if errors:
        return _user_page(request, target, errors, status=400)
    values = {"daily_job_limit": limit, "disabled": bool(disabled), "role": role}
    if new_password:
        values["password"] = new_password
    auth.update_user(user_id, **values)
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


# ---------- 작업 ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = _user(request)
    owner = request.query_params.get("owner")
    owner_id = int(owner) if user["role"] == "admin" and owner and owner.isdigit() else None
    jobs = store.list_jobs(user, owner_id)
    summaries = {}
    for j in jobs:
        try:
            summaries[j["id"]] = views.summary(j)
        except Exception:
            summaries[j["id"]] = ""
    running = any(j["status"] == "running" for j in jobs)
    owners = {u["id"]: u["username"] for u in auth.list_users()} if user["role"] == "admin" else {}
    return _render(request, "index.html", {"jobs": jobs, "summaries": summaries, "running": running, "owners": owners,
                                           "owner_id": owner_id})


def _new_form(request: Request, errors: list[str] | None = None, form: dict | None = None, status: int = 200):
    admin = _user(request)["role"] == "admin"
    return _render(request, "new.html", {
        "library": store.library() if admin else [], "submissions": store.submissions() if admin else [],
        "errors": errors or [], "form": form or {}, "admin": admin, "quota": _quota(_user(request))}, status)


def _kst_midnight() -> float:
    now = datetime.now(views.KST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _quota(user: dict) -> dict | None:
    if user.get("daily_job_limit") is None:
        return None
    used = auth.jobs_today(user["id"], _kst_midnight())
    return {"limit": user["daily_job_limit"], "used": used, "left": max(0, user["daily_job_limit"] - used)}


@app.get("/new", response_class=HTMLResponse)
def new_job_form(request: Request):
    return _new_form(request)


_ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")
_ARXIV_HOSTS = ("arxiv.org", "www.arxiv.org", "export.arxiv.org")


def _parse_arxiv_id(raw: str) -> str | None:
    """arXiv ID 또는 URL(abs/pdf/html)에서 정규 ID만 뽑는다(SSRF 방지를 위해 허용된 형식만 받는다)."""
    s = (raw or "").strip()
    if not s:
        return None
    if s.lower().startswith("arxiv:"):
        s = s[6:].strip()
    if s.lower().startswith(("http://", "https://")):
        u = urlsplit(s)
        if (u.hostname or "").lower() not in _ARXIV_HOSTS:
            return None
        path = u.path.strip("/")
        for prefix in ("abs/", "pdf/", "html/"):
            if path.startswith(prefix):
                s = path[len(prefix):]
                break
        else:
            return None
        if s.endswith(".pdf"):
            s = s[:-4]
    return s if _ARXIV_ID_RE.fullmatch(s) else None


class _ArxivOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlsplit(newurl)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "arxiv.org" or host.endswith(".arxiv.org")):
            raise urllib.error.URLError(f"blocked non-arXiv redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_arxiv_opener = urllib.request.build_opener(_ArxivOnlyRedirect)


def _fetch_arxiv_pdf(arxiv_id: str) -> tuple[bytes, str]:
    """arXiv 공식 PDF 엔드포인트에서만 PDF를 내려받는다(최대 20MB, 리다이렉트도 arxiv.org HTTPS로 제한)."""
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    req = urllib.request.Request(url, headers={"User-Agent": "PaperAgent/1.0"})
    try:
        with _arxiv_opener.open(req, timeout=30) as r:
            data = r.read(MAX_UPLOAD + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"arXiv 논문({arxiv_id})을 찾을 수 없습니다.") from None
        raise ValueError(f"arXiv에서 PDF를 내려받지 못했습니다(HTTP {e.code}).") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ValueError(f"arXiv에 연결하지 못했습니다: {e}") from None
    if len(data) > MAX_UPLOAD:
        raise ValueError("arXiv PDF 파일이 너무 큽니다(20MB까지).")
    if b"%PDF-" not in data[:1024]:
        raise ValueError("arXiv에서 올바른 PDF 파일을 받지 못했습니다.")
    return data, f"arxiv_{arxiv_id.replace('/', '_')}.pdf"


async def _read_upload(f: UploadFile | None) -> bytes | None:
    if f is None or not f.filename:
        return None
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"{f.filename}: 파일이 너무 큽니다(20MB까지).")
    return data


@app.post("/jobs")
async def create_job(
    request: Request,
    library_key: str = Form(""),
    arxiv_url: str = Form(""),
    paper_file: UploadFile | None = File(None),
    rubric_file: UploadFile | None = File(None),
    addendum_file: UploadFile | None = File(None),
    limit: str = Form(""),
    mock: str = Form(""),
    skip_submission: str = Form(""),
    new_rubric: str = Form(""),
):
    # 사용자는 논문 파일 또는 arXiv 링크/ID를 올린다. 예제 논문(실험용 설정)을 고르면 그것을 쓴다.
    # rubric_file, addendum_file은 화면에는 없고 API로 시험할 때만 쓴다(없으면 채점 기준을 자동으로 설계한다).
    # 같은 논문(파일 내용이 같으면)을 다시 올리면 전에 설계한 채점 기준을 다시 쓴다. 그래야 결과를 비교할 수 있다.
    user = _user(request)
    admin = user["role"] == "admin"
    arxiv_url = arxiv_url.strip()
    source = "library" if library_key else "upload"
    form = {"library_key": library_key, "arxiv_url": arxiv_url, "limit": limit, "mock": mock,
            "skip_submission": skip_submission, "new_rubric": new_rubric}
    errors = []
    if not admin and (library_key or limit.strip() or mock or skip_submission or new_rubric):
        errors.append("실험용 설정은 관리자만 쓸 수 있습니다.")
    limit = limit.strip()
    if limit and (not limit.isdigit() or int(limit) < 1):
        errors.append("검증 항목 수는 1 이상의 숫자로 적어 주세요.")
    if skip_submission and skip_submission not in {s["dir"] for s in store.submissions()}:
        errors.append("고른 기존 제출물을 찾을 수 없습니다.")
    has_file = paper_file is not None and bool(paper_file.filename)
    if sum(bool(x) for x in (library_key, has_file, arxiv_url)) > 1:
        errors.append("논문 파일, arXiv 주소, 예제 논문 가운데 하나만 골라 주세요.")
    quota = _quota(user)
    if quota and quota["left"] <= 0:
        errors.append(f"오늘 만들 수 있는 작업 {quota['limit']}개를 모두 썼습니다. 내일 다시 시도하거나 관리자에게 문의해 주세요.")
    if errors:
        return _new_form(request, errors, form, 400)

    job_id = store.new_id()
    job = {"id": job_id, "created_at": time.time(), "status": "running", "stage": "queued",
           "judge_run": job_id, "events": [], "user_id": user["id"], "llm_model": os.environ.get("LLM_MODEL")}
    if limit:
        job["limit"] = int(limit)
    counts = None  # (전체 항목 수, 코드 구현 항목 수). 논문 준비 단계가 있으면 그 뒤에 정한다

    if source == "upload":
        try:
            paper = await _read_upload(paper_file)
            rubric = await _read_upload(rubric_file)
            addendum = await _read_upload(addendum_file)
        except ValueError as e:
            return _new_form(request, [str(e)], form, 400)
        paper_filename = paper_file.filename if paper_file and paper_file.filename else ""
        if paper is None and arxiv_url:
            aid = _parse_arxiv_id(arxiv_url)
            if not aid:
                return _new_form(request, ["올바른 arXiv ID 또는 주소가 아닙니다(예: 2201.02177 또는 https://arxiv.org/abs/2201.02177)."], form, 400)
            try:
                paper, paper_filename = await asyncio.to_thread(_fetch_arxiv_pdf, aid)
            except ValueError as e:
                return _new_form(request, [str(e)], form, 400)
        is_pdf = False
        if paper is None:
            errors.append("논문 파일을 올리거나 arXiv 주소를 입력해 주세요.")
        else:
            name = paper_filename.lower()
            is_pdf = b"%PDF-" in paper[:1024]  # PDF 규격은 앞쪽 1KB 안에 머리말이 있으면 된다
            if name.endswith(".pdf") and not is_pdf:
                errors.append("PDF 파일을 읽을 수 없습니다. 빈 파일이거나 손상된 파일입니다.")
            elif not is_pdf and not name.endswith(TEXT_EXT):
                errors.append("논문은 PDF 또는 markdown(.md) 파일로 올려 주세요.")
        texts = {}
        if not errors:
            try:
                texts = {"paper.md": None if is_pdf else paper.decode("utf-8-sig"),
                         "addendum.md": addendum.decode("utf-8-sig") if addendum else None}
            except UnicodeDecodeError:
                errors.append("논문(.md)과 추가 설명 파일을 UTF-8 텍스트로 읽을 수 없습니다.")
            if texts.get("paper.md") is not None and len(texts["paper.md"].strip()) < MIN_PAPER_CHARS:
                errors.append("논문 내용이 너무 짧습니다. 논문 본문 전체를 올려 주세요.")
        if errors:
            return _new_form(request, errors, form, 400)
        paper_dir = store.job_dir(job_id) / "paper"
        paper_dir.mkdir(parents=True)
        if is_pdf:
            (paper_dir / "paper.pdf").write_bytes(paper)
        else:
            (paper_dir / "paper.md").write_text(texts["paper.md"])
        if texts["addendum.md"]:
            (paper_dir / "addendum.md").write_text(texts["addendum.md"])
        if rubric is not None:
            (paper_dir / "rubric.json").write_bytes(rubric)
            try:
                counts = store.count_leaves(paper_dir / "rubric.json")
                if counts[0] == 0:
                    raise ValueError("rubric has no leaves")
            except Exception:
                shutil.rmtree(store.job_dir(job_id), ignore_errors=True)
                return _new_form(request, ["rubric.json을 읽을 수 없습니다. PaperBench 채점 기준 형식인지 확인해 주세요. "
                                           "채점 기준이 없으면 비워 두세요. 자동으로 만듭니다."], form, 400)
        title = Path(paper_filename).stem if is_pdf else store.paper_title(paper_dir / "paper.md")
        fp = records.fingerprint(paper)
        paper_id = records.register_paper(fp, None if is_pdf else title, paper_filename,
                                          "pdf" if is_pdf else "md", user["id"])
        known = records.get_paper(paper_id)
        if known is not None and known.title:
            title = known.title
        source_label = f"arXiv ({_parse_arxiv_id(arxiv_url)})" if arxiv_url else ("직접 올린 논문" + (" (PDF)" if is_pdf else ""))
        job.update(paper_dir=paper_dir.as_posix(), title=title, paper_fingerprint=fp, paper_id=paper_id,
                   source_label=source_label)
        reuse = None
        if rubric is not None:
            job.update(rubric_id=records.register_rubric(paper_id, paper_dir / "rubric.json", "given", job_id),
                       rubric_origin="given")
        elif not new_rubric:
            reuse = records.latest_rubric(paper_id)
        if reuse is not None:
            (paper_dir / "rubric.json").write_text(reuse.content)
            counts = (reuse.leaves, reuse.code_dev)
            job.update(rubric_id=reuse.id, rubric_origin=reuse.origin, rubric_from=reuse.job_id)
            store.add_event(job, f"전에 올린 논문과 같은 파일이라 그때의 채점 기준(작업 {reuse.job_id})을 다시 씁니다. "
                                 "결과를 이전 작업과 비교할 수 있습니다.")
            if is_pdf and records.fetch_prepared_md(fp, reuse.job_id, paper_dir / "paper.md"):
                (paper_dir / "meta.json").write_text(json.dumps({"title": title}, ensure_ascii=False))
        need_md = is_pdf and not (paper_dir / "paper.md").exists()
        need_rubric = rubric is None and reuse is None
        if need_md or need_rubric:
            # 에이전트보다 먼저 논문 준비 Job이 PDF를 바꾸고 채점 기준을 만든다(orchestrator의 prepare 단계)
            job.update(stage="prepare_wait", prepare_pdf=need_md, rubric_auto=need_rubric)
            counts = None
        records.upload_paper(job, "paper.pdf" if is_pdf else "paper.md", paper,
                             "application/pdf" if is_pdf else "text/markdown; charset=utf-8")
    else:
        item = next((x for x in store.library() if x["key"] == library_key), None)
        if item is None:
            return _new_form(request, ["논문을 골라 주세요."], form, 400)
        counts = (item["leaves"], item["code_dev"])
        fp = "library:" + item["key"]
        paper_id = records.register_paper(fp, item["title"], None, "library", user["id"])
        records.set_paper(paper_id, title=item["title"])
        job.update(paper_dir=item["dir"], title=item["title"], source_label=f"{item['kind']} 예제",
                   paper_fingerprint=fp, paper_id=paper_id, rubric_origin="given",
                   rubric_id=records.register_rubric(paper_id, Path(item["dir"]) / "rubric.json", "given", job_id))

    if counts:
        store.plan_judging(job, *counts)
    job["mock"] = mock == "1"
    if skip_submission:
        job.update(skip_agent=True, submission_dir=skip_submission)
    else:
        job["submission_dir"] = (store.DATA / "agent_runs" / job_id / "submission").as_posix()
    store.add_event(job, "작업을 접수했습니다")
    store.save(job)
    log.info("job %s created by %s: %s", job_id, user["username"], job["title"])
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _job_context(request: Request, job: dict) -> dict:
    ctx = views.live_context(job)
    ctx["compare_n"] = stats.paper_job_count(job.get("paper_id"), _user(request))
    return ctx


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    return _render(request, "job.html", _job_context(request, _job_or_404(request, job_id)))


@app.get("/jobs/{job_id}/live", response_class=HTMLResponse)
def job_live(request: Request, job_id: str):
    """작업 상세 화면에서 진행 중일 때 몇 초마다 다시 받는 부분."""
    return _render(request, "_live.html", _job_context(request, _job_or_404(request, job_id)))


@app.get("/api/jobs/{job_id}")
def job_api(request: Request, job_id: str) -> dict:
    job = _job_or_404(request, job_id)
    out = {key: job.get(key) for key in ("id", "title", "status", "stage", "error", "created_at", "finished_at",
                                         "score", "planned_leaves", "mock", "skip_agent")}
    out["agent"] = views.agent_progress(job)
    judge = views.judge_progress(job)
    out["judge"] = {key: v for key, v in judge.items() if key != "recent"} if judge else None
    return out


@app.get("/api/jobs/{job_id}/files")
def job_files_api(request: Request, job_id: str) -> dict:
    """만든 코드 파일 목록. Slack 도우미(hermes/paper_mcp.py)가 코드를 설명할 때 쓴다."""
    job = _job_or_404(request, job_id)
    return {"id": job_id, "files": [{"path": f["path"], "size": f["size"]}
                                     for f in views.files_in(_submission_dir(job))]}


@app.get("/api/jobs/{job_id}/raw/{path:path}")
def job_file_raw(request: Request, job_id: str, path: str):
    """코드 파일 하나의 내용(텍스트). 숨김 파일과 제출물 밖의 경로는 주지 않는다."""
    job = _job_or_404(request, job_id)
    sub = _submission_dir(job)
    target = (sub / path).resolve()
    if not target.is_relative_to(sub) or not target.is_file() or not views.is_shown(target.relative_to(sub)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    if target.stat().st_size > MAX_VIEW:
        raise HTTPException(413, "파일이 너무 큽니다")
    return PlainTextResponse(target.read_text(errors="replace"))


@app.get("/jobs/{job_id}/code.zip")
def download_zip(request: Request, job_id: str):
    job = _job_or_404(request, job_id)
    if blob.enabled() and job.get("archived_at") and job.get("status") == "done":
        # 끝난 작업은 S3에 올린 묶음을 브라우저가 바로 받는다(15분 동안 유효한 주소)
        return RedirectResponse(blob.presign(records.bundle_key(job), f"paperagent-{job_id}.zip"), status_code=303)
    data = views.bundle_zip(job)
    if data is None:
        raise HTTPException(404, "아직 만든 코드가 없습니다")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="paperagent-{job_id}.zip"'})


@app.get("/jobs/{job_id}/report.md")
def download_report(request: Request, job_id: str):
    report = views.report_md(_job_or_404(request, job_id))
    if report is None:
        raise HTTPException(404, "작업이 끝나야 보고서를 만들 수 있습니다")
    return Response(report, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="paperagent-{job_id}-report.md"'})


@app.get("/jobs/{job_id}/files/{path:path}", response_class=HTMLResponse)
def view_file(request: Request, job_id: str, path: str):
    job = _job_or_404(request, job_id)
    sub = _submission_dir(job)
    target = (sub / path).resolve()
    if not target.is_relative_to(sub) or not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    if target.stat().st_size > MAX_VIEW:
        text = "(파일이 너무 커서 화면에 보여 줄 수 없습니다. zip으로 내려받아 확인해 주세요.)"
    else:
        text = target.read_text(errors="replace")
    return _render(request, "file.html", {"job": job, "path": path, "text": text})


# ---------- GPU 실행 ----------

def _owner_or_admin(request: Request, job: dict) -> None:
    user = _user(request)
    if user["role"] != "admin" and job.get("user_id") != user["id"]:
        raise HTTPException(403, "작업을 만든 사람과 관리자만 실행할 수 있습니다")


def _back(next_url: str, default: str, **params: str) -> RedirectResponse:
    """관리 화면(/admin/...)에서 눌렀으면 그 화면으로, 아니면 default로 돌아간다. 알림은 쿼리로 붙인다."""
    url = next_url if next_url.startswith("/admin/") and not next_url.startswith("//") else default
    if params:
        url += ("&" if "?" in url else "?") + "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return RedirectResponse(url, status_code=303)


@app.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: str, next: str = Form("")):
    job = _job_or_404(request, job_id)
    user = _user(request)
    if user["role"] != "admin" and job.get("user_id") != user["id"]:
        raise HTTPException(403, "작업을 만든 사람과 관리자만 취소할 수 있습니다")
    reason = admin.request_cancel(job, user["username"])
    if reason:
        raise HTTPException(409, reason)
    if next.startswith("/admin/"):
        return _back(next, "", cancelled=job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry")
def job_retry(request: Request, job_id: str, next: str = Form("")):
    me = _require_admin(request)
    reason = admin.retry(_job_or_404(request, job_id), me["username"])
    if reason:
        raise HTTPException(409, reason)
    if next.startswith("/admin/"):
        return _back(next, "", retried=job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/delete")
def job_delete(request: Request, job_id: str, next: str = Form("")):
    me = _require_admin(request)
    job = store.load(job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    reason = admin.delete_job(job, me["username"])
    if reason:
        raise HTTPException(409, reason)
    return _back(next, "/", deleted=job_id)


# ---------- 관리 메뉴: 개요, 작업 관리, 사용자, 서버·Pod, 저장소 ----------

@app.get("/admin", response_class=HTMLResponse)
def admin_overview(request: Request):
    _require_admin(request)
    return _render(request, "admin_overview.html", {"s": admin.status("public", "host", "queue", "llm", "rds"),
                                                    "lab_hours": admin.LAB_SESSION_SEC // 3600})


@app.get("/admin/system")
def admin_system_moved(request: Request):
    _require_admin(request)
    return RedirectResponse("/admin", status_code=303)


JOB_FILTERS = ("running", "done", "failed", "cancelled")


@app.get("/admin/jobs", response_class=HTMLResponse)
def admin_jobs(request: Request, status: str = "", owner: str = ""):
    _require_admin(request)
    owner_id = int(owner) if owner.isdigit() else None
    every = store.list_jobs(None, owner_id)
    counts = {"": len(every), **{k: sum(1 for j in every if j["status"] == k) for k in JOB_FILTERS}}
    status = status if status in JOB_FILTERS else ""
    jobs = [j for j in every if not status or j["status"] == status]
    summaries = {}
    for j in jobs:
        try:
            summaries[j["id"]] = views.summary(j)
        except Exception:
            summaries[j["id"]] = ""
    users = auth.list_users()
    return _render(request, "admin_jobs.html", {
        "jobs": jobs, "summaries": summaries, "counts": counts, "status": status, "owner_id": owner_id,
        "users": users, "owners": {u["id"]: u["username"] for u in users},
        "next_url": f"/admin/jobs?status={status}&owner={owner_id or ''}",
        "running": any(j["status"] == "running" for j in jobs)})


@app.get("/admin/servers", response_class=HTMLResponse)
def admin_servers(request: Request):
    _require_admin(request)
    return _render(request, "admin_servers.html", {"s": admin.status("cluster", "host")})


def _storage_page(request: Request, error: str = "", status: int = 200):
    return _render(request, "admin_storage.html", {"s": admin.status("rds", "s3", "redis", "host"), "error": error},
                   status)


@app.get("/admin/storage", response_class=HTMLResponse)
def admin_storage(request: Request):
    _require_admin(request)
    return _storage_page(request)


@app.post("/admin/system/rds-stop")
def admin_rds_stop(request: Request):
    me = _require_admin(request)
    try:
        reason = admin.stop_rds(me["username"])
    except Exception as e:
        reason = f"RDS를 끄지 못했습니다: {type(e).__name__}: {str(e)[:300]}"
    if reason:
        return _storage_page(request, reason, 409)
    return _render(request, "rds_stopping.html", {})


@app.post("/jobs/{job_id}/gpu")
def gpu_action(request: Request, job_id: str, action: str = Form("")):
    """action: run(GPU에서 실행, 작업 화면의 실행 버튼), cancel(실행 취소)."""
    job = _job_or_404(request, job_id)
    _owner_or_admin(request, job)
    if action == "run":
        reason = gpu.runnable(job)
        if reason:
            raise HTTPException(409, reason)
        gpu.request(job, _user(request))
        log.info("job %s: GPU run requested by %s", job_id, _user(request)["username"])
    elif action == "cancel":
        reason = gpu.cancel(job)
        if reason:
            raise HTTPException(409, reason)
    else:
        raise HTTPException(400, "알 수 없는 요청입니다")
    return RedirectResponse(f"/jobs/{job_id}#gpu-card", status_code=303)


@app.get("/jobs/{job_id}/gpu/log")
def gpu_log(request: Request, job_id: str):
    path = views.gpu_log_path(_job_or_404(request, job_id))
    if path is None:
        raise HTTPException(404, "실행 기록이 없습니다")
    data = path.read_bytes()
    if len(data) > 2 * MAX_VIEW:
        data = "(앞부분 생략: 전체 기록은 코드 묶음(.zip)의 PAPERAGENT_GPU_RUN/reproduce.log에 있습니다)\n...\n".encode() \
            + data[-2 * MAX_VIEW:]
    return PlainTextResponse(data.decode("utf-8", errors="replace"), headers={"X-Content-Type-Options": "nosniff"})


@app.get("/jobs/{job_id}/gpu/files/{path:path}")
def gpu_file(request: Request, job_id: str, path: str):
    """실행 중 생긴 파일. 그림은 화면에 보이고, 글은 텍스트로, 나머지는 내려받는다. 실행한 코드가 만든 파일이라 HTML로는 열지 않는다."""
    target = views.gpu_output_path(_job_or_404(request, job_id), path)
    if target is None:
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"}
    ctype = views.IMAGE_EXT.get(target.suffix.lower())
    if ctype:
        return Response(target.read_bytes(), media_type=ctype, headers=headers)
    if target.suffix.lower() in (".txt", ".log", ".csv", ".tsv", ".json", ".jsonl", ".md", ".yaml", ".yml") \
            and target.stat().st_size <= MAX_VIEW:
        return PlainTextResponse(target.read_text(errors="replace"), headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
    return Response(target.read_bytes(), media_type="application/octet-stream", headers=headers)


# ---------- 비교, 통계 ----------

@app.get("/papers/{paper_id}", response_class=HTMLResponse)
def compare_page(request: Request, paper_id: int, rubric: int | None = None):
    data = stats.compare(paper_id, _user(request), rubric)
    if data is None:
        raise HTTPException(404, "논문을 찾을 수 없습니다")
    return _render(request, "compare.html", data)


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return _render(request, "stats.html", stats.overview(_user(request)))
