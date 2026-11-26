# 웹 이미지: 논문 접수, 작업 진행(Job 생성), 결과 화면.
# 빌드(mini-paperbench 폴더에서): docker build -f docker/web.Dockerfile -t paperbench/web:<태그> .
FROM python:3.11.9-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
COPY web/requirements.txt web-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r web-requirements.txt
# judge/: 루브릭 읽기와 Redis 키 이름, k8s/jobs/: Job 템플릿
COPY judge/ judge/
COPY k8s/jobs/ k8s/jobs/
COPY web/ web/
# data/ 와 results/ 는 hostPath(/data)로 붙인다
EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
