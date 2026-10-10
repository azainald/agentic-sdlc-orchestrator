import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.contracts import TelemetrySink
from orchestrator.models import DecisionRecord, Run, RunMetrics, RunState
from orchestrator.telemetry import NullTelemetry, Telemetry, to_prometheus_text


def finished(run_id, state, *, latency_ms=None, started=None, ended=None, decisions=(), **metrics) -> Run:
    return Run(
        id=run_id, scenario="s", requirement="r", state=state,
        decisions=[DecisionRecord(ts=ts, run_id=run_id, kind=kind, rationale="", outcome="applied")
                   for kind, ts in decisions],
        metrics=RunMetrics(started_at=started, ended_at=ended, e2e_latency_ms=latency_ms, **metrics),
    )


# ---- events -------------------------------------------------------------
def test_events_are_ordered_and_filtered_by_run():
    t = Telemetry()
    t.emit("a", "one", stage_id="s1", x=1)
    t.emit("b", "other")
    t.emit("a", "two")
    events = t.events_for("a")
    assert [e["event"] for e in events] == ["one", "two"]
    assert events[0]["stage_id"] == "s1" and events[0]["details"] == {"x": 1}
    assert set(events[0]) == {"ts", "run_id", "stage_id", "event", "details"}
    assert events[0]["ts"] <= events[1]["ts"]
    assert t.events_for("missing") == []


def test_events_for_returns_a_copy():
    t = Telemetry()
    t.emit("a", "one")
    t.events_for("a").clear()
    assert len(t.events_for("a")) == 1


def test_event_log_is_bounded():
    t = Telemetry(max_events=3)
    for i in range(5):
        t.emit("a", f"e{i}")
    assert [e["event"] for e in t.events_for("a")] == ["e2", "e3", "e4"]


def test_emit_is_thread_safe():
    t = Telemetry(max_events=10_000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: [t.emit("a", "e", i=i) for _ in range(50)], range(8)))
    assert len(t.events_for("a")) == 400


# ---- metrics ------------------------------------------------------------
def test_empty_snapshot_has_none_not_zero_division():
    snap = Telemetry().snapshot()
    assert snap["runs_started"] == 0
    for key in ("success_rate", "retry_rate_per_run", "rollback_rate", "mttr_ms",
                "avg_e2e_latency_ms", "p95_e2e_latency_ms"):
        assert snap[key] is None
    assert snap["generated_at"] > 0


def test_aggregation_over_mixed_runs():
    t = Telemetry()
    for rid in "abcde":
        t.run_started(rid)
    t.run_finished(finished("a", RunState.SUCCEEDED, latency_ms=100, approvals_requested=1))
    t.run_finished(finished("b", RunState.FAILED, latency_ms=200, retries=2, fallbacks=1, rollbacks=1))
    t.run_finished(finished("c", RunState.STOPPED, latency_ms=300, replans=1))
    t.run_finished(finished("d", RunState.ROLLED_BACK, latency_ms=400, retries=1, rollbacks=2))
    t.run_finished(finished("e", RunState.SUCCEEDED, latency_ms=500, started=10.0, ended=10.5,
                            retries=1, decisions=[("retry", 10.1), ("retry", 10.3)]))
    snap = t.snapshot()

    assert snap["runs_started"] == 5
    assert (snap["runs_succeeded"], snap["runs_failed"], snap["runs_stopped"], snap["runs_rolled_back"]) == (2, 1, 1, 1)
    assert snap["success_rate"] == pytest.approx(2 / 5)
    assert snap["retries_total"] == 4
    assert snap["retry_rate_per_run"] == pytest.approx(4 / 5)
    assert snap["fallbacks_total"] == 1
    assert snap["rollbacks_total"] == 3
    assert snap["rollback_rate"] == pytest.approx(1 / 5)
    assert snap["replans_total"] == 1
    assert snap["approvals_requested_total"] == 1
    assert snap["avg_e2e_latency_ms"] == pytest.approx(300)
    assert snap["p95_e2e_latency_ms"] == 500          # nearest-rank: ceil(.95*5) = 5th sample


def test_mttr_only_counts_succeeded_runs_that_needed_recovery():
    t = Telemetry()
    # recovered: first retry at 10.0s, ended 12.0s -> 2000 ms
    t.run_finished(finished("a", RunState.SUCCEEDED, started=9.0, ended=12.0,
                            decisions=[("gate", 9.5), ("retry", 10.0), ("fallback", 11.0)]))
    # recovered: first rollback at 20.0s, ended 24.0s -> 4000 ms
    t.run_finished(finished("b", RunState.SUCCEEDED, started=19.0, ended=24.0,
                            decisions=[("rollback", 20.0)]))
    # clean success: no sample
    t.run_finished(finished("c", RunState.SUCCEEDED, started=0.0, ended=1.0, decisions=[("gate", 0.5)]))
    # failed despite retry: not a recovery
    t.run_finished(finished("d", RunState.FAILED, started=0.0, ended=5.0, decisions=[("retry", 1.0)]))
    assert t.snapshot()["mttr_ms"] == pytest.approx(3000)


def test_latency_falls_back_to_run_started_clock():
    t = Telemetry()
    t.run_started("a")
    t.run_finished(finished("a", RunState.SUCCEEDED))   # no e2e_latency_ms recorded
    assert t.snapshot()["avg_e2e_latency_ms"] >= 0


def test_non_terminal_run_is_ignored():
    t = Telemetry()
    t.run_finished(finished("a", RunState.RUNNING, latency_ms=1))
    snap = t.snapshot()
    assert snap["success_rate"] is None and snap["avg_e2e_latency_ms"] is None


def test_p95_over_twenty_samples():
    t = Telemetry()
    for i in range(1, 21):
        t.run_finished(finished(f"r{i}", RunState.SUCCEEDED, latency_ms=float(i)))
    assert t.snapshot()["p95_e2e_latency_ms"] == 19.0   # ceil(.95*20) = 19th sample


# ---- audit sink ---------------------------------------------------------
def test_jsonl_audit_file_written(tmp_path):
    path = tmp_path / "audit.jsonl"
    t = Telemetry(audit_path=str(path))
    t.emit("a", "one", stage_id="s", k="v")
    t.emit("a", "two")
    # flushed per event: readable before close
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["event"] for l in lines] == ["one", "two"]
    assert json.loads(lines[0])["details"] == {"k": "v"}
    t.close()
    t.emit("a", "after_close")       # still fine, just no longer audited
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_audit_appends_across_instances(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    for name in ("first", "second"):
        t = Telemetry(audit_path=path)
        t.emit("a", name)
        t.close()
    assert len(open(path, encoding="utf-8").read().splitlines()) == 2


def test_audit_disabled_by_default(tmp_path):
    t = Telemetry()
    t.emit("a", "x")
    assert list(tmp_path.iterdir()) == []


# ---- prometheus / null --------------------------------------------------
def test_prometheus_text_format():
    t = Telemetry()
    t.run_started("a")
    t.run_finished(finished("a", RunState.SUCCEEDED, latency_ms=120.0))
    text = to_prometheus_text(t.snapshot())
    lines = text.splitlines()
    assert text.endswith("\n")
    assert "# TYPE orchestrator_runs_started gauge" in lines
    assert "orchestrator_runs_started 1" in lines
    assert "orchestrator_success_rate 1.0" in lines
    assert not any("mttr_ms" in l for l in lines)       # None is omitted
    # every sample line is "<name> <number>"
    for l in lines:
        if not l.startswith("#"):
            name, value = l.split(" ")
            float(value)


def test_null_telemetry_satisfies_protocol():
    sink: TelemetrySink = NullTelemetry()
    sink.emit("a", "x", stage_id="s", k=1)
    sink.run_started("a")
    sink.run_finished(finished("a", RunState.SUCCEEDED))
    assert sink.events_for("a") == []
    assert sink.snapshot() == {}
    for name in ("emit", "run_started", "run_finished", "events_for", "snapshot"):
        assert callable(getattr(Telemetry(), name)) and callable(getattr(NullTelemetry(), name))
