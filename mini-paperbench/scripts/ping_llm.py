"""LLM 연결 확인: .env의 설정으로 "say OK"를 1회 호출하고 응답과 토큰 사용량을 출력한다."""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

base_url = os.environ["LLM_BASE_URL"]
model = os.environ["LLM_MODEL"]
client = OpenAI(base_url=base_url, api_key=os.environ["LLM_API_KEY"])

resp = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "say OK"}],
)
u = resp.usage
print(f"base_url: {base_url}")
print(f"model:    {resp.model}")
print(f"response: {resp.choices[0].message.content!r}")
print(f"tokens:   input={u.prompt_tokens} output={u.completion_tokens} total={u.total_tokens}")
