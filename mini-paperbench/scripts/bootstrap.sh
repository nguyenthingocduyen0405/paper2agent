#!/usr/bin/env bash
# 빈 Ubuntu VM에 k3s를 설치하고 paperbench를 배포한다.
# 여러 번 실행해도 안전하다. 이미 된 단계는 건너뛴다.
#
# 준비: 이 저장소를 VM에 clone하고, mini-paperbench/.env에 LLM_API_KEY를 넣는다.
#       LLM을 Claude 구독(claude-bridge)으로 쓰면 CLAUDE_CODE_OAUTH_TOKEN도 넣는다.
#       웹 로그인과 DB: DATABASE_URL(RDS, scripts/aws_setup.sh가 넣는다), ADMIN_USERNAME, ADMIN_PASSWORD, SERVICE_TOKEN.
#       S3: S3_BUCKET. 이 값들이 없으면 웹은 로컬 SQLite와 디스크만 쓴다(로그인할 관리자도 없다).
#       GPU 실행: JPUSHARE_API_KEY(JPUShare에서 만든 jpk_ 키). 없으면 GPU 실행을 묻지도, 버튼을 보이지도 않는다.
#       고정 공개 주소: PUBLIC_URL. http://로 시작하면(학교 서버 http://<NAT 주소>:<포트>) VM의 80번을 웹으로 연다.
#       https://로 시작하면(AWS Elastic IP, https://<IP>.sslip.io) Caddy가 80/443번을 받고 인증서를 저절로 받는다.
# 사용(mini-paperbench 폴더에서): scripts/bootstrap.sh
#
# 단계: 1 스왑 → 2 k3s → 3 이미지 빌드 → 4 /data 준비 → 5 Secret → 6 kubectl apply
set -euo pipefail
cd "$(dirname "$0")/.."

TAG=$(git rev-parse --short HEAD)   # 이미지 태그 = 커밋 해시 (latest를 쓰지 않는다)
KUBECTL="sudo k3s kubectl"
step() { echo; echo "=== $* ==="; }
# ConfigMap의 LLM_BASE_URL이 claude-bridge를 가리키면 브리지도 띄운다
USE_BRIDGE=0
grep -q '^ *LLM_BASE_URL: *"http://claude-bridge' k8s/10-configmap.yaml && USE_BRIDGE=1

# 1. 스왑: 메모리가 작은 VM에서 빌드·실행이 죽지 않게 2GB를 만든다
step "1/6 swap"
if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
else
  echo "skip: swap already on"
fi

# 2. k3s (traefik은 쓰지 않는다)
step "2/6 k3s"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik" sh -
else
  echo "skip: k3s installed"
fi
until $KUBECTL get nodes 2>/dev/null | grep -q " Ready"; do echo "waiting for node..."; sleep 5; done

# 3. 이미지: docker로 빌드해서 k3s(containerd)에 직접 넣는다. 레지스트리가 필요 없다.
step "3/6 images (tag $TAG)"
if ! command -v docker >/dev/null; then
  sudo apt-get update && sudo apt-get install -y docker.io
fi
IMAGES="judge-worker agent web"
[ "$USE_BRIDGE" = 1 ] && IMAGES="$IMAGES claude-bridge"
for img in $IMAGES; do
  if sudo k3s ctr images ls -q | grep -q "paperbench/$img:$TAG"; then
    echo "skip: paperbench/$img:$TAG already in k3s"
    continue
  fi
  sudo docker build -f "docker/$img.Dockerfile" -t "paperbench/$img:$TAG" .
  sudo docker save "paperbench/$img:$TAG" | sudo k3s ctr images import -
done

# 4. /data: 논문·JudgeEval·합성 논문을 받아서 hostPath(/data)에 둔다
step "4/6 /data"
if [ ! -f /data/papers/semantic-self-consistency/paper.md ] || [ ! -d /data/synthetic ]; then
  sudo apt-get install -y git-lfs python3 rsync
  scripts/fetch_data.sh
  python3 scripts/import_synthetic.py datasets/synthetic_*.zip
  sudo mkdir -p /data/results
  sudo rsync -a data/ /data/
else
  echo "skip: /data ready"
fi
sudo mkdir -p /data/results /data/agent_runs

# 5. Secret: .env에서 LLM_API_KEY 한 줄만 가져온다. 파일로 저장하지 않는다.
step "5/6 secret llm-keys"
$KUBECTL apply -f k8s/00-namespace.yaml
if [ ! -f .env ] || ! grep -q '^LLM_API_KEY=.\+' .env; then
  echo "ERROR: put LLM_API_KEY=... in mini-paperbench/.env first"; exit 1
fi
grep '^LLM_API_KEY=' .env | $KUBECTL -n paperbench create secret generic llm-keys \
  --from-env-file=/dev/stdin --dry-run=client -o yaml | $KUBECTL apply -f -
if [ "$USE_BRIDGE" = 1 ]; then
  if ! grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.\+' .env; then
    echo "ERROR: put CLAUDE_CODE_OAUTH_TOKEN=... (claude setup-token) in mini-paperbench/.env first"; exit 1
  fi
  grep '^CLAUDE_CODE_OAUTH_TOKEN=' .env | $KUBECTL -n paperbench create secret generic claude-token \
    --from-env-file=/dev/stdin --dry-run=client -o yaml | $KUBECTL apply -f -
fi
# 웹: DB 주소, 첫 관리자, 서비스 토큰. 값은 Secret에만 두고 화면에 찍지 않는다
WEB_KEYS='^(DATABASE_URL|ADMIN_USERNAME|ADMIN_PASSWORD|SERVICE_TOKEN)=.'
if grep -Eq "$WEB_KEYS" .env; then
  grep -E "$WEB_KEYS" .env | $KUBECTL -n paperbench create secret generic web-secrets \
    --from-env-file=/dev/stdin --dry-run=client -o yaml | $KUBECTL apply -f -
fi
S3_BUCKET=$(grep -E '^S3_BUCKET=' .env | head -1 | cut -d= -f2- | tr -d '\r"' || true)
PUBLIC_URL=$(grep -E '^PUBLIC_URL=' .env | head -1 | cut -d= -f2- | tr -d '\r"' || true)
PUBLIC_HOST=""   # https 주소일 때만: Caddy가 인증서를 받을 이름
case "$PUBLIC_URL" in https://*) PUBLIC_HOST=${PUBLIC_URL#https://}; PUBLIC_HOST=${PUBLIC_HOST%%/*} ;; esac
GPU_RUN=0
grep -q '^JPUSHARE_API_KEY=.\+' .env && GPU_RUN=1
# GPU 실행(JPUShare): API 키는 gpu-run Job에만 들어간다(k8s/jobs/gpu-run.yaml)
if grep -q '^JPUSHARE_API_KEY=.\+' .env; then
  grep '^JPUSHARE_API_KEY=' .env | $KUBECTL -n paperbench create secret generic jpushare \
    --from-env-file=/dev/stdin --dry-run=client -o yaml | $KUBECTL apply -f -
fi

# 6. 상시 실행되는 것들: ConfigMap, Redis, (claude-bridge), judge 워커, 웹
step "6/6 apply"
MANIFESTS="k8s/10-configmap.yaml k8s/20-redis.yaml"
[ "$USE_BRIDGE" = 1 ] && MANIFESTS="$MANIFESTS k8s/25-claude-bridge.yaml"
# 임시 공개 주소(Cloudflare quick tunnel)는 .env에 PUBLIC_TUNNEL=1이 있을 때만 연다
grep -q '^PUBLIC_TUNNEL=1' .env 2>/dev/null && MANIFESTS="$MANIFESTS k8s/55-cloudflared.yaml"
# 고정 공개 주소: https면 Caddy(80/443), http면 80번만. 둘 다 VM의 80번을 쓰므로 안 쓰는 쪽은 지운다
if [ -n "$PUBLIC_HOST" ]; then
  MANIFESTS="$MANIFESTS k8s/57-caddy.yaml"
  $KUBECTL -n paperbench delete service web-public --ignore-not-found
else
  [ -n "$PUBLIC_URL" ] && MANIFESTS="$MANIFESTS k8s/56-web-public.yaml"
  $KUBECTL -n paperbench delete deployment/caddy service/caddy configmap/caddy --ignore-not-found
fi
for f in $MANIFESTS k8s/30-judge-worker.yaml k8s/40-web.yaml k8s/45-network.yaml; do
  sed -e "s/__TAG__/$TAG/g" -e "s/__S3_BUCKET__/${S3_BUCKET:-}/g" -e "s|__PUBLIC_URL__|${PUBLIC_URL:-}|g" -e "s|__PUBLIC_HOST__|${PUBLIC_HOST}|g" \
      -e "s/__GPU_RUN__/$GPU_RUN/g" "$f" | $KUBECTL apply -f -
done
$KUBECTL -n paperbench rollout restart deployment/web   # Secret web-secrets가 바뀌었을 수 있다
if [ "$USE_BRIDGE" = 1 ]; then
  # Secret이 바뀌어도 Pod는 그대로라서, 다시 띄워 새 토큰을 읽게 한다
  $KUBECTL -n paperbench rollout restart deployment/claude-bridge
  $KUBECTL -n paperbench rollout status deployment/claude-bridge --timeout=180s
fi
$KUBECTL -n paperbench rollout status deployment/redis --timeout=120s
$KUBECTL -n paperbench rollout status deployment/judge-worker --timeout=120s
$KUBECTL -n paperbench rollout status deployment/web --timeout=120s
[ -n "$PUBLIC_HOST" ] && { $KUBECTL -n paperbench rollout status deployment/caddy --timeout=120s || true; }
$KUBECTL -n paperbench get pods
echo
if grep -q '^PUBLIC_TUNNEL=1' .env 2>/dev/null; then
  $KUBECTL -n paperbench rollout status deployment/cloudflared --timeout=120s || true
  URL=""
  for _ in $(seq 1 20); do
    URL=$($KUBECTL -n paperbench logs deployment/cloudflared 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)
    [ -n "$URL" ] && break
    sleep 3
  done
  echo "외부 공개 주소(이번 세션 동안): ${URL:-아직 없음. 웹의 /admin/system에서 확인}"
fi
[ -n "$PUBLIC_URL" ] && echo "고정 공개 주소: $PUBLIC_URL"
echo "done. next: scripts/k8s_run.sh judge <run> <작업 수>   (자세한 사용법은 k8s/README.md)"
echo "웹 화면: 내 컴퓨터에서 ssh -i <키> -L 8080:localhost:30080 ubuntu@<이 VM 주소> 를 실행하고 http://localhost:8080 을 연다"
