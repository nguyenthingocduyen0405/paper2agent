#!/usr/bin/env bash
# Slack 도우미(Hermes Agent)를 띄우거나 설정을 다시 읽힌다. bootstrap.sh를 먼저 실행해 둔 상태여야 한다.
# 사용(mini-paperbench 폴더에서): scripts/hermes_up.sh [Slack 토큰 파일]
#
# 토큰 파일(처음 한 번만, 기본 ~/slack-hermes.env):
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
#   SLACK_ALLOWED_USERS=U0...,U0...   (대화할 수 있는 Slack member ID. 비우면 아무도 못 쓴다)
# 파일 내용은 Secret hermes-slack에 넣고 파일은 지운다. 이후에는 인자 없이 실행해도 된다.
set -euo pipefail
cd "$(dirname "$0")/.."

K="sudo k3s kubectl -n paperbench"
ENV_FILE=${1:-$HOME/slack-hermes.env}

if [ -f "$ENV_FILE" ]; then
  # Windows 메모장으로 만든 파일: BOM과 CR을 지운다
  CLEAN=$(sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$ENV_FILE" | grep -E '^(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_ALLOWED_USERS)=' || true)
  for k in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS; do
    echo "$CLEAN" | grep -q "^$k=.\+" || { echo "ERROR: $k is empty in $ENV_FILE"; exit 1; }
  done
  echo "$CLEAN" | grep -q '^SLACK_BOT_TOKEN=xoxb-' || { echo "ERROR: SLACK_BOT_TOKEN must start with xoxb-"; exit 1; }
  echo "$CLEAN" | grep -q '^SLACK_APP_TOKEN=xapp-' || { echo "ERROR: SLACK_APP_TOKEN must start with xapp-"; exit 1; }
  echo "$CLEAN" | $K create secret generic hermes-slack --from-env-file=/dev/stdin --dry-run=client -o yaml | $K apply -f -
  shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"
  echo "Secret hermes-slack updated; $ENV_FILE removed"
fi
$K get secret hermes-slack >/dev/null 2>&1 || { echo "ERROR: no Secret hermes-slack. Pass the Slack token file."; exit 1; }

$K create configmap hermes-files --from-file=hermes/ --dry-run=client -o yaml | $K apply -f -
sudo k3s kubectl apply -f k8s/50-hermes.yaml
$K rollout restart deployment/hermes
$K rollout status deployment/hermes --timeout=600s
$K get pods -l app=hermes
echo "logs: $K logs deploy/hermes -f"
