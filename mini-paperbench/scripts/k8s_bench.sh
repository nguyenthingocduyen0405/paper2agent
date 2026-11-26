#!/usr/bin/env bash
# k3s에서 '다음 단계' 두 실험을 한다. 시험용 judge(MOCK, 작업 1개 = 3초, LLM 호출 없음)를 쓴다.
#   1) 워커 수: 같은 작업 40개를 워커 1, 2, 4, 8개로 채점 (각 REPEAT회)
#   2) 장애 복구: 작업 20개를 워커 3개로 채점하다가 4.5초 뒤 워커 Pod 1개를 강제 삭제 (각 REPEAT회)
# 로컬 실험(results/t3_w*.json, t2_kill.json)과 같은 조건이다. 다른 점: 워커는 k3s Pod이고,
# 강제 종료는 kill -9 대신 `kubectl delete pod --grace-period=0 --force`이다.
#
# 사용(mini-paperbench 폴더에서): scripts/k8s_bench.sh [REPEAT=3]
# 결과: /data/results/<run>.json (집계), ~/bench_logs/<run>.* (시간 기록, 워커 로그, Pod 목록)
set -euo pipefail
cd "$(dirname "$0")/.."
REPEAT=${1:-3}
TAG=$(git rev-parse --short HEAD)
K="sudo k3s kubectl -n paperbench"
LOG=~/bench_logs; mkdir -p "$LOG"
STAMP=$(date +%m%d%H%M)

job_name() { echo "$1-$2" | tr '[:upper:]_' '[:lower:]-' | cut -c1-63; }
to_json_array() { python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"; }
run_job() {  # run_job <템플릿> <Job 이름> <인자...>
  local tpl=$1 name=$2; shift 2
  $K delete job "$name" --ignore-not-found >/dev/null
  sed -e "s/__NAME__/$name/" -e "s/__TAG__/$TAG/" -e "s|__ARGS__|$(to_json_array "$@")|" "k8s/jobs/$tpl.yaml" | $K apply -f - >/dev/null
  $K wait --for=condition=complete "job/$name" --timeout=600s >/dev/null
}
redis_cli() { $K exec deploy/redis -- redis-cli "$@"; }
now() { date +%s.%N; }
calc() { python3 -c "print(round($1, 2))"; }

workers_down() {
  $K scale deploy/judge-worker --replicas=0 >/dev/null
  $K wait --for=delete pod -l app=judge-worker --timeout=180s >/dev/null 2>&1 || true
}

# one_run <run> <작업 수> <워커 수> <CLAIM_IDLE_SEC> <라벨> [강제 삭제까지 초]
one_run() {
  local run=$1 n=$2 w=$3 claim=$4 label=$5 kill_after=${6:-}
  local victim="" t_kill="" first=""
  echo "== $run: $label"
  # 작업을 먼저 큐에 넣고, 워커 0개 → w개로 한 번에 띄운다(모든 워커가 거의 동시에 시작하도록)
  workers_down
  run_job task-generator "$(job_name gen "$run")" --run "$run" --limit "$n"
  $K set env deploy/judge-worker RUN_ID="$run" MOCK_JUDGE=1 RATE_LIMIT_RPM=100000 CLAIM_IDLE_SEC="$claim" >/dev/null
  local t_scale; t_scale=$(now)
  $K scale deploy/judge-worker --replicas="$w" >/dev/null
  $K rollout status deploy/judge-worker --timeout=300s >/dev/null
  local t_ready; t_ready=$(now)
  [ "$w" -ge 8 ] && { sudo k3s kubectl top node > "$LOG/$run.top.txt" 2>&1 || true; }

  if [ -n "$kill_after" ]; then
    until [ "$(redis_cli llen "pb:$run:events")" -gt 0 ]; do sleep 0.2; done
    first=$(redis_cli lindex "pb:$run:events" 0 | python3 -c 'import json, sys; print(json.load(sys.stdin)["t"])')
    sleep "$(python3 -c "import time; print(max(0.0, $first + $kill_after - time.time()))")"
    victim=$($K get pods -l app=judge-worker --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
    t_kill=$(now)
    $K delete pod "$victim" --grace-period=0 --force >/dev/null 2>&1
    echo "   force-deleted $victim at +$(calc "$t_kill - $first")s after first task start"
  fi

  local total; total=$(redis_cli get "pb:$run:total")
  until [ "$(redis_cli hlen "pb:$run:results")" -ge "$total" ]; do sleep 1; done
  local t_done; t_done=$(now)

  $K get pods -l app=judge-worker -o jsonpath='{range .items[*]}{.metadata.name} created={.metadata.creationTimestamp} started={.status.containerStatuses[0].state.running.startedAt}{"\n"}{end}' > "$LOG/$run.pods.txt"
  $K logs -l app=judge-worker --prefix --tail=-1 --max-log-requests=20 > "$LOG/$run.workers.log" 2>&1 || true
  run_job analyze "$(job_name analyze "$run")" --run "$run" --label "k3s, $label"
  python3 - "$LOG/$run.timing.json" <<PY
import json, sys
json.dump({
  "run": "$run", "label": "$label", "workers_requested": $w, "claim_idle_sec": $claim,
  "scale_to_ready_sec": round($t_ready - $t_scale, 2),
  "scale_to_all_done_sec": round($t_done - $t_scale, 2),
  "victim_pod": "$victim" or None,
  "kill_after_first_start_sec": (round(${t_kill:-0} - ${first:-0}, 2) if "$victim" else None),
  "kill_epoch": (${t_kill:-0} or None),
}, open(sys.argv[1], "w"), indent=1)
PY
  sudo python3 -c "import json; d=json.load(open('/data/results/$run.json')); print('   ', {k: d[k] for k in ('completed','total','errors','workers','wall_time_sec','retries','leaves_completed_more_than_once')})"
  redis_cli del "pb:$run:tasks" "pb:$run:total" "pb:$run:meta" "pb:$run:results" "pb:$run:events" "pb:$run:retries" "pb:$run:failures" >/dev/null
}

restore() {
  echo "== restore judge-worker (RUN_ID=multi, 6 replicas, configmap defaults)"
  $K set env deploy/judge-worker RUN_ID=multi MOCK_JUDGE- RATE_LIMIT_RPM- CLAIM_IDLE_SEC- >/dev/null
  $K scale deploy/judge-worker --replicas=6 >/dev/null
  $K rollout status deploy/judge-worker --timeout=300s >/dev/null
}
trap restore EXIT

echo "tag=$TAG stamp=$STAMP repeat=$REPEAT"
for r in $(seq 1 "$REPEAT"); do
  for w in 1 2 4 8; do
    one_run "b${STAMP}_w${w}_r${r}" 40 "$w" 120 "40 tasks, mock 3s, $w workers, rep $r"
  done
done
for r in $(seq 1 "$REPEAT"); do
  one_run "b${STAMP}_kill_r${r}" 20 3 10 "20 tasks, mock 3s, 3 workers, force-delete 1 pod after 4.5s, CLAIM_IDLE_SEC=10, rep $r" 4.5
done
echo "ALL DONE"
