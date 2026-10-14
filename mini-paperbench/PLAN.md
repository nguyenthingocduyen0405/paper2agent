# 미니 PaperBench 구현 계획 (연구실 서버, Claude Code용)

> 이 문서는 Claude Code 에이전트가 읽고 단계별로 진행하기 위한 작업 지시서다.
> 사용 방법: "PLAN.md를 읽고 0단계를 진행해줘"처럼 단계 하나씩 시킨다.

---

## 에이전트 작업 규칙 (항상 지킬 것)

1. **한 번에 한 단계만** 진행한다. 단계가 끝나면 아래 "보고 형식"으로 보고하고 멈춘다. 다음 단계는 사용자가 시킬 때만 시작한다.
2. **각 단계의 완료 기준을 직접 실행해서 확인**한다. 실행하지 않고 "될 것이다"라고 보고하지 않는다.
3. 여기는 **공용 GPU 서버**다.
   - GPU를 쓰지 않는다. 모든 실행에 `CUDA_VISIBLE_DEVICES=""`를 둔다.
   - `sudo`를 쓰지 않는다. 시스템 패키지를 설치하지 않는다. 필요한 것은 conda 환경 안에 설치한다.
   - 상주 프로세스(Redis 등)는 작업이 끝나면 끈다. 기본 포트 대신 겹치지 않는 포트(예: Redis 6380)를 쓴다.
4. **비밀 키**는 `.env`에서만 읽는다. 코드, 로그, 출력, 커밋에 키를 넣지 않는다. `.env`는 `.gitignore`에 있어야 한다.
5. **유료 API 호출 전에는 멈추고 물어본다.** 예상 호출 수와 예상 토큰 수를 함께 보고한다. 무료 등급·MOCK 모드 실행은 묻지 않아도 된다.
6. 계획과 다른 결정이 필요하면(라이브러리 변경, 구조 변경 등) 먼저 이유를 보고하고 승인을 받는다.
7. 수업 프로젝트다. **작게, 기본 기능 먼저.** 요청하지 않은 기능을 추가하지 않는다.

### 보고 형식

```
## N단계 보고
- 한 일:
- 확인 방법과 결과: (실행한 명령, 핵심 출력)
- 만든/바꾼 파일:
- 문제·결정 필요 사항:
- 측정값: (해당 시 호출 수, 토큰 수, 시간)
```

---

## 프로젝트 요약

PaperBench(arXiv 2504.01848)의 흐름을 작게 재현한다.

- **judge**: 제출물(코드 폴더)을 루브릭의 리프 노드마다 0/1로 채점한다. Redis 큐 + 워커 여러 개로 병렬 처리한다.
- **에이전트**: LLM이 논문을 읽고 재현 코드를 작성한다. 실행은 하지 않는다(Code-Dev 방식, GPU 불필요).
- 대상 논문: `semantic-self-consistency` 1편.
- 제출물 형식(PaperBench): 루트에 `reproduce.sh`, `README.md`, `src/`, `requirements.txt`.
- 최종적으로 쿠버네티스(k3s/k3d)에 배포한다. 단, 이 서버에서는 우선 쿠버네티스 없이 Python 프로세스로 완성한다.

### LLM 설정 (전환 가능하게)

모든 LLM 호출은 `openai` 라이브러리 하나로 하고, 아래 환경 변수만 읽는다. 제공자 전환은 `.env` 수정만으로 끝나야 한다.

| 변수 | 1순위: Gemini | 2순위: OpenAI |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `https://api.openai.com/v1` |
| `LLM_MODEL` | `gemini-2.5-flash-lite` | `gpt-5-nano` |
| `LLM_API_KEY` | Gemini 키 | OpenAI 키 |

- 모델 이름과 엔드포인트는 실행 전에 실제로 1회 호출해서 확인한다(지원 종료된 모델일 수 있음).
- `MOCK_JUDGE=1`이면 LLM을 부르지 않고 `MOCK_DELAY_SEC`(기본 3초) 기다린 뒤 무작위 0/1을 돌려준다.

### 목표 디렉터리 구조

```
.
├── PLAN.md
├── CLAUDE.md
├── .env.example        # 키 이름만, 값 없음
├── judge/              # judge 함수, 워커, 작업 생성기, 집계
├── agent/              # 에이전트 P1~P5
├── k8s/                # (6단계) YAML
├── scripts/            # 설치·실행·수집 스크립트
├── data/               # 논문·루브릭·제출물 (git에 올리지 않음, 스크립트로 받기)
└── results/            # 실험 결과 JSON (git에 커밋)
```

---

## 0단계: 서버 환경 확인 (설치하지 말고 확인만)

할 일: 아래를 실행하고 결과를 표로 보고한다.

```bash
sudo -n true 2>/dev/null && echo "sudo: 가능" || echo "sudo: 불가"
docker ps >/dev/null 2>&1 && echo "docker: 가능" || echo "docker: 불가"
which conda mamba git git-lfs python3
curl -sI https://generativelanguage.googleapis.com | head -1
curl -sI https://api.openai.com | head -1
df -h ~ ; free -h ; nproc
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv   # 현재 사용 현황만 확인
```

완료 기준: sudo, docker, git-lfs, 두 API 접속 가능 여부, 디스크·메모리 여유가 보고됨.

---

## 1단계: 저장소와 환경

할 일:
1. conda 환경 `pb` 생성 (Python 3.11). 필요한 것: `openai`, `pydantic`, `tenacity`, `redis`(Python 클라이언트), `python-dotenv`, `pytest`.
2. conda-forge에서 `redis`(서버)와, 0단계에서 없으면 `git-lfs` 설치.
3. `.gitignore`(`.env`, `data/`, `__pycache__` 등), `.env.example`, `CLAUDE.md` 생성.
   - `CLAUDE.md`에는 "작업 전 PLAN.md의 에이전트 작업 규칙을 따른다"와 프로젝트 요약 3~4줄만 쓴다.
4. `.env` 파일은 사용자가 직접 채운다. 에이전트는 `chmod 600 .env`만 안내한다.
5. LLM 연결 확인 스크립트 `scripts/ping_llm.py`: 환경 변수로 모델에 "say OK" 1회 호출, 응답과 토큰 사용량 출력.

완료 기준:
- `redis-server --port 6380 --daemonize yes` 후 `redis-cli -p 6380 ping` → `PONG`, 확인 후 종료.
- `python scripts/ping_llm.py` 가 Gemini(무료 등급)에서 응답을 받음.

---

## 2단계: judge 연결 (시간 상한: 하루)

가장 불확실한 단계다. 시간 상한을 넘기면 대안으로 간다.

할 일:
1. `openai/frontier-evals` 저장소를 `third_party/`에 clone (git-lfs 필요). `project/paperbench` 안에서 다음을 찾아 보고한다.
   - SimpleJudge 코드 위치, LLM 호출 순서(호출 종류와 횟수), 사용하는 API 파라미터
   - `semantic-self-consistency`의 논문 파일, 루브릭(JSON), 리프 노드 수, 그중 Code-Dev 해당 리프 수
   - JudgeEval 데이터 위치, 이 논문의 제출물과 사람 채점 결과 존재 여부
2. **경로 A (원본 judge)**: SimpleJudge를 Gemini 설정으로 리프 1개에 실행한다. 프레임워크 의존성이 깊으면 억지로 맞추지 말고 경로 B로 간다.
3. **경로 B (간이 judge)**: 원본 judge의 프롬프트를 가져와 `judge/judge.py`에 함수 하나로 작성한다.
   - 입력: 논문 텍스트, 리프 기준, 제출물의 관련 파일 내용
   - 출력: `{"score": 0 또는 1, "reason": "..."}`
   - **프롬프트 순서: 논문 본문을 맨 앞에** 둔다(호출 간 앞부분이 같아야 캐시 할인을 받음).
   - JSON 파싱 실패 시 재요청 최대 3회.

완료 기준: 리프 1개에 대해 0/1과 근거가 출력됨. 호출 1회당 입력·출력 토큰 수를 보고함.

전환 기준 (하나라도 해당하면 보고 후 OpenAI 전환 여부를 사용자에게 묻는다):
- 경로 B까지 해도 Gemini에서 리프 1개 채점이 안 됨
- 리프 10개 시험 채점에서 형식 오류가 10% 넘게 남

---

## 3단계: 큐와 워커 (MOCK 모드만)

쿠버네티스 없이 Python 프로세스로 만든다. 이 단계에서는 **유료·무료 LLM을 부르지 않는다.**

구조:
- 큐: **Redis Streams + consumer group**. 리프 1개 = 메시지 1개.
- `judge/generate_tasks.py`: 루브릭을 읽어 리프마다 메시지를 넣고, 전체 작업 수를 Redis에 기록.
- `judge/worker.py`: 메시지를 읽고 judge 호출 → 결과 저장 → `XACK`. 일정 시간 확인 안 된 메시지는 `XAUTOCLAIM`으로 가져가 처리.
- 결과 저장: 리프 ID를 키로 덮어쓰기(같은 리프가 두 번 처리돼도 결과 1개).
- 공용 rate limiter: Redis 기반, 분당 요청 수와 분당 토큰 수를 설정값으로.
- `judge/analyze.py`: 완료 수 = 전체 수이면 집계, 처리 시간·재시도 횟수를 `results/`에 JSON으로 저장.

완료 기준 (직접 실행해서 확인):
1. 작업 20개, 워커 3개, `MOCK_JUDGE=1` → 20개 모두 결과 있음.
2. 같은 조건에서 처리 중인 워커 1개를 `kill -9` → 남은 워커가 이어받아 20개 모두 결과 있음, 중복 없음.
3. 워커 1·2·4개로 작업 40개 처리 시간 비교 (MOCK이므로 거의 선형으로 줄어야 함).

추가 보고: 장애 복구가 어떻게 동작하는지 5줄 이내로 설명 (사용자가 발표에서 설명해야 함).

---

## 4단계: JudgeEval 채점과 F1

할 일:
1. 실제 judge를 워커에 연결한다.
2. **실행 전에 멈추고 보고**: 대상 제출물 수, 리프 수, 예상 호출 수, 예상 토큰 수, 무료 등급 제한으로 걸릴 예상 시간.
3. 승인 후 JudgeEval 제출물을 채점하고, 사람 채점과 비교해 F1(리프 단위 0/1)을 계산한다.

완료 기준: `results/judgeeval_<모델명>.json`에 리프별 점수, F1, 처리 시간, 실패·재시도 수가 저장됨.

---

## 5단계: 에이전트 P1~P5

정해진 단계를 순서대로 밟는 파이프라인이다. 도구 호출 루프 없음, 웹 검색 없음, 입력은 논문뿐.

| 단계 | 하는 일 | 출력 |
| --- | --- | --- |
| P1 분석 | 핵심 기여·구성 요소 정리 | `analysis.md` |
| P2 설계 | 만들 파일 목록 | `plan.json` (JSON 모드) |
| P3 작성 | 파일마다 호출 1회, **코드 블록 1개만** 출력받아 스크립트가 저장 | `src/*.py` 등 |
| P4 점검 | `python -m py_compile`로 문법 검사, 오류 파일만 재생성 + 빠진 것 보완 | 수정된 파일 |
| P5 마무리 | `README.md`, `reproduce.sh`, `requirements.txt` | 제출물 완성 |

규칙:
- 모든 중간 결과를 run 폴더에 파일로 저장한다. 중간에 죽으면 끝난 단계는 건너뛰고 재시작한다.
- 호출 수 상한 50회를 코드에 하드코딩한다.
- run마다 호출 수, 토큰 수, 시간을 `results/agent_run_<id>.json`에 기록한다.
- 유료 등급에서 실행할 때는 규칙 5에 따라 먼저 묻는다.

완료 기준: 제출물 폴더 1개 생성 → 4단계 파이프라인으로 채점해서 점수가 나옴.

---

## 6단계: 컨테이너화와 쿠버네티스

0단계 결과에 따라 진행 방식을 사용자에게 확인받는다.

| 서버 상태 | 진행 |
| --- | --- |
| docker 가능 | 이 서버에서 k3d로 클러스터를 만들어 실험. 실험할 때만 켜고 끝나면 `k3d cluster stop` |
| docker 불가 | Dockerfile, YAML, `scripts/bootstrap.sh`만 작성. 실행은 사용자가 학교 VM에서 함 |

만들 것:
- Dockerfile 2개: `judge-worker`(워커·작업 생성기·집계 공용, command만 다름), `agent`
- `k8s/`: Namespace `paperbench`, Secret `llm-keys`(.env에서 생성, 파일로 커밋하지 않음), ConfigMap(모델명·base URL·제한값), Redis Deployment+Service, judge-worker Deployment, task-generator·agent-run·analyze Job
- 데이터는 hostPath `/data` 고정 경로 (단일 노드, 결과 수집이 쉬움)
- 이미지 태그는 `latest` 대신 버전 또는 커밋 해시
- `scripts/bootstrap.sh`: 빈 VM에서 스왑 → k3s(`--disable traefik`) → `/data` 준비 → Secret 생성 → `kubectl apply`. 여러 번 실행해도 안전하게(이미 된 단계는 건너뜀).
- `scripts/collect.sh`: VM의 `/data/results`를 로컬 `results/`로 복사

완료 기준: (k3d 가능 시) 클러스터에서 MOCK 모드로 3단계 완료 기준 1·2를 다시 통과. Pod 삭제(`kubectl delete pod`)로 장애 복구 확인.

---

## 7단계: 실험 (사용자와 함께)

1. 확장성: 워커 1·2·4개 × (MOCK, 실제 judge) 처리 시간 비교
2. 장애 복구: 채점 중 워커 Pod 삭제 → 전부 완료되는지
3. 전체 파이프라인: 에이전트 → 채점 → 점수, JudgeEval 제출물과 같은 judge·같은 리프로 비교

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
| 7 | 여유분, 확장(judge 2개 비교: 같은 API에서 모델명만 바꿈) |
| 8 | 보고서, 발표 |
