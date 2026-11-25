"""GPU 실행 단계: 작업이 끝난 뒤 사용자가 고르면 코드를 학교 공용 GPU(JPUShare)에서 실행하고, 실행·결과 항목을 검증한다.

작업 자체의 status는 'done' 그대로 두고 job["gpu"]에 진행을 적는다. GPU 실행이 잘못돼도 코드 작성·검증 결과는 남는다.
  queued → running(k8s Job gpu-run, agent/gpu_run.py) → judge_wait → judging(작업 생성 → 워커 채점) → analyze → done
  끝: done | failed(실행 자체를 못 함) | cancelled
gpu_runs 테이블: 진행 중인 실행을 찾고(오케스트레이터) 통계를 낸다. 공용 GPU라 동시에 MAX_GPU_RUNS개만 돌린다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from sqlalchemy import select

from judge.common import connect, k
from judge.rubric import load_leaves
from web import db, k8s, records, store

log = logging.getLogger("gpu")
EXEC_CATEGORIES = ("Code Execution", "Result Analysis")
ACTIVE = ("queued", "running", "judge_wait", "judging", "analyze")
MAX_GPU_RUNS = int(os.environ.get("MAX_GPU_RUNS", "1"))
GPU_MINUTES = int(os.environ.get("GPU_RUN_MINUTES", "30"))
GPU_MAX_FIXES = int(os.environ.get("GPU_MAX_FIXES", "2"))
GPU_TIER = os.environ.get("GPU_TIER", "jpu-48-edu")
JUDGE_STALL_SEC = float(os.environ.get("JUDGE_STALL_SEC", "1800"))
_redis = None
_seen: dict[str, tuple[int, float]] = {}

# JPUShare 키가 있는 곳(bootstrap.sh가 GPU_RUN=1)만 GPU 실행을 묻고 버튼을 보인다
ENABLED = os.environ.get("GPU_RUN", "1") != "0"


def redis():
    global _redis
    if _redis is None:
        _redis = connect()
    return _redis


def gpu_dir(job: dict) -> Path:
    return store.DATA / "agent_runs" / job["id"] / "gpu"


def state(job: dict) -> str | None:
    return (job.get("gpu") or {}).get("status")


def is_active(job: dict) -> bool:
    return state(job) in ACTIVE


def runnable(job: dict) -> str:
    """실행할 수 없으면 그 이유, 할 수 있으면 빈 문자열."""
    if not ENABLED:
        return "이 서버에서는 GPU 실행을 쓰지 않습니다."
    if job.get("status") != "done":
        return "작업이 끝난 뒤에 실행할 수 있습니다."
    if job.get("skip_agent"):
        return "PaperAgent가 만든 코드가 아니어서 실행하지 않습니다."
    if not (Path(job["submission_dir"]) / "reproduce.sh").is_file():
        return "reproduce.sh가 없어 실행할 수 없습니다."
    if is_active(job):
        return "이미 실행하고 있습니다."
    return ""


def exec_leaves(job: dict) -> int:
    try:
        return sum(1 for leaf in load_leaves(Path(job["paper_dir"]) / "rubric.json")
                   if leaf["task_category"] in EXEC_CATEGORIES)
    except (OSError, ValueError, KeyError):
        return 0


def _save_run(job: dict) -> None:
    g = job["gpu"]
    res = g.get("result") or {}
    last = (res.get("attempts") or [{}])[-1]
    score = g.get("exec_score") or {}
    with db.engine().begin() as conn:
        db.upsert(conn, db.gpu_runs, {
            "job_id": job["id"], "n": g["n"], "user_id": g.get("user_id"), "state": g["status"],
            "final": res.get("final"), "tier": g.get("tier"), "attempts": len(res.get("attempts") or []) or None,
            "fixes": sum(1 for a in res.get("attempts") or [] if a.get("fix")) if res else None,
            "run_sec": last.get("run_sec"), "queue_sec": last.get("queue_sec"),
            "exec_passed": score.get("passed"), "exec_judged": score.get("judged"),
            "requested_at": g["requested_at"], "finished_at": g.get("finished_at")}, ["job_id", "n"])


def request(job: dict, user: dict) -> None:
    n = (job.get("gpu") or {}).get("n", 0) + 1
    job["gpu"] = {"choice": "run", "status": "queued", "n": n, "requested_at": time.time(),
                  "requested_by": user["username"], "user_id": user["id"], "exec_run": f"{job['id']}g{n}",
                  "minutes": GPU_MINUTES, "tier": GPU_TIER, "max_fixes": GPU_MAX_FIXES}
    store.add_event(job, f"GPU 실행을 요청했습니다({n}번째, 최대 {GPU_MINUTES}분)")
    store.save(job)
    _save_run(job)


def cancel(job: dict) -> str:
    g = job.get("gpu") or {}
    if g.get("status") == "queued":
        _end(job, "cancelled", "")
        store.add_event(job, "GPU 실행 요청을 취소했습니다")
    elif g.get("status") == "running":
        d = gpu_dir(job)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cancel").write_text("cancel requested by the user\n")  # gpu-run Job이 보고 JPUShare 작업을 취소한다
        g["cancel_requested"] = True
        store.add_event(job, "GPU 실행 취소를 요청했습니다")
    else:
        return "지금은 취소할 수 없습니다."
    store.save(job)
    _save_run(job)
    return ""


def active_jobs() -> list[dict]:
    q = (select(db.gpu_runs.c.job_id).where(db.gpu_runs.c.state.in_(ACTIVE))
         .order_by(db.gpu_runs.c.requested_at))
    with db.engine().connect() as conn:
        ids = list(dict.fromkeys(conn.execute(q).scalars()))
    jobs = [store.load(i) for i in ids]
    return [j for j in jobs if j and is_active(j)]


def judge_busy(jobs: list[dict] | None = None) -> bool:
    return any(state(j) in ("judging", "analyze") for j in (jobs if jobs is not None else active_jobs()))


def _end(job: dict, status: str, error: str) -> None:
    g = job["gpu"]
    g.update(status=status, finished_at=time.time())
    if error:
        g["error"] = error
    job["archived_at"] = None   # GPU 실행 결과까지 다시 S3에 올린다


def _summary(result: dict) -> dict:
    attempts = []
    for a in result.get("attempts") or []:
        sub, start, end = a.get("submitted_at"), a.get("started_at"), a.get("finished_at")
        attempts.append({
            "n": a.get("n"), "jpushare_id": a.get("jpushare_id"), "state": a.get("state"), "exit_code": a.get("exit_code"),
            "queue_sec": round(start - sub, 1) if sub and start else None,
            "run_sec": round(end - start, 1) if start and end else None,
            "time_limit": a.get("time_limit"), "unavailable": a.get("unavailable") or [],
            "packages": len(a.get("packages") or []), "error": a.get("error"),
            "fix": {"diagnosis": a["fix"].get("diagnosis"), "files": a["fix"].get("files")} if a.get("fix") else None})
    return {"final": result.get("final"), "message": result.get("message"), "attempts": attempts,
            "quick": bool(result.get("quick")),
            "changed_files": result.get("changed_files") or [], "outputs": (result.get("outputs") or [])[:300],
            "n_outputs": len(result.get("outputs") or []), "judged_dir": result.get("judged_dir")}


def _read_result(job: dict) -> dict | None:
    p = gpu_dir(job) / "result.json"
    try:
        res = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    # 이번 요청 전의 실행 결과는 쓰지 않는다
    return res if res.get("finished_at", 0) >= job["gpu"].get("started_at", 0) else None


def step(job: dict, busy: dict) -> None:
    g = job["gpu"]
    st = g["status"]
    if st == "queued":
        if busy["gpu"] >= MAX_GPU_RUNS:
            return
        name = k8s.job_name("gpu", f"{job['id']}-{g['n']}")
        k8s.create_job("gpu-run", name, ["--run-id", job["id"], "--submission-dir", job["submission_dir"],
                                         "--minutes", str(g["minutes"]), "--max-fixes", str(g["max_fixes"]),
                                         "--tier", g["tier"]])
        g.update(status="running", started_at=time.time(), k8s_job=name)
        busy["gpu"] += 1
        store.add_event(job, "GPU 실행을 시작했습니다")

    elif st == "running":
        kstate, detail = k8s.job_state(g["k8s_job"])
        result = _read_result(job)
        if result is not None and kstate in ("succeeded", "failed", "missing"):
            text = k8s.job_logs(g["k8s_job"], tail=300)
            if text:
                store.job_dir(job["id"]).mkdir(parents=True, exist_ok=True)
                (store.job_dir(job["id"]) / f"gpu-run-{g['n']}.log").write_text(text)
            g["result"] = _summary(result)
            records.after_gpu(job)
            final = result.get("final")
            store.add_event(job, f"GPU 실행이 끝났습니다: {result.get('message', final)}")
            if final in ("error", "cancelled") or not result.get("judged_dir"):
                _end(job, "cancelled" if final == "cancelled" else "failed" if final == "error" else "done",
                     result.get("message", "") if final == "error" else "")
            elif exec_leaves(job) == 0:
                _end(job, "done", "")
                store.add_event(job, "채점 기준에 실행·결과 항목이 없어 검증은 하지 않았습니다")
            else:
                g["status"] = "judge_wait"
        elif kstate in ("failed", "missing"):
            _end(job, "failed", "GPU 실행 Job이 결과를 남기지 못하고 끝났습니다. " + (detail or "")
                 + "\n" + k8s.job_logs(g["k8s_job"], tail=30))

    elif st == "judge_wait":
        if busy["judge"]:
            return
        judged = g["result"]["judged_dir"]
        args = ["--run", g["exec_run"], "--paper-dir", job["paper_dir"], "--submission-dir", judged]
        for cat in EXEC_CATEGORIES:
            args += ["--category", cat]
        k8s.create_job("task-generator", k8s.job_name("gen", g["exec_run"]), args)
        g.update(status="judging", judge_step="gen", judge_started_at=time.time())
        busy["judge"] = True
        store.add_event(job, "실행 기록으로 실행·결과 항목 검증을 시작했습니다")

    elif st == "judging":
        r = redis()
        run = g["exec_run"]
        if g.get("judge_step") == "gen":
            kstate, detail = k8s.job_state(k8s.job_name("gen", run))
            if kstate == "succeeded" or (kstate == "missing" and r.exists(k(run, "total"))):
                k8s.assign_workers(run, bool(job.get("mock")))
                g["judge_step"] = "scoring"
            elif kstate in ("failed", "missing"):
                _end(job, "done", "실행·결과 항목을 검증하지 못했습니다(검증 항목 생성 실패).")
            return
        if r.get(k(run, "aborted")):
            _end(job, "done", "Claude 사용 한도에 걸렸거나 인증에 실패해 실행·결과 항목 검증을 멈췄습니다.")
            return
        total = r.get(k(run, "total"))
        if total is None:
            _end(job, "done", "작업 큐가 초기화되어 실행·결과 항목 검증을 이어갈 수 없습니다.")
            return
        mock = "1" if job.get("mock") else "0"
        if k8s.worker_run() != (run, mock):
            k8s.assign_workers(run, bool(job.get("mock")))
        done = r.hlen(k(run, "results"))
        if done >= int(total):
            _seen.pop(run, None)
            k8s.create_job("analyze", k8s.job_name("analyze", run),
                           ["--run", run, "--label", f"web {job['id']} gpu {g['n']}, {total} tasks"])
            g["status"] = "analyze"
            return
        seen, since = _seen.get(run, (-1, time.time()))
        if done != seen:
            _seen[run] = (done, time.time())
        elif time.time() - since > JUDGE_STALL_SEC:
            _seen.pop(run, None)
            _end(job, "done", f"실행·결과 항목 검증이 {JUDGE_STALL_SEC / 60:.0f}분 동안 진행되지 않아 멈췄습니다.")

    elif st == "analyze":
        kstate, detail = k8s.job_state(k8s.job_name("analyze", g["exec_run"]))
        path = store.RESULTS / f"{g['exec_run']}.json"
        if kstate == "succeeded" or (kstate == "missing" and path.exists()):
            leaves = json.loads(path.read_text()).get("leaves", {})
            ok = [v for v in leaves.values() if "error" not in v]
            g["exec_score"] = {"passed": sum(v["score"] for v in ok), "judged": len(ok), "errors": len(leaves) - len(ok)}
            records.after_exec_judge(job)
            _end(job, "done", "")
            s = g["exec_score"]
            store.add_event(job, f"실행·결과 항목 검증을 마쳤습니다: {s['judged']}개 중 {s['passed']}개 충족")
        elif kstate in ("failed", "missing"):
            _end(job, "done", "실행·결과 항목 검증 결과를 정리하지 못했습니다.")


def tick(busy: dict) -> None:
    jobs = active_jobs()
    busy.setdefault("gpu", sum(1 for j in jobs if state(j) == "running"))
    for job in jobs:
        before = (state(job), job["gpu"].get("judge_step"))
        try:
            step(job, busy)
        except Exception:  # 쿠버네티스·Redis 일시 오류: 다음 차례에 다시 한다
            log.exception("gpu step failed for job %s", job["id"])
            continue
        if (state(job), job["gpu"].get("judge_step")) != before:
            store.save(job)
            _save_run(job)
            log.info("job %s gpu → %s", job["id"], state(job))
