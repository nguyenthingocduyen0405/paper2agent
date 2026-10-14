# CLAUDE.md

작업 전 PLAN.md의 "에이전트 작업 규칙"을 따른다.

PaperBench(arXiv 2504.01848)를 작게 재현하는 수업 프로젝트다.
judge는 제출물을 루브릭 리프마다 0/1로 채점하며, Redis Streams 큐와 워커 여러 개로 병렬 처리한다.
에이전트는 논문(semantic-self-consistency)을 읽고 재현 코드를 작성만 한다(Code-Dev, GPU 불필요).
LLM은 openai 라이브러리 + .env의 LLM_BASE_URL/LLM_MODEL/LLM_API_KEY로만 호출하며, 최종적으로 k3s에 배포한다.
