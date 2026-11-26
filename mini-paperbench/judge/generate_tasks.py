"""루브릭을 읽어 리프마다 메시지 1개를 Redis Stream에 넣고, 전체 작업 수를 기록한다.

사용: python -m judge.generate_tasks --run r1 [--limit 20] [--category "Code Development"] [--category ...]
"""
import argparse
import json
import time
from pathlib import Path

from redis.exceptions import ResponseError

from judge.common import ACTIVE_RUNS, GROUP, connect, k
from judge.rubric import load_leaves


def enqueue_run(r, run: str, paper_dir: Path, submission_dir: Path, leaves: list[dict],
                *, global_queue: bool = False, mock: bool = False, quick: bool = False) -> str:
    if r.exists(k(run, "total")):
        raise SystemExit(f"run '{run}' already exists; use a new --run name")
    # 항목은 항상 run별 스트림에 넣는다. multi-run 워커는 ACTIVE_RUNS 목록의 run을 돌아가며 한 항목씩
    # 가져가므로, 먼저 들어온 큰 작업이 끝날 때까지 뒤 작업이 기다리지 않는다.
    stream = k(run, "tasks")
    try:
        r.xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    target = r.pipeline(transaction=True) if hasattr(r, "pipeline") else r
    target.hset(k(run, "meta"), mapping={
        "paper_dir": str(Path(paper_dir).resolve()),
        "submission_dir": str(Path(submission_dir).resolve()),
        "mock": "1" if mock else "0",
        "quick": "1" if quick else "0",
        "created_at": time.time(),
    })
    target.set(k(run, "total"), len(leaves))
    for leaf in leaves:
        fields = {"leaf_id": leaf["id"], "leaf": json.dumps(leaf)}
        if global_queue:
            fields["run"] = run
        target.xadd(stream, fields)
    if global_queue:
        target.rpush(ACTIVE_RUNS, run)  # 항목과 total을 넣은 뒤에 목록에 올린다(빈 run으로 보고 지우지 않게)
    if target is not r:
        target.execute()
    return stream


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--paper-dir", default="data/papers/semantic-self-consistency")
    ap.add_argument("--submission-dir", default="data/judge_eval/semantic-self-consistency/0/submission")
    ap.add_argument("--limit", type=int, default=None, help="앞에서부터 N개 리프만")
    ap.add_argument("--category", action="append", default=None, help="여러 번 줄 수 있다")
    ap.add_argument("--global-queue", action="store_true", help="multi-run 워커가 돌아가며 처리하는 run 목록(pb:global:runs)에 올린다")
    ap.add_argument("--mock", action="store_true", help="이 run만 모의 채점(MOCK_JUDGE)으로 표시한다")
    ap.add_argument("--quick", action="store_true", help="GPU 실행이 QUICK=1 모드로 돌았음을 judge에 알린다")
    args = ap.parse_args()

    r = connect()
    leaves = load_leaves(Path(args.paper_dir) / "rubric.json")
    if args.category:
        leaves = [l for l in leaves if l["task_category"] in args.category]
    leaves = leaves[: args.limit]
    stream = enqueue_run(r, args.run, Path(args.paper_dir), Path(args.submission_dir), leaves,
                         global_queue=args.global_queue, mock=args.mock, quick=args.quick)
    print(f"run={args.run} tasks={len(leaves)} stream={stream}")


if __name__ == "__main__":
    main()
