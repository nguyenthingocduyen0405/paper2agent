# CLAUDE.md

작업 전에 PLAN.md의 "에이전트 작업 규칙"을 읽고 따른다.

## 프로젝트 요약

- PaperBench(arXiv 2504.01848)를 작게 따라 만드는 수업 프로젝트다.
- judge는 제출물을 채점한다. 루브릭의 리프마다 0 또는 1을 준다.
- judge는 Redis Streams 큐와 워커 여러 개로 병렬 처리한다.
- 에이전트는 논문(semantic-self-consistency)을 읽고 재현 코드를 쓴다. 코드를 실행하지는 않는다.
- LLM은 `openai` 라이브러리로만 부른다. 설정은 `.env`의 `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`에서 읽는다.
- 마지막에는 k3s에 배포한다.
