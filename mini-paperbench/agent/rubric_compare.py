"""실험: PaperAgent가 설계한 채점 기준을 PaperBench에서 사람이 만든 채점 기준과 비교한다.

PaperBench 논문마다
1. 서비스와 같은 조건(논문 본문만, addendum 없음)으로 agent.prepare.make_rubric을 불러 채점 기준을 만든다.
2. 사람 기준(rubric.json)을 깊이 3까지 펼쳐 H1, H1.2, H1.2.3 같은 번호를 붙이고,
   LLM에게 자동 기준의 항목(G1, G2, ...)마다 같은 것을 요구하는 사람 기준 노드를 고르게 한다.
   label: match(같은 요구사항이 있음) / partial(겹치지만 값·범위 등 중요한 부분이 다름) / none(대응하는 노드가 없음)
3. 지표
   - 대응률: 사람 기준에 대응하는 노드가 있는 자동 항목의 비율 (match만 / match+partial)
   - 영역 포괄률: 사람 기준의 최상위 영역 가운데 자동 항목이 하나라도 대응하는 영역의 비율(가중치 반영). 2단계 노드도 같이 잰다.
   - 근거 문장 원문 확인률, 항목 유형 비율, 항목 수
결과: <out>/<논문>/rubric_auto.json, compare.json, prepare/calls.jsonl(생성 호출), calls.jsonl(대조 호출)
      <out>/summary.json, <out>/summary.md

사용: python -m agent.rubric_compare --papers-dir data/papers --out data/rubric_compare [--only a,b] [--workers 2]
이미 compare.json이 있는 논문은 건너뛴다(다시 실행해도 안전하다).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent.llm import LLM
from agent.prepare import build_rubric, fit_paper, make_rubric

MAX_DEPTH = 3
CATEGORIES = ("Code Development", "Code Execution", "Result Analysis")

SYSTEM = """You compare two grading rubrics written for reproducing the same research paper from scratch.
Rubric H was written by the paper's authors together with experts. Rubric G was generated automatically.
You decide, for each item of G, whether rubric H asks for the same thing."""

INSTRUCTION = """For every item of rubric G, find the nodes of rubric H that ask for the same thing:
the same method step, model or data setting, hyperparameter, experiment, metric or result.
Choose the most specific matching nodes (deeper ids), at most 3. A node whose subtree contains the requirement also counts
when no deeper listed node matches.

Label each G item:
- "match": an H node asks for the same requirement (wording may differ; G may be more or less detailed than H).
- "partial": an H node is about the same thing, but G differs in an important detail (a different value, dataset, scope or claim).
- "none": no H node asks for this.
For "partial" and "none", add a short note saying what differs or what G asks for that H does not. Leave the note empty for "match".
Return exactly one entry per G item, in order."""


class Match(BaseModel):
    g: str
    label: Literal["match", "partial", "none"]
    h: list[str]
    note: str


class Matches(BaseModel):
    matches: list[Match]


def number_tree(root: dict) -> dict[str, dict]:
    """사람 기준을 깊이 MAX_DEPTH까지 H1, H1.2 같은 번호로. 값: node, depth, weight(형제 사이 정규화 가중치를 곱한 값),
    leaves(아래 리프 수), top(최상위 영역 번호)."""
    out: dict[str, dict] = {}

    def leaves(n: dict) -> int:
        return 1 if not n.get("sub_tasks") else sum(leaves(c) for c in n["sub_tasks"])

    def walk(node: dict, hid: str, depth: int, weight: float, top: str) -> None:
        out[hid] = {"node": node, "depth": depth, "weight": weight, "leaves": leaves(node), "top": top}
        children = node.get("sub_tasks") or []
        if depth >= MAX_DEPTH or not children:
            return
        total = sum(c.get("weight", 1) for c in children) or len(children)
        for i, c in enumerate(children, 1):
            walk(c, f"{hid}.{i}", depth + 1, weight * c.get("weight", 1) / total, top)

    kids = root.get("sub_tasks") or []
    total = sum(c.get("weight", 1) for c in kids) or len(kids)
    for i, c in enumerate(kids, 1):
        walk(c, f"H{i}", 1, c.get("weight", 1) / total, f"H{i}")
    return out


def h_text(tree: dict[str, dict]) -> str:
    lines = []
    for hid, v in tree.items():
        n = v["node"]
        req = " ".join(n["requirements"].split())
        if len(req) > 300:
            req = req[:300] + " ..."
        extra = ""
        if v["depth"] == MAX_DEPTH and n.get("sub_tasks"):
            extra = f" (+{v['leaves']} more specific items below)"
        cat = f" ({n['task_category']})" if not n.get("sub_tasks") and n.get("task_category") else ""
        lines.append(f"{'  ' * (v['depth'] - 1)}{hid}{cat}: {req}{extra}")
    return "\n".join(lines)


def g_items(rubric: dict) -> list[dict]:
    return [{"gid": f"G{i}", "requirement": leaf["requirements"], "category": leaf["task_category"],
             "evidence_found": leaf.get("evidence_found"), "section": s["requirements"]}
            for i, (s, leaf) in enumerate(((s, leaf) for s in rubric["sub_tasks"] for leaf in s["sub_tasks"]), 1)]


def human_leaves(root: dict) -> list[dict]:
    if not root.get("sub_tasks"):
        return [root]
    return [x for c in root["sub_tasks"] for x in human_leaves(c)]


def ancestors(hid: str) -> list[str]:
    parts = hid.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def coverage(tree: dict[str, dict], hit_ids: set[str], depth: int) -> dict:
    """깊이 depth의 노드 가운데 자동 항목이 대응한 노드(그 노드나 그 아래 노드에 대응)의 비율. 개수 기준과 가중치 기준."""
    nodes = {hid: v for hid, v in tree.items() if v["depth"] == depth}
    covered = {hid for hid in nodes if any(h == hid or h.startswith(hid + ".") for h in hit_ids)}
    # 리프가 더 얕은 곳에서 끝난 가지는 그 리프가 깊이 depth를 대신한다
    shallow = {hid: v for hid, v in tree.items() if v["depth"] < depth and not v["node"].get("sub_tasks")}
    nodes.update(shallow)
    covered |= {hid for hid in shallow if hid in hit_ids}
    w_all = sum(v["weight"] for v in nodes.values()) or 1
    return {"nodes": len(nodes), "covered": len(covered),
            "pct": round(100 * len(covered) / len(nodes), 1) if nodes else None,
            "weighted_pct": round(100 * sum(nodes[h]["weight"] for h in covered) / w_all, 1),
            "missed": [hid for hid in nodes if hid not in covered]}


def compare_one(paper_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    done = out_dir / "compare.json"
    if done.exists():
        return json.loads(done.read_text())
    paper_md = fit_paper((paper_dir / "paper.md").read_text())
    human = json.loads((paper_dir / "rubric.json").read_text())

    auto_path = out_dir / "rubric_auto.json"
    if auto_path.exists():
        auto = json.loads(auto_path.read_text())
    else:
        draft = make_rubric(out_dir, paper_md, None)  # 서비스와 같은 조건: 논문만
        auto = build_rubric(draft, [paper_md])
        auto_path.write_text(json.dumps(auto, ensure_ascii=False, indent=2))

    tree = number_tree(human)
    items = g_items(auto)
    g_text = "\n".join(f"{x['gid']} ({x['category']}): {x['requirement']}" for x in items)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Rubric H (ids, requirements; indentation shows the tree):\n<rubric_h>\n{h_text(tree)}\n</rubric_h>"},
        {"role": "user", "content": f"Rubric G:\n<rubric_g>\n{g_text}\n</rubric_g>"},
        {"role": "user", "content": INSTRUCTION},
    ]
    text = LLM(out_dir).chat("match", messages, response_format={
        "type": "json_schema", "json_schema": {"name": "matches", "schema": Matches.model_json_schema()}})
    (out_dir / "match_raw.json").write_text(text)
    by_g = {m.g: m for m in Matches.model_validate_json(text).matches}

    rows, hit_strict, hit_loose = [], set(), set()
    for x in items:
        m = by_g.get(x["gid"])
        h = [hid for hid in (m.h if m else []) if hid in tree]
        label = m.label if m else "none"
        if label != "none" and not h:  # 대응 노드를 적지 않았으면 대응한 것으로 치지 않는다
            label = "none"
        rows.append({**x, "label": label, "h": h, "note": m.note if m else "(no answer)",
                     "h_text": [" ".join(tree[hid]["node"]["requirements"].split())[:300] for hid in h]})
        for hid in h:
            if label == "match":
                hit_strict.add(hid)
            hit_loose.add(hid)

    hl = human_leaves(human)
    n = len(items)
    res = {
        "paper": paper_dir.name,
        "auto_items": n,
        "auto_sections": len(auto["sub_tasks"]),
        "human_leaves": len(hl),
        "human_top": sum(1 for v in tree.values() if v["depth"] == 1),
        "evidence_found": sum(1 for x in items if x["evidence_found"]),
        "labels": {k: sum(1 for r in rows if r["label"] == k) for k in ("match", "partial", "none")},
        "precision_match_pct": round(100 * sum(r["label"] == "match" for r in rows) / n, 1),
        "precision_loose_pct": round(100 * sum(r["label"] != "none" for r in rows) / n, 1),
        "coverage_top_match": coverage(tree, hit_strict, 1),
        "coverage_top_loose": coverage(tree, hit_loose, 1),
        "coverage_d2_match": coverage(tree, hit_strict, 2),
        "coverage_d2_loose": coverage(tree, hit_loose, 2),
        "auto_categories": {c: sum(1 for x in items if x["category"] == c) for c in CATEGORIES},
        "human_categories": {c: sum(1 for leaf in hl if leaf.get("task_category") == c) for c in CATEGORIES},
        "missed_top": [{"id": hid, "text": " ".join(tree[hid]["node"]["requirements"].split())[:300]}
                       for hid in coverage(tree, hit_loose, 1)["missed"]],
        "rows": rows,
    }
    calls = [json.loads(line) for p in (out_dir / "prepare" / "calls.jsonl", out_dir / "calls.jsonl") if p.exists()
             for line in p.read_text().splitlines() if line.strip()]
    res["usage"] = {"calls": len(calls), "input_tokens": sum(c["input_tokens"] for c in calls),
                    "output_tokens": sum(c["output_tokens"] for c in calls), "sec": round(sum(c["sec"] for c in calls), 1)}
    done.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    return res


def mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def summarize(results: list[dict], out: Path, seed: int = 0) -> None:
    keys = {
        "precision_match_pct": lambda r: r["precision_match_pct"],
        "precision_loose_pct": lambda r: r["precision_loose_pct"],
        "coverage_top_match_wpct": lambda r: r["coverage_top_match"]["weighted_pct"],
        "coverage_top_loose_wpct": lambda r: r["coverage_top_loose"]["weighted_pct"],
        "coverage_d2_match_wpct": lambda r: r["coverage_d2_match"]["weighted_pct"],
        "coverage_d2_loose_wpct": lambda r: r["coverage_d2_loose"]["weighted_pct"],
        "evidence_found_pct": lambda r: round(100 * r["evidence_found"] / r["auto_items"], 1),
        "auto_items": lambda r: r["auto_items"],
        "human_leaves": lambda r: r["human_leaves"],
    }
    summary = {"papers": len(results), "mean": {k: mean([f(r) for r in results]) for k, f in keys.items()},
               "per_paper": [{"paper": r["paper"], **{k: f(r) for k, f in keys.items()}} for r in results],
               "usage": {k: sum(r["usage"][k] for r in results) for k in ("calls", "input_tokens", "output_tokens", "sec")},
               "categories_auto": {c: sum(r["auto_categories"][c] for r in results) for c in CATEGORIES},
               "categories_human": {c: sum(r["human_categories"][c] for r in results) for c in CATEGORIES}}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    m = summary["mean"]
    lines = ["# 자동 채점 기준과 PaperBench 채점 기준 비교", "",
             f"논문 {len(results)}편. 자동 기준은 논문 본문만 보고 만들었다(서비스와 같은 조건, addendum 없음).", "",
             "| 논문 | 자동 항목 | 사람 리프 | 대응률(match) | 대응률(+partial) | 최상위 영역 포괄(match, 가중) | 최상위 영역 포괄(+partial, 가중) | 2단계 포괄(+partial, 가중) | 근거 원문 확인 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for p in summary["per_paper"]:
        lines.append(f"| {p['paper']} | {p['auto_items']} | {p['human_leaves']} | {p['precision_match_pct']}% | "
                     f"{p['precision_loose_pct']}% | {p['coverage_top_match_wpct']}% | {p['coverage_top_loose_wpct']}% | "
                     f"{p['coverage_d2_loose_wpct']}% | {p['evidence_found_pct']}% |")
    lines.append(f"| 평균 | {m['auto_items']} | {m['human_leaves']} | {m['precision_match_pct']}% | {m['precision_loose_pct']}% | "
                 f"{m['coverage_top_match_wpct']}% | {m['coverage_top_loose_wpct']}% | {m['coverage_d2_loose_wpct']}% | "
                 f"{m['evidence_found_pct']}% |")
    u = summary["usage"]
    lines += ["", f"LLM 호출 {u['calls']}회, 입력 {u['input_tokens']:,} 토큰, 출력 {u['output_tokens']:,} 토큰, 합계 {u['sec']:.0f}초.", "",
              "## 사람 기준에 없는 자동 항목 (label none)", ""]
    for r in results:
        for row in r["rows"]:
            if row["label"] == "none":
                lines.append(f"- [{r['paper']}] {row['requirement']}  — {row['note']}")
    lines += ["", "## 자동 기준이 놓친 사람 기준 최상위 영역", ""]
    for r in results:
        for x in r["missed_top"]:
            lines.append(f"- [{r['paper']}] {x['id']}: {x['text']}")
    # 사람이 직접 확인할 표본: 대응 판단 20개를 무작위로
    rng = random.Random(seed)
    pool = [(r["paper"], row) for r in results for row in r["rows"]]
    lines += ["", "## 대응 판단 표본 20개 (사람 확인용)", ""]
    for paper, row in rng.sample(pool, min(20, len(pool))):
        lines.append(f"- [{paper}] {row['gid']} {row['label']}: {row['requirement']}")
        for hid, t in zip(row["h"], row["h_text"]):
            lines.append(f"  - {hid}: {t}")
        if row["note"]:
            lines.append(f"  - note: {row['note']}")
    (out / "summary.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--papers-dir", required=True, help="PaperBench 논문 폴더들(paper.md, rubric.json)이 있는 곳")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="쉼표로 구분한 논문 폴더 이름")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    papers = sorted(p for p in Path(args.papers_dir).iterdir()
                    if (p / "paper.md").is_file() and (p / "rubric.json").is_file())
    if args.only:
        want = set(args.only.split(","))
        papers = [p for p in papers if p.name in want]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(papers)} papers → {out}", flush=True)

    def run(p: Path) -> dict | None:
        try:
            r = compare_one(p, out / p.name)
            print(f"[{p.name}] items {r['auto_items']}, match {r['precision_match_pct']}%, "
                  f"top coverage {r['coverage_top_loose']['weighted_pct']}%, evidence {r['evidence_found']}/{r['auto_items']}",
                  flush=True)
            return r
        except Exception:
            print(f"[{p.name}] FAILED\n{traceback.format_exc()}", flush=True)
            return None

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = [r for r in ex.map(run, papers) if r]
    if results:
        summarize(results, out)
        print((out / "summary.md").read_text().split("\n## ")[0], flush=True)
    if len(results) < len(papers):
        sys.exit(f"{len(papers) - len(results)} papers failed")


if __name__ == "__main__":
    main()
