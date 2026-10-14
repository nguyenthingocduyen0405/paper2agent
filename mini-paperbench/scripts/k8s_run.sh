#!/usr/bin/env bash
# 클러스터(k3s)에서 실험을 실행한다. bootstrap.sh를 먼저 실행해야 한다.
#
# 사용(mini-paperbench 폴더에서):
#   scripts/k8s_run.sh judge <run> <작업 수> [작업 생성기 인자...]
#       예: scripts/k8s_run.sh judge k1 20
#       예: MOCK=1 scripts/k8s_run.sh judge m1 20            (LLM을 부르지 않는다)
#       예: scripts/k8s_run.sh judge s1 23 --paper-dir data/synthetic/clipsign \
#               --submission-dir data/synthetic/clipsign/submissions/sub_subtle
#   scripts/k8s_run.sh agent <run-id> [논문 폴더]          에이전트 P1~P5
#   scripts/k8s_run.sh scale <워커 수>                      워커 수 바꾸기
#   scripts/k8s_run.sh kill-worker                          워커 Pod 1개 삭제(장애 복구 확인)
#   scripts/k8s_run.sh status <run>                         진행 상황
set -euo pipefail
cd "$(dirname "$0")/.."

TAG=$(git rev-parse --short HEAD)
K="sudo k3s kubectl -n paperbench"

# Job 이름: 쿠버네티스 이름 규칙(소문자, '-', 63자 이하)에 맞춘다
job_name() { echo "$1-$2" | tr '[:upper:]_' '[:lower:]-' | cut -c1-63; }

# 인자 목록 → YAML의 args: ["a", "b"]
to_json_array() { python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"; }

run_job() {  # run_job <템플릿> <Job 이름> <인자...>
  local tpl=$1 name=$2; shift 2
  $K delete job "$name" --ignore-not-found >/dev/null
  sed -e "s/__NAME__/$name/" -e "s/__TAG__/$TAG/" -e "s|__ARGS__|$(to_json_array "$@")|" "k8s/jobs/$tpl.yaml" | $K apply -f -
  $K wait --for=condition=complete "job/$name" --timeout=3600s
  $K logs "job/$name" | tail -40
}

redis_cli() { $K exec deploy/redis -- redis-cli "$@"; }

progress() {  # progress <run> → "완료/전체"
  echo "$(redis_cli hlen "pb:$1:results")/$(redis_cli get "pb:$1:total")"
}

case "${1:-}" in
  judge)
    run=$2; n=$3; shift 3
    t0=$(date +%s)
    run_job task-generator "$(job_name gen "$run")" --run "$run" --limit "$n" "$@"
    # 워커에게 이 run을 맡긴다(Pod가 새로 뜬다). MOCK=1이면 LLM 대신 무작위 점수.
    $K set env deployment/judge-worker RUN_ID="$run" MOCK_JUDGE="${MOCK:-0}"
    $K rollout status deployment/judge-worker --timeout=300s
    total=$(redis_cli get "pb:$run:total")
    until [ "$(redis_cli hlen "pb:$run:results")" -ge "$total" ]; do
      echo "progress $(progress "$run")"; sleep 10
    done
    run_job analyze "$(job_name analyze "$run")" --run "$run" --label "k8s, $n tasks, ${MOCK:+mock, }$($K get deploy judge-worker -o jsonpath='{.spec.replicas}') workers"
    echo "elapsed (incl. pod start) $(( $(date +%s) - t0 ))s. result: /data/results/$run.json"
    ;;
  agent)
    id=$2; paper=${3:-data/papers/semantic-self-consistency}
    run_job agent-run "$(job_name agent "$id")" --run-id "$id" --paper-dir "$paper"
    echo "submission: /data/agent_runs/$id/submission"
    ;;
  scale)
    $K scale deployment/judge-worker --replicas="$2"
    $K rollout status deployment/judge-worker --timeout=300s
    ;;
  kill-worker)
    pod=$($K get pods -l app=judge-worker -o jsonpath='{.items[0].metadata.name}')
    echo "deleting $pod"
    $K delete pod "$pod" --wait=false
    ;;
  status)
    echo "progress $(progress "$2")"
    $K get pods
    ;;
  *)
    sed -n '2,15p' "$0"; exit 1 ;;
esac
