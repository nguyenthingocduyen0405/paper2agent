"""통계와 비교: DB에 쌓인 기록을 SQL로 묶는다.

- overview(): 작업 수, 단계별 시간, 단계·LLM별 토큰, 항목 종류별 충족률, 논문별 결과, (관리자) 사용자별 사용량
- compare(): 같은 논문, 같은 채점 기준으로 돌린 작업들의 항목별 결과를 나란히 놓는다
모의 채점(mock) 작업은 토큰·충족률 통계에서 뺀다(LLM을 부르지 않으므로).
"""
from __future__ import annotations

from sqlalchemy import and_, case, func, select

from web import db, store

STAGE_LABEL = {"prepare": "논문 준비(채점 기준 설계)", "agent": "코드 작성", "judge": "자동 검증",
               "gpu_fix": "GPU 실행 중 오류 수정", "exec_judge": "실행·결과 항목 검증"}
GPU_STAGES = ("gpu_fix", "exec_judge")  # GPU 실행을 한 작업만 쓴다: 그 작업 수로 나눈다
STAGE_ORDER = ("prepare", "agent", "judge")


def _avg(x):
    return round(x, 1) if x is not None else None


def overview(user: dict) -> dict:
    j, c, s, u, p = db.jobs, db.llm_calls, db.item_scores, db.users, db.papers
    real = and_(j.c.mock.is_(False))
    done_real = and_(real, j.c.status == "done", j.c.skip_agent.is_(False))
    with db.engine().connect() as conn:
        counts = {row.status: row.n for row in conn.execute(
            select(j.c.status, func.count().label("n")).where(real).group_by(j.c.status))}
        mock_n = conn.execute(select(func.count()).select_from(j).where(j.c.mock.is_(True))).scalar() or 0
        t = conn.execute(select(func.count().label("n"), func.avg(j.c.prepare_sec).label("prepare"),
                                func.avg(j.c.agent_sec).label("agent"), func.avg(j.c.judge_sec).label("judge"),
                                func.avg(j.c.finished_at - j.c.created_at).label("total"))
                         .where(done_real)).one()
        times = {"n": t.n, "prepare": _avg(t.prepare), "agent": _avg(t.agent), "judge": _avg(t.judge),
                 "total": _avg(t.total)}

        # 단계별 토큰: 끝난 실제 작업 한 건당 평균
        n_done = t.n or 0
        stage_rows = conn.execute(
            select(c.c.stage, func.count(func.distinct(c.c.job_id)).label("jobs"),
                   func.sum(c.c.calls).label("calls"), func.sum(c.c.input_tokens).label("inp"),
                   func.sum(c.c.cached_tokens).label("cached"), func.sum(c.c.output_tokens).label("out"))
            .select_from(c.join(j, c.c.job_id == j.c.id)).where(done_real).group_by(c.c.stage)).all()
        by_stage = {r.stage: r for r in stage_rows}
        tokens = []
        for st in STAGE_ORDER:
            r = by_stage.get(st)
            if r and n_done:
                tokens.append({"stage": STAGE_LABEL[st], "calls": _avg(r.calls / n_done), "input": round(r.inp / n_done),
                               "cached": round(r.cached / n_done), "output": round(r.out / n_done),
                               "cache_pct": round(100 * r.cached / r.inp) if r.inp else 0})
        if tokens:
            tot = {k: sum(x[k] for x in tokens) for k in ("calls", "input", "cached", "output")}
            tokens.append({"stage": "합계", **tot, "calls": round(tot["calls"], 1),
                           "cache_pct": round(100 * tot["cached"] / tot["input"]) if tot["input"] else 0, "total": True})
        for st in GPU_STAGES:
            r = by_stage.get(st)
            if r and r.jobs:
                tokens.append({"stage": f"{STAGE_LABEL[st]} (GPU 실행한 {r.jobs}건 평균)", "calls": _avg(r.calls / r.jobs),
                               "input": round(r.inp / r.jobs), "cached": round(r.cached / r.jobs),
                               "output": round(r.out / r.jobs), "cache_pct": round(100 * r.cached / r.inp) if r.inp else 0})

        g = db.gpu_runs
        gq = conn.execute(select(g.c.final, func.count().label("n"), func.avg(g.c.run_sec).label("run"),
                                 func.avg(g.c.queue_sec).label("queue"), func.avg(g.c.attempts).label("attempts"),
                                 func.sum(g.c.exec_passed).label("passed"), func.sum(g.c.exec_judged).label("judged"))
                          .where(g.c.final.is_not(None)).group_by(g.c.final)).all()
        gpu_runs = {"rows": [{"final": r.final, "n": r.n, "run": r.run, "queue": r.queue,
                              "attempts": _avg(r.attempts), "pct": round(100 * (r.passed or 0) / r.judged) if r.judged else None}
                             for r in gq],
                    "total": sum(r.n for r in gq)}

        agent_steps = [{"step": r.step, "calls": _avg(r.calls / n_done), "input": round(r.inp / n_done),
                        "output": round(r.out / n_done)}
                       for r in conn.execute(
                           select(c.c.step, func.sum(c.c.calls).label("calls"), func.sum(c.c.input_tokens).label("inp"),
                                  func.sum(c.c.output_tokens).label("out"))
                           .select_from(c.join(j, c.c.job_id == j.c.id)).where(done_real, c.c.stage == "agent")
                           .group_by(c.c.step).order_by(c.c.step)).all()] if n_done else []

        models = [{"model": r.model or "?", "jobs": r.jobs, "input": round(r.inp / r.jobs), "output": round(r.out / r.jobs)}
                  for r in conn.execute(
                      select(j.c.llm_model.label("model"), func.count(func.distinct(c.c.job_id)).label("jobs"),
                             func.sum(c.c.input_tokens).label("inp"), func.sum(c.c.output_tokens).label("out"))
                      .select_from(c.join(j, c.c.job_id == j.c.id)).where(done_real).group_by(j.c.llm_model)).all()]

        cats = [{"category": r.category or "기타", "n": r.n, "passed": r.passed or 0, "errors": r.errors or 0,
                 "pct": round(100 * (r.passed or 0) / (r.n - (r.errors or 0))) if r.n - (r.errors or 0) else 0}
                for r in conn.execute(
                    select(s.c.category, func.count().label("n"), func.sum(s.c.score).label("passed"),
                           func.sum(case((s.c.score.is_(None), 1), else_=0)).label("errors"))
                    .select_from(s.join(j, s.c.job_id == j.c.id)).where(real, j.c.status == "done")
                    .group_by(s.c.category)).all()]

        paper_q = (select(p.c.id, p.c.title, p.c.kind, func.count(j.c.id).label("runs"),
                          func.sum(j.c.score_passed).label("passed"), func.sum(j.c.score_judged).label("judged"),
                          func.max(j.c.created_at).label("last"))
                   .select_from(p.join(j, j.c.paper_id == p.c.id))
                   .where(j.c.status == "done").group_by(p.c.id, p.c.title, p.c.kind).order_by(func.max(j.c.created_at).desc()))
        if user["role"] not in store.SEE_ALL_ROLES:
            paper_q = paper_q.where(j.c.user_id == user["id"])
        papers = [{"id": r.id, "title": r.title or "(제목 없음)", "kind": r.kind, "runs": r.runs, "last": r.last,
                   "pct": round(100 * (r.passed or 0) / r.judged) if r.judged else None}
                  for r in conn.execute(paper_q).all()]

        users = []
        if user["role"] == "admin":
            tok = (select(c.c.job_id, func.sum(c.c.input_tokens).label("inp"), func.sum(c.c.output_tokens).label("out"))
                   .group_by(c.c.job_id).subquery())
            users = [{"username": r.username, "role": r.role, "jobs": r.jobs or 0, "done": r.done or 0,
                      "failed": r.failed or 0, "input": r.inp or 0, "output": r.out or 0, "last": r.last}
                     for r in conn.execute(
                         select(u.c.username, u.c.role, func.count(j.c.id).label("jobs"),
                                func.sum(case((j.c.status == "done", 1), else_=0)).label("done"),
                                func.sum(case((j.c.status == "failed", 1), else_=0)).label("failed"),
                                func.sum(tok.c.inp).label("inp"), func.sum(tok.c.out).label("out"),
                                func.max(j.c.created_at).label("last"))
                         .select_from(u.outerjoin(j, j.c.user_id == u.c.id).outerjoin(tok, tok.c.job_id == j.c.id))
                         .group_by(u.c.id, u.c.username, u.c.role).order_by(u.c.id)).all()]
    return {"counts": counts, "mock": mock_n, "times": times, "tokens": tokens, "agent_steps": agent_steps,
            "gpu_runs": gpu_runs,
            "models": models, "cats": cats, "papers": papers, "users": users}


def compare(paper_id: int, user: dict, rubric_id: int | None = None) -> dict | None:
    j, s, ri, r, c = db.jobs, db.item_scores, db.rubric_items, db.rubrics, db.llm_calls
    with db.engine().connect() as conn:
        paper = conn.execute(select(db.papers).where(db.papers.c.id == paper_id)).first()
        if paper is None:
            return None
        jq = select(j).where(j.c.paper_id == paper_id).order_by(j.c.created_at)
        if user["role"] not in store.SEE_ALL_ROLES:
            jq = jq.where(j.c.user_id == user["id"])
        all_jobs = conn.execute(jq).all()
        if not all_jobs:
            return None
        rubric_counts: dict[int, int] = {}
        for row in all_jobs:
            if row.rubric_id and row.status == "done":
                rubric_counts[row.rubric_id] = rubric_counts.get(row.rubric_id, 0) + 1
        rubric_rows = {row.id: row for row in conn.execute(
            select(r.c.id, r.c.origin, r.c.job_id, r.c.leaves, r.c.created_at).where(r.c.paper_id == paper_id)).all()}
        if rubric_id not in rubric_counts:
            rubric_id = max(rubric_counts, key=lambda k: (rubric_counts[k], k)) if rubric_counts else None
        cols = [row for row in all_jobs if row.rubric_id == rubric_id and row.status == "done"] if rubric_id else []
        ids = [row.id for row in cols]
        scores = {}
        tokens = {}
        if ids:
            for row in conn.execute(select(s.c.job_id, s.c.leaf_id, s.c.score, s.c.reason, s.c.error)
                                    .where(s.c.job_id.in_(ids))).all():
                scores[(row.job_id, row.leaf_id)] = row
            for row in conn.execute(select(c.c.job_id, func.sum(c.c.input_tokens).label("inp"),
                                           func.sum(c.c.output_tokens).label("out"))
                                    .where(c.c.job_id.in_(ids)).group_by(c.c.job_id)).all():
                tokens[row.job_id] = (row.inp or 0, row.out or 0)
        items = conn.execute(select(ri).where(ri.c.rubric_id == rubric_id).order_by(ri.c.position)).all() if rubric_id else []
    judged = {lid for (_, lid) in scores}
    rows = []
    for it in items:
        if it.leaf_id not in judged:
            continue
        cells = []
        for col in cols:
            v = scores.get((col.id, it.leaf_id))
            if v is None:
                cells.append({"mark": "", "cls": "", "title": "검증하지 않음"})
            elif v.error:
                cells.append({"mark": "오류", "cls": "wait", "title": v.error})
            else:
                cells.append({"mark": "충족" if v.score else "미충족", "cls": "ok" if v.score else "bad", "title": v.reason or ""})
        marks = {x["mark"] for x in cells if x["mark"] in ("충족", "미충족")}
        rows.append({"requirement": it.requirement, "section": it.section, "cells": cells, "differs": len(marks) > 1})
    columns = [{"id": col.id, "created_at": col.created_at, "model": "모의 채점" if col.mock else (col.llm_model or "?"),
                "passed": col.score_passed, "judged": col.score_judged,
                "pct": round(100 * col.score_passed / col.score_judged) if col.score_judged else None,
                "minutes": round((col.finished_at - col.created_at) / 60, 1) if col.finished_at else None,
                "input": tokens.get(col.id, (0, 0))[0], "output": tokens.get(col.id, (0, 0))[1]} for col in cols]
    others = [{"id": row.id, "status": row.status, "created_at": row.created_at, "rubric_id": row.rubric_id}
              for row in all_jobs if row.id not in ids]
    rubrics = [{"id": rid, "jobs": n, "origin": rubric_rows[rid].origin if rid in rubric_rows else "?",
                "job_id": rubric_rows[rid].job_id if rid in rubric_rows else None}
               for rid, n in sorted(rubric_counts.items())]
    return {"paper": paper, "rubric_id": rubric_id, "rubrics": rubrics, "columns": columns, "rows": rows,
            "differs": sum(1 for x in rows if x["differs"]), "others": others}


def paper_job_count(paper_id: int | None, user: dict) -> int:
    if not paper_id:
        return 0
    q = select(func.count()).select_from(db.jobs).where(db.jobs.c.paper_id == paper_id, db.jobs.c.status == "done")
    if user["role"] not in store.SEE_ALL_ROLES:
        q = q.where(db.jobs.c.user_id == user["id"])
    with db.engine().connect() as conn:
        return conn.execute(q).scalar() or 0
