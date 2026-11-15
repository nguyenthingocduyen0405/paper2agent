"""논문 준비: PDF를 markdown으로 바꾸고, 채점 기준이 없으면 논문을 읽고 만든다.

웹에서 PDF를 올렸거나 rubric.json 없이 올렸을 때 에이전트(P1~P5)보다 먼저 돈다.
결과는 paper-dir 안에 생긴다. 이미 있으면 건너뛴다(다시 실행해도 안전하다).
  paper.md     PDF에서 뽑은 본문 (pymupdf4llm)
  rubric.json  PaperBench 형식 채점 기준 (LLM 1회 호출, JSON 스키마 출력)
  meta.json    {"title": ..., "rubric_generated": true/false}
  prepare/calls.jsonl  LLM 호출 기록

사용: python -m agent.prepare --paper-dir data/web/jobs/<id>/paper [--make-rubric]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agent.llm import LLM

MIN_ITEMS, MAX_ITEMS = 20, 40
# 논문 본문 상한(글자 수). 에이전트는 매 호출에 논문 전체와 지금까지 쓴 코드를 함께 보내므로 넉넉히 남겨 둔다.
# PaperBench 논문 가운데 가장 긴 것이 약 19만 자다.
MAX_PAPER_CHARS = 250_000
MIN_PAPER_CHARS = 2_000
REFERENCES_RE = re.compile(r"^#{1,6}\s*(\d+\.?\s*)?(references|bibliography|참고\s*문헌)\b.*$", re.I | re.M)

SYSTEM = """You are an expert ML researcher who writes grading rubrics for paper reproductions, in the style of the PaperBench benchmark.
A reproduction is a codebase written from scratch that implements the paper's methods and experiments.
An automated judge reads the submitted code and decides pass or fail for each rubric item, so every item must be specific and checkable."""

INSTRUCTION = f"""Write the grading rubric for reproducing this paper.

Group the items into sections, one per core contribution or experiment of the paper (usually 3 to 8 sections).
Each item must:
- state exactly one requirement that can be judged pass or fail;
- be specific to this paper: name the concrete method step, formula, architecture, dataset, split, hyperparameter value or metric
  (write "The learning rate is set to 1e-3 for all runs", not "Hyperparameters follow the paper");
- be understandable on its own, without reading the other items.

Item categories:
- "Code Development": the code implements something (a method or one of its steps, a model, a data pipeline, a hyperparameter setting, an evaluation metric). Most items must be this category.
- "Code Execution": running the code produces something (a training run completes, a results file is written).
- "Result Analysis": the produced results match or support a specific claim of the paper (a number in a table, a trend in a figure).

Write {MIN_ITEMS} to {MAX_ITEMS} items in total, at least two thirds of them "Code Development".
Cover every core contribution. Do not write items about the writing, related work, or anything the paper (or the addendum, if given) puts out of scope.
Also return the paper's title exactly as written in the paper, in normal capitalization."""


class Item(BaseModel):
    requirement: str = Field(description="One specific, checkable requirement")
    category: Literal["Code Development", "Code Execution", "Result Analysis"]


class Section(BaseModel):
    name: str = Field(description="The contribution or experiment this group covers, as one sentence")
    items: list[Item]


class RubricDraft(BaseModel):
    title: str
    sections: list[Section]


def pdf_to_markdown(pdf: Path) -> str:
    import pymupdf4llm  # 에이전트 이미지에만 있다

    return pymupdf4llm.to_markdown(str(pdf))


def write_atomic(path: Path, text: str) -> None:
    """반쯤 쓴 파일이 남지 않게 임시 파일에 쓰고 바꿔 넣는다(Pod가 중간에 죽어도 다음 실행이 깨진 파일을 보지 않는다)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def fit_paper(md: str) -> str:
    """너무 길면 참고문헌부터 뒤를 잘라 본다. 그래도 길면 실패로 끝낸다."""
    if len(md) <= MAX_PAPER_CHARS:
        return md
    refs = [m.start() for m in REFERENCES_RE.finditer(md)]
    if refs and refs[-1] > len(md) // 3:
        md = md[:refs[-1]].rstrip() + "\n"
        print(f"paper trimmed at the references section → {len(md)} chars", flush=True)
    if len(md) > MAX_PAPER_CHARS:
        raise SystemExit(f"논문이 너무 깁니다(본문 {len(md):,}자, 최대 {MAX_PAPER_CHARS:,}자). "
                         "부록을 뺀 본문만 PDF로 올려 주세요.")
    return md


def first_heading(md: str) -> str | None:
    m = re.search(r"^#{1,2}\s+(.+?)\s*$", md[:5000], re.M)
    return " ".join(m.group(1).replace("*", "").split()) if m else None


def build_rubric(draft: RubricDraft) -> dict:
    """LLM 초안(섹션 > 항목)을 PaperBench 루브릭 트리로 바꾼다. 리프는 sub_tasks가 빈 노드."""
    def node(requirements: str, sub_tasks: list, category: str | None = None) -> dict:
        return {"id": str(uuid.uuid4()), "requirements": requirements.strip(), "weight": 1,
                "sub_tasks": sub_tasks, "task_category": category, "finegrained_task_category": None}

    sections = [node(s.name, [node(i.requirement, [], i.category) for i in s.items if i.requirement.strip()])
                for s in draft.sections]
    sections = [s for s in sections if s["sub_tasks"]]
    leaves = [leaf for s in sections for leaf in s["sub_tasks"]]
    if not any(leaf["task_category"] == "Code Development" for leaf in leaves):
        raise ValueError("the generated rubric has no Code Development items")
    return node(f'The core contributions of the paper "{draft.title.strip()}" have been reproduced.', sections)


def make_rubric(paper_dir: Path, paper_md: str, addendum: str | None) -> RubricDraft:
    run_dir = paper_dir / "prepare"
    run_dir.mkdir(exist_ok=True)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"The paper is below:\n<paper>\n{paper_md}\n</paper>"},
    ]
    if addendum:
        messages.append({"role": "user", "content": f"The addendum below clarifies the paper and the scope of the reproduction:\n<addendum>\n{addendum}\n</addendum>"})
    messages.append({"role": "user", "content": INSTRUCTION})
    text = LLM(run_dir).chat("rubric", messages, response_format={
        "type": "json_schema", "json_schema": {"name": "rubric", "schema": RubricDraft.model_json_schema()}})
    (run_dir / "rubric_raw.json").write_text(text)
    return RubricDraft.model_validate_json(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--make-rubric", action="store_true", help="rubric.json이 없으면 논문을 읽고 만든다")
    args = ap.parse_args()
    paper_dir = Path(args.paper_dir)
    meta_path = paper_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    md_path, pdf_path = paper_dir / "paper.md", paper_dir / "paper.pdf"
    if not md_path.exists():
        if not pdf_path.exists():
            raise SystemExit("paper.md and paper.pdf are both missing")
        print("PDF → markdown ...", flush=True)
        md = pdf_to_markdown(pdf_path)
        if len(md.strip()) < MIN_PAPER_CHARS:
            raise SystemExit("PDF에서 글자를 거의 뽑지 못했습니다. 스캔본(이미지)일 수 있습니다.")
        write_atomic(md_path, fit_paper(md))
        print(f"paper.md: {len(md)} chars", flush=True)
    paper_md = md_path.read_text()
    if len(paper_md.strip()) < MIN_PAPER_CHARS:
        raise SystemExit("논문 내용이 너무 짧습니다. 논문 본문 전체를 올려 주세요.")
    if len(paper_md) > MAX_PAPER_CHARS:  # 직접 올린 markdown
        paper_md = fit_paper(paper_md)
        write_atomic(md_path, paper_md)
    if not meta.get("title"):
        meta["title"] = first_heading(paper_md)

    rubric_path = paper_dir / "rubric.json"
    if args.make_rubric and not rubric_path.exists():
        print("rubric: asking the LLM ...", flush=True)
        addendum_path = paper_dir / "addendum.md"
        draft = make_rubric(paper_dir, paper_md, addendum_path.read_text() if addendum_path.exists() else None)
        rubric = build_rubric(draft)
        write_atomic(rubric_path, json.dumps(rubric, ensure_ascii=False, indent=2))
        meta.update(title=draft.title.strip() or meta.get("title"), rubric_generated=True)
        n = sum(len(s["sub_tasks"]) for s in rubric["sub_tasks"])
        code_dev = sum(1 for s in rubric["sub_tasks"] for leaf in s["sub_tasks"] if leaf["task_category"] == "Code Development")
        print(f"rubric.json: {len(rubric['sub_tasks'])} sections, {n} items ({code_dev} Code Development)", flush=True)
    if not rubric_path.exists():
        raise SystemExit("rubric.json is missing (run with --make-rubric to generate it)")

    write_atomic(meta_path, json.dumps(meta, ensure_ascii=False))
    print(f"title: {meta.get('title')}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):  # 사용자에게 보여 줄 실패 이유: 웹이 이 파일을 읽어 작업 화면에 띄운다
            args = argparse.ArgumentParser()
            args.add_argument("--paper-dir")
            known, _ = args.parse_known_args()
            if known.paper_dir:
                write_atomic(Path(known.paper_dir) / "prepare_error.txt", e.code)
        raise
