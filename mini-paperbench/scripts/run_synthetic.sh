#!/usr/bin/env bash
# 합성 논문 × 제출물 5개를 실제 judge로 채점하고 정답 라벨과 비교한다.
# 사용: scripts/run_synthetic.sh [이름 접두사]   (기본 syn)
#   PAPERS="clipsign procrastigd" 처럼 논문을 고를 수 있다. 없으면 data/synthetic/의 전부.
# 모델·제한값은 환경 변수로 넘긴다(LLM_MODEL, RATE_LIMIT_RPM 등). Redis는 미리 켜 둔다.
set -euo pipefail
cd "$(dirname "$0")/.."
P=${1:-syn}
# 결과 파일 이름에 쓸 모델 이름: 환경 변수가 없으면 .env에서 읽는다
MODEL=${LLM_MODEL:-$(grep -s '^LLM_MODEL=' .env | cut -d= -f2)}
MODEL=${MODEL//\//_}   # 'Qwen/Qwen2.5-7B-Instruct' → 'Qwen_Qwen2.5-7B-Instruct' (파일 이름에 / 를 쓸 수 없다)
pairs=(); checks=()
PAPERS=${PAPERS:-$(ls data/synthetic)}
for paper in $PAPERS; do
  for sub in sub_correct sub_subtle sub_partial sub_wrong_method sub_empty; do
    run=${P}_${paper}_${sub}
    n=$(python -c "import sys; sys.path.insert(0,'.'); from judge.rubric import load_leaves; print(len(load_leaves('data/synthetic/$paper/rubric.json')))")
    GEN_ARGS="--paper-dir data/synthetic/$paper --submission-dir data/synthetic/$paper/submissions/$sub" \
      scripts/run_local.sh "$run" "$n" "${WORKERS:-2}" > "logs/$run.out" 2>&1
    echo "$run: $(grep -o '"completed": [0-9]*' "logs/$run.out")"
    label=data/synthetic_labels/$paper/$sub.json
    # 빈 제출물은 건전성 검사로만 본다(README 권장)
    if [ "$sub" = sub_empty ]; then checks+=(--check "$run=$label"); else pairs+=(--pair "$run=$label"); fi
  done
done
python -m judge.judgeeval --name "${P}_${MODEL:-model}" "${pairs[@]}" "${checks[@]}"
