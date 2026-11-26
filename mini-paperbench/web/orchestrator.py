"""작업 진행: 몇 초마다 실행 중인 작업을 한 단계씩 앞으로 보낸다.

[prepare_wait → prepare(논문 준비 Job)] → queued → agent(P1~P5 Job) → judge_wait
  → judge(작업 생성 Job, 워커 배정, 채점) → analyze(집계 Job)
  → [repair_wait → repair(자동 수정 Job) → judge_wait → judge → analyze(고친 코드 전체 재검증)] → done
- 자동 수정은 첫 검증에서 미충족 항목이 있을 때 한 번 한다(MAX_REPAIR_ROUNDS). 수정이나 재검증이 실패하면
  작업을 실패로 두지 않고 고치기 전 코드와 첫 검증 결과로 마친다.
- 논문 준비는 PDF를 올렸거나 채점 기준 없이 올렸을 때만 한다(PDF → markdown, 채점 기준 자동 생성).
- 에이전트 Job은 동시에 MAX_AGENT_JOBS개까지 돌린다.
- 검증은 MAX_JUDGE_JOBS개 작업까지 동시에 한다. judge 워커가 작업들을 돌아가며 한 항목씩 채점하므로
  (judge/worker.py 라운드 로빈) 뒤에 들어온 작업도 바로 진행되고, 아래 '멈춤' 판정에 걸리지 않는다.
상태는 DB(jobs 테이블)에 있다. 웹 Pod가 다시 떠도 이어서 진행한다.
단계가 끝나면 records가 LLM 호출, 채점 기준, 검증 결과를 DB에 적고, 끝난 작업은 S3에 올린다.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from judge.common import connect, expire_run, k
from web import blob, gpu, k8s, records, store

log = logging.getLogger("orchestrator")
MAX_AGENT_JOBS = int(os.environ.get("MAX_AGENT_JOBS", "3"))
MAX_PREPARE_JOBS = int(os.environ.get("MAX_PREPARE_JOBS", "3"))
MAX_JUDGE_JOBS = int(os.environ.get("MAX_JUDGE_JOBS", "4"))
MULTI_RUN_JUDGE = os.environ.get("MULTI_RUN_JUDGE", "1") == "1"
# 첫 검증에서 미충족 항목이 있으면 judge의 이유를 근거로 코드를 고치고 다시 검증한다(0이면 끈다)
MAX_REPAIR_ROUNDS = int(os.environ.get("MAX_REPAIR_ROUNDS", "1"))
# 채점 결과가 이 시간 동안 하나도 늘지 않으면 멈춘 것으로 본다(워커 Pod가 없거나 이미지 문제 등).
# 한 항목은 claude-bridge 제한 시간(600초)과 재시도를 합쳐도 이보다 짧다.
JUDGE_STALL_SEC = float(os.environ.get("JUDGE_STALL_SEC", "1800"))
# 같은 작업에서 예외가 이 횟수만큼 연달아 나면(약 10분) 실패로 끝낸다. 일시적인 쿠버네티스·Redis 오류는 그 전에 풀린다.
MAX_STEP_ERRORS = int(os.environ.get("MAX_STEP_ERRORS", "120"))
_redis = None
_judge_seen: dict[str, tuple[int, float]] = {}  # job id → (마지막으로 본 결과 수, 그때 시각). 웹 Pod가 다시 뜨면 새로 잰다
_step_errors: dict[str, int] = {}
RATE_LIMIT_HINTS = ("rate limit", "ratelimit", "429")


def redis():
    global _redis
    if _redis is None:
        _redis = connect()
    return _redis


def _sfx(job: dict) -> str:
    """관리자가 다시 시도하면 쿠버네티스 Job 이름을 바꾼다(지우는 중인 예전 Job과 이름이 겹치지 않게)."""
    return f"t{job['retries']}" if job.get("retries") else ""


def prepare_job(job: dict) -> str:
    return k8s.job_name("prepare", job["id"] + _sfx(job))


def agent_job(job: dict) -> str:
    return k8s.job_name("agent", job["id"] + _sfx(job))


def repair_job(job: dict) -> str:
    return k8s.job_name("repair", job["id"] + _sfx(job))


def gen_job(job: dict) -> str:
    return k8s.job_name("gen", job["judge_run"])


def analyze_job(job: dict) -> str:
    return k8s.job_name("analyze", job["judge_run"])


def _save_log(job: dict, name: str, filename: str) -> None:
    text = k8s.job_logs(name, tail=300)
    if text:
        d = store.job_dir(job["id"])  # 예제 논문으로 만든 작업은 폴더가 아직 없을 수 있다
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_text(text)


def _expire_run(run: str | None) -> None:
    if not run:
        return
    try:
        expire_run(redis(), run)
    except Exception:
        log.debug("failed to set TTL on Redis keys for run %s", run, exc_info=True)


def _fail(job: dict, message: str, detail: str = "") -> None:
    if any(h in detail.lower() for h in RATE_LIMIT_HINTS):
        message += " (Claude 사용 한도에 걸린 것으로 보입니다. 한도가 풀린 뒤 새 작업으로 다시 시작해 주세요.)"
    job.update(status="failed", failed_stage=job["stage"], error=message,
               error_detail=detail.strip()[-4000:], finished_at=time.time())
    _expire_run(job.get("judge_run"))
    store.add_event(job, message)


def _rejudging(job: dict) -> bool:
    return bool((job.get("repair") or {}).get("rejudge"))


def _wants_repair(job: dict, score: dict) -> bool:
    return (MAX_REPAIR_ROUNDS > 0 and not job.get("repair") and not job.get("mock") and not job.get("skip_agent")
            and score["judged"] > score["passed"])


def _finish(job: dict, score: dict, text: str = "검증을 마쳤습니다") -> None:
    job.update(stage="done", status="done", finished_at=time.time(), score=score)
    _expire_run(job.get("judge_run"))
    rep = job.get("repair") or {}
    if rep.get("first_run") and rep.get("first_run") != job.get("judge_run"):
        _expire_run(rep["first_run"])
    store.add_event(job, text)


def _undo_repair(job: dict, message: str, detail: str = "") -> None:
    """자동 수정이나 재검증이 실패했다: 고치기 전 코드로 되돌리고 첫 검증 결과로 마친다."""
    rep = job["repair"]
    before = records.agent_dir(job) / "submission_before_repair"
    sub = Path(job["submission_dir"])
    if before.is_dir():
        shutil.rmtree(sub, ignore_errors=True)
        shutil.copytree(before, sub)
    rep.update(rejudge=False, undone=True, error=message, error_detail=detail.strip()[-2000:])
    if rep.get("run"):
        _expire_run(rep["run"])
    job["judge_run"] = rep["first_run"]
    _finish(job, rep["before"], message)
    records.after_judge(job)


def _judge_fail(job: dict, message: str, detail: str = "") -> None:
    """검증 단계의 실패. 고친 코드를 다시 검증하던 중이면 작업 전체를 실패로 두지 않는다."""
    if _rejudging(job):
        _judge_seen.pop(job["id"], None)
        _undo_repair(job, f"{message}. 고치기 전 코드와 첫 검증 결과로 마칩니다", detail)
    else:
        _fail(job, message, detail)


def _score(job: dict) -> dict:
    data = json.loads((store.RESULTS / f"{job['judge_run']}.json").read_text())
    ok = [v for v in data["leaves"].values() if "error" not in v]
    return {"passed": sum(v["score"] for v in ok), "judged": len(ok), "errors": len(data["leaves"]) - len(ok)}


def _finish_prepare(job: dict) -> None:
    """논문 준비가 끝났다: 제목과 검증할 항목 수를 정한다."""
    paper_dir = Path(job["paper_dir"])
    meta_path = paper_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta.get("title"):
        job["title"] = meta["title"]
    elif (paper_dir / "paper.md").exists():
        job["title"] = store.paper_title(paper_dir / "paper.md")
    total, code_dev = store.count_leaves(paper_dir / "rubric.json")
    store.plan_judging(job, total, code_dev)
    if job.get("rubric_auto"):
        sections = len(json.loads((paper_dir / "rubric.json").read_text()).get("sub_tasks", []))
        text = f"채점 기준을 설계했습니다: {sections}개 영역, {total}개 항목 (코드 구현 {code_dev}개)"
        if meta.get("evidence_total"):
            job["evidence"] = {"found": meta["evidence_found"], "total": meta["evidence_total"]}
            text += f". 항목마다 단 논문 근거 문장 중 {meta['evidence_found']}개를 원문에서 그대로 찾았습니다"
        store.add_event(job, text)
    store.add_event(job, "논문 준비를 마쳤습니다")


def _cancel(job: dict) -> None:
    """웹에서 취소를 요청했다: 이 작업의 쿠버네티스 Job을 지우고 judge 워커를 놓아준다.
    코드를 고치던 중이면 고치기 전 코드와 첫 검증 결과로 마친다(그 Pod가 완전히 멈춘 뒤에 되돌린다)."""
    by = job["cancel_requested"]
    names = [prepare_job(job), agent_job(job), repair_job(job), gen_job(job), analyze_job(job)]
    for name in names:
        k8s.delete_job(name)
    if job["stage"] == "repair" and k8s.pods_alive(repair_job(job)):
        return  # 다음 차례에 다시 본다
    if job["stage"] in ("judge", "analyze"):
        if MULTI_RUN_JUDGE:
            try:
                redis().set(k(job["judge_run"], "aborted"), f"cancelled by {by}")
            except Exception:
                log.debug("failed to mark aborted on cancel for %s", job["judge_run"], exc_info=True)
        elif k8s.worker_run()[0] == job["judge_run"]:
            k8s.assign_workers("idle", False)
    _judge_seen.pop(job["id"], None)
    job.pop("cancel_requested", None)
    job["cancel_requested_done"] = by
    if job["stage"] in ("repair_wait", "repair") or _rejudging(job):
        _undo_repair(job, f"{by}님이 코드 수정을 취소해 고치기 전 코드와 첫 검증 결과로 마칩니다")
        return
    job.update(status="cancelled", failed_stage=job["stage"], error=f"{by}님이 작업을 취소했습니다",
               finished_at=time.time())
    _expire_run(job.get("judge_run"))
    store.add_event(job, job["error"])


def step(job: dict, busy: dict) -> None:
    if job.get("cancel_requested"):
        _cancel(job)
        return
    stage = job["stage"]

    if stage == "prepare_wait":
        if busy["prepare"] >= MAX_PREPARE_JOBS:
            return
        args = ["--paper-dir", job["paper_dir"]] + (["--make-rubric"] if job.get("rubric_auto") else [])
        k8s.create_job("prepare", prepare_job(job), args)
        busy["prepare"] += 1
        job.update(stage="prepare", prepare_started_at=time.time())
        what = [x for x, on in (("PDF 읽기", job.get("prepare_pdf")), ("채점 기준 설계", job.get("rubric_auto"))) if on]
        store.add_event(job, f"논문 준비를 시작했습니다 ({', '.join(what)})")

    elif stage == "prepare":
        state, detail = k8s.job_state(prepare_job(job))
        paper_dir = Path(job["paper_dir"])
        ready = (paper_dir / "paper.md").exists() and (paper_dir / "rubric.json").exists() \
            and (paper_dir / "meta.json").exists()
        if state == "succeeded" or (state == "missing" and ready):
            _save_log(job, prepare_job(job), "prepare.log")
            _finish_prepare(job)
            job.update(stage="queued", prepare_finished_at=time.time())
            records.after_prepare(job)
        elif state == "failed":
            _save_log(job, prepare_job(job), "prepare.log")
            reason_file = paper_dir / "prepare_error.txt"  # agent.prepare가 남긴 사용자용 실패 이유
            reason = reason_file.read_text().strip() if reason_file.exists() else ""
            _fail(job, reason or "논문을 준비하지 못했습니다", detail + "\n" + k8s.job_logs(prepare_job(job), tail=30))
        elif state == "missing":
            _fail(job, "논문 준비 Job을 찾을 수 없습니다")

    elif stage == "queued":
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
            records.after_agent(job)
        elif state == "failed":
            _save_log(job, agent_job(job), "agent.log")
            _fail(job, "코드 작성 단계에서 오류가 났습니다", detail + "\n" + k8s.job_logs(agent_job(job), tail=30))
        elif state == "missing":
            _fail(job, "코드 작성 Job을 찾을 수 없습니다")

    elif stage == "judge_wait":
        limit = MAX_JUDGE_JOBS if MULTI_RUN_JUDGE else 1
        if int(busy["judge"]) < limit:
            args = ["--run", job["judge_run"], "--paper-dir", job["paper_dir"], "--submission-dir", job["submission_dir"]]
            if job.get("judge_category"):
                args += ["--category", job["judge_category"]]
            if job.get("limit"):
                args += ["--limit", str(job["limit"])]
            if MULTI_RUN_JUDGE:
                args += ["--global-queue"]
                if job.get("mock"):
                    args += ["--mock"]
            k8s.create_job("task-generator", gen_job(job), args)
            busy["judge"] = int(busy["judge"]) + 1
            if _rejudging(job):
                job.update(stage="judge", judge_step="gen", rejudge_started_at=time.time())
                store.add_event(job, "고친 코드를 처음부터 다시 검증합니다")
            else:
                job.update(stage="judge", judge_step="gen", judge_started_at=time.time())
                store.add_event(job, "자동 검증을 시작했습니다")

    elif stage == "judge":
        r = redis()
        run = job["judge_run"]
        if job.get("judge_step") == "gen":
            state, detail = k8s.job_state(gen_job(job))
            if state == "succeeded" or (state == "missing" and r.exists(k(run, "total"))):
                _save_log(job, gen_job(job), "judge-tasks.log")
                if not MULTI_RUN_JUDGE:
                    k8s.assign_workers(run, bool(job.get("mock")))
                job["judge_step"] = "scoring"
            elif state in ("failed", "missing"):
                _judge_fail(job, "검증 항목을 만들지 못했습니다", detail + "\n" + k8s.job_logs(gen_job(job), tail=30))
            return
        if r.get(k(run, "aborted")):
            _judge_seen.pop(job["id"], None)
            _judge_fail(job, "Claude 사용 한도에 걸렸거나 인증에 실패해 검증을 멈췄습니다", r.get(k(run, "aborted")) or "")
            return
        total = r.get(k(run, "total"))
        if total is None:
            _judge_seen.pop(job["id"], None)
            _judge_fail(job, "작업 큐가 초기화되어 검증을 이어갈 수 없습니다", "Redis의 채점 기록(AOF)이 지워졌습니다. 새 작업으로 다시 시작해 주세요.")
            return
        mock = "1" if job.get("mock") else "0"
        if not MULTI_RUN_JUDGE and k8s.worker_run() != (run, mock):
            # bootstrap.sh를 다시 돌리거나 k8s_run.sh를 쓰면 워커의 RUN_ID가 바뀐다. 다시 맡긴다.
            log.warning("job %s: workers were reassigned elsewhere, assigning back", job["id"])
            k8s.assign_workers(run, bool(job.get("mock")))
        done = r.hlen(k(run, "results"))
        if done >= int(total):
            _judge_seen.pop(job["id"], None)
            label = f"web {job['id']}, {total} tasks" + (", mock" if job.get("mock") else "")
            k8s.create_job("analyze", analyze_job(job), ["--run", run, "--label", label])
            job["stage"] = "analyze"
            return
        # 진행이 멈췄는지: 결과 수가 JUDGE_STALL_SEC 동안 그대로면 실패로 끝내 다음 작업이 기다리지 않게 한다
        seen, since = _judge_seen.get(job["id"], (-1, time.time()))
        if done != seen:
            _judge_seen[job["id"]] = (done, time.time())
        elif time.time() - since > JUDGE_STALL_SEC:
            _judge_seen.pop(job["id"], None)
            try:
                r.set(k(run, "aborted"), "stalled")
            except Exception:
                pass
            _judge_fail(job, f"자동 검증이 {JUDGE_STALL_SEC / 60:.0f}분 동안 진행되지 않아 멈췄습니다",
                  f"{done}/{total} 항목 검증 후 멈춤. judge-worker Pod 상태를 확인해 주세요 (kubectl -n paperbench get pods).")

    elif stage == "repair_wait":
        if busy["agent"] < MAX_AGENT_JOBS:
            args = ["--run-id", job["id"], "--paper-dir", job["paper_dir"], "--results", f"results/{job['judge_run']}.json"]
            if job.get("judge_category"):
                args += ["--category", job["judge_category"]]
            k8s.create_job("agent-repair", repair_job(job), args)
            busy["agent"] += 1
            job["stage"] = "repair"
            job["repair"]["started_at"] = time.time()

    elif stage == "repair":
        state, detail = k8s.job_state(repair_job(job))
        summary_path = records.agent_dir(job) / "repair" / "summary.json"
        rep = job["repair"]
        if state == "succeeded" or (state == "missing" and summary_path.exists()):
            _save_log(job, repair_job(job), "repair.log")
            s = json.loads(summary_path.read_text())
            rep.update(finished_at=time.time(), changed=s["changed_files"], reverted=s.get("reverted", []),
                       not_fixable=len(s.get("not_fixable", [])))
            records.after_repair(job)
            if s["changed_files"]:
                run = f"{job['id']}r1"
                rep.update(rejudge=True, run=run)
                job.update(judge_run=run, stage="judge_wait")
                store.add_event(job, f"코드를 고쳤습니다(파일 {len(s['changed_files'])}개: {', '.join(s['changed_files'])})")
            else:
                _finish(job, rep["before"], "코드를 고쳐서 충족할 수 있는 미충족 항목이 없어 첫 검증 결과로 마칩니다")
        elif state in ("failed", "missing"):
            _save_log(job, repair_job(job), "repair.log")
            records.after_repair(job)
            _undo_repair(job, "검증 결과로 코드를 고치다 오류가 나서 고치기 전 코드와 첫 검증 결과로 마칩니다",
                         detail + "\n" + k8s.job_logs(repair_job(job), tail=30))

    elif stage == "analyze":
        state, detail = k8s.job_state(analyze_job(job))
        done_file = store.RESULTS / f"{job['judge_run']}.json"
        if state == "succeeded" or (state == "missing" and done_file.exists()):
            score = _score(job)
            if score["judged"] == 0 and score["errors"]:
                errors = [v.get("error", "") for v in json.loads(done_file.read_text())["leaves"].values()][:5]
                _judge_fail(job, "모든 항목의 검증이 실패했습니다", "\n".join(errors))
                return
            if _rejudging(job):
                rep = job["repair"]
                rep.update(after=score, rejudge=False)
                b = rep["before"]
                _finish(job, score, f"다시 검증을 마쳤습니다: 충족 {b['passed']}/{b['judged']} → {score['passed']}/{score['judged']}")
                records.after_rejudge(job)
            elif _wants_repair(job, score):
                job.update(stage="repair_wait", judge_finished_at=time.time(), score=score,
                           repair={"before": score, "first_run": job["judge_run"]})
                store.add_event(job, f"첫 검증을 마쳤습니다: {score['judged']}개 중 {score['passed']}개 충족. "
                                     f"미충족 {score['judged'] - score['passed']}개 항목과 그 이유를 근거로 코드를 고칩니다")
                records.after_judge(job)
            else:
                job["judge_finished_at"] = time.time()
                _finish(job, score)
                records.after_judge(job)
        elif state in ("failed", "missing"):
            _judge_fail(job, "검증 결과를 정리하지 못했습니다", detail + "\n" + k8s.job_logs(analyze_job(job), tail=30))


def tick() -> None:
    jobs = store.running_jobs()
    gpu_jobs = gpu.active_jobs()
    busy = {"prepare": sum(1 for j in jobs if j["stage"] == "prepare"),
            "agent": sum(1 for j in jobs if j["stage"] in ("agent", "repair")),
            # multi-run 모드에서는 MAX_JUDGE_JOBS개까지 동시에 검증한다
            "judge": sum(1 for j in jobs if j["stage"] in ("judge", "analyze")) + gpu.judge_count(gpu_jobs),
            "gpu": sum(1 for j in gpu_jobs if gpu.state(j) == "running")}
    for job in jobs:
        before = (job["stage"], job["status"], job.get("judge_step"))
        try:
            step(job, busy)
            _step_errors.pop(job["id"], None)
        except Exception as e:  # 쿠버네티스·Redis 일시 오류: 다음 차례에 다시 시도한다
            log.exception("job %s (stage %s)", job["id"], job["stage"])
            n = _step_errors[job["id"]] = _step_errors.get(job["id"], 0) + 1
            if n < MAX_STEP_ERRORS:
                continue
            # 계속 같은 예외가 나면(깨진 파일 등) 영원히 '진행 중'으로 두지 않고 실패로 끝낸다
            _step_errors.pop(job["id"], None)
            _fail(job, "작업을 진행하다 같은 오류가 계속 났습니다", f"{type(e).__name__}: {e}")
        if job["status"] in ("failed", "cancelled") and before[1] == "running":
            records.after_failure(job)
        if (job["stage"], job["status"], job.get("judge_step")) != before:
            if store.save_running(job):
                log.info("job %s → %s/%s", job["id"], job["status"], job["stage"])
            else:
                log.info("job %s was changed elsewhere (no longer running); this step is not saved", job["id"])
    gpu.tick(busy)
    if blob.enabled():
        for job in store.unarchived_jobs():
            if records.archive(job):
                store.patch(job["id"], archived_at=job["archived_at"])
                log.info("job %s archived to S3", job["id"])
