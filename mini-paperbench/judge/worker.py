"""워커: Stream에서 리프를 하나씩 받아 채점하고, 결과를 저장한 뒤 XACK한다.

장애 복구: 매 반복마다 먼저 XAUTOCLAIM으로 CLAIM_IDLE_SEC 넘게 ACK되지 않은 메시지
(죽은 워커가 들고 있던 것)를 가져온다. 없으면 XREADGROUP으로 새 메시지를 읽는다.
결과는 리프 ID를 키로 HSET하므로 같은 리프가 두 번 처리돼도 결과는 1개다.

모드:
- RUN_ID=multi (또는 *, auto): 여러 run의 작업을 재시작 없이 동시에 처리한다. 채점 중인 run은
  목록(pb:global:runs)에 있고, 워커는 그 목록을 돌아가며 run마다 한 항목씩 가져간다(라운드 로빈).
  그래서 먼저 들어온 큰 작업이 끝날 때까지 뒤 작업이 기다리지 않고 모든 run이 같이 진행된다.
  항목은 run별 스트림(pb:<run>:tasks)에 있고, 각 메시지의 run 필드로 논문·제출물을 찾아 채점한다.
  예전 공용 스트림(pb:global:tasks)에 남은 메시지도 처리한다.
- RUN_ID=<run>: 기존 단일 run 모드(scripts/k8s_run.sh, k8s_bench.sh와 호환).

사용: python -m judge.worker --run multi
     python -m judge.worker --run r1 [--exit-when-done]
"""
import argparse
import collections
import json
import os
import socket
import sys
import time
from pathlib import Path

from redis.exceptions import ResponseError

from judge.common import (ACTIVE_RUNS, CLAIM_IDLE_SEC, GLOBAL_STREAM, GROUP, MAX_DELIVERIES,
                          MULTI_RUN_IDS, RATE_LIMIT_RPM, RATE_LIMIT_TPM, connect, k)
from judge.judge import is_fatal_llm_error, judge_leaf
from judge.ratelimit import RateLimiter

MAX_RUN_CACHE = 16
# multi-run 모드에서 죽은 워커가 들고 있던 항목을 이 간격(초)마다 찾는다(CLAIM_IDLE_SEC보다 훨씬 짧으면 된다)
RECLAIM_EVERY_SEC = float(os.environ.get("RECLAIM_EVERY_SEC", "15"))


def wait_for_run(r, run: str, poll_sec: float = 5.0) -> None:
    """작업 생성기보다 워커가 먼저 뜰 수 있다(쿠버네티스). run이 생길 때까지 한 번만 알리고 기다린다."""
    logged = False
    while not r.exists(k(run, "total")):
        if not logged:
            print(f"waiting for run '{run}' to be created", flush=True)
            logged = True
        time.sleep(poll_sec)


def _load_run_ctx(r, run: str, cache: collections.OrderedDict) -> dict | None:
    """run별 논문·제출물 메타를 LRU 캐시로 불러온다."""
    if run in cache:
        cache.move_to_end(run)
        return cache[run]
    meta = r.hgetall(k(run, "meta"))
    if not meta or "paper_dir" not in meta or "submission_dir" not in meta:
        return None
    paper_dir = Path(meta["paper_dir"])
    submission_dir = Path(meta["submission_dir"])
    try:
        paper_md = (paper_dir / "paper.md").read_text()
        addendum = "\n".join(
            (paper_dir / f).read_text() for f in ("addendum.md", "judge.addendum.md") if (paper_dir / f).exists()
        ).strip()
    except OSError:
        return None
    ctx = {
        "paper_md": paper_md,
        "addendum": addendum,
        "submission_dir": submission_dir,
        "est_tokens": (len(paper_md) + 40_000) // 4,
        "mock": meta.get("mock") == "1" or os.environ.get("MOCK_JUDGE") == "1",
        "quick": meta.get("quick") == "1",
    }
    cache[run] = ctx
    while len(cache) > MAX_RUN_CACHE:
        cache.popitem(last=False)
    return ctx


def process_message(r, stream: str, consumer: str, limiter: RateLimiter,
                    msg_id: str, fields: dict, reclaimed: bool,
                    run_override: str | None = None,
                    cache: collections.OrderedDict | None = None) -> None:
    run = run_override or fields.get("run")
    leaf = json.loads(fields["leaf"])
    leaf_id = fields["leaf_id"]

    def log(msg: str) -> None:
        prefix = f"[{consumer} {run}]" if run_override is None and run else f"[{consumer}]"
        print(f"{prefix} {msg}", flush=True)

    def save(result: dict) -> None:
        r.hset(k(run, "results"), leaf_id, json.dumps(result, ensure_ascii=False))

    def ack_and_trim() -> None:
        r.xack(stream, GROUP, msg_id)
        if stream == GLOBAL_STREAM and hasattr(r, "xdel"):
            r.xdel(stream, msg_id)

    if not run or r.exists(k(run, "aborted")) or not r.exists(k(run, "total")):
        # 중단되었거나 이미 정리된 run의 메시지는 버리고 ACK한다(다른 run을 막지 않게)
        ack_and_trim()
        return

    if reclaimed:
        r.incr(k(run, "retries"))
        # 몇 번째 배달인지 확인해서, 계속 실패하는 메시지는 실패로 기록하고 끝낸다
        info = r.xpending_range(stream, GROUP, min=msg_id, max=msg_id, count=1)
        if info and info[0]["times_delivered"] > MAX_DELIVERIES:
            save({"leaf_id": leaf_id, "error": "max deliveries exceeded", "worker": consumer})
            ack_and_trim()
            log(f"gave up {leaf_id[:8]}")
            return

    run_cache = cache if cache is not None else collections.OrderedDict()
    ctx = _load_run_ctx(r, run, run_cache)
    if ctx is None:
        ack_and_trim()
        return

    started = time.time()
    r.rpush(k(run, "events"), json.dumps({"leaf_id": leaf_id, "worker": consumer, "event": "start", "t": started}))
    # 모의 채점은 LLM을 부르지 않으므로 공용 호출 한도를 쓰지 않는다(실제 작업이 쓸 한도를 뺏지 않게)
    waited = 0.0 if ctx["mock"] else limiter.acquire(ctx["est_tokens"])
    try:
        out = judge_leaf(ctx["paper_md"], ctx["addendum"], ctx["submission_dir"], leaf,
                         mock=ctx["mock"], quick=ctx["quick"])
    except Exception as e:  # ACK하지 않는다 → CLAIM_IDLE_SEC 뒤 다른 워커(또는 자신)가 다시 가져간다
        r.incr(k(run, "failures"))
        log(f"error on {leaf_id[:8]}: {type(e).__name__}: {str(e)[:200]}")
        if is_fatal_llm_error(e):
            # 사용 한도·인증 실패: 재시도해도 풀리지 않는다. run을 중단 표시한다.
            r.set(k(run, "aborted"), f"{type(e).__name__}: {str(e)[:300]}")
            if run_override is None:
                ack_and_trim()
        return
    finished = time.time()
    if r.exists(k(run, "total")) and not r.exists(k(run, "aborted")):
        save({
            "leaf_id": leaf_id,
            "category": leaf["task_category"],
            "requirements": leaf["requirements"],
            "score": out["score"],
            "verdict": out.get("verdict"),
            "summary": out.get("summary", ""),
            "reason": out["reason"],
            "usage": out["usage"],
            "parse_retries": out["parse_retries"],
            "worker": consumer,
            "started_at": started,
            "finished_at": finished,
            "ratelimit_wait_sec": round(waited, 2),
        })
        r.rpush(k(run, "events"), json.dumps({"leaf_id": leaf_id, "worker": consumer, "event": "done", "t": finished}))
    ack_and_trim()  # 결과 저장 후 ACK + 공용 스트림이면 XDEL로 메모리 누적 방지
    log(f"done {leaf_id[:8]} {out.get('verdict') or out['score']} {finished - started:.1f}s")


def _ensure_group(r, stream: str) -> None:
    try:
        r.xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def _run_finished(r, run: str) -> bool:
    """더 채점할 항목이 없는 run인지: 기록이 지워졌거나, 중단됐거나, 결과가 다 모였다."""
    total = r.get(k(run, "total"))
    if total is None or r.exists(k(run, "aborted")):
        return True
    return r.hlen(k(run, "results")) >= int(total)


def _reclaim_one(r, consumer: str, limiter: RateLimiter, cache: collections.OrderedDict) -> bool:
    """죽은 워커가 CLAIM_IDLE_SEC 넘게 들고 있던 항목을 하나 가져와 채점한다. 채점했으면 True."""
    streams = [k(run, "tasks") for run in r.lrange(ACTIVE_RUNS, 0, -1)] + [GLOBAL_STREAM]
    for stream in streams:
        if not r.exists(stream):  # run 기록이 만료되었거나 지워졌다
            continue
        try:
            resp = r.xautoclaim(stream, GROUP, consumer, min_idle_time=int(CLAIM_IDLE_SEC * 1000), count=1)
        except ResponseError as e:
            if "NOGROUP" in str(e):  # run 기록이 만료되어 스트림이 없다
                continue
            raise
        for msg_id, fields in resp[1]:
            if not fields:  # 이미 지워진 메시지
                r.xack(stream, GROUP, msg_id)
                continue
            print(f"[{consumer}] reclaimed {fields.get('run', '?')}/{fields['leaf_id'][:8]}", flush=True)
            process_message(r, stream, consumer, limiter, msg_id, fields, reclaimed=True, cache=cache)
            return True
    return False


def _read_one(r, consumer: str, limiter: RateLimiter, cache: collections.OrderedDict) -> bool:
    """채점 중인 run을 돌아가며 새 항목 하나를 가져와 채점한다. 채점했으면 True.

    LMOVE로 목록 맨 앞 run을 맨 뒤로 옮기면서 가져오므로, 여러 워커가 같이 돌아도 run마다 차례가 돌아간다.
    새 항목이 없는 run은 건너뛰고, 다 끝난 run은 목록에서 뺀다.
    """
    for _ in range(r.llen(ACTIVE_RUNS)):
        run = r.lmove(ACTIVE_RUNS, ACTIVE_RUNS, "LEFT", "RIGHT")
        if run is None:
            break
        stream = k(run, "tasks")
        resp = None
        if r.exists(stream):  # 없으면 run 기록이 만료되었거나 지워졌다
            try:
                resp = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1)
            except ResponseError as e:
                if "NOGROUP" not in str(e):
                    raise
        if resp and resp[0][1]:
            msg_id, fields = resp[0][1][0]
            process_message(r, stream, consumer, limiter, msg_id, fields, reclaimed=False, cache=cache)
            return True
        if _run_finished(r, run):
            r.lrem(ACTIVE_RUNS, 0, run)
    return False


def multi_step(r, consumer: str, limiter: RateLimiter, cache: collections.OrderedDict, state: dict,
               block_ms: int | None = 1000) -> bool:
    """multi-run 워커의 한 번 반복. 항목 하나를 채점했으면 True."""
    if not state.get("group_ready"):
        _ensure_group(r, GLOBAL_STREAM)
        state["group_ready"] = True
    now = time.time()
    if now - state.get("last_reclaim", 0.0) >= RECLAIM_EVERY_SEC:
        state["last_reclaim"] = now
        if _reclaim_one(r, consumer, limiter, cache):
            state["last_reclaim"] = 0.0  # 하나 찾았으면 다음 반복에서 또 찾는다
            return True
    if _read_one(r, consumer, limiter, cache):
        return True
    # 새 항목이 없다: 예전 공용 스트림에 남은 항목을 잠깐 기다리며 본다(빈 반복으로 CPU를 쓰지 않게)
    resp = r.xreadgroup(GROUP, consumer, {GLOBAL_STREAM: ">"}, count=1, block=block_ms)
    if resp and resp[0][1]:
        msg_id, fields = resp[0][1][0]
        process_message(r, GLOBAL_STREAM, consumer, limiter, msg_id, fields, reclaimed=False, cache=cache)
        return True
    return False


def run_multi_loop(r, consumer: str, limiter: RateLimiter) -> None:
    """채점 중인 여러 run을 재시작 없이 돌아가며 처리한다(라운드 로빈)."""
    cache: collections.OrderedDict = collections.OrderedDict()
    state: dict = {}
    print(f"[{consumer}] started (multi-run, round robin over {ACTIVE_RUNS})", flush=True)
    while True:
        try:
            multi_step(r, consumer, limiter, cache, state)
        except ResponseError as e:
            if "NOGROUP" in str(e):
                state["group_ready"] = False  # 예전 공용 스트림이 지워졌다: 다음 반복에서 다시 만든다
                continue
            print(f"[{consumer}] redis error: {type(e).__name__}: {str(e)[:200]}", flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"[{consumer}] unexpected loop error: {type(e).__name__}: {str(e)[:200]}", flush=True)
            time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=os.environ.get("RUN_ID"))
    ap.add_argument("--exit-when-done", action="store_true", help="모든 작업이 끝나면 종료")
    args = ap.parse_args()
    if not args.run:
        ap.error("--run or RUN_ID is required")

    r = connect()
    run = args.run
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    limiter = RateLimiter(r, RATE_LIMIT_RPM, RATE_LIMIT_TPM)

    if run in MULTI_RUN_IDS:
        run_multi_loop(r, consumer, limiter)
        return

    stream = k(run, "tasks")
    wait_for_run(r, run)
    cache: collections.OrderedDict = collections.OrderedDict()
    _load_run_ctx(r, run, cache)

    print(f"[{consumer}] started", flush=True)
    total = int(r.get(k(run, "total")))
    idle_logged = False
    while True:
        if r.exists(k(run, "aborted")):
            print(f"[{consumer}] run aborted: {r.get(k(run, 'aborted'))}", flush=True)
            sys.exit(3)
        if not r.exists(k(run, "total")):
            # run 기록이 지워졌다(정리했거나 Redis가 다시 떴다). 죽지 않고 다른 run을 맡을 때까지 쉰다.
            if not idle_logged:
                print(f"[{consumer}] run is gone from Redis; idle until reassigned", flush=True)
                idle_logged = True
            time.sleep(30)
            continue
        idle_logged = False
        try:
            # 1) 죽은 워커가 들고 있던 메시지 먼저
            _, claimed, _ = r.xautoclaim(stream, GROUP, consumer, min_idle_time=int(CLAIM_IDLE_SEC * 1000), count=1)
            if claimed:
                msg_id, fields = claimed[0]
                print(f"[{consumer}] reclaimed {fields['leaf_id'][:8]}", flush=True)
                process_message(r, stream, consumer, limiter, msg_id, fields, reclaimed=True,
                                run_override=run, cache=cache)
                continue
            # 2) 새 메시지
            resp = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1, block=2000)
        except ResponseError as e:
            if "NOGROUP" not in str(e):
                raise
            continue  # 스트림이 방금 지워졌다: 위에서 run이 없어진 것으로 처리한다
        if resp:
            msg_id, fields = resp[0][1][0]
            process_message(r, stream, consumer, limiter, msg_id, fields, reclaimed=False,
                            run_override=run, cache=cache)
            continue
        # 3) 읽을 게 없음
        if args.exit_when_done and r.hlen(k(run, "results")) >= total:
            print(f"[{consumer}] all tasks done, exiting", flush=True)
            break


if __name__ == "__main__":
    main()
