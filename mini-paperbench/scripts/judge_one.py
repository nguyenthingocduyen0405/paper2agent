"""JudgeEval 제출물의 리프 N개를 judge로 채점하고 사람 채점과 나란히 출력한다.

사용: python scripts/judge_one.py [--n 1] [--start 0] [--category "Code Development"]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.judge import judge_leaf  # noqa: E402
from judge.rubric import load_leaves  # noqa: E402

PAPER = Path("data/papers/semantic-self-consistency")
JE = Path("data/judge_eval/semantic-self-consistency/0")

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=1)
ap.add_argument("--start", type=int, default=0)
ap.add_argument("--category", default=None)
args = ap.parse_args()

paper_md = (PAPER / "paper.md").read_text()
addendum = "\n".join((PAPER / f).read_text() for f in ("addendum.md", "judge.addendum.md")).strip()

leaves = load_leaves(PAPER / "rubric.json")
if args.category:
    leaves = [l for l in leaves if l["task_category"] == args.category]
human = {}
def walk(n):
    if not n["sub_tasks"]:
        human[n["id"]] = n["score"]
    for c in n["sub_tasks"]:
        walk(c)
walk(json.loads((JE / "expected_result.json").read_text()))

for leaf in leaves[args.start : args.start + args.n]:
    t = time.time()
    try:
        r = judge_leaf(paper_md, addendum, JE / "submission", leaf)
    except Exception as e:
        print(json.dumps({"id": leaf["id"], "error": str(e)[:300]}, ensure_ascii=False))
        continue
    print(json.dumps({
        "id": leaf["id"],
        "category": leaf["task_category"],
        "criterion": leaf["requirements"][:120],
        "score": r["score"],
        "human": human.get(leaf["id"]),
        "reason": r["reason"][:300],
        "usage": r["usage"],
        "parse_retries": r["parse_retries"],
        "sec": round(time.time() - t, 1),
    }, ensure_ascii=False))
