#!/usr/bin/env bash
# 로컬 GPU에서 작은 언어 모델을 OpenAI 호환 API로 띄운다(vLLM). judge 코드는 바꾸지 않는다.
#   judge 쪽 설정: LLM_BASE_URL=http://127.0.0.1:8011/v1  LLM_MODEL=<모델 이름>  LLM_API_KEY=local
#
# 사용(mini-paperbench 폴더에서):
#   GPU=2 scripts/vllm_server.sh start      서버 켜기 (준비될 때까지 기다린다)
#   scripts/vllm_server.sh stop             서버 끄기
#   scripts/vllm_server.sh status
# 바꿀 수 있는 값: MODEL(기본 Qwen/Qwen2.5-7B-Instruct), GPU(필수, 비어 있는 GPU 번호), PORT(기본 8011),
#   MAX_LEN(기본 16384), EXTRA_ARGS(vLLM에 더 줄 인자)
#   JudgeEval처럼 32k 토큰이 넘는 논문은 YaRN으로 늘린다:
#   MAX_LEN=49152 EXTRA_ARGS='--rope-scaling {"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}'
#
# 공용 서버이므로 실험이 끝나면 반드시 stop 한다.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_BIN=${ENV_BIN:-$HOME/miniconda3/envs/pbgpu/bin}
MODEL=${MODEL:-Qwen/Qwen2.5-7B-Instruct}
PORT=${PORT:-8011}
MAX_LEN=${MAX_LEN:-16384}
PIDFILE=logs/vllm_$PORT.pid
export HF_HOME=${HF_HOME:-$PWD/data/hf}   # 모델 파일은 프로젝트 안에 둔다(공용 캐시를 쓰지 않는다)
mkdir -p logs "$HF_HOME"

case "${1:-}" in
  start)
    : "${GPU:?set GPU=<free GPU index>, e.g. GPU=2}"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    # shellcheck disable=SC2086  # EXTRA_ARGS는 일부러 단어로 나눈다
    CUDA_VISIBLE_DEVICES=$GPU nohup "$ENV_BIN/vllm" serve "$MODEL" \
      --host 127.0.0.1 --port "$PORT" \
      --max-model-len "$MAX_LEN" --gpu-memory-utilization 0.90 \
      --api-key local ${EXTRA_ARGS:-} > "logs/vllm_$PORT.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "starting $MODEL on GPU $GPU, port $PORT (pid $!) ..."
    until curl -sf -H "Authorization: Bearer local" "http://127.0.0.1:$PORT/v1/models" >/dev/null; do
      kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { echo "server died; see logs/vllm_$PORT.log"; tail -20 "logs/vllm_$PORT.log"; exit 1; }
      sleep 5
    done
    echo "ready: http://127.0.0.1:$PORT/v1"
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
    fi
    echo "stopped"
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running"; fi
    ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
