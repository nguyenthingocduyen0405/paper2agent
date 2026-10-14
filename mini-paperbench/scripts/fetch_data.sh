#!/usr/bin/env bash
# 논문·루브릭·JudgeEval 제출물을 data/에 준비한다. 여러 번 실행해도 안전하다.
set -euo pipefail
cd "$(dirname "$0")/.."

PAPER=semantic-self-consistency
REPO=third_party/frontier-evals
SRC=$REPO/project/paperbench/data

if [ ! -d "$REPO" ]; then
  mkdir -p third_party
  GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 https://github.com/openai/frontier-evals.git "$REPO"
fi

# 저장소의 .lfsconfig가 data/ 전체를 fetchexclude로 막고 있어서 덮어쓴다.
git -C "$REPO" lfs install --local >/dev/null
git -C "$REPO" -c lfs.fetchexclude= lfs pull \
  -I "project/paperbench/data/papers/$PAPER/**,project/paperbench/data/judge_eval/$PAPER/**"

mkdir -p data/papers data/judge_eval/$PAPER/0
cp -r "$SRC/papers/$PAPER" data/papers/
cp "$SRC/judge_eval/$PAPER/0/grading/expected_result.json" data/judge_eval/$PAPER/0/
if [ ! -d data/judge_eval/$PAPER/0/submission ]; then
  tar xf "$SRC/judge_eval/$PAPER/0/submission.tar" -C data/judge_eval/$PAPER/0 2>/dev/null
fi
echo "done:"; ls data/papers/$PAPER data/judge_eval/$PAPER/0
