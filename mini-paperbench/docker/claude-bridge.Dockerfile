# claude-bridge 이미지: OpenAI 호환 /v1/chat/completions → Claude 구독(Claude Agent SDK, CLAUDE_CODE_OAUTH_TOKEN).
# 빌드(mini-paperbench 폴더에서): docker build -f docker/claude-bridge.Dockerfile -t paperbench/claude-bridge:<태그> .
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY claude-bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY claude-bridge/app.py .
# SDK에 들어 있는 Claude Code CLI는 HOME/.claude에 쓸 수 있어야 한다
RUN useradd --create-home --uid 10003 bridge
USER bridge
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
