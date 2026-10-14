#!/usr/bin/env bash
# 빈 Ubuntu VM에 k3s를 설치하고 paperbench를 배포한다.
# 여러 번 실행해도 안전하다. 이미 된 단계는 건너뛴다.
#
# 준비: 이 저장소를 VM에 clone하고, mini-paperbench/.env에 LLM_API_KEY를 넣는다.
# 사용(mini-paperbench 폴더에서): scripts/bootstrap.sh
#
# 단계: 1 스왑 → 2 k3s → 3 이미지 빌드 → 4 /data 준비 → 5 Secret → 6 kubectl apply
set -euo pipefail
cd "$(dirname "$0")/.."

TAG=$(git rev-parse --short HEAD)   # 이미지 태그 = 커밋 해시 (latest를 쓰지 않는다)
KUBECTL="sudo k3s kubectl"
step() { echo; echo "=== $* ==="; }

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
for img in judge-worker agent; do
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

# 6. 상시 실행되는 것들: ConfigMap, Redis, judge 워커
step "6/6 apply"
for f in k8s/10-configmap.yaml k8s/20-redis.yaml k8s/30-judge-worker.yaml; do
  sed "s/__TAG__/$TAG/g" "$f" | $KUBECTL apply -f -
done
$KUBECTL -n paperbench rollout status deployment/redis --timeout=120s
$KUBECTL -n paperbench rollout status deployment/judge-worker --timeout=120s
$KUBECTL -n paperbench get pods
echo
echo "done. next: scripts/k8s_run.sh judge <run> <작업 수>   (자세한 사용법은 k8s/README.md)"
