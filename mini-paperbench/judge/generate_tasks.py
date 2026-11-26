"""루브릭을 읽어 리프마다 메시지 1개를 Redis Stream에 넣고, 전체 작업 수를 기록한다.

사용: python -m judge.generate_tasks --run r1 [--limit 20] [--category "Code Development"] [--category ...]
"""
import argparse
import json
import time
from pathlib import Path

from redis.exceptions import ResponseError

from judge.common import GLOBAL_STREAM, GROUP, connect, k
from judge.rubric import load_leaves


def enqueue_run(r, run: str, paper_dir: Path, submission_dir: Path, leaves: list[dict],
                *, global_queue: bool = False, mock: bool = False) -> str:
    if r.exists(k(run, "total")):
        raise SystemExit(f"run '{run}' already exists; use a new --run name")
    stream = GLOBAL_STREAM if global_queue else k(run, "tasks")
    try:
        r.xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    r.hset(k(run, "meta"), mapping={
        "paper_dir": str(Path(paper_dir).resolve()),
        "submission_dir": str(Path(submission_dir).resolve()),
        "mock": "1" if mock else "0",
        "created_at": time.time(),
    })
    r.set(k(run, "total"), len(leaves))
    for leaf in leaves:
        fields = {"leaf_id": leaf["id"], "leaf": json.dumps(leaf)}
        if global_queue:
            fields["run"] = run
        r.xadd(stream, fields)
    return stream


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--paper-dir", default="data/papers/semantic-self-consistency")
    ap.add_argument("--submission-dir", default="data/judge_eval/semantic-self-consistency/0/submission")
    ap.add_argument("--limit", type=int, default=None, help="앞에서부터 N개 리프만")
    ap.add_argument("--category", action="append", default=None, help="여러 번 줄 수 있다")
    ap.add_argument("--global-queue", action="store_true", help="공용 multi-run 스트림(pb:global:tasks)에 넣는다")
    ap.add_argument("--mock", action="store_true", help="이 run만 모의 채점(MOCK_JUDGE)으로 표시한다")
    args = ap.parse_args()

    r = connect()
    leaves = load_leaves(Path(args.paper_dir) / "rubric.json")
    if args.category:
        leaves = [l for l in leaves if l["task_category"] in args.category]
    leaves = leaves[: args.limit]
    stream = enqueue_run(r, args.run, Path(args.paper_dir), Path(args.submission_dir), leaves,
                         global_queue=args.global_queue, mock=args.mock)
    print(f"run={args.run} tasks={len(leaves)} stream={stream}")


if __name__ == "__main__":
    main()
