"""judge 결과를 정답 라벨과 비교해 F1 등을 계산한다. LLM을 부르지 않는다.

입력: analyze.py가 만든 results/<run>.json 과 정답 라벨 파일의 짝 (여러 개 가능)
  - JudgeEval 형식: expected_result.json (루브릭 트리에 사람 점수)
  - 합성 논문 형식: labels/sub_*.json ({"labels": [{"leaf_id", "score", "reason"}]})
출력: results/judgeeval_<name>.json

사용:
  python -m judge.judgeeval --name gemini-3.5-flash-lite \
      --pair je_gemini-3.5-flash-lite=data/judge_eval/semantic-self-consistency/0/expected_result.json
  # --check 짝은 지표에 넣지 않고 따로 정확도만 보고한다(예: 빈 제출물 건전성 검사)
"""
import argparse
import collections
import json
from pathlib import Path


def load_labels(path: Path) -> dict[str, dict]:
    """{leaf_id: {"score", "requirements", "why"}}"""
    data = json.loads(path.read_text())
    if "labels" in data:
        return {l["leaf_id"]: {"score": int(l["score"]), "requirements": l.get("leaf_key", ""), "why": l.get("reason", "")}
                for l in data["labels"]}
    out = {}
    def walk(n: dict) -> None:
        if not n["sub_tasks"]:
            out[n["id"]] = {"score": int(n["score"]), "requirements": n["requirements"], "why": ""}
        for c in n["sub_tasks"]:
            walk(c)
    walk(data)
    return out


def metrics(pairs: list[tuple[int, int]]) -> dict:
    """pairs = [(judge, 정답)]. 1점을 양성으로 본다."""
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
        # 정답에 0점이 없으면 0점 쪽 지표와 macro-F1은 의미가 없다(None)
        "recall_score0": round(tn / (tn + fp), 4) if tn + fp else None,
        "f1_score0": round(f1_neg, 4) if tn + fp else None,
        "macro_f1": round((f1_pos + f1_neg) / 2, 4) if tn + fp and tp + fn else None,
        "accuracy": round((tp + tn) / n, 4) if n else None,
    }


def collect(pair: str) -> tuple[str, dict, list[dict]]:
    run_name, label_path = pair.split("=", 1)
    run = json.loads(Path(f"results/{run_name}.json").read_text())
    labels = load_labels(Path(label_path))
    leaves = []
    for lid, r in run["leaves"].items():
        if "error" in r or lid not in labels:
            continue
        leaves.append({
            "run": run_name,
            "id": lid,
            "category": r["category"],
            "criterion": r.get("requirements") or labels[lid]["requirements"],
            "judge": int(r["score"]),
            "label": labels[lid]["score"],
            "label_why": labels[lid]["why"],
            "reason": r["reason"],
        })
    return run_name, run, leaves


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--pair", action="append", required=True, help="<run>=<라벨 파일>")
    ap.add_argument("--check", action="append", default=[], help="지표에서 빼고 따로 볼 <run>=<라벨 파일>")
    args = ap.parse_args()

    runs, leaves = {}, []
    for pair in args.pair:
        name, run, ls = collect(pair)
        runs[name] = run
        leaves += ls

    pairs = [(l["judge"], l["label"]) for l in leaves]
    by_cat, by_run = collections.defaultdict(list), collections.defaultdict(list)
    for l in leaves:
        by_cat[l["category"]].append((l["judge"], l["label"]))
        by_run[l["run"]].append((l["judge"], l["label"]))

    checks = {}
    for pair in args.check:
        name, run, ls = collect(pair)
        checks[name] = {"n": len(ls), "accuracy": round(sum(l["judge"] == l["label"] for l in ls) / len(ls), 4) if ls else None,
                        "judge_ones": sum(l["judge"] for l in ls)}

    out = {
        "name": args.name,
        "graded": len(leaves),
        "overall": metrics(pairs),
        # 비교 기준: 아무것도 보지 않고 전부 1점을 주는 judge
        "baseline_always_1": metrics([(1, y) for _, y in pairs]),
        "by_category": {k: metrics(v) for k, v in sorted(by_cat.items())},
        "by_run": {k: metrics(v) for k, v in by_run.items()},
        "checks": checks,
        "runs": {k: {key: r[key] for key in ("wall_time_sec", "workers", "retries", "failures", "parse_retries",
                                             "ratelimit_wait_sec", "usage", "errors", "submission_dir")}
                 for k, r in runs.items()},
        "usage_total": {key: sum(r["usage"][key] for r in runs.values())
                        for key in ("calls", "input_tokens", "output_tokens", "cached_tokens")},
        "disagreements": [l for l in leaves if l["judge"] != l["label"]],
        "leaves": leaves,
    }
    path = Path(f"results/judgeeval_{args.name}.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({k: out[k] for k in ("name", "graded", "overall", "baseline_always_1", "by_run", "checks", "usage_total")},
                     ensure_ascii=False, indent=1))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
