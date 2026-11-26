"""큐·워커·집계가 같이 쓰는 Redis 키 이름과 설정."""
import os

import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
GROUP = "workers"
GLOBAL_STREAM = "pb:global:tasks"   # 예전 공용 스트림. 지금은 남은 메시지만 처리한다
# multi-run 모드에서 채점 중인 run 목록. 워커가 돌아가며 run마다 한 항목씩 가져간다(라운드 로빈).
ACTIVE_RUNS = "pb:global:runs"
MULTI_RUN_IDS = ("multi", "*", "auto")

# 이 시간(초) 동안 XACK되지 않은 메시지는 죽은 워커의 것으로 보고 다른 워커가 가져간다.
# 채점 1건의 최대 소요 시간보다 길어야 한다.
CLAIM_IDLE_SEC = float(os.environ.get("CLAIM_IDLE_SEC", "120"))
MAX_DELIVERIES = int(os.environ.get("MAX_DELIVERIES", "3"))  # 이 횟수를 넘으면 실패로 기록하고 끝낸다

RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "15"))
RATE_LIMIT_TPM = int(os.environ.get("RATE_LIMIT_TPM", "250000"))

# 완료된 run의 Redis 키를 이 시간(초) 뒤 자동 정리한다(기본 7일, 0 이하이면 유지).
RUN_TTL_SEC = int(os.environ.get("REDIS_RUN_TTL_SEC", str(7 * 24 * 3600)))
RUN_KEYS = ("tasks", "total", "meta", "results", "events", "retries", "failures", "aborted")


def connect() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def k(run: str, name: str) -> str:
    """run별 키: tasks, total, meta, results, events, retries, failures, aborted"""
    return f"pb:{run}:{name}"


def expire_run(r, run: str, ttl_sec: int = RUN_TTL_SEC) -> int:
    """완료·중단된 run의 Redis 키에 만료 시간을 설정한다."""
    if ttl_sec <= 0 or not run:
        return 0
    updated = 0
    for suffix in RUN_KEYS:
        if r.expire(k(run, suffix), ttl_sec):
            updated += 1
    return updated
