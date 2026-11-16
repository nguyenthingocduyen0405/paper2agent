"""자동 수정: judge가 미충족으로 본 항목과 그 이유를 근거로 코드를 고친다(코드는 실행하지 않는다).

웹(orchestrator)이 첫 검증이 끝난 뒤 미충족 항목이 있으면 부른다. 고친 뒤 웹이 전체 항목을 처음부터 다시 검증한다.
1. 계획(호출 1회, JSON): 고칠 파일마다 어느 항목을 위해 무엇을 바꿀지 정한다.
   코드를 바꿔서는 충족할 수 없는 항목(실제 실행 결과가 있어야 하는 것 등)은 따로 적는다.
2. 고치기(파일마다 호출 1회): 에이전트의 P3와 같은 방법으로 파일 전체를 다시 쓴다(논문, 분석, 설계, 지금 코드를 함께 준다).
   고칠 수 있는 파일: src/ 아래 .py, README.md, reproduce.sh, requirements.txt. 최대 MAX_REPAIR_FILES개.
3. 점검: py_compile. 문법 오류가 나면 오류를 알려 주고 한 번 다시 쓴다. 그래도 안 되면 원래 파일로 되돌린다.
처음 코드는 <run>/submission_before_repair/에 남기고, 바뀐 내용은 <run>/repair/changes.diff에 적는다.
결과: <run>/repair/summary.json, plan.json, calls.jsonl

사용: python -m agent.repair --run-id <작업 id> --paper-dir <논문 폴더> --results results/<run>.json [--category "Code Development"]
"""
from __future__ import annotations

import argparse
import difflib
import json
import shutil
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from agent.llm import LLM, MAX_CALLS
from agent.pipeline import Agent

MAX_REPAIR_FILES = 6
MAX_REASON_CHARS = 1500
EDITABLE_TOP = ("README.md", "reproduce.sh", "requirements.txt")


class FileFix(BaseModel):
    path: str = Field(description="File to change or create, relative to the submission root")
    items: list[str] = Field(description="Ids of the failed items (F1, F2, ...) this change addresses")
    change: str = Field(description="What to change in this file, concretely, following the paper")


class NotFixable(BaseModel):
    item: str
    why: str


class RepairPlan(BaseModel):
    fixes: list[FileFix]
    not_fixable: list[NotFixable]


def failed_items(results_path: Path, category: str | None) -> list[dict]:
    leaves = json.loads(results_path.read_text()).get("leaves", {})
    out = []
    for lid, v in leaves.items():
        if v.get("error") or v.get("score") != 0:
            continue
        if category and v.get("category") not in (None, category):
            continue
        out.append({"fid": f"F{len(out) + 1}", "leaf_id": lid, "requirement": v.get("requirements", ""),
                    "reason": (v.get("reason") or "")[:MAX_REASON_CHARS]})
    return out


def clean_path(path: str) -> str | None:
    """고쳐도 되는 경로면 정리해서, 아니면 None."""
    p = path.strip().replace("\\", "/").lstrip("./")
    parts = PurePosixPath(p).parts
    if not p or ".." in parts or p.startswith("/"):
        return None
    if p in EDITABLE_TOP:
        return p
    if parts[0] != "src":
        p = "src/" + p
    return p if p.endswith(".py") else None


def compile_errors(agent: Agent) -> dict[str, str]:
    """문법 오류가 있는 파일 → 오류. 경로는 '/'로 맞춘다."""
    return {PurePosixPath(Path(k).as_posix()).as_posix(): v for k, v in agent._compile_errors().items()}


def snapshot(sub: Path, before: Path) -> None:
    if not before.exists():
        shutil.copytree(sub, before, ignore=shutil.ignore_patterns("__pycache__"))


def diff(before: Path, sub: Path, paths: list[str]) -> str:
    chunks = []
    for p in paths:
        old = (before / p).read_text().splitlines(keepends=True) if (before / p).is_file() else []
        new = (sub / p).read_text().splitlines(keepends=True) if (sub / p).is_file() else []
        chunks += difflib.unified_diff(old, new, f"a/{p}", f"b/{p}")
    return "".join(chunks)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--results", required=True, help="첫 검증 결과 results/<run>.json")
    ap.add_argument("--category", default=None, help="검증한 항목 종류(예: Code Development). 이 종류의 미충족 항목만 고친다")
    args = ap.parse_args()

    run_dir = Path("data/agent_runs") / args.run_id
    rep_dir = run_dir / "repair"
    rep_dir.mkdir(parents=True, exist_ok=True)
    paper_dir = Path(args.paper_dir)
    addendum = (paper_dir / "addendum.md").read_text() if (paper_dir / "addendum.md").exists() else None
    agent = Agent(run_dir, (paper_dir / "paper.md").read_text(), addendum)
    agent.llm = LLM(rep_dir)  # 수정 단계 호출은 따로 센다(에이전트 기록과 섞지 않는다)
    sub, before = agent.sub, run_dir / "submission_before_repair"

    items = failed_items(Path(args.results), args.category)
    summary: dict = {"failed_items": len(items), "fixes": [], "not_fixable": [], "changed_files": [], "reverted": []}
    if not items:
        (rep_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
        print("no failed items", flush=True)
        return
    snapshot(sub, before)

    listing = "\n\n".join(f"{x['fid']}: {x['requirement']}\n  Judge's reason: {x['reason']}" for x in items)
    plan_path = rep_dir / "plan.json"
    if plan_path.exists():
        plan = RepairPlan.model_validate_json(plan_path.read_text())
    else:
        text = agent.llm.chat("R-plan", agent._prefix() + agent._context() + [{"role": "user", "content": f"""An automated judge read your codebase and checked it against the grading rubric of the paper.
These rubric items were judged NOT satisfied. Each comes with the judge's reasoning:
<failed_items>
{listing}
</failed_items>

Plan how to change the code so that it satisfies these items, following the paper exactly.
- Group the changes by file. For each file to change (or create), list the item ids it addresses and describe the change concretely.
- Change at most {MAX_REPAIR_FILES} files: Python files under src/, README.md, reproduce.sh or requirements.txt.
- Only change what the items need, and keep everything that already works.
- Implement the requirement for real. Do not just add comments or docstrings that claim it is done, and never hard-code the paper's reported numbers as results.
- If you are sure the code already does what an item asks, do not change code only to convince the judge:
  list the item under not_fixable and say where it is implemented.
- If an item cannot be satisfied by changing code (for example it needs results from actually running the experiments),
  list it under not_fixable with the reason, and do not plan a change for it."""}],
            response_format={"type": "json_schema", "json_schema": {"name": "repair_plan", "schema": RepairPlan.model_json_schema()}})
        plan = RepairPlan.model_validate_json(text)
        plan_path.write_text(plan.model_dump_json(indent=1))

    by_fid = {x["fid"]: x for x in items}
    fixes: list[tuple[str, FileFix]] = []
    for f in plan.fixes:
        p = clean_path(f.path)
        if p and p not in [x[0] for x in fixes] and any(i in by_fid for i in f.items):
            fixes.append((p, f))
    fixes = fixes[:MAX_REPAIR_FILES]
    summary["not_fixable"] = [{"leaf_id": by_fid[n.item]["leaf_id"], "requirement": by_fid[n.item]["requirement"], "why": n.why}
                              for n in plan.not_fixable if n.item in by_fid]

    for path, f in fixes:
        if agent.llm.calls_so_far() >= MAX_CALLS - 1:
            break
        old = (sub / path).read_text() if (sub / path).is_file() else None
        targets = "\n".join(f"- {by_fid[i]['requirement']}\n  Judge's reason: {by_fid[i]['reason']}" for i in f.items if i in by_fid)
        verb = "Change" if old is not None else "Create"
        instruction = (f"{verb} `{path}` so that the codebase satisfies these rubric items:\n{targets}\n\n"
                       f"Planned change for this file: {f.change}\n\n"
                       "Implement it for real; do not only add comments. Keep everything else in the file as it is, "
                       "and keep it consistent with the other files (names, imports, command-line options).")
        agent._write_file("R-fix", path, instruction)
        if path.endswith(".py"):
            errors = compile_errors(agent)
            if path in errors:
                agent._write_file("R-fix", path, f"The new version of `{path}` fails `python -m py_compile` with:\n"
                                                 f"{errors[path]}\nFix it. Keep the planned change: {f.change}")
                if path in compile_errors(agent):  # 그래도 안 되면 원래대로
                    if old is None:
                        (sub / path).unlink()
                    else:
                        (sub / path).write_text(old)
                    summary["reverted"].append(path)
                    agent.log(f"R reverted {path} (syntax error)")
                    continue
        new = (sub / path).read_text()
        if new != old:
            summary["changed_files"].append(path)
            summary["fixes"].append({"path": path, "change": f.change, "new_file": old is None,
                                     "items": [by_fid[i]["leaf_id"] for i in f.items if i in by_fid]})
            agent.log(f"R changed {path}")
    if (sub / "reproduce.sh").exists():
        (sub / "reproduce.sh").chmod(0o755)

    (rep_dir / "changes.diff").write_text(diff(before, sub, summary["changed_files"]))
    calls = [json.loads(line) for line in (rep_dir / "calls.jsonl").read_text().splitlines() if line.strip()] \
        if (rep_dir / "calls.jsonl").exists() else []
    summary["usage"] = {"calls": len(calls), "input_tokens": sum(c["input_tokens"] for c in calls),
                        "output_tokens": sum(c["output_tokens"] for c in calls), "sec": round(sum(c["sec"] for c in calls), 1)}
    (rep_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps({k: summary[k] for k in ("failed_items", "changed_files", "reverted", "usage")}, ensure_ascii=False), flush=True)
    print(f"not fixable: {len(summary['not_fixable'])}", flush=True)


if __name__ == "__main__":
    main()
