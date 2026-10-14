"""에이전트용 LLM 호출: 호출 수 상한, 기록(calls.jsonl), 재시도."""
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

MAX_CALLS = 50  # run 하나의 호출 수 상한 (재시작해도 누적)


class CallLimitExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, run_dir: Path):
        self.client = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])
        self.model = os.environ["LLM_MODEL"]
        self.log_path = run_dir / "calls.jsonl"

    def calls_so_far(self) -> int:
        return sum(1 for _ in self.log_path.open()) if self.log_path.exists() else 0

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)),
        wait=wait_exponential(multiplier=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _create(self, **kwargs):
        return self.client.chat.completions.create(model=self.model, **kwargs)

    def chat(self, step: str, messages: list[dict], **kwargs) -> str:
        if self.calls_so_far() >= MAX_CALLS:
            raise CallLimitExceeded(f"call limit {MAX_CALLS} reached")
        t = time.time()
        resp = self._create(messages=messages, **kwargs)
        u = resp.usage
        details = getattr(u, "prompt_tokens_details", None)
        record = {
            "step": step,
            "input_tokens": u.prompt_tokens,
            "output_tokens": u.completion_tokens,
            "cached_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
            "sec": round(time.time() - t, 2),
            "finish_reason": resp.choices[0].finish_reason,
        }
        with self.log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return resp.choices[0].message.content or ""
