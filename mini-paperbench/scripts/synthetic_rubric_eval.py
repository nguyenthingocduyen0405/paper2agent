"""실험: 자동으로 설계한 채점 기준이 잘 만든 코드와 잘못 만든 코드를 가려내는가.

합성 논문마다 정답 라벨이 있는 제출물 5개(sub_correct, sub_subtle, sub_partial, sub_wrong_method, sub_empty)를
1) 논문만 올려 PaperAgent가 설계한 채점 기준(자동 기준)으로 검증하고,
2) (--with-human) 합성 논문에 딸린 원래 채점 기준으로도 검증해서,
원래 기준의 정답 라벨 점수(사람이 정한 정답)와 비교한다. 정답 라벨은 judge에게 보이지 않는 data/synthetic_labels에 있다.

서비스와 똑같은 길로 돈다: 웹 API(/jobs)에 작업을 만들고, orchestrator가 논문 준비 → 검증을 진행한다.
같은 논문의 첫 작업만 채점 기준을 새로 설계하고(new_rubric), 나머지 4개는 그 기준을 다시 쓴다(같은 기준으로 비교).

웹 Pod 안에서 실행한다(관리자 세션을 DB에 직접 만들어 쓰므로 비밀번호가 필요 없다):
  kubectl -n paperbench exec -i deploy/web -- sh -c 'cat > /tmp/sre.py'  < scripts/synthetic_rubric_eval.py
  kubectl -n paperbench exec deploy/web -- sh -c 'nohup python /tmp/sre.py --papers 1957_fatigued_perceptron,clipsign \
      > data/experiments/synthetic_rubric_eval.log 2>&1 &'
진행 상태는 data/experiments/synthetic_rubric_eval/state.json에 남는다. 중간에 끊겨도 다시 실행하면 이어서 한다.
결과: data/experiments/synthetic_rubric_eval/summary.json, summary.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, "/app")
from web import auth, db, store  # noqa: E402

VARIANTS = ["sub_correct", "sub_subtle", "sub_partial", "sub_wrong_method", "sub_empty"]
OUT = store.DATA / "experiments" / "synthetic_rubric_eval"
BASE = "http://127.0.0.1:8000"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_state() -> dict:
    p = OUT / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(state: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(OUT / "state.json")


def admin_cookie() -> str:
    with db.engine().connect() as conn:
        row = conn.execute(db.users.select().where(db.users.c.role == "admin").order_by(db.users.c.id).limit(1)).first()
    if row is None:
        sys.exit("관리자 계정이 없습니다")
    return f"{auth.COOKIE}={auth.new_session(row.id)}"


def post_job(cookie: str, fields: dict, paper: Path | None) -> str:
    """multipart로 /jobs에 보내고 새 작업 id를 돌려준다."""
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    if paper is not None:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"paper_file\"; filename=\"paper.md\"\r\n"
                 "Content-Type: text/markdown\r\n\r\n").encode() + paper.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/jobs", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": cookie})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 303:
            return e.headers["location"].rsplit("/", 1)[1]
        sys.exit(f"작업을 만들지 못했습니다: HTTP {e.code}\n{e.read().decode(errors='replace')[:2000]}")
    sys.exit("작업을 만들지 못했습니다: 응답에 새 작업 주소가 없습니다")


def wait(job_ids: list[str], until: str) -> None:
    """until=rubric: 논문 준비가 끝날 때까지, until=done: 작업이 끝날 때까지."""
    last = None
    while True:
        jobs = [store.load(j) for j in job_ids]
        if any(j["status"] == "failed" for j in jobs):
            bad = [j for j in jobs if j["status"] == "failed"]
            sys.exit(f"작업 실패: {[(j['id'], j.get('error')) for j in bad]}")
        if until == "rubric" and all(j["stage"] not in ("prepare_wait", "prepare") for j in jobs):
            return
        if until == "done" and all(j["status"] == "done" for j in jobs):
            return
        now = [(j["id"], j["stage"]) for j in jobs]
        if now != last:
            log("waiting: " + ", ".join(f"{i}:{s}" for i, s in now))
            last = now
        time.sleep(20)


def true_score(name: str, variant: str) -> float:
    labels = json.loads((store.DATA / "synthetic_labels" / name / f"{variant}.json").read_text())["labels"]
    return sum(x["score"] for x in labels) / len(labels)


def pairwise_agreement(a: list[float], b: list[float]) -> float | None:
    """5개 제출물의 모든 쌍에서 두 점수의 순서가 같은 비율. 정답 라벨이 같은 쌍은 뺀다."""
    agree = total = 0
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            if a[i] == a[j]:
                continue
            total += 1
            agree += (b[i] - b[j]) * (a[i] - a[j]) > 0
    return round(100 * agree / total, 1) if total else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--papers", required=True, help="data/synthetic 아래 폴더 이름, 쉼표로 구분")
    ap.add_argument("--with-human", action="store_true", help="원래 채점 기준으로도 검증한다")
    args = ap.parse_args()
    names = args.papers.split(",")
    state = load_state()
    cookie = admin_cookie()

    for name in names:
        root = store.DATA / "synthetic" / name
        paper = root / "paper.md"
        if not paper.is_file():
            sys.exit(f"{paper} 없음")
        st = state.setdefault(name, {"auto": {}, "human": {}})
        sub = lambda v: (root / "submissions" / v).as_posix()  # noqa: E731
        # 자동 기준: 첫 작업이 기준을 새로 설계하고, 끝나면 나머지가 그 기준을 다시 쓴다
        if VARIANTS[0] not in st["auto"]:
            st["auto"][VARIANTS[0]] = post_job(cookie, {"skip_submission": sub(VARIANTS[0]), "new_rubric": "1"}, paper)
            save_state(state)
            log(f"{name}: {VARIANTS[0]} → {st['auto'][VARIANTS[0]]} (rubric is being designed)")
        first = st["auto"][VARIANTS[0]]
        wait([first], "rubric")
        rubric_id = store.load(first).get("rubric_id")
        for v in VARIANTS[1:]:
            if v not in st["auto"]:
                st["auto"][v] = post_job(cookie, {"skip_submission": sub(v)}, paper)
                save_state(state)
                job = store.load(st["auto"][v])
                if job.get("rubric_id") != rubric_id:
                    sys.exit(f"{name}/{v}: 같은 채점 기준을 다시 쓰지 않았습니다 ({job.get('rubric_id')} != {rubric_id})")
                log(f"{name}: {v} → {st['auto'][v]}")
        if args.with_human:
            for v in VARIANTS:
                if v not in st["human"]:
                    st["human"][v] = post_job(cookie, {"library_key": f"synthetic/{name}", "skip_submission": sub(v)}, None)
                    save_state(state)
                    log(f"{name}: {v} (original rubric) → {st['human'][v]}")

    all_ids = [i for n in names for kind in ("auto", "human") for i in state[n][kind].values()]
    wait(all_ids, "done")

    rows, summary = [], {"papers": {}}
    for name in names:
        st = state[name]
        truth = [true_score(name, v) for v in VARIANTS]
        auto = [store.load(st["auto"][v])["score"] for v in VARIANTS]
        auto_pct = [s["passed"] / s["judged"] if s["judged"] else 0 for s in auto]
        human = [store.load(st["human"][v])["score"] for v in VARIANTS] if st["human"] else None
        human_pct = [s["passed"] / s["judged"] if s["judged"] else 0 for s in human] if human else None
        first = store.load(st["auto"][VARIANTS[0]])
        summary["papers"][name] = {
            "title": first.get("title"), "rubric_job": first["id"], "evidence": first.get("evidence"),
            "auto_items_judged": auto[0]["judged"],
            "truth_pct": [round(100 * x, 1) for x in truth],
            "auto_pct": [round(100 * x, 1) for x in auto_pct],
            "human_pct": [round(100 * x, 1) for x in human_pct] if human_pct else None,
            "order_agreement_auto": pairwise_agreement(truth, auto_pct),
            "order_agreement_human": pairwise_agreement(truth, human_pct) if human_pct else None,
            "jobs": st,
        }
        for i, v in enumerate(VARIANTS):
            rows.append((name, v, truth[i], auto[i], human[i] if human else None))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    lines = ["# 자동 채점 기준이 좋은 코드와 나쁜 코드를 가려내는가", "",
             "합성 논문의 제출물 5개를 검증했다. 정답은 원래 채점 기준의 정답 라벨 점수(사람이 정함).", "",
             "| 논문 | 제출물 | 정답 라벨 | judge + 원래 기준 | judge + 자동 기준 |", "|---|---|---|---|---|"]
    for name, v, t, a, h in rows:
        hs = f"{h['passed']}/{h['judged']} ({100 * h['passed'] / h['judged']:.0f}%)" if h and h["judged"] else "-"
        a_s = f"{a['passed']}/{a['judged']} ({100 * a['passed'] / a['judged']:.0f}%)" if a["judged"] else "0/0"
        lines.append(f"| {name} | {v} | {100 * t:.0f}% | {hs} | {a_s} |")
    lines += ["", "순서 일치율: 제출물 두 개씩 짝지었을 때, 정답 라벨 점수가 높은 쪽이 judge 점수도 높은 짝의 비율(정답이 같은 짝은 뺌).", ""]
    for name, p in summary["papers"].items():
        lines.append(f"- {name}: 자동 기준 {p['order_agreement_auto']}%"
                     + (f", 원래 기준 {p['order_agreement_human']}%" if p["order_agreement_human"] is not None else "")
                     + (f" (자동 기준 근거 문장 원문 확인 {p['evidence']['found']}/{p['evidence']['total']})" if p.get("evidence") else ""))
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
