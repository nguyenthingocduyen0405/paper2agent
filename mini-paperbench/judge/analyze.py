"""완료 수 = 전체 수이면 결과를 집계해 results/<run>.json으로 저장한다.

사용: python -m judge.analyze --run r1 [--label "mock w3"]
"""
import argparse
import collections
import json
import sys
from pathlib import Path

from judge.common import connect, k

ap = argparse.ArgumentParser()
ap.add_argument("--run", required=True)
ap.add_argument("--label", default="")
ap.add_argument("--out-dir", default="results")
args = ap.parse_args()

r = connect()
run = args.run
total = int(r.get(k(run, "total")) or 0)
results = {lid: json.loads(v) for lid, v in r.hgetall(k(run, "results")).items()}
if total == 0 or len(results) < total:
    print(f"not done yet: {len(results)}/{total}")
    sys.exit(1)

meta = r.hgetall(k(run, "meta"))
events = [json.loads(e) for e in r.lrange(k(run, "events"), 0, -1)]
ok = [v for v in results.values() if "error" not in v]
starts = [e["t"] for e in events if e["event"] == "start"]
done_per_leaf = collections.Counter(e["leaf_id"] for e in events if e["event"] == "done")
start_per_leaf = collections.Counter(e["leaf_id"] for e in events if e["event"] == "start")

summary = {
    "run": run,
    "label": args.label,
    "total": total,
    "completed": len(results),
    "errors": len(results) - len(ok),
    "workers": len({v["worker"] for v in ok}),
    "tasks_per_worker": dict(collections.Counter(v["worker"] for v in ok)),
    # 처리 시간: 첫 작업 시작 ~ 마지막 작업 끝
    "wall_time_sec": round(max(v["finished_at"] for v in ok) - min(starts), 2) if ok else None,
    "mean_task_sec": round(sum(v["finished_at"] - v["started_at"] for v in ok) / len(ok), 2) if ok else None,
    "retries": int(r.get(k(run, "retries")) or 0),  # XAUTOCLAIM으로 다시 가져간 횟수
    "failures": int(r.get(k(run, "failures")) or 0),  # judge 예외 횟수
    "parse_retries": sum(v["parse_retries"] for v in ok),
    "ratelimit_wait_sec": round(sum(v["ratelimit_wait_sec"] for v in ok), 2),
    # 중복 확인: 결과는 리프당 1개(해시 키). 처리 시작/완료가 2번 이상인 리프 목록
    "leaves_started_more_than_once": [lid for lid, c in start_per_leaf.items() if c > 1],
    "leaves_completed_more_than_once": [lid for lid, c in done_per_leaf.items() if c > 1],
    "usage": {
        key: sum(v["usage"][key] for v in ok)
        for key in ("calls", "input_tokens", "output_tokens", "cached_tokens")
    },
    "score_mean": round(sum(v["score"] for v in ok) / len(ok), 4) if ok else None,
    "submission_dir": meta.get("submission_dir"),
    "leaves": results,
}

out = Path(args.out_dir) / f"{run}.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
print(json.dumps({k_: v for k_, v in summary.items() if k_ != "leaves"}, ensure_ascii=False, indent=1))
print(f"saved {out}")
