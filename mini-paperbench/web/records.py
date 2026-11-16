"""작업이 남긴 기록을 DB와 S3에 옮긴다.

DB(web/db.py)
  papers, rubrics, rubric_items  같은 논문을 다시 올리면 채점 기준을 다시 써서 결과를 비교할 수 있게 한다
  llm_calls                      논문 준비(prepare/calls.jsonl), 코드 작성(calls.jsonl), 검증(항목마다 usage)
  item_scores                    항목별 검증 결과
S3(web/blob.py)
  올린 논문 원본, 끝난 작업의 코드 묶음·결과·로그. 디스크(/data)에서 지워져도 ensure_local()이 되살린다.
단계가 끝날 때 orchestrator가 부른다. 여기서 난 오류는 작업을 실패시키지 않는다(기록만 빠진다).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path

from sqlalchemy import delete, select, update

from judge.rubric import load_leaves
from web import blob, db, store

log = logging.getLogger("records")
ARCHIVE_RETRY_SEC = 300
_archive_failed_at: dict[str, float] = {}


# ---------- 논문과 채점 기준 ----------

def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def register_paper(fp: str, title: str | None, filename: str | None, kind: str, user_id: int | None,
                   s3_key: str | None = None) -> int:
    with db.engine().begin() as conn:
        row = conn.execute(select(db.papers).where(db.papers.c.fingerprint == fp)).first()
        if row is None:
            res = conn.execute(db.papers.insert().values(
                fingerprint=fp, title=title, filename=filename, kind=kind, s3_key=s3_key,
                created_by=user_id, created_at=time.time()))
            return res.inserted_primary_key[0]
        return row.id


def set_paper(paper_id: int, **values) -> None:
    with db.engine().begin() as conn:
        conn.execute(update(db.papers).where(db.papers.c.id == paper_id).values(**values))


def get_paper(paper_id: int):
    with db.engine().connect() as conn:
        return conn.execute(select(db.papers).where(db.papers.c.id == paper_id)).first()


def latest_rubric(paper_id: int):
    """이 논문에 마지막으로 쓴 채점 기준(없으면 None)."""
    q = (select(db.rubrics).where(db.rubrics.c.paper_id == paper_id)
         .order_by(db.rubrics.c.created_at.desc(), db.rubrics.c.id.desc()).limit(1))
    with db.engine().connect() as conn:
        return conn.execute(q).first()


def _sections(root: dict) -> dict[str, str]:
    """리프 id → 최상위 영역 이름."""
    out = {}

    def walk(node: dict, section: str) -> None:
        if not node.get("sub_tasks"):
            out[node["id"]] = section
            return
        for child in node["sub_tasks"]:
            walk(child, section)

    for group in root.get("sub_tasks") or []:
        walk(group, group.get("requirements", ""))
    return out


def register_rubric(paper_id: int, rubric_path: Path, origin: str, job_id: str | None) -> int:
    """채점 기준을 저장한다. 같은 논문에 내용이 같은 기준이 있으면 그 id를 돌려준다."""
    content = rubric_path.read_text()
    sha = hashlib.sha256(content.encode()).hexdigest()
    with db.engine().connect() as conn:
        row = conn.execute(select(db.rubrics.c.id).where(db.rubrics.c.paper_id == paper_id,
                                                         db.rubrics.c.content_sha == sha)).first()
    if row:
        return row.id
    leaves = load_leaves(rubric_path)
    sections = _sections(json.loads(content))
    code_dev = sum(1 for leaf in leaves if leaf["task_category"] == store.CODE_DEV)
    with db.engine().begin() as conn:
        rid = conn.execute(db.rubrics.insert().values(
            paper_id=paper_id, content_sha=sha, origin=origin, job_id=job_id, content=content,
            leaves=len(leaves), code_dev=code_dev, created_at=time.time())).inserted_primary_key[0]
        if leaves:
            conn.execute(db.rubric_items.insert(), [
                {"rubric_id": rid, "leaf_id": leaf["id"], "position": i, "category": leaf["task_category"],
                 "section": sections.get(leaf["id"]), "requirement": leaf["requirements"]}
                for i, leaf in enumerate(leaves)])
    return rid


def paper_key(job: dict, name: str) -> str:
    return f"papers/{job['paper_fingerprint']}/{name}"


def upload_paper(job: dict, name: str, data: bytes, content_type: str) -> None:
    """올린 논문 원본을 S3에 둔다(같은 논문이 이미 있으면 건너뛴다)."""
    if not blob.enabled() or not job.get("paper_fingerprint"):
        return
    key = paper_key(job, name)
    try:
        if not blob.exists(key):
            blob.put_bytes(data, key, content_type)
        set_paper(job["paper_id"], s3_key=key)
    except Exception:
        log.exception("job %s: paper upload to S3 failed", job["id"])


def fetch_prepared_md(fp: str, from_job: str | None, dest: Path) -> bool:
    """같은 논문을 예전에 준비한 본문(paper.md)을 가져온다: 그 작업의 폴더, 없으면 S3."""
    if from_job:
        src = store.job_dir(from_job) / "paper" / "paper.md"
        if src.is_file():
            shutil.copyfile(src, dest)
            return True
    if blob.enabled():
        try:
            return blob.get_file(f"papers/{fp}/paper.md", dest)
        except Exception:
            log.exception("paper.md download failed (%s)", fp)
    return False


# ---------- LLM 호출과 검증 결과 ----------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def ingest_calls(job: dict, stage: str, path: Path) -> int:
    rows = [{"job_id": job["id"], "stage": stage, "step": str(c.get("step", ""))[:32], "model": job.get("llm_model"),
             "calls": 1, "input_tokens": int(c.get("input_tokens") or 0), "cached_tokens": int(c.get("cached_tokens") or 0),
             "output_tokens": int(c.get("output_tokens") or 0), "sec": c.get("sec"),
             "finish_reason": c.get("finish_reason")} for c in _read_jsonl(path)]
    with db.engine().begin() as conn:
        conn.execute(delete(db.llm_calls).where(db.llm_calls.c.job_id == job["id"], db.llm_calls.c.stage == stage))
        if rows:
            conn.execute(db.llm_calls.insert(), rows)
    return len(rows)


def ingest_results(job: dict, run: str | None = None, stage: str = "judge") -> int:
    """검증 결과(results/<run>.json)를 item_scores와 llm_calls에 옮긴다. 코드 검증(judge)과 실행 검증(exec_judge)이 같은 함수를 쓴다."""
    path = store.RESULTS / f"{run or job['judge_run']}.json"
    if not path.is_file():
        return 0
    leaves = json.loads(path.read_text()).get("leaves", {})
    model = "mock" if job.get("mock") else job.get("llm_model")
    scores, calls = [], []
    for lid, v in leaves.items():
        sec = round(v["finished_at"] - v["started_at"], 2) if v.get("finished_at") and v.get("started_at") else None
        err = v.get("error")
        scores.append({"job_id": job["id"], "leaf_id": lid[:128], "category": v.get("category"),
                       "score": None if err else v.get("score"), "reason": v.get("reason"), "error": err, "sec": sec})
        u = v.get("usage") or {}
        if u:
            calls.append({"job_id": job["id"], "stage": stage, "step": lid[:32], "model": model,
                          "calls": int(u.get("calls") or 1), "input_tokens": int(u.get("input_tokens") or 0),
                          "cached_tokens": int(u.get("cached_tokens") or 0),
                          "output_tokens": int(u.get("output_tokens") or 0), "sec": sec, "finish_reason": None})
    with db.engine().begin() as conn:
        ids = [x["leaf_id"] for x in scores]
        if ids:
            conn.execute(delete(db.item_scores).where(db.item_scores.c.job_id == job["id"], db.item_scores.c.leaf_id.in_(ids)))
        conn.execute(delete(db.llm_calls).where(db.llm_calls.c.job_id == job["id"], db.llm_calls.c.stage == stage))
        if scores:
            conn.execute(db.item_scores.insert(), scores)
        if calls:
            conn.execute(db.llm_calls.insert(), calls)
    return len(scores)


def agent_dir(job: dict) -> Path:
    return store.DATA / "agent_runs" / job["id"]


def _safe(fn, what: str, job: dict) -> None:
    try:
        fn()
    except Exception:
        log.exception("job %s: %s failed", job["id"], what)


def after_prepare(job: dict) -> None:
    """논문 준비가 끝났다: 설계한 채점 기준과 LLM 호출을 기록하고, 준비한 본문을 S3에 둔다."""
    paper_dir = Path(job["paper_dir"])

    def rubric() -> None:
        if job.get("paper_id") and (paper_dir / "rubric.json").is_file() and not job.get("rubric_id"):
            job["rubric_id"] = register_rubric(job["paper_id"], paper_dir / "rubric.json",
                                               "auto" if job.get("rubric_auto") else "given", job["id"])
        if job.get("paper_id") and job.get("title"):
            set_paper(job["paper_id"], title=job["title"])

    def md() -> None:
        if blob.enabled() and job.get("paper_fingerprint") and (paper_dir / "paper.md").is_file():
            key = paper_key(job, "paper.md")
            if not blob.exists(key):
                blob.put_file(paper_dir / "paper.md", key, "text/markdown; charset=utf-8")

    _safe(rubric, "rubric record", job)
    _safe(lambda: ingest_calls(job, "prepare", paper_dir / "prepare" / "calls.jsonl"), "prepare calls", job)
    _safe(md, "paper.md upload", job)


def after_agent(job: dict) -> None:
    _safe(lambda: ingest_calls(job, "agent", agent_dir(job) / "calls.jsonl"), "agent calls", job)


def after_judge(job: dict) -> None:
    _safe(lambda: ingest_results(job), "results", job)


def after_repair(job: dict) -> None:
    """자동 수정이 끝났다: 수정에 쓴 LLM 호출을 남긴다."""
    _safe(lambda: ingest_calls(job, "repair", agent_dir(job) / "repair" / "calls.jsonl"), "repair calls", job)


def after_rejudge(job: dict) -> None:
    """고친 코드의 재검증이 끝났다: 항목 점수를 최종 결과로 바꾸고, 재검증 호출은 따로 남긴다."""
    _safe(lambda: ingest_results(job, stage="rejudge"), "rejudge results", job)


def gpu_dir(job: dict) -> Path:
    return agent_dir(job) / "gpu"


def after_gpu(job: dict) -> None:
    """GPU 실행이 끝났다: 오류를 고칠 때 쓴 LLM 호출을 남긴다."""
    _safe(lambda: ingest_calls(job, "gpu_fix", gpu_dir(job) / "calls.jsonl"), "gpu fix calls", job)


def after_exec_judge(job: dict) -> None:
    run = (job.get("gpu") or {}).get("exec_run")
    if run:
        _safe(lambda: ingest_results(job, run, "exec_judge"), "exec results", job)


def after_failure(job: dict) -> None:
    """실패한 작업도 그때까지 쓴 LLM 호출은 남긴다(비용 통계에 들어가야 한다)."""
    if job.get("paper_dir"):
        _safe(lambda: ingest_calls(job, "prepare", Path(job["paper_dir"]) / "prepare" / "calls.jsonl"),
              "prepare calls", job)
    _safe(lambda: ingest_calls(job, "agent", agent_dir(job) / "calls.jsonl"), "agent calls", job)


def backfill(job: dict) -> None:
    """예전 작업(파일 기록)을 DB에 옮길 때: 논문, 채점 기준, 호출, 결과를 한 번에 채운다."""
    paper_dir = Path(job.get("paper_dir", ""))
    if not job.get("paper_id") and job.get("paper_dir"):
        src = next((p for p in (paper_dir / "paper.pdf", paper_dir / "paper.md") if p.is_file()), None)
        if paper_dir.resolve().is_relative_to(store.JOBS_DIR.resolve()) and src is not None:
            job["paper_fingerprint"] = fingerprint(src.read_bytes())
            kind = "pdf" if src.suffix == ".pdf" else "md"
        else:
            job["paper_fingerprint"] = "library:" + paper_dir.as_posix().removeprefix("data/")
            kind = "library"
        title = job.get("title")
        if kind == "library" and (paper_dir / "paper.md").is_file():
            title = store.paper_title(paper_dir / "paper.md")  # 예제 논문은 작업 제목이 아니라 논문 제목으로
        job["paper_id"] = register_paper(job["paper_fingerprint"], title, src.name if src else None,
                                         kind, job.get("user_id"))
    if job.get("paper_id") and not job.get("rubric_id") and (paper_dir / "rubric.json").is_file():
        job["rubric_id"] = register_rubric(job["paper_id"], paper_dir / "rubric.json",
                                           "auto" if job.get("rubric_auto") else "given", job["id"])
        job.setdefault("rubric_origin", "auto" if job.get("rubric_auto") else "given")
    _safe(lambda: ingest_calls(job, "prepare", paper_dir / "prepare" / "calls.jsonl"), "prepare calls", job)
    _safe(lambda: ingest_calls(job, "agent", agent_dir(job) / "calls.jsonl"), "agent calls", job)
    if job.get("status") == "done":
        _safe(lambda: ingest_results(job), "results", job)


# ---------- S3 보관과 되살리기 ----------

def _put_if(path: Path, key: str, content_type: str | None = None) -> None:
    if path.is_file():
        blob.put_file(path, key, content_type)


def archive(job: dict) -> bool:
    """끝난 작업을 S3에 올린다. 성공하면 job['archived_at']을 적는다(저장은 부르는 쪽이 한다)."""
    if not blob.enabled():
        return False
    last = _archive_failed_at.get(job["id"])
    if last and time.time() - last < ARCHIVE_RETRY_SEC:
        return False
    from web import views  # views가 orchestrator를 거쳐 이 모듈을 부르므로 여기서 가져온다
    pre = f"jobs/{job['id']}/"
    try:
        bundle = views.bundle_zip(job)
        if bundle is not None:
            blob.put_bytes(bundle, pre + f"paperagent-{job['id']}.zip", "application/zip")
        _put_if(store.RESULTS / f"{job['judge_run']}.json", pre + "results.json", "application/json")
        _put_if(store.RESULTS / f"agent_run_{job['id']}.json", pre + "agent/agent_run.json", "application/json")
        for name in ("analysis.md", "plan.json", "calls.jsonl"):
            _put_if(agent_dir(job) / name, pre + f"agent/{name}")
        rep = job.get("repair") or {}
        if rep.get("first_run") and rep["first_run"] != job["judge_run"]:
            _put_if(store.RESULTS / f"{rep['first_run']}.json", pre + "results_before_repair.json", "application/json")
        for name in REPAIR_FILES:
            _put_if(agent_dir(job) / "repair" / name, pre + f"agent/repair/{name}")
        paper_dir = Path(job.get("paper_dir", ""))
        if job.get("paper_dir") and paper_dir.resolve().is_relative_to(store.JOBS_DIR.resolve()):
            for name in ("paper.md", "rubric.json", "meta.json", "addendum.md", "prepare/calls.jsonl"):
                _put_if(paper_dir / name, pre + f"paper/{name}")
        g = job.get("gpu") or {}
        if g.get("result") and gpu_dir(job).is_dir():
            blob.put_bytes(gpu_zip(job), pre + "gpu.zip", "application/zip")
            _put_if(store.RESULTS / f"{g.get('exec_run')}.json", pre + f"gpu/{g.get('exec_run')}.json", "application/json")
        jd = store.job_dir(job["id"])
        if jd.is_dir():
            for log_file in jd.glob("*.log"):
                blob.put_file(log_file, pre + f"logs/{log_file.name}", "text/plain; charset=utf-8")
        job["archived_at"] = time.time()
        blob.put_bytes(json.dumps(job, ensure_ascii=False, indent=1).encode(), pre + "job.json", "application/json")
    except Exception:
        _archive_failed_at[job["id"]] = time.time()
        log.exception("job %s: archive to S3 failed (retry in %ss)", job["id"], ARCHIVE_RETRY_SEC)
        job.pop("archived_at", None)
        return False
    _archive_failed_at.pop(job["id"], None)
    return True


GPU_ZIP_SKIP = ("code/", "judged/")
REPAIR_FILES = ("summary.json", "plan.json", "calls.jsonl", "changes.diff")
GPU_FILE_MAX = 5 * 1024 * 1024


def gpu_zip(job: dict) -> bytes:
    """GPU 실행 기록 묶음(S3 보관용): 진행·결과 JSON, 시도별 로그와 결과 파일, 고친 코드 파일, judge가 본 reproduce.log."""
    base = gpu_dir(job)
    changed = set(((job.get("gpu") or {}).get("result") or {}).get("changed_files") or [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.stat().st_size > GPU_FILE_MAX:
                continue
            rel = p.relative_to(base).as_posix()
            if rel.startswith("code/") and rel[5:] not in changed:
                continue
            if rel.startswith("judged/") and rel != "judged/reproduce.log":
                continue
            if rel == "cancel":
                continue
            z.write(p, rel)
    return buf.getvalue()


def bundle_key(job: dict) -> str:
    return f"jobs/{job['id']}/paperagent-{job['id']}.zip"


def _extract_code(data: bytes, sub: Path, job_id: str) -> None:
    root = f"paperagent-{job_id}/"
    tmp = sub.with_name(sub.name + ".restore")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(root):
                continue
            rel = info.filename[len(root):]
            if not rel or rel.startswith("PAPERAGENT_"):
                continue
            target = (tmp / rel).resolve()
            if not target.is_relative_to(tmp.resolve()):  # 압축 파일 밖으로 나가는 경로는 무시한다
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
    shutil.rmtree(sub, ignore_errors=True)
    tmp.rename(sub)


def _extract_into(data: bytes, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if info.is_dir() or not target.is_relative_to(root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))


def ensure_local(job: dict) -> list[str]:
    """화면에 필요한 파일이 디스크에 없으면 S3에서 되살린다(EC2 디스크가 바뀌었거나 /data를 지운 경우)."""
    restored: list[str] = []
    if not blob.enabled() or not job.get("archived_at"):
        return restored
    pre = f"jobs/{job['id']}/"
    try:
        sub = Path(job["submission_dir"])
        if not job.get("skip_agent") and not sub.is_dir():
            tmp = store.DATA / "web" / f".restore-{job['id']}.zip"
            if blob.get_file(bundle_key(job), tmp):
                _extract_code(tmp.read_bytes(), sub, job["id"])
                tmp.unlink(missing_ok=True)
                restored.append("code")
        res = store.RESULTS / f"{job['judge_run']}.json"
        if not res.is_file() and blob.get_file(pre + "results.json", res):
            restored.append("results")
        if not job.get("skip_agent"):
            ar = store.RESULTS / f"agent_run_{job['id']}.json"
            if not ar.is_file() and blob.get_file(pre + "agent/agent_run.json", ar):
                restored.append("agent_run")
            for name in ("analysis.md", "plan.json", "calls.jsonl"):
                p = agent_dir(job) / name
                if not p.is_file() and blob.get_file(pre + f"agent/{name}", p):
                    restored.append(name)
            rep = job.get("repair") or {}
            if rep.get("first_run") and rep["first_run"] != job["judge_run"]:
                p = store.RESULTS / f"{rep['first_run']}.json"
                if not p.is_file() and blob.get_file(pre + "results_before_repair.json", p):
                    restored.append("results before repair")
            if rep.get("finished_at"):
                for name in REPAIR_FILES:
                    p = agent_dir(job) / "repair" / name
                    if not p.is_file() and blob.get_file(pre + f"agent/repair/{name}", p):
                        restored.append(f"repair/{name}")
        g = job.get("gpu") or {}
        # 진행 중인 실행은 되살리지 않는다(지금 Job이 쓰고 있는 파일이다). 끝난 실행만
        if g.get("result") and g.get("status") in ("done", "failed", "cancelled") \
                and not (gpu_dir(job) / "result.json").is_file():
            tmp = store.DATA / "web" / f".restore-{job['id']}-gpu.zip"
            if blob.get_file(pre + "gpu.zip", tmp):
                _extract_into(tmp.read_bytes(), gpu_dir(job))
                tmp.unlink(missing_ok=True)
                restored.append("gpu")
        if g.get("exec_score"):
            er = store.RESULTS / f"{g['exec_run']}.json"
            if not er.is_file() and blob.get_file(pre + f"gpu/{g['exec_run']}.json", er):
                restored.append("gpu results")
        paper_dir = Path(job.get("paper_dir", ""))
        if job.get("paper_dir") and paper_dir.resolve().is_relative_to(store.JOBS_DIR.resolve()):
            for name in ("paper.md", "rubric.json", "meta.json"):
                p = paper_dir / name
                if not p.is_file() and blob.get_file(pre + f"paper/{name}", p):
                    restored.append(name)
    except Exception:
        log.exception("job %s: restore from S3 failed", job["id"])
    if restored:
        log.info("job %s: restored from S3: %s", job["id"], ", ".join(restored))
    return restored
