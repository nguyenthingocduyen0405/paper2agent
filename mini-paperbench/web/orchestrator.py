"""작업 진행: 몇 초마다 실행 중인 작업을 한 단계씩 앞으로 보낸다.

queued → agent(P1~P5 Job) → judge_wait → judge(작업 생성 Job, 워커 배정, 채점) → analyze(집계 Job) → done
- 에이전트 Job은 동시에 MAX_AGENT_JOBS개까지 돌린다.
- judge 워커는 한 번에 run 하나만 처리하므로 채점은 작업마다 차례로 한다.
상태는 job.json에만 있다. 웹 Pod가 다시 떠도 이어서 진행한다.
"""
from __future__ import annotations

import json
import logging
import os
import time

from judge.common import connect, k
from web import k8s, store

log = logging.getLogger("orchestrator")
MAX_AGENT_JOBS = int(os.environ.get("MAX_AGENT_JOBS", "1"))
_redis = None


def redis():
    global _redis
    if _redis is None:
        _redis = connect()
    return _redis


def agent_job(job: dict) -> str:
    return k8s.job_name("agent", job["id"])


def gen_job(job: dict) -> str:
    return k8s.job_name("gen", job["judge_run"])


def analyze_job(job: dict) -> str:
    return k8s.job_name("analyze", job["judge_run"])


def _save_log(job: dict, name: str, filename: str) -> None:
    text = k8s.job_logs(name, tail=300)
    if text:
        (store.job_dir(job["id"]) / filename).write_text(text)


def _fail(job: dict, message: str, detail: str = "") -> None:
    job.update(status="failed", failed_stage=job["stage"], error=message,
               error_detail=detail.strip()[-4000:], finished_at=time.time())
    store.add_event(job, message)


def _score(job: dict) -> dict:
    data = json.loads((store.RESULTS / f"{job['judge_run']}.json").read_text())
    ok = [v for v in data["leaves"].values() if "error" not in v]
    return {"passed": sum(v["score"] for v in ok), "judged": len(ok), "errors": len(data["leaves"]) - len(ok)}


def step(job: dict, busy: dict) -> None:
    stage = job["stage"]

    if stage == "queued":
        if job.get("skip_agent"):
            job["stage"] = "judge_wait"
            store.add_event(job, "기존 제출물을 쓰므로 코드 작성 단계를 건너뜁니다")
        elif busy["agent"] < MAX_AGENT_JOBS:
            k8s.create_job("agent-run", agent_job(job), ["--run-id", job["id"], "--paper-dir", job["paper_dir"]])
            job.update(stage="agent", agent_started_at=time.time())
            busy["agent"] += 1
            store.add_event(job, "코드 작성을 시작했습니다")

    elif stage == "agent":
        state, detail = k8s.job_state(agent_job(job))
        done_file = store.RESULTS / f"agent_run_{job['id']}.json"
        if state == "succeeded" or (state == "missing" and done_file.exists()):
            _save_log(job, agent_job(job), "agent.log")
            job.update(stage="judge_wait", agent_finished_at=time.time())
            store.add_event(job, "코드 작성을 마쳤습니다")
        elif state == "failed":
            _save_log(job, agent_job(job), "agent.log")
            _fail(job, "코드 작성 단계에서 오류가 났습니다", detail + "\n" + k8s.job_logs(agent_job(job), tail=30))
        elif state == "missing":
            _fail(job, "코드 작성 Job을 찾을 수 없습니다")

    elif stage == "judge_wait":
        if not busy["judge"]:
            args = ["--run", job["judge_run"], "--paper-dir", job["paper_dir"], "--submission-dir", job["submission_dir"]]
            if job.get("judge_category"):
                args += ["--category", job["judge_category"]]
            if job.get("limit"):
                args += ["--limit", str(job["limit"])]
            k8s.create_job("task-generator", gen_job(job), args)
            job.update(stage="judge", judge_step="gen", judge_started_at=time.time())
            busy["judge"] = True
            store.add_event(job, "자동 검증을 시작했습니다")

    elif stage == "judge":
        r = redis()
        run = job["judge_run"]
        if job.get("judge_step") == "gen":
            state, detail = k8s.job_state(gen_job(job))
            if state == "succeeded" or (state == "missing" and r.exists(k(run, "total"))):
                _save_log(job, gen_job(job), "judge-tasks.log")
                k8s.assign_workers(run, bool(job.get("mock")))
                job["judge_step"] = "scoring"
            elif state in ("failed", "missing"):
                _fail(job, "검증 항목을 만들지 못했습니다", detail + "\n" + k8s.job_logs(gen_job(job), tail=30))
            return
        if r.get(k(run, "aborted")):
            _fail(job, "LLM 사용 한도에 걸려 검증을 멈췄습니다", r.get(k(run, "aborted")) or "")
            return
        total = r.get(k(run, "total"))
        if total is None:
            _fail(job, "작업 큐가 초기화되어 검증을 이어갈 수 없습니다", "Redis Pod가 다시 뜨면 큐가 비워집니다. 새 작업으로 다시 시작해 주세요.")
            return
        if r.hlen(k(run, "results")) >= int(total):
            label = f"web {job['id']}, {total} tasks" + (", mock" if job.get("mock") else "")
            k8s.create_job("analyze", analyze_job(job), ["--run", run, "--label", label])
            job["stage"] = "analyze"

    elif stage == "analyze":
        state, detail = k8s.job_state(analyze_job(job))
        done_file = store.RESULTS / f"{job['judge_run']}.json"
        if state == "succeeded" or (state == "missing" and done_file.exists()):
            job.update(stage="done", status="done", finished_at=time.time(), score=_score(job))
            store.add_event(job, "검증을 마쳤습니다")
        elif state in ("failed", "missing"):
            _fail(job, "검증 결과를 정리하지 못했습니다", detail + "\n" + k8s.job_logs(analyze_job(job), tail=30))


def tick() -> None:
    jobs = [j for j in sorted(store.list_jobs(), key=lambda j: j["created_at"]) if j["status"] == "running"]
    busy = {"agent": sum(1 for j in jobs if j["stage"] == "agent"),
            "judge": any(j["stage"] in ("judge", "analyze") for j in jobs)}
    for job in jobs:
        before = (job["stage"], job["status"], job.get("judge_step"))
        try:
            step(job, busy)
        except Exception:  # 쿠버네티스·Redis 일시 오류: 다음 차례에 다시 시도한다
            log.exception("job %s (stage %s)", job["id"], job["stage"])
            continue
        if (job["stage"], job["status"], job.get("judge_step")) != before:
            store.save(job)
            log.info("job %s → %s/%s", job["id"], job["status"], job["stage"])
