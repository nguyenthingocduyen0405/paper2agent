# 미니 PaperBench 구현 계획

> 이 문서는 작업 지시서다. Claude Code 에이전트가 읽고 단계별로 진행한다.
> 사용법: "PLAN.md를 읽고 0단계를 진행해줘"처럼 **한 단계씩** 시킨다.
> 모든 경로는 이 폴더(`mini-paperbench/`)를 기준으로 한다.

---

## 진행 상황

| 단계 | 상태 | 메모 |
| --- | --- | --- |
| 0. 서버 확인 | 완료 | sudo 불가, docker 불가, git-lfs 없음(1단계에서 설치) |
| 1. 환경 | 완료 | 모델을 `gemini-3.5-flash-lite`로 바꿈(2.5는 신규 사용자에게 종료됨) |
| 2. judge | 완료 | 경로 B(간이 judge). JSON 스키마 출력으로 형식 오류 0% |
| 3. 큐와 워커 | 완료 | MOCK 테스트 3개 통과. 결과는 `results/t*.json` |
| 4. JudgeEval F1 | 완료 | A안(리프 77개). F1 0.986, macro-F1 0.893. 더 제대로 하는 계획은 `FUTURE_PLAN.md` |
| 5~7 | 대기 | |

---

## 에이전트 작업 규칙 (항상 지킨다)

1. **한 번에 한 단계만** 한다.
   - 단계가 끝나면 "보고 형식"대로 보고하고 멈춘다.
   - 다음 단계는 사용자가 시킬 때만 시작한다.
2. **완료 기준은 직접 실행해서 확인한다.**
   - 실행하지 않고 "될 것이다"라고 보고하지 않는다.
3. 이 서버는 **여러 사람이 같이 쓰는 GPU 서버**다.
   - GPU를 쓰지 않는다. 모든 실행에 `CUDA_VISIBLE_DEVICES=""`를 붙인다.
   - `sudo`를 쓰지 않는다. 시스템 패키지를 설치하지 않는다.
   - 필요한 것은 conda 환경 안에 설치한다.
   - 계속 떠 있는 프로세스(Redis 등)는 작업이 끝나면 끈다.
   - 기본 포트 대신 다른 포트를 쓴다. 예: Redis는 6380.
4. **비밀 키**는 `.env`에서만 읽는다.
   - 키를 코드, 로그, 출력, 커밋에 넣지 않는다.
   - `.env`는 `.gitignore`에 있어야 한다.
5. **유료 API를 부르기 전에는 멈추고 물어본다.**
   - 예상 호출 수와 예상 토큰 수를 같이 보고한다.
   - 무료 등급과 MOCK 모드는 묻지 않아도 된다.
6. 계획과 다르게 해야 하면 먼저 이유를 보고하고 승인을 받는다.
   - 예: 라이브러리 변경, 구조 변경.
7. 수업 프로젝트다. **작게 만든다. 기본 기능을 먼저 만든다.**
   - 요청하지 않은 기능은 추가하지 않는다.

### 보고 형식

```
## N단계 보고
- 한 일:
- 확인 방법과 결과: (실행한 명령, 핵심 출력)
- 만든/바꾼 파일:
- 문제·결정 필요 사항:
- 측정값: (해당하면 호출 수, 토큰 수, 시간)
```

---

## 프로젝트 요약

PaperBench(arXiv 2504.01848)의 흐름을 작게 따라 만든다.

- **judge**: 제출물(코드 폴더)을 채점한다.
  - 루브릭의 리프(가장 아래 항목)마다 0 또는 1을 준다.
  - Redis 큐와 워커 여러 개로 병렬 처리한다.
- **에이전트**: LLM이 논문을 읽고 재현 코드를 쓴다.
  - 코드를 실행하지는 않는다(Code-Dev 방식). GPU가 필요 없다.
- 대상 논문: `semantic-self-consistency` 1편.
- 제출물 형식(PaperBench와 같음): 루트에 `reproduce.sh`, `README.md`, `src/`, `requirements.txt`.
- 마지막에는 쿠버네티스(k3s/k3d)에 배포한다.
  - 이 서버에서는 먼저 쿠버네티스 없이 Python 프로세스로 완성한다.

### LLM 설정

- 모든 LLM 호출은 `openai` 라이브러리 하나로 한다.
- 아래 환경 변수만 읽는다.
- 제공자를 바꿀 때는 `.env`만 고친다. 코드는 고치지 않는다.

| 변수 | 1순위: Gemini | 2순위: OpenAI |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `https://api.openai.com/v1` |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | `gpt-5-nano` |
| `LLM_API_KEY` | Gemini 키 | OpenAI 키 |

- 모델 이름과 주소는 실행 전에 1번 실제로 호출해서 확인한다. 모델이 종료됐을 수 있다.
- `MOCK_JUDGE=1`이면 LLM을 부르지 않는다.
  - `MOCK_DELAY_SEC`초(기본 3초) 기다린 뒤 0 또는 1을 무작위로 돌려준다.

### 폴더 구조

```
mini-paperbench/
├── PLAN.md           # 이 문서
├── FUTURE_PLAN.md    # 나중에 할 일 메모
├── CLAUDE.md         # 에이전트용 짧은 안내
├── .env.example      # 키 이름만 있다. 값은 없다
├── judge/            # judge 함수, 워커, 작업 생성기, 집계
├── agent/            # 에이전트 P1~P5 (5단계)
├── k8s/              # 쿠버네티스 YAML (6단계)
├── scripts/          # 설치, 실행, 수집 스크립트
├── data/             # 논문, 루브릭, 제출물. git에 올리지 않는다. scripts/fetch_data.sh로 받는다
└── results/          # 실험 결과 JSON. git에 커밋한다
```

---

## 0단계: 서버 환경 확인

**설치하지 않는다. 확인만 한다.**

할 일: 아래를 실행하고 결과를 표로 보고한다.

```bash
sudo -n true 2>/dev/null && echo "sudo: 가능" || echo "sudo: 불가"
docker ps >/dev/null 2>&1 && echo "docker: 가능" || echo "docker: 불가"
which conda mamba git git-lfs python3
curl -sI https://generativelanguage.googleapis.com | head -1
curl -sI https://api.openai.com | head -1
df -h ~ ; free -h ; nproc
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv   # 사용 현황만 본다
```

완료 기준: 아래가 모두 보고된다.
- sudo, docker, git-lfs가 되는지
- 두 API에 접속되는지
- 디스크와 메모리가 얼마나 남았는지

---

## 1단계: 저장소와 환경

할 일:
1. conda 환경 `pb`를 만든다(Python 3.11).
   - 설치할 것: `openai`, `pydantic`, `tenacity`, `redis`(Python 클라이언트), `python-dotenv`, `pytest`.
2. conda-forge에서 `redis`(서버)를 설치한다. 0단계에서 `git-lfs`가 없었으면 같이 설치한다.
3. `.gitignore`, `.env.example`, `CLAUDE.md`를 만든다.
   - `.gitignore`에는 `.env`, `data/`, `__pycache__` 등을 넣는다.
   - `CLAUDE.md`에는 두 가지만 쓴다.
     - "작업 전 PLAN.md의 에이전트 작업 규칙을 따른다."
     - 프로젝트 요약 3~4줄.
4. `.env`는 사용자가 직접 채운다.
   - 에이전트는 `chmod 600 .env`만 안내한다.
5. LLM 연결 확인 스크립트 `scripts/ping_llm.py`를 만든다.
   - 환경 변수의 모델에 "say OK"를 1번 보낸다.
   - 응답과 토큰 사용량을 출력한다.

완료 기준:
- `redis-server --port 6380 --daemonize yes`를 실행한다.
- `redis-cli -p 6380 ping`의 결과가 `PONG`이다. 확인 후 Redis를 끈다.
- `python scripts/ping_llm.py`가 Gemini(무료 등급)에서 응답을 받는다.

---

## 2단계: judge 연결

**시간 상한: 하루.** 가장 불확실한 단계다. 하루를 넘기면 다른 방법으로 간다.

할 일:
1. `openai/frontier-evals` 저장소를 `third_party/`에 받는다(git-lfs 필요).
   - `project/paperbench` 안에서 아래를 찾아 보고한다.
     - SimpleJudge 코드 위치. LLM을 어떤 순서로 몇 번 부르는지. 어떤 API 파라미터를 쓰는지.
     - `semantic-self-consistency`의 논문 파일과 루브릭(JSON). 리프 수. 그중 Code-Dev 리프 수.
     - JudgeEval 데이터 위치. 이 논문의 제출물과 사람 채점 결과가 있는지.
2. **경로 A (원본 judge)**: SimpleJudge를 Gemini 설정으로 리프 1개에 실행한다.
   - 원본 코드가 다른 패키지에 많이 의존하면 억지로 맞추지 않는다. 경로 B로 간다.
3. **경로 B (간이 judge)**: 원본 judge의 프롬프트를 가져온다. `judge/judge.py`에 함수 하나로 만든다.
   - 입력: 논문 텍스트, 리프 기준, 제출물의 관련 파일 내용.
   - 출력: `{"score": 0 또는 1, "reason": "..."}`.
   - **논문 본문을 프롬프트 맨 앞에 둔다.** 호출마다 앞부분이 같아야 캐시 할인을 받는다.
   - JSON을 읽지 못하면 최대 3번 다시 요청한다.

완료 기준:
- 리프 1개에 대해 0/1 점수와 이유가 출력된다.
- 호출 1번의 입력 토큰 수와 출력 토큰 수를 보고한다.

다른 제공자로 바꾸는 기준: 아래 중 하나라도 해당하면 보고한다. 그리고 OpenAI로 바꿀지 사용자에게 묻는다.
- 경로 B로도 Gemini에서 리프 1개를 채점하지 못한다.
- 리프 10개를 시험 채점했을 때 형식 오류가 10%를 넘는다.

---

## 3단계: 큐와 워커 (MOCK 모드만)

- 쿠버네티스 없이 Python 프로세스로 만든다.
- 이 단계에서는 **LLM을 부르지 않는다.** 유료든 무료든 부르지 않는다.

구조:
- 큐: **Redis Streams + consumer group**. 리프 1개가 메시지 1개다.
- `judge/generate_tasks.py`: 루브릭을 읽는다. 리프마다 메시지를 넣는다. 전체 작업 수를 Redis에 적는다.
- `judge/worker.py`: 메시지를 읽고 judge를 부른다. 결과를 저장하고 `XACK`한다.
  - 오랫동안 ACK되지 않은 메시지는 `XAUTOCLAIM`으로 가져와 처리한다.
- 결과 저장: 리프 ID를 키로 덮어쓴다. 같은 리프를 두 번 처리해도 결과는 1개다.
- 공용 rate limiter: Redis로 만든다. 분당 요청 수와 분당 토큰 수를 설정값으로 둔다.
- `judge/analyze.py`: 완료 수가 전체 수와 같으면 집계한다.
  - 처리 시간과 재시도 횟수를 `results/`에 JSON으로 저장한다.

완료 기준 (직접 실행해서 확인한다):
1. 작업 20개, 워커 3개, `MOCK_JUDGE=1` → 20개 모두 결과가 있다.
2. 같은 조건에서 처리 중인 워커 1개를 `kill -9`로 죽인다.
   - 남은 워커가 이어받는다. 20개 모두 결과가 있다. 중복이 없다.
3. 워커 1개, 2개, 4개로 작업 40개를 처리하고 시간을 비교한다.
   - MOCK이므로 시간이 거의 비례해서 줄어야 한다.

추가 보고: 장애 복구가 어떻게 되는지 5줄 이내로 설명한다. 사용자가 발표에서 설명해야 한다.

---

## 4단계: JudgeEval 채점과 F1

할 일:
1. 실제 judge를 워커에 연결한다.
2. **실행 전에 멈추고 보고한다.**
   - 대상 제출물 수, 리프 수, 예상 호출 수, 예상 토큰 수.
   - 무료 등급 제한 때문에 걸릴 예상 시간.
3. 승인을 받으면 JudgeEval 제출물을 채점한다.
   - 사람 채점과 비교해서 F1을 계산한다. 리프 단위, 0/1 기준이다.

결정한 내용:
- **A안으로 한다**: 논문 1편의 리프 77개 전체.
- F1과 함께 아래도 저장한다.
  - 혼동 행렬, macro-F1, 카테고리별 결과.
  - 비교 기준: "모두 1점" judge의 점수.
- 이유: 사람 점수 77개 중 72개가 1점이다. F1만 보면 judge의 실력을 알 수 없다.
- 더 많은 논문으로 하는 계획은 `FUTURE_PLAN.md`에 있다.

완료 기준: `results/judgeeval_<모델명>.json`에 아래가 저장된다.
- 리프별 점수, F1, 처리 시간, 실패 수, 재시도 수.

---

## 5단계: 에이전트 P1~P5

- 정해진 단계를 순서대로 실행하는 파이프라인이다.
- 도구 호출 루프가 없다. 웹 검색이 없다. 입력은 논문뿐이다.

| 단계 | 하는 일 | 출력 |
| --- | --- | --- |
| P1 분석 | 핵심 기여와 구성 요소를 정리한다 | `analysis.md` |
| P2 설계 | 만들 파일 목록을 정한다 | `plan.json` (JSON 모드) |
| P3 작성 | 파일마다 1번 호출한다. **코드 블록 1개만** 받는다. 스크립트가 저장한다 | `src/*.py` 등 |
| P4 점검 | `python -m py_compile`로 문법을 검사한다. 오류가 난 파일만 다시 만든다. 빠진 파일을 채운다 | 수정된 파일 |
| P5 마무리 | `README.md`, `reproduce.sh`, `requirements.txt`를 만든다 | 제출물 완성 |

규칙:
- 중간 결과를 모두 run 폴더에 파일로 저장한다.
  - 중간에 멈추면, 다시 시작할 때 끝난 단계는 건너뛴다.
- 호출 수 상한은 50회다. 코드에 고정한다.
- run마다 호출 수, 토큰 수, 시간을 `results/agent_run_<id>.json`에 기록한다.
- 유료 등급으로 실행할 때는 규칙 5에 따라 먼저 묻는다.

완료 기준:
- 제출물 폴더 1개가 생긴다.
- 그 폴더를 4단계 파이프라인으로 채점해서 점수가 나온다.

---

## 6단계: 컨테이너와 쿠버네티스

0단계 결과에 따라 진행 방식을 사용자에게 확인받는다.

| 서버 상태 | 진행 방식 |
| --- | --- |
| docker 가능 | 이 서버에서 k3d로 클러스터를 만들어 실험한다. 실험할 때만 켠다. 끝나면 `k3d cluster stop` |
| docker 불가 (**지금 서버**) | Dockerfile, YAML, `scripts/bootstrap.sh`만 만든다. 실행은 사용자가 학교 VM에서 한다 |

만들 것:
- Dockerfile 2개
  - `judge-worker`: 워커, 작업 생성기, 집계가 같이 쓴다. 실행 명령(command)만 다르다.
  - `agent`
- `k8s/` 폴더
  - Namespace `paperbench`
  - Secret `llm-keys`: `.env`에서 만든다. 파일로 커밋하지 않는다.
  - ConfigMap: 모델 이름, base URL, 제한값.
  - Redis Deployment + Service
  - judge-worker Deployment
  - Job: task-generator, agent-run, analyze
- 데이터는 hostPath `/data`에 둔다. 노드가 1개라서 결과를 모으기 쉽다.
- 이미지 태그는 `latest`를 쓰지 않는다. 버전이나 커밋 해시를 쓴다.
- `scripts/bootstrap.sh`: 빈 VM에서 아래를 순서대로 한다.
  - 스왑 → k3s 설치(`--disable traefik`) → `/data` 준비 → Secret 생성 → `kubectl apply`.
  - 여러 번 실행해도 안전해야 한다. 이미 된 단계는 건너뛴다.
- `scripts/collect.sh`: VM의 `/data/results`를 로컬 `results/`로 복사한다.

완료 기준 (k3d가 될 때):
- 클러스터에서 MOCK 모드로 3단계 완료 기준 1, 2를 다시 통과한다.
- `kubectl delete pod`로 Pod를 지워서 장애 복구를 확인한다.

---

## 7단계: 실험 (사용자와 함께)

1. 확장성: 워커 1, 2, 4개 × (MOCK, 실제 judge)의 처리 시간을 비교한다.
2. 장애 복구: 채점 중에 워커 Pod를 지운다. 그래도 모두 끝나는지 본다.
3. 전체 파이프라인: 에이전트 → 채점 → 점수.
   - JudgeEval 제출물과 비교한다. 같은 judge, 같은 리프를 쓴다.

결과는 모두 `results/`에 저장하고 커밋한다.

---

## 일정 (8주)

| 주 | 단계 |
| --- | --- |
| 1 | 0~2 |
| 2 | 3, 4 시작 |
| 3 | 4 완료(F1), 5 시작 |
| 4 | 5 완료 |
| 5 | 6 |
| 6 | 7 |
| 7 | 여유 시간. 확장: judge 2개 비교(같은 API, 모델 이름만 바꿈) |
| 8 | 보고서, 발표 |
