"""화면에 보여 줄 값: 상태 이름, 단계, 에이전트 진행, 채점 진행, 결과, 보고서."""
from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from judge.common import k
from judge.rubric import load_leaves
from web import gpu, k8s, orchestrator, store

KST = timezone(timedelta(hours=9))
AGENT_CALL_LIMIT = 50  # agent/llm.py의 MAX_CALLS
FINAL_FILES = ("requirements.txt", "reproduce.sh", "README.md")
STEP_LABELS = ("논문 준비", "코드 작성", "자동 검증", "결과")
STAGE_STEP = {"prepare_wait": 0, "prepare": 0, "queued": 1, "agent": 1, "judge_wait": 2, "judge": 2, "analyze": 2,
              "repair_wait": 2, "repair": 2, "done": 4}
STAGE_STATUS = {
    "prepare_wait": ("준비 대기", "wait"),
    "prepare": ("논문 준비 중", "run"),
    "queued": ("대기 중", "wait"),
    "agent": ("코드 작성 중", "run"),
    "judge_wait": ("검증 대기 중", "wait"),
    "judge": ("자동 검증 중", "run"),
    "analyze": ("결과 정리 중", "run"),
    "repair_wait": ("수정 대기 중", "wait"),
    "repair": ("코드 고치는 중", "run"),
}
REPAIR_DIFF_MAX = 200 * 1024


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


def is_shown(rel: Path) -> bool:
    """사용자에게 보여 줄 파일인가: 숨김 파일·폴더(.git, .DS_Store, ._*)와 __pycache__는 뺀다."""
    return not any(part.startswith(".") or part == "__pycache__" for part in rel.parts)


def files_in(sub: Path) -> list[dict]:
    if not sub.is_dir():
        return []
    out = []
    for p in sorted(sub.rglob("*")):
        if p.is_file() and is_shown(p.relative_to(sub)):
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


def leaf_evidence(job: dict) -> dict[str, dict]:
    """자동으로 설계한 채점 기준이면 리프 id → {"text": 논문 근거 문장, "found": 원문에서 찾았는가}."""
    try:
        root = json.loads((Path(job["paper_dir"]) / "rubric.json").read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}

    def walk(node: dict) -> None:
        if not node.get("sub_tasks"):
            if node.get("paper_evidence"):
                out[node["id"]] = {"text": node["paper_evidence"], "found": bool(node.get("evidence_found"))}
            return
        for child in node["sub_tasks"]:
            walk(child)

    walk(root)
    return out


def _scored_rows(job: dict, path: Path) -> list[dict]:
    """검증 결과 파일의 항목을 루브릭 순서로."""
    by_id = json.loads(path.read_text()).get("leaves", {})
    try:
        rubric = load_leaves(Path(job["paper_dir"]) / "rubric.json")
    except (OSError, ValueError, KeyError):
        rubric = []
    evidence = leaf_evidence(job)
    known = {leaf["id"] for leaf in rubric}
    ordered = [(leaf["id"], leaf["requirements"], by_id[leaf["id"]]) for leaf in rubric if leaf["id"] in by_id]
    ordered += [(lid, v.get("requirements", lid), v) for lid, v in by_id.items() if lid not in known]
    return [{"n": i + 1, "id": lid, "requirements": req, "score": v.get("score"), "reason": v.get("reason", ""),
             "error": v.get("error"), "category": v.get("category"), "evidence": evidence.get(lid)}
            for i, (lid, req, v) in enumerate(ordered)]


def results(job: dict) -> dict | None:
    """끝난 작업의 결과: 항목별 점수(루브릭 순서), 파일 목록."""
    path = store.RESULTS / f"{job['judge_run']}.json"
    if job["status"] != "done" or not path.exists():
        return None
    items = _scored_rows(job, path)
    rep = job.get("repair") or {}
    if rep.get("after") and rep.get("first_run"):
        before = _leaves(store.RESULTS / f"{rep['first_run']}.json")
        for x in items:
            x["before"] = (before.get(x["id"]) or {}).get("score")
    ok = [x for x in items if not x["error"]]
    passed = sum(x["score"] for x in ok)
    return {"rows": items, "judged": len(ok), "passed": passed, "failed": len(ok) - passed,
            "errors": len(items) - len(ok), "pct": round(100 * passed / len(ok)) if ok else 0,
            "files": files_in(Path(job["submission_dir"]))}


def _leaves(path: Path) -> dict:
    try:
        return json.loads(path.read_text()).get("leaves", {})
    except (OSError, ValueError):
        return {}


def repair_view(job: dict) -> dict | None:
    """검증 결과로 코드를 고친 과정: 첫 검증과 재검증 점수, 고친 파일, 항목별 변화, 고칠 수 없다고 본 항목."""
    rep = job.get("repair")
    if not rep:
        return None
    d = store.DATA / "agent_runs" / job["id"] / "repair"
    try:
        s = json.loads((d / "summary.json").read_text())
    except (OSError, ValueError):
        s = {}
    out = {"before": rep.get("before") or {}, "after": rep.get("after"), "undone": rep.get("undone"),
           "error": rep.get("error"), "changed": s.get("changed_files") or rep.get("changed") or [],
           "fixes": s.get("fixes") or [], "not_fixable": s.get("not_fixable") or [], "reverted": s.get("reverted") or [],
           "usage": s.get("usage"), "state": "done"}
    if job["status"] == "running":
        out["state"] = "rejudging" if rep.get("rejudge") else "repairing"
    elif rep.get("undone"):
        out["state"] = "undone"
    elif not rep.get("after"):
        out["state"] = "nothing"
    b = out["before"]
    out["failed_before"] = (b.get("judged") or 0) - (b.get("passed") or 0)
    if rep.get("after") and rep.get("first_run"):
        before, after = _leaves(store.RESULTS / f"{rep['first_run']}.json"), _leaves(store.RESULTS / f"{job['judge_run']}.json")
        pairs = [((before.get(lid) or {}).get("score"), v.get("score")) for lid, v in after.items()]
        out.update(fixed=sum(1 for p in pairs if p == (0, 1)), broke=sum(1 for p in pairs if p == (1, 0)),
                   still=sum(1 for p in pairs if p == (0, 0)))
    diff_path = d / "changes.diff"
    if diff_path.is_file() and not rep.get("undone"):
        text = diff_path.read_text(errors="replace")
        out["diff"] = text[:REPAIR_DIFF_MAX] + ("\n... (생략)" if len(text) > REPAIR_DIFF_MAX else "")
    return out


def summary(job: dict) -> str:
    """작업 목록에 보여 줄 한 줄 진행 상황."""
    if job["status"] == "done":
        s = job.get("score") or {}
        text = f"충족 {s.get('passed', '?')}/{s.get('judged', '?')}"
        rep = job.get("repair") or {}
        if rep.get("after"):
            text += f" (고치기 전 {rep['before']['passed']}/{rep['before']['judged']})"
        g = job.get("gpu") or {}
        if gpu.is_active(job):
            text += f", GPU {GPU_STATUS.get(g['status'], (g['status'], ''))[0]}"
        elif g.get("exec_score"):
            e = g["exec_score"]
            text += f", 실행 검증 {e['passed']}/{e['judged']}"
        return text
    if job["status"] == "failed":
        return job.get("error", "")
    stage = job["stage"]
    if stage in ("prepare_wait", "prepare"):
        p = prepare_progress(job)
        cur = next((s for s in p if s["state"] == "current"), None) if p else None
        return f"{cur['label']} 중" if cur else "논문 준비 중"
    if stage == "agent":
        a = agent_progress(job)
        cur = next((s for s in a["steps"] if s["state"] == "current"), None) if a else None
        return f"{cur['code']} {cur['label']} {cur['detail']}".strip() if cur else "코드 작성 중"
    if stage in ("repair_wait", "repair"):
        b = (job.get("repair") or {}).get("before") or {}
        return f"미충족 {(b.get('judged') or 0) - (b.get('passed') or 0)}개 고치는 중"
    if stage == "judge":
        j = judge_progress(job)
        again = "다시 " if (job.get("repair") or {}).get("rejudge") else ""
        return f"{again}검증 {j['done']}/{j['total'] or '?'}"
    return {"queued": "차례를 기다리는 중", "judge_wait": "검증 차례를 기다리는 중",
            "analyze": "결과 정리 중"}.get(stage, "")


def prepare_progress(job: dict) -> list[dict] | None:
    """논문 준비 단계(PDF 변환, 채점 기준 만들기)의 진행. 준비가 필요 없는 작업이면 None."""
    if not (job.get("prepare_pdf") or job.get("rubric_auto")):
        return None
    paper_dir = Path(job["paper_dir"])
    raw = []
    if job.get("prepare_pdf"):
        raw.append(("PDF에서 본문 뽑기", (paper_dir / "paper.md").exists()))
    if job.get("rubric_auto"):
        raw.append(("채점 기준 설계", (paper_dir / "rubric.json").exists()))
    out, current_given = [], False
    for label, done in raw:
        if done or job["stage"] not in ("prepare_wait", "prepare") and job["status"] != "failed":
            state = "done"
        elif not current_given:
            state = "failed" if job["status"] == "failed" else ("current" if job["stage"] == "prepare" else "todo")
            current_given = True
        else:
            state = "todo"
        out.append({"label": label, "state": state})
    return out


CATEGORY_LABEL = {"Code Development": ("코드 구현", "cd"), "Code Execution": ("실행", "other"),
                  "Result Analysis": ("결과 확인", "other")}


def rubric_view(job: dict) -> dict | None:
    """채점 기준 전체: 최상위 영역마다 리프 항목. 아직 없으면 None."""
    path = Path(job["paper_dir"]) / "rubric.json"
    if not path.is_file():
        return None
    try:
        root = json.loads(path.read_text())
    except ValueError:
        return None

    def leaves(node: dict) -> list[dict]:
        if not node.get("sub_tasks"):
            return [node]
        return [leaf for child in node["sub_tasks"] for leaf in leaves(child)]

    sections = []
    for group in root.get("sub_tasks") or [root]:
        items = [{"requirement": leaf["requirements"],
                  "cat": CATEGORY_LABEL.get(leaf.get("task_category"), (leaf.get("task_category") or "기타", "other")),
                  "evidence": ({"text": leaf["paper_evidence"], "found": bool(leaf.get("evidence_found"))}
                               if leaf.get("paper_evidence") else None)}
                 for leaf in leaves(group)]
        sections.append({"name": group["requirements"] if group is not root else "", "items": items})
    items = [i for s in sections for i in s["items"]]
    with_ev = [i for i in items if i["evidence"]]
    return {"sections": sections, "total": len(items), "code_dev": sum(1 for i in items if i["cat"][1] == "cd"),
            "evidence": {"total": len(with_ev), "found": sum(1 for i in with_ev if i["evidence"]["found"])}
            if with_ev else None,
            "auto": bool(job.get("rubric_auto")) or job.get("rubric_origin") == "auto",
            "reused_from": job.get("rubric_from") if not job.get("rubric_auto") else None}


def live_context(job: dict) -> dict:
    ctx = {"job": job, "steps": steps(job), "prep": prepare_progress(job), "rubric": rubric_view(job),
           "agent": agent_progress(job),
           "judge": judge_progress(job), "res": results(job), "elapsed": fmt_duration(elapsed(job)), "log": "",
           "gpu": gpu_view(job), "ask_gpu": gpu.should_ask(job), "gpu_minutes": gpu.GPU_MINUTES,
           "repair": repair_view(job),
           "gpu_fixes": gpu.GPU_MAX_FIXES}
    if job["status"] == "running" and job["stage"] in ("agent", "prepare", "repair"):
        try:
            ctx["log"] = k8s.job_logs(k8s.job_name(job["stage"], job["id"]), tail=12)
        except Exception:  # 로그는 없어도 화면은 보여 준다
            pass
    return ctx


GPU_STATUS = {
    "queued": ("실행 대기", "wait"), "running": ("실행 중", "run"), "judge_wait": ("검증 대기", "wait"),
    "judging": ("실행 결과 검증 중", "run"), "analyze": ("결과 정리 중", "run"), "done": ("끝남", "ok"),
    "failed": ("실행 못 함", "bad"), "cancelled": ("취소됨", "wait"), "self": ("직접 실행", "wait"),
}
GPU_PHASE = {  # agent/gpu_run.py가 status.json에 적는 단계
    "preparing": "실행 준비 중 (필요한 패키지를 받고 코드를 묶는 중)", "queued": "GPU 대기열에서 차례를 기다리는 중",
    "running": "GPU에서 실행 중", "checking": "실행 기록과 결과 파일을 받는 중", "fixing": "오류를 고치는 중",
    "cancelling": "취소하는 중", "done": "실행을 마쳤습니다",
}
FINAL_LABEL = {"succeeded": ("끝까지 실행됨", "ok"), "failed": ("오류로 멈춤", "bad"), "time_limit": ("시간 제한에 걸림", "wait"),
               "cancelled": ("취소됨", "wait"), "error": ("실행 못 함", "bad")}
JPU_STATE = {"SUCCEEDED": "성공", "FAILED_RUNTIME": "실행 오류", "FAILED_ENV": "환경 오류", "FAILED_OOM_CONFIRMED": "메모리 부족",
             "FAILED_OOM_SUSPECT": "메모리 부족 의심", "TIMED_OUT": "시간 초과", "CANCELLED": "취소", "KILLED_BY_ADMIN": "관리자가 중지",
             "FAILED_SUBMISSION": "제출 실패", "UPLOAD_FAILED": "업로드 실패", "RUNNING": "실행 중", "ASSIGNED": "배정됨",
             "GLOBAL_QUEUE_WAIT": "대기열", "USER_QUEUE_WAIT": "대기열", "ESCALATION_WAIT": "대기열", "SUBMITTED": "제출함",
             "CANCEL_REQUESTED": "취소 중"}
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def _tail_lines(path: Path, n: int) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def gpu_final_dir(job: dict) -> Path | None:
    """마지막 시도의 결과 폴더(gpu/attempts/<n>)."""
    res = (job.get("gpu") or {}).get("result") or {}
    attempts = res.get("attempts") or []
    if not attempts:
        return None
    d = gpu.gpu_dir(job) / "attempts" / str(attempts[-1]["n"])
    return d if d.is_dir() else None


def gpu_view(job: dict) -> dict | None:
    g = job.get("gpu")
    if not g:
        return None
    label, cls = GPU_STATUS.get(g["status"], (g["status"], "wait"))
    view = {"status": g["status"], "label": label, "cls": cls, "n": g.get("n"), "error": g.get("error"),
            "minutes": g.get("minutes"), "tier": g.get("tier"), "active": gpu.is_active(job),
            "can_cancel": g["status"] in ("queued", "running") and not g.get("cancel_requested"),
            "can_rerun": not gpu.runnable(job), "requested_by": g.get("requested_by"),
            "elapsed": fmt_duration((g.get("finished_at") or time.time()) - g["requested_at"]) if g.get("requested_at") else ""}
    if g["status"] == "running":
        try:
            st = json.loads((gpu.gpu_dir(job) / "status.json").read_text())
        except (OSError, ValueError):
            st = {}
        if st.get("started_at", 0) >= g.get("started_at", 0) - 5:
            view["phase"] = GPU_PHASE.get(st.get("state"), "실행 준비 중")
            view["attempt"] = st.get("attempt")
            view["live_attempts"] = [{"n": a.get("n"), "state": JPU_STATE.get(a.get("state"), a.get("state")),
                                      "fix": (a.get("fix") or {}).get("diagnosis")} for a in st.get("attempts") or []]
        else:
            view["phase"] = "실행 준비 중"
        if g.get("cancel_requested"):
            view["phase"] = "취소하는 중"
    res = g.get("result")
    if res:
        view["final"], view["final_cls"] = FINAL_LABEL.get(res.get("final"), (res.get("final"), "wait"))
        view["message"] = res.get("message")
        view["quick"] = res.get("quick")
        view["attempts"] = [{**a, "state_label": JPU_STATE.get(a.get("state"), a.get("state")),
                             "run": fmt_duration(a.get("run_sec")), "queue": fmt_duration(a.get("queue_sec"))}
                            for a in res.get("attempts") or []]
        view["changed_files"] = res.get("changed_files") or []
        final_dir = gpu_final_dir(job)
        view["log_tail"] = _tail_lines(final_dir / "reproduce.log", 60) if final_dir else ""
        if final_dir and not view["log_tail"]:
            view["log_tail"] = _tail_lines(final_dir / "logs" / "stdout.log", 60)
        view["outputs"] = [{"path": o, "image": Path(o).suffix.lower() in IMAGE_EXT} for o in res.get("outputs") or []][:100]
        view["n_outputs"] = res.get("n_outputs", len(view["outputs"]))
    score = g.get("exec_score")
    path = store.RESULTS / f"{g.get('exec_run')}.json"
    if score and path.exists():
        rows = _scored_rows(job, path)
        view["exec"] = {"rows": rows, **score, "failed": score["judged"] - score["passed"],
                        "pct": round(100 * score["passed"] / score["judged"]) if score["judged"] else 0}
    return view


def gpu_output_path(job: dict, rel: str) -> Path | None:
    """실행 결과 파일 하나(마지막 시도의 files/ 아래). 밖으로 나가는 경로는 None."""
    d = gpu_final_dir(job)
    if d is None:
        return None
    root = (d / "files").resolve()
    target = (root / rel).resolve()
    return target if target.is_relative_to(root) and target.is_file() else None


def gpu_log_path(job: dict) -> Path | None:
    d = gpu_final_dir(job)
    if d is None:
        return None
    for p in (d / "reproduce.log", d / "logs" / "stdout.log"):
        if p.is_file():
            return p
    return None


def bundle_zip(job: dict) -> bytes | None:
    """내려받기 묶음: 만든 코드(숨김 파일 제외) + 보고서 + 채점 기준. 코드가 아직 없으면 None."""
    sub = Path(job["submission_dir"]).resolve()
    if not sub.is_dir():
        return None
    root = Path(f"paperagent-{job['id']}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(sub.rglob("*")):
            if p.is_file() and is_shown(p.relative_to(sub)):
                z.write(p, root / p.relative_to(sub))
        report = report_md(job)
        if report:
            z.writestr(str(root / "PAPERAGENT_REPORT.md"), report)
        rubric = Path(job["paper_dir"]) / "rubric.json"
        if rubric.is_file():
            z.write(rubric, root / "PAPERAGENT_RUBRIC.json")
        diff = store.DATA / "agent_runs" / job["id"] / "repair" / "changes.diff"
        if (job.get("repair") or {}).get("after") and diff.is_file():
            z.write(diff, root / "PAPERAGENT_REPAIR.diff")
        g = job.get("gpu") or {}
        if g.get("result"):
            run_root = root / "PAPERAGENT_GPU_RUN"
            log_path = gpu_log_path(job)
            if log_path:
                z.write(log_path, run_root / "reproduce.log")
            code = gpu.gpu_dir(job) / "code"
            for rel in g["result"].get("changed_files") or []:
                if (code / rel).is_file():
                    z.write(code / rel, run_root / "fixed_files" / rel)
            final_dir = gpu_final_dir(job)
            if final_dir and (final_dir / "files").is_dir():
                for p in sorted((final_dir / "files").rglob("*")):
                    if p.is_file() and p.stat().st_size <= 5 * 1024 * 1024:
                        z.write(p, run_root / "outputs" / p.relative_to(final_dir / "files"))
    return buf.getvalue()


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
    rv = repair_view(job)
    if rv and rv["state"] == "done":
        b = rv["before"]
        lines.append(f"- 첫 검증: {b['judged']}개 항목 중 {b['passed']}개 충족. 미충족 항목을 근거로 코드를 고친 뒤 전체를 다시 검증했습니다.")
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
    if rv and rv["state"] != "nothing":
        lines += ["", "## 검증 결과로 코드 고치기", ""]
        b = rv["before"]
        if rv["state"] == "done":
            lines += [f"첫 검증에서 미충족이던 {rv['failed_before']}개 항목과 judge가 적은 이유를 근거로 코드를 고치고, "
                      f"모든 항목을 처음부터 다시 검증했습니다. 다시 검증할 때도 같은 judge를 썼습니다.", "",
                      f"- 충족: {b['passed']}/{b['judged']} → {rv['after']['passed']}/{rv['after']['judged']}",
                      f"- 고쳐서 충족: {rv.get('fixed', 0)}개, 고친 뒤에도 미충족: {rv.get('still', 0)}개, "
                      f"고친 뒤 미충족으로 바뀐 항목: {rv.get('broke', 0)}개",
                      f"- 고친 파일: {', '.join(rv['changed']) or '없음'} (바뀐 내용은 PAPERAGENT_REPAIR.diff)"]
            for f in rv["fixes"]:
                lines.append(f"  - `{f['path']}`: {f['change']}")
        elif rv["state"] == "undone":
            lines.append(f"- {rv['error']}")
        if rv["not_fixable"]:
            lines.append("- 코드를 고쳐서는 충족할 수 없다고 본 항목:")
            lines += [f"  - {x['requirement']} (이유: {x['why']})" for x in rv["not_fixable"]]
        lines.append("")
    lines += [
        "",
        "## 항목별 검증 결과",
        "",
        "논문의 채점 기준(rubric) 가운데 코드 구현에 관한 항목(Code Development)을 하나씩 검증했습니다.",
        "",
    ]
    for x in res["rows"]:
        mark = "검증 오류" if x["error"] else ("충족" if x["score"] else "미충족")
        if x.get("before") == 0 and x["score"] == 1:
            mark += " (고쳐서 충족)"
        elif x.get("before") == 1 and x["score"] == 0:
            mark += " (고친 뒤 미충족)"
        lines += [f"### {x['n']}. {mark}", "", f"기준: {x['requirements']}", ""]
        lines += _evidence_lines(x.get("evidence"))
        lines += [f"판단 이유: {x['reason'] or x['error']}", ""]
    rub = rubric_view(job)
    if rub:
        lines += ["## 채점 기준", ""]
        if rub["auto"]:
            lines.append("PaperAgent가 논문을 읽고 설계한 채점 기준입니다. 논문의 핵심 기여마다 영역을 나누고, "
                         "영역마다 확인할 수 있는 세부 항목을 정했습니다.")
            if rub.get("evidence"):
                ev = rub["evidence"]
                lines.append(f"항목마다 그 기준이 나온 논문 문장을 함께 적었고, {ev['total']}개 중 {ev['found']}개는 "
                             "논문 원문에서 같은 문장을 찾아 확인했습니다. 찾지 못한 문장은 표시해 두었습니다.")
        else:
            lines.append("서버에 준비된 채점 기준을 썼습니다(PaperBench 예제는 논문 저자가 만든 기준).")
        judged_all = not job.get("judge_category")  # 코드 구현 항목이 없는 기준이면 전체를 검증했다
        target = "항목" if judged_all else "코드 구현 항목"
        limited = f"(실험용 설정에 따라 앞에서부터 {job['limit']}개만)" if job.get("limit") else ""
        lines += ["", f"전체 {len(rub['sections'])}개 영역, {rub['total']}개 항목 가운데 {target} "
                      f"{len(res['rows'])}개를 자동 검증했습니다{limited}. 전체 기준은 PAPERAGENT_RUBRIC.json에 있습니다.", ""]
        later = [(s["name"], i) for s in rub["sections"] for i in s["items"] if i["cat"][1] != "cd"]
        if later and not judged_all:
            lines += ["## 직접 실행해서 확인할 항목", "",
                      "코드를 실행한 뒤 확인할 항목입니다. 자동 검증에는 넣지 않았습니다.", ""]
            lines += [f"- [{i['cat'][0]}] {i['requirement']}" for _, i in later[:100]]
            if len(later) > 100:
                lines.append(f"- 외 {len(later) - 100}개 (PAPERAGENT_RUBRIC.json 참고)")
            lines.append("")
    gv = gpu_view(job)
    ran = bool(gv and gv.get("attempts"))
    if ran:
        lines += ["## GPU 실행", "",
                  f"학교 공용 GPU(JPUShare, {gv['tier']})에서 `reproduce.sh`를 최대 {gv['minutes']}분 동안 실행했습니다"
                  + (" (빠른 모드 QUICK=1: 논문보다 줄인 단계와 시드). " if gv.get("quick") else ". ")
                  + "오류가 나면 PaperAgent가 로그를 읽고 코드를 고쳐 다시 실행했습니다.", "",
                  f"- 결과: {gv['final']}. {gv.get('message') or ''}".rstrip()]
        for a in gv["attempts"]:
            line = f"- {a['n']}번째 실행: {a['state_label']}"
            if a.get("run"):
                line += f", {a['run']} 동안 실행"
            lines.append(line)
            if a.get("fix"):
                lines.append(f"  - 고친 내용: {a['fix']['diagnosis']} (파일: {', '.join(a['fix']['files']) or '없음'})")
        if gv.get("changed_files"):
            lines.append(f"- 실행하면서 고친 파일은 PAPERAGENT_GPU_RUN/fixed_files/에 있습니다: {', '.join(gv['changed_files'])}")
        lines.append("- 실행 기록은 PAPERAGENT_GPU_RUN/reproduce.log, 실행 중 생긴 파일은 PAPERAGENT_GPU_RUN/outputs/에 있습니다.")
        lines.append("")
        if gv.get("exec"):
            e = gv["exec"]
            lines += ["### 실행·결과 항목 검증", "",
                      f"실행 기록과 결과 파일을 보고 실행(Code Execution)과 결과 확인(Result Analysis) 항목 {e['judged']}개를 검증했습니다. "
                      f"{e['passed']}개 충족({e['pct']}%).", ""]
            for x in e["rows"]:
                mark = "검증 오류" if x["error"] else ("충족" if x["score"] else "미충족")
                lines += [f"#### {x['n']}. {mark}", "", f"기준: {x['requirements']}", ""]
                lines += _evidence_lines(x.get("evidence"))
                lines += [f"판단 이유: {x['reason'] or x['error']}", ""]
    lines += ["## 한계", ""]
    if ran:
        lines += [f"- 공용 GPU에서 {gv['minutes']}분까지만 실행했습니다. 논문 전체 실험이 더 오래 걸리면 결과가 일부만 나옵니다.",
                  "- 실행 환경에는 인터넷이 없어 데이터셋이나 사전 학습 모델을 내려받는 코드는 실패할 수 있습니다."]
    else:
        lines += ["- 코드는 실행하지 않았습니다. 검증은 코드 내용이 기준을 구현했는지만 봅니다.",
                  "- 실제 실행 결과와 논문 수치의 비교는 직접 해야 합니다."]
    lines += ["- 검증도 LLM이 하므로 틀릴 수 있습니다. 그래서 항목마다 판단 이유를 함께 적었습니다.", ""]
    return "\n".join(lines)


def _evidence_lines(ev: dict | None) -> list[str]:
    if not ev:
        return []
    note = "" if ev["found"] else " (논문 원문에서 같은 문장을 찾지 못했습니다. 직접 확인해 주세요)"
    return [f"논문 근거: “{ev['text']}”{note}", ""]
