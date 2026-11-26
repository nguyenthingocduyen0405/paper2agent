"""워커: Stream에서 리프를 하나씩 받아 채점하고, 결과를 저장한 뒤 XACK한다.

장애 복구: 매 반복마다 먼저 XAUTOCLAIM으로 CLAIM_IDLE_SEC 넘게 ACK되지 않은 메시지
(죽은 워커가 들고 있던 것)를 가져온다. 없으면 XREADGROUP으로 새 메시지를 읽는다.
결과는 리프 ID를 키로 HSET하므로 같은 리프가 두 번 처리돼도 결과는 1개다.

모드:
- RUN_ID=multi (또는 *, auto): 공용 스트림(pb:global:tasks)에서 여러 run의 작업을
  재시작 없이 동시에 처리한다. 각 메시지의 run 필드로 논문·제출물을 찾아 채점한다.
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

from judge.common import (CLAIM_IDLE_SEC, GLOBAL_STREAM, GROUP, MAX_DELIVERIES,
                          MULTI_RUN_IDS, RATE_LIMIT_RPM, RATE_LIMIT_TPM, connect, k)
from judge.judge import is_fatal_llm_error, judge_leaf
from judge.ratelimit import RateLimiter

MAX_RUN_CACHE = 16


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
    paper_md = (paper_dir / "paper.md").read_text()
    addendum = "\n".join(
        (paper_dir / f).read_text() for f in ("addendum.md", "judge.addendum.md") if (paper_dir / f).exists()
    ).strip()
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

    if not run or r.exists(k(run, "aborted")) or not r.exists(k(run, "total")):
        # 중단되었거나 이미 정리된 run의 메시지는 버리고 ACK한다(다른 run을 막지 않게)
        r.xack(stream, GROUP, msg_id)
        return

    if reclaimed:
        r.incr(k(run, "retries"))
        # 몇 번째 배달인지 확인해서, 계속 실패하는 메시지는 실패로 기록하고 끝낸다
        info = r.xpending_range(stream, GROUP, min=msg_id, max=msg_id, count=1)
        if info and info[0]["times_delivered"] > MAX_DELIVERIES:
            save({"leaf_id": leaf_id, "error": "max deliveries exceeded", "worker": consumer})
            r.xack(stream, GROUP, msg_id)
            log(f"gave up {leaf_id[:8]}")
            return

    run_cache = cache if cache is not None else collections.OrderedDict()
    ctx = _load_run_ctx(r, run, run_cache)
    if ctx is None:
        r.xack(stream, GROUP, msg_id)
        return

    started = time.time()
    r.rpush(k(run, "events"), json.dumps({"leaf_id": leaf_id, "worker": consumer, "event": "start", "t": started}))
    waited = limiter.acquire(0 if ctx["mock"] else ctx["est_tokens"])
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
                r.xack(stream, GROUP, msg_id)
        return
    finished = time.time()
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
    r.xack(stream, GROUP, msg_id)  # 결과 저장 후 ACK: 저장 전에 죽으면 다시 처리된다
    r.rpush(k(run, "events"), json.dumps({"leaf_id": leaf_id, "worker": consumer, "event": "done", "t": finished}))
    log(f"done {leaf_id[:8]} {out.get('verdict') or out['score']} {finished - started:.1f}s")


def _ensure_group(r, stream: str) -> None:
    try:
        r.xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def run_multi_loop(r, consumer: str, limiter: RateLimiter) -> None:
    """공용 스트림(pb:global:tasks)에서 여러 run의 리프를 재시작 없이 동시에 처리한다."""
    stream = GLOBAL_STREAM
    _ensure_group(r, stream)
    cache: collections.OrderedDict = collections.OrderedDict()
    print(f"[{consumer}] started (multi-run queue: {stream})", flush=True)
    while True:
        try:
            _, claimed, _ = r.xautoclaim(stream, GROUP, consumer, min_idle_time=int(CLAIM_IDLE_SEC * 1000), count=1)
            if claimed:
                msg_id, fields = claimed[0]
                print(f"[{consumer}] reclaimed {fields.get('run', '?')}/{fields['leaf_id'][:8]}", flush=True)
                process_message(r, stream, consumer, limiter, msg_id, fields, reclaimed=True, cache=cache)
                continue
            resp = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1, block=2000)
        except ResponseError as e:
            if "NOGROUP" in str(e):
                _ensure_group(r, stream)
                continue
            raise
        if resp:
            msg_id, fields = resp[0][1][0]
            process_message(r, stream, consumer, limiter, msg_id, fields, reclaimed=False, cache=cache)


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
