#!/usr/bin/env bash
# 로컬 Redis(포트 6380) 켜기/끄기. 디스크에 저장하지 않는다.
set -euo pipefail
PORT=${REDIS_PORT:-6380}
case "${1:-}" in
  start) redis-cli -p "$PORT" ping >/dev/null 2>&1 && echo "already running" \
           || { redis-server --port "$PORT" --daemonize yes --save "" --appendonly no --logfile "" >/dev/null; sleep 0.5; redis-cli -p "$PORT" ping; } ;;
  stop)  redis-cli -p "$PORT" shutdown nosave 2>/dev/null || true; echo "stopped" ;;
  *) echo "usage: $0 start|stop"; exit 1 ;;
esac
