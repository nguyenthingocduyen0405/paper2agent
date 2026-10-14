#!/usr/bin/env bash
# 쿠버네티스 없이 로컬 프로세스로 채점 1회를 돌린다.
# 사용: scripts/run_local.sh <run이름> <작업 수> <워커 수> [kill_after_sec]
#   kill_after_sec를 주면 그 시간 뒤 처리 중인 워커 1개를 kill -9 한다(장애 복구 확인용).
# MOCK_JUDGE, MOCK_DELAY_SEC, CLAIM_IDLE_SEC, RATE_LIMIT_* 는 환경 변수로 넘긴다.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=$1; N=$2; W=$3; KILL_AFTER=${4:-}
export CUDA_VISIBLE_DEVICES=""
LOG=logs/$RUN; mkdir -p "$LOG"

python -m judge.generate_tasks --run "$RUN" --limit "$N"
pids=()
for i in $(seq 1 "$W"); do
  python -m judge.worker --run "$RUN" --exit-when-done > "$LOG/w$i.log" 2>&1 &
  pids+=($!)
done
echo "workers: ${pids[*]}"

if [ -n "$KILL_AFTER" ]; then
  sleep "$KILL_AFTER"
  victim=${pids[0]}
  echo "kill -9 $victim (last log line: $(tail -1 "$LOG/w1.log"))"
  kill -9 "$victim"
  pids=("${pids[@]:1}")
fi

for p in "${pids[@]}"; do wait "$p" || true; done
python -m judge.analyze --run "$RUN" --label "$N tasks, $W workers${KILL_AFTER:+, kill -9 after ${KILL_AFTER}s}"
