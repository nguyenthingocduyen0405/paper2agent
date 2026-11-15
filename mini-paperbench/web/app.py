"""PaperAgent 웹: 논문을 받아 작업을 만들고, 진행 상황과 결과를 보여 준다.

화면: 작업 목록(/), 새 작업(/new), 작업 상세(/jobs/<id>), 파일 보기.
작업 진행(Job 생성, 워커 배정, 집계)은 orchestrator.tick()이 백그라운드에서 TICK_SEC마다 맡는다.
실행(쿠버네티스 안, mini-paperbench 폴더 기준): uvicorn web.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web import orchestrator, store, views

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("web")
TICK_SEC = float(os.environ.get("TICK_SEC", "5"))
MAX_UPLOAD = 20 * 1024 * 1024
MIN_PAPER_CHARS = 2000  # agent/prepare.py와 같은 값: 이보다 짧은 글은 논문 본문으로 보지 않는다
TEXT_EXT = (".md", ".markdown", ".txt")
MAX_VIEW = 1024 * 1024
HERE = Path(__file__).resolve().parent


async def _orchestrate() -> None:
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
templates.env.globals.update(fmt_time=views.fmt_time, status_label=views.status_label)


def _job_or_404(job_id: str) -> dict:
    job = store.load(job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return job


def _submission_dir(job: dict) -> Path:
    return Path(job["submission_dir"]).resolve()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    jobs = store.list_jobs()
    summaries = {}
    for j in jobs:
        try:
            summaries[j["id"]] = views.summary(j)
        except Exception:
            summaries[j["id"]] = ""
    running = any(j["status"] == "running" for j in jobs)
    return templates.TemplateResponse(request, "index.html", {"jobs": jobs, "summaries": summaries, "running": running})


def _new_form(request: Request, errors: list[str] | None = None, form: dict | None = None, status: int = 200):
    return templates.TemplateResponse(
        request, "new.html",
        {"library": store.library(), "submissions": store.submissions(), "errors": errors or [], "form": form or {}},
        status_code=status)


@app.get("/new", response_class=HTMLResponse)
def new_job_form(request: Request):
    return _new_form(request)


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
    paper_file: UploadFile | None = File(None),
    rubric_file: UploadFile | None = File(None),
    addendum_file: UploadFile | None = File(None),
    limit: str = Form(""),
    mock: str = Form(""),
    skip_submission: str = Form(""),
):
    # 사용자는 논문만 올린다. 예제 논문(실험용 설정)을 고르면 그것을 쓴다.
    # rubric_file, addendum_file은 화면에는 없고 API로 시험할 때만 쓴다(없으면 채점 기준을 자동으로 설계한다).
    source = "library" if library_key else "upload"
    form = {"library_key": library_key, "limit": limit, "mock": mock, "skip_submission": skip_submission}
    errors = []
    limit = limit.strip()
    if limit and (not limit.isdigit() or int(limit) < 1):
        errors.append("검증 항목 수는 1 이상의 숫자로 적어 주세요.")
    if skip_submission and skip_submission not in {s["dir"] for s in store.submissions()}:
        errors.append("고른 기존 제출물을 찾을 수 없습니다.")
    if library_key and paper_file is not None and paper_file.filename:
        errors.append("예제 논문을 고르면 파일을 올리지 않습니다. 둘 중 하나만 골라 주세요.")
    if errors:
        return _new_form(request, errors, form, 400)

    job_id = store.new_id()
    job = {"id": job_id, "created_at": time.time(), "status": "running", "stage": "queued",
           "judge_run": job_id, "events": []}
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
        is_pdf = False
        if paper is None:
            errors.append("논문 파일을 올려 주세요.")
        else:
            name = paper_file.filename.lower()
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
        title = Path(paper_file.filename).stem if is_pdf else store.paper_title(paper_dir / "paper.md")
        job.update(paper_dir=paper_dir.as_posix(), title=title,
                   source_label="직접 올린 논문" + (" (PDF)" if is_pdf else ""))
        if is_pdf or rubric is None:
            # 에이전트보다 먼저 논문 준비 Job이 PDF를 바꾸고 채점 기준을 만든다(orchestrator의 prepare 단계)
            job.update(stage="prepare_wait", prepare_pdf=is_pdf, rubric_auto=rubric is None)
    else:
        item = next((x for x in store.library() if x["key"] == library_key), None)
        if item is None:
            return _new_form(request, ["논문을 골라 주세요."], form, 400)
        counts = (item["leaves"], item["code_dev"])
        job.update(paper_dir=item["dir"], title=item["title"], source_label=f"{item['kind']} 예제")

    if counts:
        store.plan_judging(job, *counts)
    job["mock"] = mock == "1"
    if skip_submission:
        job.update(skip_agent=True, submission_dir=skip_submission)
    else:
        job["submission_dir"] = (store.DATA / "agent_runs" / job_id / "submission").as_posix()
    store.add_event(job, "작업을 접수했습니다")
    store.save(job)
    log.info("job %s created: %s", job_id, job["title"])
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    return templates.TemplateResponse(request, "job.html", views.live_context(_job_or_404(job_id)))


@app.get("/jobs/{job_id}/live", response_class=HTMLResponse)
def job_live(request: Request, job_id: str):
    """작업 상세 화면에서 진행 중일 때 몇 초마다 다시 받는 부분."""
    return templates.TemplateResponse(request, "_live.html", views.live_context(_job_or_404(job_id)))


@app.get("/api/jobs/{job_id}")
def job_api(job_id: str) -> dict:
    job = _job_or_404(job_id)
    out = {key: job.get(key) for key in ("id", "title", "status", "stage", "error", "created_at", "finished_at",
                                         "score", "planned_leaves", "mock", "skip_agent")}
    out["agent"] = views.agent_progress(job)
    judge = views.judge_progress(job)
    out["judge"] = {key: v for key, v in judge.items() if key != "recent"} if judge else None
    return out


@app.get("/api/jobs/{job_id}/files")
def job_files_api(job_id: str) -> dict:
    """만든 코드 파일 목록. Slack 도우미(hermes/paper_mcp.py)가 코드를 설명할 때 쓴다."""
    job = _job_or_404(job_id)
    return {"id": job_id, "files": [{"path": f["path"], "size": f["size"]}
                                     for f in views.files_in(_submission_dir(job))]}


@app.get("/api/jobs/{job_id}/raw/{path:path}")
def job_file_raw(job_id: str, path: str):
    """코드 파일 하나의 내용(텍스트). 숨김 파일과 제출물 밖의 경로는 주지 않는다."""
    job = _job_or_404(job_id)
    sub = _submission_dir(job)
    target = (sub / path).resolve()
    if not target.is_relative_to(sub) or not target.is_file() or not views.is_shown(target.relative_to(sub)):
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    if target.stat().st_size > MAX_VIEW:
        raise HTTPException(413, "파일이 너무 큽니다")
    return PlainTextResponse(target.read_text(errors="replace"))


@app.get("/jobs/{job_id}/code.zip")
def download_zip(job_id: str):
    job = _job_or_404(job_id)
    sub = _submission_dir(job)
    if not sub.is_dir():
        raise HTTPException(404, "아직 만든 코드가 없습니다")
    root = Path(f"paperagent-{job_id}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(sub.rglob("*")):
            if p.is_file() and views.is_shown(p.relative_to(sub)):
                z.write(p, root / p.relative_to(sub))
        report = views.report_md(job)
        if report:
            z.writestr(str(root / "PAPERAGENT_REPORT.md"), report)
        rubric = Path(job["paper_dir"]) / "rubric.json"
        if rubric.is_file():
            z.write(rubric, root / "PAPERAGENT_RUBRIC.json")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="paperagent-{job_id}.zip"'})


@app.get("/jobs/{job_id}/report.md")
def download_report(job_id: str):
    report = views.report_md(_job_or_404(job_id))
    if report is None:
        raise HTTPException(404, "작업이 끝나야 보고서를 만들 수 있습니다")
    return Response(report, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="paperagent-{job_id}-report.md"'})


@app.get("/jobs/{job_id}/files/{path:path}", response_class=HTMLResponse)
def view_file(request: Request, job_id: str, path: str):
    job = _job_or_404(job_id)
    sub = _submission_dir(job)
    target = (sub / path).resolve()
    if not target.is_relative_to(sub) or not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    if target.stat().st_size > MAX_VIEW:
        text = "(파일이 너무 커서 화면에 보여 줄 수 없습니다. zip으로 내려받아 확인해 주세요.)"
    else:
        text = target.read_text(errors="replace")
    return templates.TemplateResponse(request, "file.html", {"job": job, "path": path, "text": text})
