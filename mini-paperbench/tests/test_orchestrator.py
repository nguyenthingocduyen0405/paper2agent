from __future__ import annotations

from web import orchestrator


def test_wants_repair_conditions():
    base = {"id": "j1", "mock": False, "skip_agent": False}
    assert orchestrator._wants_repair(base, {"passed": 3, "judged": 5, "errors": 0})
    assert not orchestrator._wants_repair(base, {"passed": 5, "judged": 5, "errors": 0})
    assert not orchestrator._wants_repair({**base, "mock": True}, {"passed": 1, "judged": 5, "errors": 0})
    assert not orchestrator._wants_repair({**base, "skip_agent": True}, {"passed": 1, "judged": 5, "errors": 0})
    assert not orchestrator._wants_repair({**base, "repair": {"before": {}}}, {"passed": 1, "judged": 5, "errors": 0})


def test_finish_and_fail_expire_redis_runs(monkeypatch):
    expired_runs: list[str] = []
    monkeypatch.setattr(orchestrator, "_expire_run", lambda run: expired_runs.append(run) if run else None)

    job = {"id": "j1", "stage": "analyze", "status": "running", "judge_run": "j1r1",
           "repair": {"first_run": "j1"}, "events": []}
    orchestrator._finish(job, {"passed": 4, "judged": 4, "errors": 0})
    assert job["status"] == "done" and job["stage"] == "done"
    assert expired_runs == ["j1r1", "j1"]

    expired_runs.clear()
    job2 = {"id": "j2", "stage": "judge", "status": "running", "judge_run": "j2", "events": []}
    orchestrator._fail(job2, "실패", "HTTP 429 rate limit exceeded")
    assert job2["status"] == "failed"
    assert "한도" in job2["error"]
    assert expired_runs == ["j2"]


def test_step_queued_skip_agent_moves_to_judge_wait():
    job = {"id": "j3", "stage": "queued", "status": "running", "skip_agent": True, "events": []}
    orchestrator.step(job, {"prepare": 0, "agent": 0, "judge": 0, "gpu": 0})
    assert job["stage"] == "judge_wait"
    assert job["events"]


def test_step_judge_wait_multi_run_allows_parallel_without_worker_reassign(monkeypatch):
    created = []
    reassigned = []
    monkeypatch.setattr(orchestrator, "MULTI_RUN_JUDGE", True)
    monkeypatch.setattr(orchestrator, "MAX_JUDGE_JOBS", 4)
    monkeypatch.setattr(orchestrator.k8s, "create_job", lambda tpl, name, args: created.append((tpl, name, args)))
    monkeypatch.setattr(orchestrator.k8s, "assign_workers", lambda *a, **kw: reassigned.append(a))

    busy = {"prepare": 0, "agent": 0, "judge": 2, "gpu": 0}
    job = {
        "id": "j4", "stage": "judge_wait", "status": "running",
        "judge_run": "j4", "paper_dir": "/data/p", "submission_dir": "/data/s",
        "mock": True, "events": [],
    }
    orchestrator.step(job, busy)
    assert job["stage"] == "judge" and job["judge_step"] == "gen"
    assert busy["judge"] == 3
    assert "--global-queue" in created[0][2] and "--mock" in created[0][2]
    assert reassigned == []


def test_cancel_marks_run_aborted_at_once_and_is_not_reported_as_llm_failure(monkeypatch):
    fakeredis = __import__("pytest").importorskip("fakeredis")
    from judge.common import k
    from web import admin

    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(orchestrator, "_redis", r)
    monkeypatch.setattr(orchestrator, "MULTI_RUN_JUDGE", True)
    patched = {}
    monkeypatch.setattr(admin.store, "patch", lambda job_id, **kw: patched.update(kw))

    job = {"id": "j5", "stage": "judge", "judge_step": "scoring", "status": "running",
           "judge_run": "j5", "events": []}
    r.set(k("j5", "total"), 10)
    assert admin.request_cancel(job, "admin") == ""
    assert patched == {"cancel_requested": "admin"}
    assert r.get(k("j5", "aborted")) == "cancelled by admin"  # 워커는 다음 차례를 기다리지 않고 바로 멈춘다

    # 취소 요청을 아직 읽지 못한 차례: 실패로 끝내지 않고 취소 처리를 기다린다
    orchestrator.step(job, {"prepare": 0, "agent": 0, "judge": 1, "gpu": 0})
    assert job["status"] == "running" and "error" not in job

    # Claude 한도 등 진짜 중단은 그대로 실패로 처리한다
    r.set(k("j5", "aborted"), "RateLimitError: subscription rate limit")
    monkeypatch.setattr(orchestrator, "_expire_run", lambda run: None)
    orchestrator.step(job, {"prepare": 0, "agent": 0, "judge": 1, "gpu": 0})
    assert job["status"] == "failed"


def test_failed_judge_stops_remaining_items_but_keeps_an_earlier_reason(monkeypatch):
    fakeredis = __import__("pytest").importorskip("fakeredis")
    from judge.common import k

    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(orchestrator, "_redis", r)
    monkeypatch.setattr(orchestrator, "MULTI_RUN_JUDGE", True)
    monkeypatch.setattr(orchestrator, "_expire_run", lambda run: None)

    job = {"id": "j6", "stage": "judge", "status": "running", "judge_run": "j6", "events": []}
    orchestrator._fail(job, "작업을 진행하다 같은 오류가 계속 났습니다", "boom")
    assert r.get(k("j6", "aborted")).startswith("failed:")  # 워커가 남은 항목을 버린다

    r.set(k("j7", "aborted"), "stalled")
    job7 = {"id": "j7", "stage": "judge", "status": "running", "judge_run": "j7", "events": []}
    orchestrator._fail(job7, "멈춤")
    assert r.get(k("j7", "aborted")) == "stalled"

    job8 = {"id": "j8", "stage": "agent", "status": "running", "judge_run": "j8", "events": []}
    orchestrator._fail(job8, "코드 작성 실패")
    assert r.get(k("j8", "aborted")) is None  # 검증 전 단계의 실패는 run을 건드리지 않는다
