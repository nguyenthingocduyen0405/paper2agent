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
