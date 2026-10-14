"""큐·워커·집계가 같이 쓰는 Redis 키 이름과 설정."""
import os

import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
GROUP = "workers"

# 이 시간(초) 동안 XACK되지 않은 메시지는 죽은 워커의 것으로 보고 다른 워커가 가져간다.
# 채점 1건의 최대 소요 시간보다 길어야 한다.
CLAIM_IDLE_SEC = float(os.environ.get("CLAIM_IDLE_SEC", "120"))
MAX_DELIVERIES = int(os.environ.get("MAX_DELIVERIES", "3"))  # 이 횟수를 넘으면 실패로 기록하고 끝낸다

RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "15"))
RATE_LIMIT_TPM = int(os.environ.get("RATE_LIMIT_TPM", "250000"))


def connect() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def k(run: str, name: str) -> str:
    """run별 키: stream, total, meta, results, events, retries"""
    return f"pb:{run}:{name}"
