"""화면에 보여 줄 값: 상태 이름, 단계, 에이전트 진행, 채점 진행, 결과, 보고서."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from judge.common import k
from judge.rubric import load_leaves
from web import k8s, orchestrator, store

KST = timezone(timedelta(hours=9))
AGENT_CALL_LIMIT = 50  # agent/llm.py의 MAX_CALLS
FINAL_FILES = ("requirements.txt", "reproduce.sh", "README.md")
STEP_LABELS = ("논문 접수", "코드 작성", "자동 검증", "결과")
STAGE_STEP = {"queued": 1, "agent": 1, "judge_wait": 2, "judge": 2, "analyze": 2, "done": 4}
STAGE_STATUS = {
    "queued": ("대기 중", "wait"),
    "agent": ("코드 작성 중", "run"),
    "judge_wait": ("검증 대기 중", "wait"),
    "judge": ("자동 검증 중", "run"),
    "analyze": ("결과 정리 중", "run"),
}


def fmt_time(t: float | None) -> str:
    return datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d %H:%M") if t else ""


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"


def elapsed(job: dict) -> float:
    return (job.get("finished_at") or time.time()) - job["created_at"]


def status_label(job: dict) -> tuple[str, str]:
    if job["status"] == "done":
        return "완료", "ok"
    if job["status"] == "failed":
        return "실패", "bad"
    return STAGE_STATUS.get(job["stage"], (job["stage"], "run"))


def steps(job: dict) -> list[dict]:
    cur = STAGE_STEP.get(job["stage"], 1)
    out = []
    for i, label in enumerate(STEP_LABELS):
        if i == 1 and job.get("skip_agent"):
            label = "코드 작성 (건너뜀)"
        if job["status"] == "done" or i < cur:
            state = "done"
        elif i == cur:
            state = "failed" if job["status"] == "failed" else "current"
        else:
            state = "todo"
        out.append({"label": label, "state": state})
    return out


def files_in(sub: Path) -> list[dict]:
    if not sub.is_dir():
        return []
    out = []
    for p in sorted(sub.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            size = p.stat().st_size
            out.append({"path": p.relative_to(sub).as_posix(), "size": size,
                        "size_label": f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"})
    return out


def agent_progress(job: dict) -> dict | None:
    """run 폴더의 파일로 P1~P5 진행을 읽는다(에이전트는 단계마다 파일을 남긴다)."""
    if job.get("skip_agent"):
        return None
    run_dir = store.DATA / "agent_runs" / job["id"]
    sub = run_dir / "submission"
    plan = None
    try:
        plan = json.loads((run_dir / "plan.json").read_text())["files"]
    except (OSError, ValueError, KeyError):
        pass
    written = sum(1 for f in plan if (sub / f["path"]).exists()) if plan else 0
    final = [n for n in FINAL_FILES if (sub / n).exists()]
    finished = (store.RESULTS / f"agent_run_{job['id']}.json").exists()
    try:
        with (run_dir / "calls.jsonl").open() as f:
            calls = sum(1 for _ in f)
    except OSError:
        calls = 0
    raw = [
        ("P1", "논문 분석", (run_dir / "analysis.md").exists(), ""),
        ("P2", "코드 설계", plan is not None, f"파일 {len(plan)}개 계획" if plan else ""),
        ("P3", "코드 작성", bool(plan) and written >= len(plan), f"{written}/{len(plan)} 파일" if plan else ""),
        ("P4", "문법 점검과 수정", bool(final) or finished, ""),
        ("P5", "실행 안내 작성", finished, f"{len(final)}/{len(FINAL_FILES)} 파일" if final else ""),
    ]
    running = job["status"] == "running" and job["stage"] == "agent"
    failed = job["status"] == "failed" and job.get("failed_stage") == "agent"
    out, marked = [], False
    for code, label, done, detail in raw:
        if done:
            state = "done"
        elif not marked and (running or failed):
            state, marked = ("current" if running else "failed"), True
        else:
            state = "todo"
        out.append({"code": code, "label": label, "state": state, "detail": detail})
    return {"steps": out, "calls": calls, "call_limit": AGENT_CALL_LIMIT,
            "files": [f["path"] for f in files_in(sub)]}


def judge_progress(job: dict) -> dict | None:
    if job["stage"] not in ("judge", "analyze"):
        return None
    run = job["judge_run"]
    r = orchestrator.redis()
    total = int(r.get(k(run, "total")) or 0)
    results = [json.loads(v) for v in r.hvals(k(run, "results"))]
    ok = [x for x in results if "error" not in x]
    recent = sorted(ok, key=lambda x: x.get("finished_at", 0), reverse=True)[:5]
    return {"total": total, "done": len(results), "passed": sum(x["score"] for x in ok),
            "pct": round(100 * len(results) / total) if total else 0, "recent": recent}


def results(job: dict) -> dict | None:
    """끝난 작업의 결과: 항목별 점수(루브릭 순서), 파일 목록."""
    path = store.RESULTS / f"{job['judge_run']}.json"
    if job["status"] != "done" or not path.exists():
        return None
    by_id = json.loads(path.read_text()).get("leaves", {})
    try:
        rubric = load_leaves(Path(job["paper_dir"]) / "rubric.json")
    except (OSError, ValueError, KeyError):
        rubric = []
    known = {leaf["id"] for leaf in rubric}
    ordered = [(leaf["requirements"], by_id[leaf["id"]]) for leaf in rubric if leaf["id"] in by_id]
    ordered += [(v.get("requirements", lid), v) for lid, v in by_id.items() if lid not in known]
    items = [{"n": i + 1, "requirements": req, "score": v.get("score"), "reason": v.get("reason", ""),
              "error": v.get("error")} for i, (req, v) in enumerate(ordered)]
    ok = [x for x in items if not x["error"]]
    passed = sum(x["score"] for x in ok)
    return {"rows": items, "judged": len(ok), "passed": passed, "failed": len(ok) - passed,
            "errors": len(items) - len(ok), "pct": round(100 * passed / len(ok)) if ok else 0,
            "files": files_in(Path(job["submission_dir"]))}


def summary(job: dict) -> str:
    """작업 목록에 보여 줄 한 줄 진행 상황."""
    if job["status"] == "done":
        s = job.get("score") or {}
        return f"충족 {s.get('passed', '?')}/{s.get('judged', '?')}"
    if job["status"] == "failed":
        return job.get("error", "")
    stage = job["stage"]
    if stage == "agent":
        a = agent_progress(job)
        cur = next((s for s in a["steps"] if s["state"] == "current"), None) if a else None
        return f"{cur['code']} {cur['label']} {cur['detail']}".strip() if cur else "코드 작성 중"
    if stage == "judge":
        j = judge_progress(job)
        return f"검증 {j['done']}/{j['total'] or '?'}"
    return {"queued": "차례를 기다리는 중", "judge_wait": "검증 차례를 기다리는 중",
            "analyze": "결과 정리 중"}.get(stage, "")


def live_context(job: dict) -> dict:
    ctx = {"job": job, "steps": steps(job), "agent": agent_progress(job), "judge": judge_progress(job),
           "res": results(job), "elapsed": fmt_duration(elapsed(job)), "log": ""}
    if job["status"] == "running" and job["stage"] == "agent":
        try:
            ctx["log"] = k8s.job_logs(k8s.job_name("agent", job["id"]), tail=12)
        except Exception:  # 로그는 없어도 화면은 보여 준다
            pass
    return ctx


def report_md(job: dict) -> str | None:
    res = results(job)
    if res is None:
        return None
    lines = [
        "# PaperAgent 결과 보고서",
        "",
        f"논문: {job['title']}",
        f"작업 ID: {job['id']}",
        f"작업 시각: {fmt_time(job['created_at'])} ~ {fmt_time(job.get('finished_at'))} (한국 시간)",
        "",
        "## 요약",
        "",
        f"- 만든 파일: {len(res['files'])}개",
        f"- 자동 검증: {res['judged']}개 항목 중 {res['passed']}개 충족 ({res['pct']}%)",
    ]
    if res["errors"]:
        lines.append(f"- 검증하지 못한 항목: {res['errors']}개")
    if job.get("mock"):
        lines.append("- 주의: 모의 채점 결과입니다. LLM을 부르지 않고 무작위 점수를 매겼으므로 점수에 의미가 없습니다.")
    if job.get("skip_agent"):
        lines.append("- 이 작업은 코드를 새로 작성하지 않고 기존 제출물을 검증했습니다.")
    lines += [
        "",
        "## 코드 사용 방법",
        "",
        "1. 압축을 풀고 README.md를 읽습니다.",
        "2. 터미널에서 `bash reproduce.sh`를 실행합니다. 필요한 패키지는 requirements.txt에 있습니다.",
        "3. 출력된 결과를 논문의 표와 그림에 나온 수치와 비교합니다.",
        "",
        "## 만든 파일",
        "",
    ]
    lines += [f"- `{f['path']}`" for f in res["files"]]
    lines += [
        "",
        "## 항목별 검증 결과",
        "",
        "논문의 채점 기준(rubric) 가운데 코드 구현에 관한 항목(Code Development)을 하나씩 검증했습니다.",
        "",
    ]
    for x in res["rows"]:
        mark = "검증 오류" if x["error"] else ("충족" if x["score"] else "미충족")
        lines += [f"### {x['n']}. {mark}", "", f"기준: {x['requirements']}", "",
                  f"근거: {x['reason'] or x['error']}", ""]
    lines += [
        "## 한계",
        "",
        "- 코드는 실행하지 않았습니다. 검증은 코드 내용이 기준을 구현했는지만 봅니다.",
        "- 실제 실행 결과와 논문 수치의 비교는 직접 해야 합니다.",
        "- 검증도 LLM이 하므로 틀릴 수 있습니다. 그래서 항목마다 근거를 함께 적었습니다.",
        "",
    ]
    return "\n".join(lines)
