"""작업 기록과 논문 목록.

작업 하나 = data/web/jobs/<id>/job.json. 직접 올린 논문은 같은 폴더의 paper/에 둔다.
경로는 모두 /app 기준 상대 경로다. Job Pod도 같은 볼륨을 /app/data에 붙이므로 그대로 넘길 수 있다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

from judge.rubric import load_leaves

DATA = Path("data")
RESULTS = Path("results")
JOBS_DIR = DATA / "web" / "jobs"
CODE_DEV = "Code Development"
ID_RE = re.compile(r"w[0-9a-f]{6}")


def new_id() -> str:
    """w + 16진수 6자리. 쿠버네티스 Job 이름과 Redis run 이름에 그대로 쓴다."""
    while True:
        job_id = "w" + secrets.token_hex(3)
        if not (JOBS_DIR / job_id).exists():
            return job_id


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def save(job: dict) -> None:
    d = job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = time.time()
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1))
    os.replace(tmp, d / "job.json")  # 읽는 쪽이 반쯤 쓴 파일을 보지 않게 한다


def load(job_id: str) -> dict | None:
    if not ID_RE.fullmatch(job_id or ""):
        return None
    p = job_dir(job_id) / "job.json"
    return json.loads(p.read_text()) if p.exists() else None


def list_jobs() -> list[dict]:
    jobs = []
    if JOBS_DIR.is_dir():
        for p in JOBS_DIR.glob("*/job.json"):
            try:
                jobs.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
    return sorted(jobs, key=lambda j: j["created_at"], reverse=True)


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
