# 에이전트 이미지: 논문을 읽고 제출물을 만든다(P1~P5).
# 빌드(mini-paperbench 폴더에서): docker build -f docker/agent.Dockerfile -t paperbench/agent:<태그> .
FROM python:3.11.9-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CUDA_VISIBLE_DEVICES=""
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ agent/
CMD ["python", "-m", "agent.pipeline", "--help"]
