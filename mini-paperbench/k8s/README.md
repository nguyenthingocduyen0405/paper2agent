# 쿠버네티스(k3s)에서 실행하기

연구실 서버에는 docker가 없다. 그래서 아래는 **학교 VM**에서 한다.
VM은 Ubuntu이고 `sudo`를 쓸 수 있어야 한다.

## 1. 준비 (처음 한 번)

```bash
git clone https://github.com/nguyenthingocduyen0405/paper2agent.git
cd paper2agent/mini-paperbench
cp .env.example .env && chmod 600 .env
nano .env        # LLM_API_KEY= 뒤에 키를 넣는다
```

## 2. 설치와 배포

```bash
scripts/bootstrap.sh
```

이 스크립트는 아래를 순서대로 한다. 여러 번 실행해도 안전하다.

1. 스왑 2GB를 만든다.
2. k3s를 설치한다(traefik 없이).
3. 이미지 2개를 빌드해서 k3s에 넣는다. 태그는 커밋 해시다.
4. 논문, JudgeEval, 합성 논문을 `/data`에 둔다.
5. `.env`의 `LLM_API_KEY`로 Secret `llm-keys`를 만든다.
6. ConfigMap, Redis, judge 워커(2개)를 띄운다.

## 3. 실험

모든 명령은 `mini-paperbench` 폴더에서 실행한다.

| 하고 싶은 것 | 명령 |
| --- | --- |
| MOCK으로 20개 채점 (LLM 호출 없음) | `MOCK=1 scripts/k8s_run.sh judge m1 20` |
| 실제 judge로 채점 | `scripts/k8s_run.sh judge k1 77` |
| 합성 논문 제출물 채점 | `scripts/k8s_run.sh judge s1 23 --paper-dir data/synthetic/clipsign --submission-dir data/synthetic/clipsign/submissions/sub_subtle` |
| 워커 수 바꾸기 | `scripts/k8s_run.sh scale 4` |
| 장애 복구 확인 (채점 중 다른 터미널에서) | `scripts/k8s_run.sh kill-worker` |
| 진행 상황 | `scripts/k8s_run.sh status m1` |
| 에이전트 실행 | `scripts/k8s_run.sh agent a1` |

- run 이름은 매번 새로 정한다. 같은 이름은 다시 쓸 수 없다.
- 결과는 VM의 `/data/results/<run>.json`에 저장된다.

## 4. 결과 가져오기

```bash
scripts/collect.sh 사용자@VM주소     # 내 컴퓨터에서
scripts/collect.sh                  # VM 안에서
```

## 5. 끝나면

```bash
sudo systemctl stop k3s       # 클러스터 끄기 (다시 켤 때: sudo systemctl start k3s)
```

## 구조

| 리소스 | 하는 일 |
| --- | --- |
| Namespace `paperbench` | 모든 것이 여기에 있다 |
| ConfigMap `llm-config` | 모델 이름, 주소, 제한값, MOCK 설정 |
| Secret `llm-keys` | API 키. 파일로 커밋하지 않는다 |
| Deployment+Service `redis` | 큐와 결과 저장소. 디스크에 저장하지 않는다 |
| Deployment `judge-worker` | 워커. `RUN_ID`로 처리할 run을 정한다 |
| Job `task-generator`, `analyze`, `agent-run` | `k8s/jobs/`의 템플릿. `k8s_run.sh`가 채워서 실행한다 |

- 데이터는 노드의 `/data`(hostPath)에 있다. 노드가 1개라서 이렇게 해도 된다.
- 장애 복구는 로컬과 같다. Pod가 지워지면 그 Pod가 처리하던 작업은 `CLAIM_IDLE_SEC`(120초) 뒤 다른 워커가 가져간다.
- 주의: Redis Pod가 다시 뜨면 큐가 비워진다. 실험 중에는 Redis Pod를 지우지 않는다.
