# judge 이미지: 워커, 작업 생성기, 집계가 같이 쓴다. 실행 명령(command)만 다르다.
# 빌드(mini-paperbench 폴더에서): docker build -f docker/judge-worker.Dockerfile -t paperbench/judge-worker:<태그> .
FROM python:3.11.9-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CUDA_VISIBLE_DEVICES=""
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY judge/ judge/
# data/ 와 results/ 는 hostPath(/data)로 붙인다
CMD ["python", "-m", "judge.worker"]
