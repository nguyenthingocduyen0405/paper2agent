#!/usr/bin/env bash
# VM의 /data/results를 로컬 results/로 복사한다.
# 사용(mini-paperbench 폴더에서): scripts/collect.sh <사용자@VM주소>
#   VM 안에서 실행할 때는 주소 없이: scripts/collect.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p results
if [ -n "${1:-}" ]; then
  rsync -av "$1:/data/results/" results/
else
  rsync -av /data/results/ results/
fi
