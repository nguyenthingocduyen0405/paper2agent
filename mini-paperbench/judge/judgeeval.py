"""judge 결과를 사람 채점(JudgeEval)과 비교해 F1 등을 계산한다. LLM을 부르지 않는다.

입력: analyze.py가 만든 results/<run>.json
출력: results/judgeeval_<모델명>.json

사용: python -m judge.judgeeval --run je_gemini-3.5-flash-lite --model gemini-3.5-flash-lite
"""
import argparse
import collections
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run", required=True)
ap.add_argument("--model", required=True)
ap.add_argument("--expected", default="data/judge_eval/semantic-self-consistency/0/expected_result.json")
ap.add_argument("--rubric", default="data/papers/semantic-self-consistency/rubric.json")
args = ap.parse_args()

run = json.loads(Path(f"results/{args.run}.json").read_text())

human: dict[str, dict] = {}
def walk(n: dict) -> None:
    if not n["sub_tasks"]:
        human[n["id"]] = n
    for c in n["sub_tasks"]:
        walk(c)
walk(json.loads(Path(args.expected).read_text()))


def metrics(pairs: list[tuple[int, int]]) -> dict:
    """pairs = [(judge, human)]. 1점을 양성으로 본다."""
    c = collections.Counter(pairs)
    tp, fp, fn, tn = c[(1, 1)], c[(1, 0)], c[(0, 1)], c[(0, 0)]
    def f1(tp, fp, fn):
        return 2 * tp / (2 * tp + fp + fn) if tp else 0.0
    f1_pos, f1_neg = f1(tp, fp, fn), f1(tn, fn, fp)
    n = len(pairs)
    return {
        "n": n,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "f1": round(f1_pos, 4),
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        # 사람 점수에 0점이 없으면 0점 F1과 macro-F1은 의미가 없다(None)
        "f1_score0": round(f1_neg, 4) if tn + fp else None,
        "macro_f1": round((f1_pos + f1_neg) / 2, 4) if tn + fp and tp + fn else None,
        "accuracy": round((tp + tn) / n, 4) if n else None,
    }


leaves = []
for lid, r in run["leaves"].items():
    if "error" in r or lid not in human:
        continue
    leaves.append({
        "id": lid,
        "category": r["category"],
        "criterion": human[lid]["requirements"],
        "judge": int(r["score"]),
        "human": int(human[lid]["score"]),
        "reason": r["reason"],
    })

pairs = [(l["judge"], l["human"]) for l in leaves]
by_cat = collections.defaultdict(list)
for l in leaves:
    by_cat[l["category"]].append((l["judge"], l["human"]))

out = {
    "model": args.model,
    "run": args.run,
    "graded": len(leaves),
    "skipped_errors": run["errors"],
    "overall": metrics(pairs),
    "by_category": {cat: metrics(p) for cat, p in sorted(by_cat.items())},
    # 비교 기준: 아무것도 보지 않고 전부 1점을 주는 judge
    "baseline_always_1": metrics([(1, h) for _, h in pairs]),
    "wall_time_sec": run["wall_time_sec"],
    "workers": run["workers"],
    "retries": run["retries"],
    "failures": run["failures"],
    "parse_retries": run["parse_retries"],
    "ratelimit_wait_sec": run["ratelimit_wait_sec"],
    "usage": run["usage"],
    "disagreements": [l for l in leaves if l["judge"] != l["human"]],
    "leaves": leaves,
}
path = Path(f"results/judgeeval_{args.model}.json")
path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps({k: out[k] for k in ("model", "graded", "overall", "by_category", "baseline_always_1",
                                      "wall_time_sec", "retries", "failures", "parse_retries", "usage")},
                 ensure_ascii=False, indent=1))
print(f"saved {path}")
