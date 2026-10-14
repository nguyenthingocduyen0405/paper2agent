"""워커: Stream에서 리프를 하나씩 받아 채점하고, 결과를 저장한 뒤 XACK한다.

장애 복구: 매 반복마다 먼저 XAUTOCLAIM으로 CLAIM_IDLE_SEC 넘게 ACK되지 않은 메시지
(죽은 워커가 들고 있던 것)를 가져온다. 없으면 XREADGROUP으로 새 메시지를 읽는다.
결과는 리프 ID를 키로 HSET하므로 같은 리프가 두 번 처리돼도 결과는 1개다.

사용: python -m judge.worker --run r1 [--exit-when-done]
"""
import argparse
import json
import os
import socket
import time
from pathlib import Path

from judge.common import (CLAIM_IDLE_SEC, GROUP, MAX_DELIVERIES, RATE_LIMIT_RPM,
                          RATE_LIMIT_TPM, connect, k)
from judge.judge import judge_leaf
from judge.ratelimit import RateLimiter

ap = argparse.ArgumentParser()
ap.add_argument("--run", required=True)
ap.add_argument("--exit-when-done", action="store_true", help="모든 작업이 끝나면 종료")
args = ap.parse_args()

r = connect()
run = args.run
stream = k(run, "tasks")
consumer = f"{socket.gethostname()}-{os.getpid()}"
limiter = RateLimiter(r, RATE_LIMIT_RPM, RATE_LIMIT_TPM)

meta = r.hgetall(k(run, "meta"))
paper_dir = Path(meta["paper_dir"])
submission_dir = Path(meta["submission_dir"])
paper_md = (paper_dir / "paper.md").read_text()
addendum = "\n".join(
    (paper_dir / f).read_text() for f in ("addendum.md", "judge.addendum.md") if (paper_dir / f).exists()
).strip()
est_tokens = (len(paper_md) + 40_000) // 4  # rate limiter용 대략적 입력 토큰 수(논문 + 제출물)


def log(msg: str) -> None:
    print(f"[{consumer}] {msg}", flush=True)


def save(leaf_id: str, result: dict) -> None:
    r.hset(k(run, "results"), leaf_id, json.dumps(result, ensure_ascii=False))


def process(msg_id: str, fields: dict, reclaimed: bool) -> None:
    leaf = json.loads(fields["leaf"])
    leaf_id = fields["leaf_id"]
    if reclaimed:
        r.incr(k(run, "retries"))
        # 몇 번째 배달인지 확인해서, 계속 실패하는 메시지는 실패로 기록하고 끝낸다
        info = r.xpending_range(stream, GROUP, min=msg_id, max=msg_id, count=1)
        if info and info[0]["times_delivered"] > MAX_DELIVERIES:
            save(leaf_id, {"leaf_id": leaf_id, "error": "max deliveries exceeded", "worker": consumer})
            r.xack(stream, GROUP, msg_id)
            log(f"gave up {leaf_id[:8]}")
            return

    started = time.time()
    r.rpush(k(run, "events"), json.dumps({"leaf_id": leaf_id, "worker": consumer, "event": "start", "t": started}))
    waited = limiter.acquire(est_tokens if os.environ.get("MOCK_JUDGE") != "1" else 0)
    try:
        out = judge_leaf(paper_md, addendum, submission_dir, leaf)
    except Exception as e:  # ACK하지 않는다 → CLAIM_IDLE_SEC 뒤 다른 워커(또는 자신)가 다시 가져간다
        r.incr(k(run, "failures"))
        log(f"error on {leaf_id[:8]}: {type(e).__name__}: {str(e)[:200]}")
        return
    finished = time.time()
    save(leaf_id, {
        "leaf_id": leaf_id,
        "category": leaf["task_category"],
        "score": out["score"],
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
    log(f"done {leaf_id[:8]} score={out['score']} {finished - started:.1f}s")


log("started")
total = int(r.get(k(run, "total")))
while True:
    # 1) 죽은 워커가 들고 있던 메시지 먼저
    _, claimed, _ = r.xautoclaim(stream, GROUP, consumer, min_idle_time=int(CLAIM_IDLE_SEC * 1000), count=1)
    if claimed:
        msg_id, fields = claimed[0]
        log(f"reclaimed {fields['leaf_id'][:8]}")
        process(msg_id, fields, reclaimed=True)
        continue
    # 2) 새 메시지
    resp = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1, block=2000)
    if resp:
        msg_id, fields = resp[0][1][0]
        process(msg_id, fields, reclaimed=False)
        continue
    # 3) 읽을 게 없음
    if args.exit_when_done and r.hlen(k(run, "results")) >= total:
        log("all tasks done, exiting")
        break
