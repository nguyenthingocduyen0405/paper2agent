"""에이전트 P1~P5: 논문을 읽고 재현 코드를 쓴다(실행하지 않음, Code-Dev 방식).

P1 분석 → analysis.md
P2 설계 → plan.json (만들 파일 목록, JSON 스키마 출력)
P3 작성 → 파일마다 호출 1회, 코드 블록 1개만 받아 저장
P4 점검 → py_compile 문법 검사. 오류 파일은 다시 만들고, 참조됐지만 없는 모듈은 새로 만든다
P5 마무리 → requirements.txt, reproduce.sh, README.md

모든 중간 결과는 run 폴더에 저장한다. 다시 실행하면 이미 있는 결과는 건너뛴다.
모든 호출에서 논문을 맨 앞에 둔다(캐시 할인).

사용: python -m agent.pipeline --run-id a1
"""
import argparse
import ast
import json
import py_compile
import re
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field

from agent.llm import LLM, MAX_CALLS

SYSTEM = """You are an expert ML research engineer. Your job is to reproduce a research paper by writing a complete, runnable codebase.
You cannot run code or browse the web. You only have the paper (and its addendum, if given).
Write real, working implementations, not placeholders or TODOs. Follow the paper's details exactly (datasets, models, prompts, hyperparameters, metrics)."""

MAX_FILES = 12  # P2가 제안할 수 있는 src 파일 수 상한
MAX_FIX_ROUNDS = 2  # P4 반복 횟수


class PlannedFile(BaseModel):
    path: str = Field(description="Path relative to the submission root, under src/, ending in .py")
    purpose: str = Field(description="What this file implements, with the concrete paper details it must follow")


class Plan(BaseModel):
    files: list[PlannedFile]


def extract_block(text: str) -> str:
    """응답에서 파일 내용을 꺼낸다. 전체가 ``` 로 감싸져 있으면 바깥 울타리만 벗긴다(README 안의 코드 블록 보호)."""
    s = text.strip()
    if s.startswith("```"):
        body = s.split("\n", 1)[1] if "\n" in s else ""
        end = body.rfind("```")
        return (body[:end] if end != -1 else body).rstrip() + "\n"
    m = re.search(r"```[^\n]*\n(.*?)```", s, re.S)
    return (m.group(1) if m else s).rstrip() + "\n"


class Agent:
    def __init__(self, run_dir: Path, paper_md: str, addendum: str | None):
        self.run_dir = run_dir
        self.sub = run_dir / "submission"
        self.sub.mkdir(parents=True, exist_ok=True)
        self.paper_md = paper_md
        self.addendum = addendum
        self.llm = LLM(run_dir)

    # 모든 호출의 앞부분: 시스템 + 논문 + addendum (같은 내용이라 캐시된다)
    def _prefix(self) -> list[dict]:
        msgs = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"The paper is below:\n<paper>\n{self.paper_md}\n</paper>"},
        ]
        if self.addendum:
            msgs.append({"role": "user", "content": f"The addendum below clarifies the paper and the scope of the reproduction. Follow it:\n<addendum>\n{self.addendum}\n</addendum>"})
        return msgs

    def _context(self) -> list[dict]:
        """P2 이후 호출의 공통 맥락: 분석 + 설계 + 지금까지 쓴 파일."""
        msgs = [{"role": "user", "content": f"Your analysis of the paper:\n<analysis>\n{(self.run_dir / 'analysis.md').read_text()}\n</analysis>"}]
        plan_path = self.run_dir / "plan.json"
        if plan_path.exists():
            msgs.append({"role": "user", "content": f"The planned files:\n<plan>\n{plan_path.read_text()}\n</plan>"})
        files = sorted(p for p in self.sub.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        if files:
            body = "\n\n".join(f"<FILE:{p.relative_to(self.sub)}>\n{p.read_text()}\n</FILE:{p.relative_to(self.sub)}>" for p in files)
            msgs.append({"role": "user", "content": f"Files written so far:\n<files>\n{body}\n</files>"})
        return msgs

    def log(self, msg: str) -> None:
        print(f"[{self.llm.calls_so_far():2d}/{MAX_CALLS} calls] {msg}", flush=True)

    # ---------- P1 ----------
    def p1_analyze(self) -> None:
        out = self.run_dir / "analysis.md"
        if out.exists():
            return self.log("P1 skip (analysis.md exists)")
        text = self.llm.chat("P1", self._prefix() + [{"role": "user", "content": """Analyze the paper for reproduction. Write a markdown document with these sections:
1. Core contributions (what must be reproduced)
2. Methods: each method/algorithm step by step, with exact formulas and parameters
3. Experimental setup: datasets (names, splits, sizes), models, prompts, sampling settings, hyperparameters
4. Evaluation: metrics and how results are computed and reported (which tables/figures)
5. Components of a codebase that reproduces this
Be concrete and complete. Output only the markdown."""}])
        out.write_text(text)
        self.log("P1 done → analysis.md")

    # ---------- P2 ----------
    def p2_plan(self) -> None:
        out = self.run_dir / "plan.json"
        if out.exists():
            return self.log("P2 skip (plan.json exists)")
        text = self.llm.chat("P2", self._prefix() + self._context() + [{"role": "user", "content": f"""Design the codebase. List the Python source files to write, at most {MAX_FILES}, all under src/.
Each file's purpose must name the concrete details it implements (datasets, models, parameters, methods).
Do not include README.md, reproduce.sh or requirements.txt; they are written later.
Order the files so that a file comes after the files it imports."""}],
            response_format={"type": "json_schema", "json_schema": {"name": "plan", "schema": Plan.model_json_schema()}})
        (self.run_dir / "plan_raw.json").write_text(text)
        plan = Plan.model_validate_json(text)
        files = []
        for f in plan.files:
            f.path = f.path.strip().lstrip("./")
            if not f.path.startswith("src/"):
                f.path = "src/" + f.path
            if f.path.endswith(".py"):
                files.append(f)
        files = files[:MAX_FILES]
        if not files:
            raise RuntimeError(f"P2 produced no usable files; see {self.run_dir / 'plan_raw.json'}")
        out.write_text(json.dumps({"files": [f.model_dump() for f in files]}, indent=1))
        self.log(f"P2 done → plan.json ({len(files)} files)")

    # ---------- P3 ----------
    def _write_file(self, step: str, path: str, instruction: str) -> None:
        text = self.llm.chat(step, self._prefix() + self._context() + [{"role": "user", "content": f"""{instruction}

Write the complete file `{path}`.
Respond with exactly ONE fenced code block containing the whole file, and nothing else."""}])
        target = self.sub / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(extract_block(text))

    def p3_write(self) -> None:
        plan = json.loads((self.run_dir / "plan.json").read_text())
        for f in plan["files"]:
            if (self.sub / f["path"]).exists():
                continue
            self._write_file("P3", f["path"], f"Purpose of this file: {f['purpose']}")
            self.log(f"P3 wrote {f['path']}")

    # ---------- P4 ----------
    def _compile_errors(self) -> dict[str, str]:
        """py_compile로 문법 검사. .pyc는 임시 파일에 써서 제출물에 __pycache__가 생기지 않게 한다."""
        errors = {}
        with tempfile.TemporaryDirectory() as tmp:
            for p in sorted(self.sub.rglob("*.py")):
                try:
                    py_compile.compile(str(p), cfile=str(Path(tmp) / "x.pyc"), doraise=True)
                except py_compile.PyCompileError as e:
                    errors[str(p.relative_to(self.sub))] = str(e)[-1500:]
        return errors

    def _missing_modules(self) -> dict[str, str]:
        """src 안에서 import했는데 파일이 없는 로컬 모듈 → {경로: 참조한 파일}."""
        existing = {str(p.relative_to(self.sub)) for p in self.sub.rglob("*.py")}
        local_stems = {Path(p).stem for p in existing} | {
            Path(f["path"]).stem for f in json.loads((self.run_dir / "plan.json").read_text())["files"]}
        missing = {}
        for p in self.sub.rglob("*.py"):
            try:
                tree = ast.parse(p.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module == "src":
                    names += [a.name for a in node.names]  # from src import x
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.level > 0 or node.module.split(".")[0] in ("src", *local_stems):
                        names.append(node.module.removeprefix("src."))
                elif isinstance(node, ast.Import):
                    names += [a.name.removeprefix("src.") for a in node.names if a.name.split(".")[0] == "src"]
                for name in names:
                    path = "src/" + name.replace(".", "/") + ".py"
                    if name and path not in existing and not (self.sub / path).with_suffix("").is_dir():
                        missing.setdefault(path, str(p.relative_to(self.sub)))
        return missing

    def p4_check(self) -> dict:
        report = {"rounds": []}
        for _ in range(MAX_FIX_ROUNDS):
            errors, missing = self._compile_errors(), self._missing_modules()
            report["rounds"].append({"compile_errors": list(errors), "missing": list(missing)})
            if not errors and not missing:
                break
            for path, err in errors.items():
                self._write_file("P4", path, f"The current version of `{path}` fails `python -m py_compile` with:\n{err}\nFix it. Keep everything else the same.")
                self.log(f"P4 fixed {path}")
            for path, ref in missing.items():
                self._write_file("P4", path, f"`{ref}` imports from `{path}`, but this file does not exist. Implement it, providing every name that the other files import from it.")
                self.log(f"P4 added {path}")
        final = self._compile_errors()
        report["final_compile_errors"] = list(final)
        report["final_missing"] = list(self._missing_modules())
        self.log(f"P4 done: compile errors={len(final)}, missing={len(report['final_missing'])}")
        return report

    # ---------- P5 ----------
    def p5_finish(self) -> None:
        tasks = [
            ("requirements.txt", "List the pip packages (with versions where important) needed by the code in src/."),
            ("reproduce.sh", "Write a bash script that installs requirements and runs the full reproduction end to end, producing all results (tables) in the paper. It must use the code in src/. Assume API keys are provided as environment variables."),
            ("README.md", "Write a README: what the paper is, how the code is organized (each file in src/), how to run reproduce.sh, and which paper results each part reproduces."),
        ]
        for path, instruction in tasks:
            if (self.sub / path).exists():
                continue
            self._write_file("P5", path, instruction)
            self.log(f"P5 wrote {path}")
        (self.sub / "reproduce.sh").chmod(0o755)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--paper-dir", default="data/papers/semantic-self-consistency")
    args = ap.parse_args()

    run_dir = Path("data/agent_runs") / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # PaperBench와 같이 addendum.md는 에이전트에게 준다. judge.addendum.md는 judge 전용이라 주지 않는다.
    paper_dir = Path(args.paper_dir)
    addendum = (paper_dir / "addendum.md").read_text() if (paper_dir / "addendum.md").exists() else None
    agent = Agent(run_dir, (paper_dir / "paper.md").read_text(), addendum)

    t = time.time()
    agent.p1_analyze()
    agent.p2_plan()
    agent.p3_write()
    p4 = agent.p4_check()
    agent.p5_finish()
    elapsed = time.time() - t

    # 기록: 재시작해도 calls.jsonl은 누적되므로 호출·토큰은 전체 run 기준이다
    calls = [json.loads(l) for l in (run_dir / "calls.jsonl").open()] if (run_dir / "calls.jsonl").exists() else []
    by_step: dict[str, dict] = {}
    for c in calls:
        s = by_step.setdefault(c["step"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "sec": 0.0})
        for key in ("input_tokens", "output_tokens", "cached_tokens", "sec"):
            s[key] += c[key]
        s["calls"] += 1
    result = {
        "run_id": args.run_id,
        "model": agent.llm.model,
        "paper_dir": str(paper_dir),
        "used_addendum": addendum is not None,
        "calls": len(calls),
        "call_limit": MAX_CALLS,
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "cached_tokens": sum(c["cached_tokens"] for c in calls),
        "llm_sec": round(sum(c["sec"] for c in calls), 1),
        "wall_sec_last_invocation": round(elapsed, 1),
        "by_step": by_step,
        "truncated_outputs": sum(1 for c in calls if c["finish_reason"] == "length"),
        "p4": p4,
        "submission_dir": str(agent.sub),
        "files": sorted(str(p.relative_to(agent.sub)) for p in agent.sub.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
    }
    out = Path("results") / f"agent_run_{args.run_id}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k not in ("by_step", "p4")}, ensure_ascii=False, indent=1))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
