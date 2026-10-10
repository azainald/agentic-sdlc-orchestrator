from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.models import (
    Approval, ApprovalStatus, Artifact, DecisionRecord, Run, RunMetrics, RunState,
    StageRuntime, StageSpec, StageState,
)
from orchestrator.store import InMemoryRunStore, SqliteRunStore


def make_run(run_id: str = "r1", created_at: float = 1000.0, state: RunState = RunState.RUNNING) -> Run:
    spec = StageSpec(id="build", title="Build", agent="dev", depends_on=["plan"],
                     fallback_agent="dev_safe", requires_approval=True, approval_kind="release")
    return Run(
        id=run_id,
        scenario="demo",
        requirement="add a shortener",
        state=state,
        created_at=created_at,
        stages={
            "plan": StageRuntime(spec=StageSpec(id="plan", title="Plan", agent="pm"),
                                 state=StageState.SUCCEEDED, attempts=1, started_at=1.0, ended_at=2.0),
            "build": StageRuntime(spec=spec, state=StageState.WAITING_APPROVAL, attempts=2,
                                  error="boom", used_fallback=True, input_hashes={"plan": "abc"}, runs=2),
        },
        artifacts={"plan": Artifact(name="plan", content={"steps": [1, 2], "nested": {"k": "v"}},
                                    sha256="abc", produced_by="plan", produced_at=2.0, version=3)},
        decisions=[DecisionRecord(ts=3.0, run_id=run_id, stage_id="build", kind="retry",
                                  rationale="flaky", outcome="applied", inputs={"attempt": 1})],
        approvals={"a1": Approval(id="a1", run_id=run_id, stage_id="build", kind="release",
                                  status=ApprovalStatus.APPROVED, requested_at=4.0, resolved_at=5.0,
                                  actor="alice", comment="ok")},
        metrics=RunMetrics(started_at=1.0, ended_at=9.0, e2e_latency_ms=8000.0, retries=1, approvals_requested=1),
        params={"inject_failure": "testing"},
        stop_requested=True,
    )


@pytest.fixture(params=["memory", "sqlite", "sqlite_file"])
def store(request, tmp_path):
    if request.param == "memory":
        s = InMemoryRunStore()
    elif request.param == "sqlite":
        s = SqliteRunStore()
    else:
        s = SqliteRunStore(str(tmp_path / "runs.db"))
    yield s
    s.close()


def test_round_trip_fidelity(store):
    run = make_run()
    store.save_run(run)
    loaded = store.load_run("r1")
    assert loaded == run
    assert loaded is not run
    assert isinstance(loaded.stages["build"].spec, StageSpec)
    assert loaded.approvals["a1"].status is ApprovalStatus.APPROVED


def test_load_missing_returns_none(store):
    assert store.load_run("nope") is None


def test_save_is_upsert(store):
    store.save_run(make_run())
    updated = make_run(state=RunState.SUCCEEDED)
    store.save_run(updated)
    assert len(store.list_runs()) == 1
    assert store.load_run("r1").state is RunState.SUCCEEDED


def test_stored_state_is_isolated_from_callers(store):
    run = make_run()
    store.save_run(run)
    run.state = RunState.FAILED                      # mutate after saving
    loaded = store.load_run("r1")
    assert loaded.state is RunState.RUNNING
    loaded.artifacts["plan"].content["steps"].append(99)   # mutate after loading
    assert store.load_run("r1").artifacts["plan"].content["steps"] == [1, 2]


def test_list_runs_ordered_by_created_at(store):
    store.save_run(make_run("late", created_at=300.0))
    store.save_run(make_run("early", created_at=100.0))
    store.save_run(make_run("mid", created_at=200.0))
    assert [r.id for r in store.list_runs()] == ["early", "mid", "late"]


def test_empty_list(store):
    assert store.list_runs() == []


def test_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "runs.db")
    first = SqliteRunStore(path)
    first.save_run(make_run())
    first.close()

    second = SqliteRunStore(path)
    try:
        assert second.load_run("r1") == make_run()
    finally:
        second.close()


def test_file_store_uses_wal(tmp_path):
    s = SqliteRunStore(str(tmp_path / "runs.db"))
    try:
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        s.close()


def test_concurrent_saves_from_many_threads(store):
    def save(i: int) -> None:
        for n in range(5):
            r = make_run(f"run-{i}", created_at=float(i))
            r.params["n"] = n
            store.save_run(r)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(save, range(40)))

    runs = store.list_runs()
    assert len(runs) == 40
    assert all(r.params["n"] == 4 for r in runs)
