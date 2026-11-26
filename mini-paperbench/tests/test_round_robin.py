"""multi-run 워커의 라운드 로빈(여러 작업을 돌아가며 한 항목씩)과 모의 채점의 호출 한도 제외를 실제 Redis 명령으로 확인한다."""
from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

fakeredis = pytest.importorskip("fakeredis")

from judge import common, generate_tasks, worker  # noqa: E402


class CountingLimiter:
    def __init__(self):
        self.calls = 0

    def acquire(self, _tokens):
        self.calls += 1
        return 0.0


def _leaves(prefix: str, n: int) -> list[dict]:
    return [{"id": f"{prefix}{i}", "requirements": f"req {prefix}{i}", "task_category": "Code Development",
             "preceding": []} for i in range(1, n + 1)]


def _paper(tmp_path: Path, name: str) -> tuple[Path, Path]:
    paper, sub = tmp_path / f"paper_{name}", tmp_path / f"sub_{name}"
    paper.mkdir()
    sub.mkdir()
    (paper / "paper.md").write_text(f"# Paper {name}\n", encoding="utf-8")
    return paper, sub


@pytest.fixture()
def r(monkeypatch):
    monkeypatch.setenv("MOCK_DELAY_SEC", "0")
    return fakeredis.FakeRedis(decode_responses=True)


def _run_all(r, limiter=None, consumer="w1", max_steps=50) -> int:
    limiter = limiter or CountingLimiter()
    cache, state, steps = collections.OrderedDict(), {}, 0
    while worker.multi_step(r, consumer, limiter, cache, state, block_ms=None):
        steps += 1
        assert steps <= max_steps
    return steps


def _record_order(monkeypatch) -> list[str]:
    """채점(judge_leaf)이 불린 순서대로 리프 ID를 모은다. 시계는 같은 값이 나올 수 있어 쓰지 않는다."""
    order: list[str] = []

    def fake_judge(paper_md, addendum, submission_dir, leaf, **kw):
        order.append(leaf["id"])
        return {"score": 1, "verdict": "met", "summary": "", "reason": "ok",
                "usage": {"calls": 0}, "parse_retries": 0}

    monkeypatch.setattr(worker, "judge_leaf", fake_judge)
    return order


def test_runs_are_judged_in_turn_not_first_come_first_served(r, tmp_path, monkeypatch):
    order = _record_order(monkeypatch)
    pa, sa = _paper(tmp_path, "A")
    pb, sb = _paper(tmp_path, "B")
    generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 4), global_queue=True, mock=True)
    generate_tasks.enqueue_run(r, "runB", pb, sb, _leaves("b", 2), global_queue=True, mock=True)

    limiter = CountingLimiter()
    assert _run_all(r, limiter) == 6

    # 큰 작업 A가 먼저 들어왔어도 B가 A 뒤에서 기다리지 않는다
    assert order == ["a1", "b1", "a2", "b2", "a3", "a4"]
    assert r.hlen(common.k("runA", "results")) == 4 and r.hlen(common.k("runB", "results")) == 2
    # 다 끝난 run은 목록에서 빠진다
    assert r.llen(common.ACTIVE_RUNS) == 0
    # 모의 채점은 LLM 호출 한도를 쓰지 않는다
    assert limiter.calls == 0


def test_real_judging_still_uses_the_rate_limiter(r, tmp_path, monkeypatch):
    pa, sa = _paper(tmp_path, "A")
    generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 2), global_queue=True)
    monkeypatch.setattr(worker, "judge_leaf", lambda *a, **kw: {
        "score": 1, "verdict": "met", "summary": "", "reason": "ok",
        "usage": {"calls": 1}, "parse_retries": 0})
    limiter = CountingLimiter()
    _run_all(r, limiter)
    assert limiter.calls == 2
    assert r.hlen(common.k("runA", "results")) == 2


def test_new_run_starts_while_a_big_run_is_in_progress(r, tmp_path):
    pa, sa = _paper(tmp_path, "A")
    pb, sb = _paper(tmp_path, "B")
    generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 5), global_queue=True, mock=True)
    limiter, cache, state = CountingLimiter(), collections.OrderedDict(), {}
    for _ in range(2):
        assert worker.multi_step(r, "w1", limiter, cache, state, block_ms=None)
    generate_tasks.enqueue_run(r, "runB", pb, sb, _leaves("b", 1), global_queue=True, mock=True)
    for _ in range(2):
        assert worker.multi_step(r, "w1", limiter, cache, state, block_ms=None)
    # B의 첫 항목은 A의 남은 3개를 기다리지 않고 바로 채점된다
    assert r.hlen(common.k("runB", "results")) == 1
    assert r.hlen(common.k("runA", "results")) == 3


def test_two_workers_share_the_turns(r, tmp_path):
    pa, sa = _paper(tmp_path, "A")
    pb, sb = _paper(tmp_path, "B")
    generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 3), global_queue=True, mock=True)
    generate_tasks.enqueue_run(r, "runB", pb, sb, _leaves("b", 3), global_queue=True, mock=True)
    caches = {w: collections.OrderedDict() for w in ("w1", "w2")}
    states = {w: {} for w in ("w1", "w2")}
    for _ in range(3):
        for w in ("w1", "w2"):
            assert worker.multi_step(r, w, CountingLimiter(), caches[w], states[w], block_ms=None)
    assert r.hlen(common.k("runA", "results")) == 3 and r.hlen(common.k("runB", "results")) == 3
    # 한 항목도 두 번 채점되지 않았다
    for run in ("runA", "runB"):
        done = [json.loads(e)["leaf_id"] for e in r.lrange(common.k(run, "events"), 0, -1)
                if json.loads(e)["event"] == "done"]
        assert len(done) == len(set(done)) == 3


def test_item_held_by_a_dead_worker_is_reclaimed(r, tmp_path, monkeypatch):
    pa, sa = _paper(tmp_path, "A")
    stream = generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 1), global_queue=True, mock=True)
    # 다른 워커가 항목을 가져간 뒤 죽었다(ACK 없음)
    assert r.xreadgroup(common.GROUP, "dead", {stream: ">"}, count=1)
    assert not worker.multi_step(r, "w2", CountingLimiter(), collections.OrderedDict(), {}, block_ms=None)
    assert r.llen(common.ACTIVE_RUNS) == 1  # 아직 안 끝났으니 목록에 남아 있어야 다시 가져갈 수 있다

    monkeypatch.setattr(worker, "CLAIM_IDLE_SEC", 0)
    assert worker.multi_step(r, "w2", CountingLimiter(), collections.OrderedDict(), {}, block_ms=None)
    assert r.hlen(common.k("runA", "results")) == 1
    assert r.get(common.k("runA", "retries")) == "1"


def test_aborted_and_deleted_runs_leave_the_list(r, tmp_path):
    pa, sa = _paper(tmp_path, "A")
    pb, sb = _paper(tmp_path, "B")
    generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 3), global_queue=True, mock=True)
    generate_tasks.enqueue_run(r, "runB", pb, sb, _leaves("b", 1), global_queue=True, mock=True)
    r.set(common.k("runA", "aborted"), "cancelled")
    r.delete(*[common.k("runB", name) for name in common.RUN_KEYS])  # 관리자가 작업을 지웠다

    _run_all(r)
    assert r.hlen(common.k("runA", "results")) == 0  # 중단된 run의 항목은 채점하지 않고 버린다
    assert r.llen(common.ACTIVE_RUNS) == 0


def test_messages_left_in_the_old_shared_stream_are_still_judged(r, tmp_path):
    pa, sa = _paper(tmp_path, "A")
    r.hset(common.k("old", "meta"), mapping={"paper_dir": str(pa), "submission_dir": str(sa), "mock": "1"})
    r.set(common.k("old", "total"), 1)
    r.xgroup_create(common.GLOBAL_STREAM, common.GROUP, id="0", mkstream=True)
    leaf = _leaves("o", 1)[0]
    r.xadd(common.GLOBAL_STREAM, {"run": "old", "leaf_id": leaf["id"], "leaf": json.dumps(leaf)})
    assert worker.multi_step(r, "w1", CountingLimiter(), collections.OrderedDict(), {}, block_ms=None)
    assert r.hlen(common.k("old", "results")) == 1


def test_items_stuck_after_an_outage_are_reclaimed_across_runs(r, tmp_path, monkeypatch):
    order = _record_order(monkeypatch)
    for name in ("A", "B", "C"):
        p, s = _paper(tmp_path, name)
        stream = generate_tasks.enqueue_run(r, f"run{name}", p, s, _leaves(name.lower(), 4), global_queue=True, mock=True)
        assert len(r.xreadgroup(common.GROUP, "dead", {stream: ">"}, count=4)[0][1]) == 4  # 장애로 ACK 못 한 항목
    monkeypatch.setattr(worker, "CLAIM_IDLE_SEC", 0)
    worker.random.seed(1)
    _run_all(r)
    assert sorted(order) == sorted(f"{x}{i}" for x in "abc" for i in range(1, 5))
    # 앞 작업의 항목을 다 되찾은 뒤에야 다음 작업으로 가지 않는다
    assert len({leaf[0] for leaf in order[:4]}) >= 2


def test_keys_expire_from_the_start(r, tmp_path):
    pa, sa = _paper(tmp_path, "A")
    generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 2), global_queue=True, mock=True)
    for name in ("tasks", "meta", "total"):
        assert 0 < r.ttl(common.k("runA", name)) <= common.RUN_TTL_SEC


def test_run_cancelled_before_enqueue_is_not_queued(r, tmp_path):
    pa, sa = _paper(tmp_path, "A")
    r.set(common.k("runA", "aborted"), "cancelled by admin")
    assert generate_tasks.enqueue_run(r, "runA", pa, sa, _leaves("a", 2), global_queue=True, mock=True) == ""
    assert not r.exists(common.k("runA", "tasks")) and not r.exists(common.k("runA", "total"))
    assert r.llen(common.ACTIVE_RUNS) == 0
