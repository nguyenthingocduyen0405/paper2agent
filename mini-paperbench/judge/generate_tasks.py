"""루브릭을 읽어 리프마다 메시지 1개를 Redis Stream에 넣고, 전체 작업 수를 기록한다.

사용: python -m judge.generate_tasks --run r1 [--limit 20] [--category "Code Development"]
"""
import argparse
import json
import time
from pathlib import Path

from judge.common import GROUP, connect, k
from judge.rubric import load_leaves

ap = argparse.ArgumentParser()
ap.add_argument("--run", required=True)
ap.add_argument("--paper-dir", default="data/papers/semantic-self-consistency")
ap.add_argument("--submission-dir", default="data/judge_eval/semantic-self-consistency/0/submission")
ap.add_argument("--limit", type=int, default=None, help="앞에서부터 N개 리프만")
ap.add_argument("--category", default=None)
args = ap.parse_args()

r = connect()
if r.exists(k(args.run, "total")):
    raise SystemExit(f"run '{args.run}' already exists; use a new --run name")

leaves = load_leaves(Path(args.paper_dir) / "rubric.json")
if args.category:
    leaves = [l for l in leaves if l["task_category"] == args.category]
leaves = leaves[: args.limit]

stream = k(args.run, "tasks")
r.xgroup_create(stream, GROUP, id="0", mkstream=True)
r.hset(k(args.run, "meta"), mapping={
    "paper_dir": str(Path(args.paper_dir).resolve()),
    "submission_dir": str(Path(args.submission_dir).resolve()),
    "created_at": time.time(),
})
for leaf in leaves:
    r.xadd(stream, {"leaf_id": leaf["id"], "leaf": json.dumps(leaf)})
r.set(k(args.run, "total"), len(leaves))
print(f"run={args.run} tasks={len(leaves)}")
