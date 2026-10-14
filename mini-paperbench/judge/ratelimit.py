"""Redis 기반 공용 rate limiter: 모든 워커가 같은 분 단위 카운터를 나눠 쓴다(고정 윈도우)."""
import time

import redis

# 요청 수와 토큰 수를 원자적으로 확인·증가한다. 한도를 넘으면 증가하지 않고 0을 돌려준다.
_ACQUIRE = """
local req = tonumber(redis.call('GET', KEYS[1]) or '0')
local tok = tonumber(redis.call('GET', KEYS[2]) or '0')
if req + 1 > tonumber(ARGV[1]) or tok + tonumber(ARGV[3]) > tonumber(ARGV[2]) then
  return 0
end
redis.call('INCR', KEYS[1]); redis.call('EXPIRE', KEYS[1], 120)
redis.call('INCRBY', KEYS[2], ARGV[3]); redis.call('EXPIRE', KEYS[2], 120)
return 1
"""


class RateLimiter:
    def __init__(self, r: redis.Redis, rpm: int, tpm: int):
        self.r, self.rpm, self.tpm = r, rpm, tpm
        self._acquire = r.register_script(_ACQUIRE)

    def acquire(self, tokens: int) -> float:
        """한도 안에서 요청 1건(+토큰)을 예약한다. 기다린 시간(초)을 돌려준다."""
        tokens = min(tokens, self.tpm)  # 한 건이 분당 한도보다 크면 영원히 못 받으므로 한도로 자른다
        waited = 0.0
        while True:
            minute = int(time.time() // 60)
            keys = [f"pb:rl:req:{minute}", f"pb:rl:tok:{minute}"]
            if self._acquire(keys=keys, args=[self.rpm, self.tpm, tokens]):
                return waited
            sleep = 60 - time.time() % 60 + 0.05  # 다음 분까지
            time.sleep(sleep)
            waited += sleep
