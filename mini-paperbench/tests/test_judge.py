from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError, RateLimitError
from pydantic import ValidationError

from judge import common, judge, rubric, worker


def test_rubric_load_leaves_preserves_order_and_preceding(tmp_path: Path):
    tree = {
        "id": "root",
        "requirements": "Root goal",
        "sub_tasks": [
            {
                "id": "s1",
                "requirements": "Section 1",
                "sub_tasks": [
                    {"id": "l1", "requirements": "Leaf 1", "task_category": "Code Development", "sub_tasks": []},
                    {"id": "l2", "requirements": "Leaf 2", "task_category": "Code Execution", "sub_tasks": []},
                ],
            },
            {
                "id": "s2",
                "requirements": "Section 2",
                "sub_tasks": [
                    {"id": "l3", "requirements": "Leaf 3", "task_category": "Result Analysis", "sub_tasks": []},
                ],
            },
        ],
    }
    p = tmp_path / "rubric.json"
    p.write_text(json.dumps(tree), encoding="utf-8")

    leaves = rubric.load_leaves(p)
    assert [x["id"] for x in leaves] == ["l1", "l2", "l3"]
    assert leaves[0]["preceding"] == ["Root goal", "Section 1"]
    assert leaves[1]["preceding"] == ["Root goal", "Section 1", "Leaf 1"]
    assert leaves[2]["preceding"] == ["Root goal", "Section 1", "Section 2"]


def test_judge_output_three_level_score():
    assert judge.JudgeOutput(reason="ok", verdict="met", summary="충족").score == 1
    assert judge.JudgeOutput(reason="half", verdict="partial", summary="부분").score == 0
    assert judge.JudgeOutput(reason="no", verdict="not_met", summary="미충족").score == 0
    with pytest.raises(ValidationError):
        judge.JudgeOutput(reason="bad", verdict="maybe", summary="?")


def test_read_submission_filters_extensions_and_skip_dirs(tmp_path: Path):
    (tmp_path / "train.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "empty.py").write_text("   \n", encoding="utf-8")
    (tmp_path / "metrics.csv").write_text("acc\n0.9\n", encoding="utf-8")
    (tmp_path / "._hidden.py").write_text("mac junk", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("skip", encoding="utf-8")

    code_text = judge.read_submission(tmp_path, "Code Development")
    assert "<FILE:train.py>" in code_text
    assert "(FILE IS EMPTY)" in code_text
    assert "metrics.csv" not in code_text
    assert "._hidden.py" not in code_text
    assert ".git" not in code_text

    res_text = judge.read_submission(tmp_path, "Result Analysis")
    assert "<FILE:metrics.csv>" in res_text
    assert "train.py" not in res_text


def test_build_messages_reproduce_log_inclusion(tmp_path: Path):
    (tmp_path / "reproduce.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (tmp_path / "reproduce.log").write_text("hi\n", encoding="utf-8")
    leaf_dev = {"requirements": "Implement model", "task_category": "Code Development", "preceding": []}
    leaf_exec = {"requirements": "Run training", "task_category": "Code Execution", "preceding": []}
    leaf_res = {"requirements": "Match Table 1", "task_category": "Result Analysis", "preceding": []}

    msgs_dev = judge.build_messages("# Paper", "", tmp_path, leaf_dev)
    msgs_exec = judge.build_messages("# Paper", "", tmp_path, leaf_exec)
    msgs_exec_quick = judge.build_messages("# Paper", "", tmp_path, leaf_exec, quick=True)
    msgs_res_quick = judge.build_messages("# Paper", "", tmp_path, leaf_res, quick=True)
    assert not any("<reproduce.log>" in m["content"] for m in msgs_dev)
    assert any("<reproduce.log>" in m["content"] for m in msgs_exec)
    assert not any("QUICK=1" in m["content"] for m in msgs_exec)
    assert any("QUICK=1" in m["content"] for m in msgs_exec_quick)
    assert any("prefer 'partial'" in m["content"] for m in msgs_res_quick)


def test_is_fatal_llm_error():
    req = httpx.Request("POST", "http://example.invalid/v1/chat/completions")
    rl_daily = RateLimitError("Quota exceeded PerDay", response=httpx.Response(429, request=req), body=None)
    rl_sub = RateLimitError("Claude subscription rate limit reached", response=httpx.Response(429, request=req), body=None)
    rl_tmp = RateLimitError("Try again in 5s", response=httpx.Response(429, request=req), body=None)
    auth_err = APIStatusError("Unauthorized", response=httpx.Response(401, request=req), body=None)
    srv_err = APIStatusError("Server error", response=httpx.Response(500, request=req), body=None)

    assert judge.is_fatal_llm_error(rl_daily)
    assert judge.is_fatal_llm_error(rl_sub)
    assert not judge.is_fatal_llm_error(rl_tmp)
    assert judge.is_fatal_llm_error(auth_err)
    assert not judge.is_fatal_llm_error(srv_err)


def test_mock_judge_leaf(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOCK_JUDGE", "1")
    monkeypatch.setenv("MOCK_DELAY_SEC", "0")
    leaf = {"requirements": "r", "task_category": "Code Development", "preceding": []}
    out = judge.judge_leaf("paper", "", tmp_path, leaf)
    assert out["verdict"] in judge.VERDICTS
    assert out["score"] == (1 if out["verdict"] == "met" else 0)
    assert out["usage"]["calls"] == 0


def test_expire_run_sets_ttl_on_existing_keys():
    class FakeRedis:
        def __init__(self, existing: set[str]):
            self.existing = existing
            self.expired: dict[str, int] = {}

        def expire(self, key: str, ttl: int) -> bool:
            if key in self.existing:
                self.expired[key] = ttl
                return True
            return False

    r = FakeRedis({common.k("r1", "tasks"), common.k("r1", "results")})
    assert common.expire_run(r, "r1", ttl_sec=3600) == 2
    assert r.expired == {"pb:r1:tasks": 3600, "pb:r1:results": 3600}
    assert common.expire_run(r, "r1", ttl_sec=0) == 0


def test_worker_wait_for_run_logs_once(capsys):
    class FakeRedis:
        def __init__(self):
            self.calls = 0

        def exists(self, _key: str) -> int:
            self.calls += 1
            return 1 if self.calls >= 4 else 0

    worker.wait_for_run(FakeRedis(), "idle", poll_sec=0)
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["waiting for run 'idle' to be created"]


def test_multi_run_enqueue_and_process_message(tmp_path: Path, monkeypatch):
    from judge import generate_tasks

    monkeypatch.setenv("MOCK_DELAY_SEC", "0")
    paper_a = tmp_path / "paper_a"
    sub_a = tmp_path / "sub_a"
    paper_b = tmp_path / "paper_b"
    sub_b = tmp_path / "sub_b"
    for d in (paper_a, sub_a, paper_b, sub_b):
        d.mkdir()
    (paper_a / "paper.md").write_text("# Paper A\n", encoding="utf-8")
    (paper_b / "paper.md").write_text("# Paper B\n", encoding="utf-8")

    class FakeRedis:
        def __init__(self):
            self.kv = {}
            self.hashes = {}
            self.lists = {}
            self.streams = {}
            self.acked = []
            self.deleted = []

        def exists(self, key):
            return 1 if key in self.kv or key in self.hashes else 0

        def get(self, key):
            return self.kv.get(key)

        def set(self, key, val):
            self.kv[key] = str(val)

        def hset(self, key, name=None, value=None, mapping=None):
            h = self.hashes.setdefault(key, {})
            if mapping:
                for mk, mv in mapping.items():
                    h[mk] = str(mv)
            elif name is not None:
                h[name] = value

        def hgetall(self, key):
            return dict(self.hashes.get(key, {}))

        def rpush(self, key, val):
            self.lists.setdefault(key, []).append(val)

        def xgroup_create(self, stream, group, id="0", mkstream=True):
            self.streams.setdefault(stream, [])

        def xadd(self, stream, fields, **_kw):
            self.streams.setdefault(stream, []).append(fields)

        def xack(self, stream, group, msg_id):
            self.acked.append((stream, msg_id))

        def xdel(self, stream, msg_id):
            self.deleted.append((stream, msg_id))

    class DummyLimiter:
        def acquire(self, _tokens):
            return 0.0

    r = FakeRedis()
    leaf_a = {"id": "la1", "requirements": "Req A", "task_category": "Code Development", "preceding": []}
    leaf_b = {"id": "lb1", "requirements": "Req B", "task_category": "Code Development", "preceding": []}
    s1 = generate_tasks.enqueue_run(r, "runA", paper_a, sub_a, [leaf_a], global_queue=True, mock=True)
    s2 = generate_tasks.enqueue_run(r, "runB", paper_b, sub_b, [leaf_b], global_queue=True, mock=True)
    assert s1 == common.GLOBAL_STREAM and s2 == common.GLOBAL_STREAM
    assert len(r.streams[common.GLOBAL_STREAM]) == 2

    # Process both interleaved runs on the same worker without restarting
    import collections
    cache = collections.OrderedDict()
    for idx, fields in enumerate(r.streams[common.GLOBAL_STREAM]):
        worker.process_message(r, common.GLOBAL_STREAM, "w1", DummyLimiter(), f"{idx}-0", fields, False, cache=cache)

    assert "la1" in r.hashes[common.k("runA", "results")]
    assert "lb1" in r.hashes[common.k("runB", "results")]
    assert len(r.acked) == 2
    assert len(r.deleted) == 2  # GLOBAL_STREAM trimmed via XDEL after XACK

    # Missing paper.md (deleted job race) returns None cleanly without crashing worker
    r.hset(common.k("runMissing", "meta"), mapping={"paper_dir": str(tmp_path / "gone"), "submission_dir": str(sub_a)})
    r.set(common.k("runMissing", "total"), "1")
    worker.process_message(
        r, common.GLOBAL_STREAM, "w1", DummyLimiter(), "8-0",
        {"run": "runMissing", "leaf_id": "lm1", "leaf": json.dumps(leaf_a)}, False, cache=cache,
    )
    assert (common.GLOBAL_STREAM, "8-0") in r.acked

    # Aborted run message is skipped & ACKed without storing a result
    r.set(common.k("runA", "aborted"), "cancelled")
    leaf_a2 = {"id": "la2", "requirements": "Req A2", "task_category": "Code Development", "preceding": []}
    worker.process_message(
        r, common.GLOBAL_STREAM, "w1", DummyLimiter(), "9-0",
        {"run": "runA", "leaf_id": "la2", "leaf": json.dumps(leaf_a2)}, False, cache=cache,
    )
    assert "la2" not in r.hashes[common.k("runA", "results")]
    assert (common.GLOBAL_STREAM, "9-0") in r.acked
