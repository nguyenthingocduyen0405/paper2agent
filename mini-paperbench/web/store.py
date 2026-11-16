"""작업 기록과 논문 목록.

작업 하나 = DB jobs 테이블의 한 행(doc 열에 작업 전체 JSON, web/db.py). 직접 올린 논문은 data/web/jobs/<id>/paper/에 둔다.
경로는 모두 /app 기준 상대 경로다. Job Pod도 같은 볼륨을 /app/data에 붙이므로 그대로 넘길 수 있다.
예전 버전이 남긴 data/web/jobs/<id>/job.json은 import_legacy()가 DB로 옮긴다.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path

from sqlalchemy import select

from judge.rubric import load_leaves
from web import db

log = logging.getLogger("store")
DATA = Path("data")
RESULTS = Path("results")
JOBS_DIR = DATA / "web" / "jobs"
CODE_DEV = "Code Development"
ID_RE = re.compile(r"w[0-9a-f]{6}")
SEE_ALL_ROLES = ("admin", "service")


def new_id() -> str:
    """w + 16진수 6자리. 쿠버네티스 Job 이름과 Redis run 이름에 그대로 쓴다."""
    while True:
        job_id = "w" + secrets.token_hex(3)
        if not (JOBS_DIR / job_id).exists() and load(job_id) is None:
            return job_id


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _span(job: dict, start: str, end: str) -> float | None:
    a, b = job.get(start), job.get(end)
    return round(b - a, 1) if a and b else None


def _row(job: dict) -> dict:
    score = job.get("score") or {}
    return {
        "id": job["id"], "user_id": job.get("user_id"), "paper_id": job.get("paper_id"),
        "rubric_id": job.get("rubric_id"), "title": job.get("title"), "status": job["status"],
        "stage": job["stage"], "llm_model": job.get("llm_model"), "mock": bool(job.get("mock")),
        "skip_agent": bool(job.get("skip_agent")), "created_at": job["created_at"],
        "updated_at": job.get("updated_at"), "finished_at": job.get("finished_at"),
        "prepare_sec": _span(job, "prepare_started_at", "prepare_finished_at"),
        "agent_sec": _span(job, "agent_started_at", "agent_finished_at"),
        "judge_sec": _span(job, "judge_started_at", "finished_at") if job["status"] == "done" else None,
        "score_passed": score.get("passed"), "score_judged": score.get("judged"), "score_errors": score.get("errors"),
        "archived_at": job.get("archived_at"),
        "doc": json.dumps(job, ensure_ascii=False),
    }


def save(job: dict) -> None:
    job["updated_at"] = time.time()
    with db.engine().begin() as conn:
        db.upsert(conn, db.jobs, _row(job), ["id"])


def patch(job_id: str, **fields) -> dict | None:
    """작업 기록의 몇 칸만 바꾼다(다시 읽고 바꿔 쓴다). 오래 걸린 일(S3 업로드 등) 사이에 사용자가 바꾼 값을 덮어쓰지 않게."""
    with db.engine().begin() as conn:
        q = select(db.jobs.c.doc).where(db.jobs.c.id == job_id)
        if conn.dialect.name == "postgresql":
            q = q.with_for_update()
        doc = conn.execute(q).scalar()
        if doc is None:
            return None
        job = json.loads(doc)
        job.update(fields)
        job["updated_at"] = time.time()
        db.upsert(conn, db.jobs, _row(job), ["id"])
        return job


def load(job_id: str) -> dict | None:
    if not ID_RE.fullmatch(job_id or ""):
        return None
    with db.engine().connect() as conn:
        doc = conn.execute(select(db.jobs.c.doc).where(db.jobs.c.id == job_id)).scalar()
    return json.loads(doc) if doc else None


def can_see(user: dict | None, job: dict) -> bool:
    return user is None or user["role"] in SEE_ALL_ROLES or job.get("user_id") == user["id"]


def list_jobs(user: dict | None = None) -> list[dict]:
    """user가 볼 수 있는 작업(관리자와 서비스 계정은 전부). user=None은 내부용으로 전부."""
    q = select(db.jobs.c.doc).order_by(db.jobs.c.created_at.desc())
    if user is not None and user["role"] not in SEE_ALL_ROLES:
        q = q.where(db.jobs.c.user_id == user["id"])
    with db.engine().connect() as conn:
        return [json.loads(d) for d in conn.execute(q).scalars()]


def running_jobs() -> list[dict]:
    q = select(db.jobs.c.doc).where(db.jobs.c.status == "running").order_by(db.jobs.c.created_at)
    with db.engine().connect() as conn:
        return [json.loads(d) for d in conn.execute(q).scalars()]


def unarchived_jobs(limit: int = 5) -> list[dict]:
    """끝났지만 아직 S3에 올리지 않은 작업."""
    q = (select(db.jobs.c.doc).where(db.jobs.c.status.in_(("done", "failed")), db.jobs.c.archived_at.is_(None))
         .order_by(db.jobs.c.created_at).limit(limit))
    with db.engine().connect() as conn:
        return [json.loads(d) for d in conn.execute(q).scalars()]


def import_legacy(owner_id: int | None) -> list[str]:
    """예전 버전이 파일(job.json)로 남긴 작업 중 DB에 없는 것을 옮긴다. 주인은 owner_id(관리자)로 한다."""
    imported = []
    if not JOBS_DIR.is_dir():
        return imported
    for p in sorted(JOBS_DIR.glob("*/job.json")):
        try:
            job = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not ID_RE.fullmatch(job.get("id", "")) or load(job["id"]) is not None:
            continue
        job.setdefault("user_id", owner_id)
        with db.engine().begin() as conn:
            db.upsert(conn, db.jobs, _row(job), ["id"])
        imported.append(job["id"])
    return imported


def add_event(job: dict, text: str) -> None:
    job.setdefault("events", []).append({"t": time.time(), "text": text})


def paper_title(paper_md: Path) -> str:
    """\\title{...}(PaperBench 변환본) 또는 첫 # 제목. 없으면 첫 줄."""
    text = paper_md.read_text(errors="replace")[:8000]
    m = re.search(r"\\title\{(.+?)\}", text, re.S)
    if m:
        return " ".join(m.group(1).split())[:200]
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        return m.group(1).strip()[:200]
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:200]
    return paper_md.parent.name


def count_leaves(rubric: Path) -> tuple[int, int]:
    """(전체 리프 수, Code Development 리프 수)"""
    leaves = load_leaves(rubric)
    return len(leaves), sum(1 for leaf in leaves if leaf["task_category"] == CODE_DEV)


def plan_judging(job: dict, total: int, code_dev: int) -> None:
    """검증할 항목을 정한다. 에이전트는 코드를 실행하지 않으므로 코드 구현 항목만 검증한다(없으면 전체)."""
    job["judge_category"] = CODE_DEV if code_dev else None
    planned = code_dev or total
    if job.get("limit"):
        planned = min(planned, job["limit"])
    job["planned_leaves"] = planned


def library() -> list[dict]:
    """서버에 있는 논문(PaperBench, 합성 논문). paper.md와 rubric.json이 모두 있는 폴더만."""
    items = []
    for root, kind in ((DATA / "papers", "PaperBench"), (DATA / "synthetic", "합성 논문")):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if (d / "paper.md").is_file() and (d / "rubric.json").is_file():
                total, code_dev = count_leaves(d / "rubric.json")
                items.append({"key": f"{root.name}/{d.name}", "dir": d.as_posix(), "kind": kind,
                              "title": paper_title(d / "paper.md"), "leaves": total, "code_dev": code_dev})
    return items


def submissions() -> list[dict]:
    """코드 작성 단계를 건너뛸 때 쓸 수 있는 기존 제출물(실험용)."""
    items = []
    for p in sorted(DATA.glob("judge_eval/*/*/submission")):
        items.append({"dir": p.as_posix(), "label": f"JudgeEval {p.parts[-3]} #{p.parts[-2]}"})
    for p in sorted(DATA.glob("synthetic/*/submissions/*")):
        if p.is_dir():
            items.append({"dir": p.as_posix(), "label": f"합성 논문 {p.parts[-3]} / {p.name}"})
    for p in sorted(DATA.glob("agent_runs/*/submission")):
        items.append({"dir": p.as_posix(), "label": f"에이전트 결과 {p.parts[-2]}"})
    return items
