"""관리자 기능: 작업 다시 시도·삭제, 사용자별 사용량, 시스템 상태, RDS 끄기.

작업 취소는 여기서 하지 않는다. 웹이 job에 cancel_requested를 적으면 orchestrator가 다음 차례에
쿠버네티스 Job을 지우고 상태를 바꾼다(작업 상태를 바꾸는 곳을 orchestrator 하나로 둬서 서로 덮어쓰지 않게).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import case, delete, func, select

from web import blob, db, gpu, k8s, orchestrator, store

log = logging.getLogger("admin")
LAB_SESSION_SEC = 4 * 3600  # AWS Academy Learner Lab 한 번의 세션
RDS_INSTANCE_ID = os.environ.get("RDS_INSTANCE_ID", "paperagent-db")
TUNNEL_METRICS = os.environ.get("TUNNEL_METRICS", "http://cloudflared:2000")  # k8s/55-cloudflared.yaml
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
RETRY_FROM = {"prepare_wait": "prepare_wait", "prepare": "prepare_wait", "queued": "queued", "agent": "queued",
              "judge_wait": "judge_wait", "judge": "judge_wait", "analyze": "judge_wait"}
STAGE_NAME = {"prepare_wait": "논문 준비", "queued": "코드 작성", "judge_wait": "자동 검증"}


# ---------- 작업 ----------

def request_cancel(job: dict, by: str) -> str:
    """진행 중인 작업의 취소를 요청한다. 실제 정리는 orchestrator가 한다. 안 되면 이유를 돌려준다."""
    if job["status"] != "running":
        return "진행 중인 작업만 취소할 수 있습니다."
    if job.get("cancel_requested"):
        return ""
    store.patch(job["id"], cancel_requested=by)
    log.info("job %s: cancel requested by %s", job["id"], by)
    return ""


def retry(job: dict, by: str) -> str:
    """실패했거나 취소한 작업을 멈춘 단계부터 다시 진행한다. 코드 작성은 끝난 단계(분석, 설계, 쓴 파일)를 건너뛴다."""
    if job["status"] not in ("failed", "cancelled"):
        return "실패했거나 취소한 작업만 다시 시도할 수 있습니다."
    stage = RETRY_FROM.get(job.get("failed_stage") or job["stage"])
    if stage is None:
        return "이 작업은 다시 시도할 수 없습니다."
    n = int(job.get("retries") or 0) + 1
    if stage == "prepare_wait":
        (Path(job["paper_dir"]) / "prepare_error.txt").unlink(missing_ok=True)
    if stage == "judge_wait":
        job["judge_run"] = f"{job['id']}t{n}"  # 멈춘 검증의 Redis 기록과 섞이지 않게 새 run으로
        job.pop("judge_step", None)
    for key in ("error", "error_detail", "failed_stage", "finished_at", "archived_at", "cancel_requested", "score"):
        job.pop(key, None)
    job.update(status="running", stage=stage, retries=n)  # retries가 바뀌면 쿠버네티스 Job 이름도 바뀐다
    store.add_event(job, f"{by}님이 {STAGE_NAME[stage]} 단계부터 다시 시도합니다 ({n}번째)")
    store.save(job)
    log.info("job %s: retry %d from %s by %s", job["id"], n, stage, by)
    return ""


def delete_job(job: dict, by: str) -> str:
    """끝난 작업을 지운다: 디스크(작업 폴더, 에이전트 기록, 검증 결과), S3, DB 기록. 논문과 채점 기준 기록은 남긴다."""
    if job["status"] == "running" or gpu.is_active(job):
        return "진행 중인 작업은 먼저 취소해 주세요."
    jid = job["id"]
    shutil.rmtree(store.job_dir(jid), ignore_errors=True)
    shutil.rmtree(store.DATA / "agent_runs" / jid, ignore_errors=True)
    for pattern in (f"{jid}.json", f"{jid}r*.json", f"{jid}t*.json", f"{jid}g*.json", f"agent_run_{jid}.json"):
        for p in store.RESULTS.glob(pattern):
            p.unlink(missing_ok=True)
    if blob.enabled():
        blob.delete_prefix(f"jobs/{jid}/")
    try:  # 검증 큐에 남은 기록(있으면)
        r = orchestrator.redis()
        keys = list(r.scan_iter(match=f"pb:{jid}*", count=500))
        if keys:
            r.delete(*keys)
    except Exception:
        log.exception("job %s: redis cleanup failed", jid)
    store.delete(jid)
    log.info("job %s deleted by %s", jid, by)
    return ""


# ---------- 사용자별 사용량 ----------

def user_usage(since_today: float) -> dict[int, dict]:
    j, c, s, g = db.jobs, db.llm_calls, db.sessions, db.gpu_runs
    out: dict[int, dict] = {}
    with db.engine().connect() as conn:
        for r in conn.execute(select(j.c.user_id, func.count().label("n"),
                                     func.sum(case((j.c.created_at >= since_today, 1), else_=0)).label("today"),
                                     func.sum(case((j.c.status == "running", 1), else_=0)).label("running"),
                                     func.sum(case((j.c.status == "done", 1), else_=0)).label("done"),
                                     func.max(j.c.created_at).label("last"))
                              .where(j.c.user_id.is_not(None)).group_by(j.c.user_id)):
            out[r.user_id] = {"jobs": r.n, "today": r.today or 0, "running": r.running or 0, "done": r.done or 0,
                              "last_job": r.last}
        for r in conn.execute(select(j.c.user_id, func.sum(c.c.input_tokens).label("inp"),
                                     func.sum(c.c.output_tokens).label("out"), func.sum(c.c.calls).label("calls"))
                              .select_from(c.join(j, c.c.job_id == j.c.id))
                              .where(j.c.user_id.is_not(None), j.c.mock.is_(False)).group_by(j.c.user_id)):
            out.setdefault(r.user_id, {}).update(input_tokens=r.inp or 0, output_tokens=r.out or 0, calls=r.calls or 0)
        for r in conn.execute(select(g.c.user_id, func.count().label("n")).where(g.c.user_id.is_not(None))
                              .group_by(g.c.user_id)):
            out.setdefault(r.user_id, {})["gpu_runs"] = r.n
        for r in conn.execute(select(s.c.user_id, func.max(s.c.created_at).label("last"), func.count().label("n"))
                              .where(s.c.expires_at > time.time()).group_by(s.c.user_id)):
            out.setdefault(r.user_id, {}).update(last_login=r.last, sessions=r.n)
    return out


# ---------- 시스템 상태 ----------

def _section(fn):
    try:
        return {"ok": True, **fn()}
    except Exception as e:  # 한 칸이 실패해도 화면은 보여 준다
        log.exception("system status: %s", fn.__name__)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


def _host() -> dict:
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    disk = shutil.disk_usage(store.DATA if store.DATA.exists() else ".")
    left = LAB_SESSION_SEC - uptime
    return {"boot_at": time.time() - uptime, "uptime": uptime, "lab_left": left,
            "mem_total": mem.get("MemTotal", 0), "mem_avail": mem.get("MemAvailable", 0),
            "swap_total": mem.get("SwapTotal", 0), "swap_free": mem.get("SwapFree", 0),
            "disk_total": disk.total, "disk_free": disk.free, "load": os.getloadavg()}


def _cluster() -> dict:
    pods = k8s.list_pods()
    jobs = [x for x in k8s.list_jobs() if x["active"]]
    run, mock = k8s.worker_run()
    # 끝난 Job의 Pod(검증 항목 만들기, 집계 등)는 많아서 따로 센다
    return {"pods": [x for x in pods if not x.get("finished")], "finished_pods": [x for x in pods if x.get("finished")],
            "jobs": jobs, "worker_run": run, "worker_mock": mock == "1",
            "workers": k8s.worker_replicas()}


def _queue() -> dict:
    running = store.running_jobs()
    gpu_jobs = gpu.active_jobs()
    return {"running": [{"id": j["id"], "title": j.get("title"), "stage": j["stage"], "user_id": j.get("user_id"),
                         "created_at": j["created_at"], "cancel": j.get("cancel_requested")} for j in running],
            "gpu": [{"id": j["id"], "title": j.get("title"), "status": gpu.state(j)} for j in gpu_jobs]}


def _redis() -> dict:
    info = orchestrator.redis().info()
    return {"memory": info.get("used_memory_human"), "aof": bool(info.get("aof_enabled")),
            "aof_status": info.get("aof_last_write_status"), "nkeys": orchestrator.redis().dbsize()}


def _llm() -> dict:
    """최근 5시간(Claude 구독 한도의 단위)과 24시간 동안 움직인 작업의 LLM 토큰, 한도에 걸린 최근 작업."""
    j, c = db.jobs, db.llm_calls
    now = time.time()
    out = {}
    with db.engine().connect() as conn:
        for label, sec in (("h5", 5 * 3600), ("h24", 24 * 3600)):
            r = conn.execute(select(func.sum(c.c.calls), func.sum(c.c.input_tokens), func.sum(c.c.output_tokens))
                             .select_from(c.join(j, c.c.job_id == j.c.id))
                             .where(j.c.updated_at >= now - sec, j.c.mock.is_(False))).one()
            out[label] = {"calls": r[0] or 0, "input": r[1] or 0, "output": r[2] or 0}
        docs = conn.execute(select(j.c.id, j.c.updated_at, j.c.doc).where(j.c.status == "failed", j.c.updated_at >= now - 24 * 3600)
                            .order_by(j.c.updated_at.desc())).all()
    limited = [(d.id, d.updated_at) for d in docs if "사용 한도" in (d.doc or "")]
    out["limited"] = [{"id": i, "at": t} for i, t in limited[:5]]
    return out


def _rds() -> dict:
    import boto3
    inst = boto3.client("rds", region_name=AWS_REGION).describe_db_instances(
        DBInstanceIdentifier=RDS_INSTANCE_ID)["DBInstances"][0]
    return {"id": RDS_INSTANCE_ID, "status": inst["DBInstanceStatus"], "class": inst["DBInstanceClass"],
            "engine": f"{inst['Engine']} {inst['EngineVersion']}", "storage_gb": inst.get("AllocatedStorage")}


def _s3() -> dict:
    if not blob.enabled():
        return {"enabled": False}
    n = size = 0
    for page in blob.client().get_paginator("list_objects_v2").paginate(Bucket=blob.bucket(), PaginationConfig={"MaxItems": 20000}):
        for o in page.get("Contents", []):
            n += 1
            size += o["Size"]
    return {"enabled": True, "bucket": blob.bucket(), "objects": n, "bytes": size}


def public_url() -> str | None:
    """Cloudflare quick tunnel이 받은 임시 공개 주소. 터널을 켜지 않았거나 아직 못 받았으면 None."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{TUNNEL_METRICS}/quicktunnel", timeout=2) as r:
            host = json.loads(r.read().decode()).get("hostname")
    except Exception:
        return None
    return f"https://{host}" if host else None


def _public() -> dict:
    return {"url": public_url()}


def system_status() -> dict:
    return {"public": _section(_public), "host": _section(_host), "cluster": _section(_cluster), "queue": _section(_queue), "redis": _section(_redis),
            "llm": _section(_llm), "rds": _section(_rds), "s3": _section(_s3), "at": time.time()}


def stop_rds(by: str) -> str:
    """RDS를 끈다. 실습 세션이 끝날 때는 AWS가 권한을 먼저 막아 자동으로 끄지 못하므로, 나가기 전에 관리자가 끈다.
    끄면 이 웹도 DB를 쓰지 못한다. 다음에 EC2가 켜질 때 paperagent-rds 서비스가 다시 켠다."""
    if store.running_jobs() or gpu.active_jobs():
        return "진행 중인 작업이 있어 끌 수 없습니다. 작업이 끝나거나 취소한 뒤에 꺼 주세요."
    import boto3
    boto3.client("rds", region_name=AWS_REGION).stop_db_instance(DBInstanceIdentifier=RDS_INSTANCE_ID)
    log.warning("RDS %s stop requested by %s", RDS_INSTANCE_ID, by)
    return ""


def delete_user_rows(user_id: int) -> None:
    """사용자를 지운다. 그 사용자의 작업과 GPU 실행 기록은 남기고 주인만 비운다."""
    with db.engine().begin() as conn:
        for jid, doc in conn.execute(select(db.jobs.c.id, db.jobs.c.doc).where(db.jobs.c.user_id == user_id)).all():
            d = json.loads(doc)
            d["user_id"] = None
            conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(user_id=None, doc=json.dumps(d, ensure_ascii=False)))
        conn.execute(db.gpu_runs.update().where(db.gpu_runs.c.user_id == user_id).values(user_id=None))
        conn.execute(delete(db.sessions).where(db.sessions.c.user_id == user_id))
        conn.execute(delete(db.users).where(db.users.c.id == user_id))
