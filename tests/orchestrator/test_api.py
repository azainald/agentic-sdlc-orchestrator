"""API tests. The app is built around a real Engine with tiny fake agents, an
in-memory store and a no-op sleep, so nothing here depends on the real scenarios.
httpx.AsyncClient + ASGITransport keeps the app on the test's own event loop
(the engine uses asyncio primitives); lifespan is driven by hand."""
import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from orchestrator.api import create_orchestrator_app
from orchestrator.engine import Engine
from orchestrator.models import AgentResult, StageSpec
from orchestrator.store import InMemoryRunStore
from orchestrator.telemetry import Telemetry
from scenarios.base import Scenario

WAIT_S = 5


# ------------------------------------------------------------------ fakes
def spec(id, deps=(), agent=None, **kw):
    kw.setdefault("backoff_ms", 1)
    return StageSpec(id=id, title=f"Stage {id}", agent=agent or id, depends_on=list(deps), **kw)


def make(name, **content):
    return lambda ctx: AgentResult(artifacts={name: content or {"ok": True}})


def boom(ctx):
    raise RuntimeError("agent exploded")


async def no_sleep(seconds):
    pass


GATE = threading.Event()  # lets a test hold the "slow" stage open


def slow(ctx):
    assert GATE.wait(WAIT_S)
    return AgentResult(artifacts={"slow_out": {"done": True}})


AGENTS = {
    "a": make("a_out", n=1), "b": make("b_out", n=2), "ship": make("shipped", ok=True),
    "after": make("after_out"), "boom": boom, "slow": slow,
}

SCENARIOS = {
    "plain": Scenario(name="plain", title="Plain", requirement="build a thing", description="a then b",
                      graph=[spec("a"), spec("b", ["a"])], params={"flavour": "default"}),
    "gated": Scenario(name="gated", title="Gated", requirement="ship a thing", description="needs a human",
                      graph=[spec("a"), spec("ship", ["a"], requires_approval=True, approval_kind="public_api_change"),
                             spec("after", ["ship"])]),
    "broken": Scenario(name="broken", title="Broken", requirement="fails", graph=[
        spec("a"), spec("boom", ["a"], max_attempts=1), spec("after", ["boom"])]),
    "slow": Scenario(name="slow", title="Slow", requirement="takes a while", graph=[
        spec("slow"), spec("after", ["slow"])]),
    "cyclic": Scenario(name="cyclic", title="Cyclic", requirement="bad", graph=[
        spec("a", ["b"], agent="a"), spec("b", ["a"], agent="b")]),
}


@pytest.fixture
async def env():
    GATE.clear()
    telemetry = Telemetry()
    engine = Engine(AGENTS, store=InMemoryRunStore(), telemetry=telemetry, sleep=no_sleep)
    app = create_orchestrator_app(engine, telemetry, SCENARIOS)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(client=client, app=app, engine=engine, telemetry=telemetry,
                                  supervisor=app.state.ctx.supervisor)
    GATE.set()


async def start(env, scenario, wait=True, **body):
    r = await env.client.post("/runs", params={"wait": str(wait).lower()}, json={"scenario": scenario, **body})
    return r


async def gated_run(env):
    """A run paused at the approval gate; returns (run_id, approval_id)."""
    r = await start(env, "gated")
    assert r.status_code == 200 and r.json()["state"] == "waiting_approval"
    return r.json()["run_id"], r.json()["pending_approvals"][0]["id"]


def approve_url(run_id, approval_id, verb="approve"):
    return f"/runs/{run_id}/approvals/{approval_id}/{verb}"


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code and body["error"]["message"]


# ------------------------------------------------------------- health / scenarios
async def test_health(env):
    r = await env.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_scenarios_expose_the_explicit_graph(env):
    r = await env.client.get("/scenarios")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()}
    assert set(by_name) == set(SCENARIOS)
    gated = by_name["gated"]
    assert gated["title"] == "Gated" and gated["description"] == "needs a human"
    stages = {s["id"]: s for s in gated["stages"]}
    assert stages["ship"]["depends_on"] == ["a"]
    assert stages["ship"]["requires_approval"] is True and stages["ship"]["approval_kind"] == "public_api_change"
    assert stages["a"]["requires_approval"] is False


async def test_scenarios_may_be_a_callable(env):
    app = create_orchestrator_app(env.engine, env.telemetry, lambda: {"only": SCENARIOS["plain"]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert [s["name"] for s in (await client.get("/scenarios")).json()] == ["only"]


# ------------------------------------------------------------------ create run
async def test_create_run_returns_202_and_executes_in_background(env):
    GATE.clear()
    r = await start(env, "slow", wait=False)
    assert r.status_code == 202
    body = r.json()
    assert body["run_id"].startswith("run-") and body["state"] == "pending"
    assert body["links"]["self"] == f"/runs/{body['run_id']}"
    assert env.supervisor.active == 1                         # a strong reference is held
    GATE.set()
    await env.supervisor.wait(body["run_id"], WAIT_S)
    await asyncio.sleep(0)
    assert env.supervisor.active == 0                         # and released when done
    view = (await env.client.get(body["links"]["self"])).json()
    assert view["state"] == "succeeded"


async def test_create_run_wait_true_returns_full_view(env):
    r = await start(env, "plain")
    assert r.status_code == 200
    view = r.json()
    assert view["state"] == "succeeded" and view["scenario"] == "plain"
    assert view["requirement"] == "build a thing"
    assert [s["id"] for s in view["stages"]] == ["a", "b"]
    assert view["stages"][0]["state"] == "succeeded" and view["stages"][0]["attempts"] == 1
    assert view["stages"][0]["duration_ms"] >= 0
    assert view["metrics"]["stages_succeeded"] == 2


async def test_wait_returns_when_run_pauses_for_approval(env):
    view = (await start(env, "gated")).json()
    assert view["state"] == "waiting_approval"
    assert [a["status"] for a in view["pending_approvals"]] == ["pending"]


async def test_wait_timeout_returns_current_state(env):
    r = await env.client.post("/runs", params={"wait": "true", "timeout": "0.05"}, json={"scenario": "slow"})
    assert r.status_code == 200 and r.json()["state"] in ("pending", "running")
    GATE.set()


async def test_create_run_overrides_requirement_and_merges_params(env):
    view = (await start(env, "plain", requirement="custom ask", params={"extra": 1})).json()
    assert view["requirement"] == "custom ask"
    assert view["params"] == {"flavour": "default", "extra": 1}


async def test_create_run_unknown_scenario_is_422(env):
    assert_error(await start(env, "nope"), 422, "unknown_scenario")


async def test_create_run_invalid_graph_is_422(env):
    assert_error(await start(env, "cyclic"), 422, "invalid_graph")


async def test_create_run_policy_violation_is_422(env):
    bad = Scenario(name="bad", title="Bad", requirement="r",
                   graph=[spec("ship", requires_approval=False, approval_kind="schema_change")])
    app = create_orchestrator_app(env.engine, env.telemetry, {"bad": bad})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert_error(await client.post("/runs", json={"scenario": "bad"}), 422, "policy_violation")


@pytest.mark.parametrize("body", [{}, {"scenario": "plain", "requirement": ""}, {"scenario": "plain", "params": 3}])
async def test_create_run_validation_errors_use_the_error_format(env, body):
    assert_error(await env.client.post("/runs", json=body), 422, "validation_error")


# ------------------------------------------------------------------ get / list
async def test_get_run_artifacts_hide_content_unless_requested(env):
    run_id = (await start(env, "plain")).json()["run_id"]
    plain = (await env.client.get(f"/runs/{run_id}")).json()
    art = {a["name"]: a for a in plain["artifacts"]}["a_out"]
    assert art["version"] == 1 and art["produced_by"] == "a" and len(art["sha256"]) == 16
    assert art["content"] is None
    full = (await env.client.get(f"/runs/{run_id}", params={"include_content": "true"})).json()
    assert {a["name"]: a for a in full["artifacts"]}["a_out"]["content"] == {"n": 1}


async def test_get_unknown_run_is_404(env):
    assert_error(await env.client.get("/runs/run-nope"), 404, "run_not_found")


async def test_list_runs_newest_first_with_state_filter(env):
    first = (await start(env, "plain")).json()["run_id"]
    second = (await start(env, "broken")).json()["run_id"]
    runs = (await env.client.get("/runs")).json()
    assert [r["run_id"] for r in runs] == [second, first]
    assert runs[0]["links"]["self"] == f"/runs/{second}"
    failed = (await env.client.get("/runs", params={"state": "failed"})).json()
    assert [r["run_id"] for r in failed] == [second]
    assert_error(await env.client.get("/runs", params={"state": "bogus"}), 422, "validation_error")


async def test_failed_run_view_shows_stage_error_and_skips(env):
    view = (await start(env, "broken")).json()
    assert view["state"] == "failed"
    stages = {s["id"]: s for s in view["stages"]}
    assert stages["boom"]["state"] == "failed" and stages["boom"]["error"] == "agent exploded"
    assert stages["after"]["state"] == "skipped"


# ------------------------------------------------------------------ approvals
async def test_approve_resumes_run_in_background(env):
    run_id, approval_id = await gated_run(env)
    r = await env.client.post(approve_url(run_id, approval_id), json={"actor": "alice", "comment": "lgtm"})
    assert r.status_code == 202
    assert r.json() == {"run_id": run_id, "approval_id": approval_id, "decision": "approved", "actor": "alice",
                        "links": r.json()["links"]}
    await env.supervisor.wait(run_id, WAIT_S)
    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["state"] == "succeeded" and view["pending_approvals"] == []
    [approval] = view["approvals"]
    assert (approval["status"], approval["actor"], approval["comment"]) == ("approved", "alice", "lgtm")


async def test_approve_with_wait_returns_200_and_final_view(env):
    run_id, approval_id = await gated_run(env)
    r = await env.client.post(approve_url(run_id, approval_id), params={"wait": "true"}, json={"actor": "alice"})
    assert r.status_code == 200 and r.json()["state"] == "succeeded"


async def test_reject_fails_the_gated_stage_and_skips_downstream(env):
    run_id, approval_id = await gated_run(env)
    r = await env.client.post(approve_url(run_id, approval_id, "reject"), params={"wait": "true"},
                              json={"actor": "bob", "comment": "not ready"})
    assert r.status_code == 200
    view = r.json()
    stages = {s["id"]: s for s in view["stages"]}
    assert view["state"] == "failed"
    assert stages["ship"]["state"] == "failed" and "bob" in stages["ship"]["error"]
    assert stages["after"]["state"] == "skipped"
    assert view["approvals"][0]["status"] == "rejected" and view["approvals"][0]["actor"] == "bob"


async def test_reject_returns_202_decision_rejected(env):
    run_id, approval_id = await gated_run(env)
    r = await env.client.post(approve_url(run_id, approval_id, "reject"), json={"actor": "bob"})
    assert r.status_code == 202 and r.json()["decision"] == "rejected"


@pytest.mark.parametrize("body", [{}, {"actor": ""}, {"actor": "   "}, {"comment": "no actor"}, {"actor": None}])
async def test_approvals_require_a_non_empty_actor(env, body):
    run_id, approval_id = await gated_run(env)
    for verb in ("approve", "reject"):
        assert_error(await env.client.post(approve_url(run_id, approval_id, verb), json=body), 422,
                     "validation_error")
    view = (await env.client.get(f"/runs/{run_id}")).json()  # nothing changed
    assert view["state"] == "waiting_approval" and len(view["pending_approvals"]) == 1


async def test_approval_without_a_body_is_422(env):
    run_id, approval_id = await gated_run(env)
    assert_error(await env.client.post(approve_url(run_id, approval_id)), 422, "validation_error")


async def test_approve_unknown_run_and_unknown_approval_are_404(env):
    run_id, _ = await gated_run(env)
    assert_error(await env.client.post(approve_url("run-nope", "x-1"), json={"actor": "a"}), 404, "run_not_found")
    assert_error(await env.client.post(approve_url(run_id, "nope-1"), json={"actor": "a"}), 404,
                 "approval_not_found")


async def test_resolving_an_approval_twice_is_409(env):
    run_id, approval_id = await gated_run(env)
    ok = await env.client.post(approve_url(run_id, approval_id), params={"wait": "true"}, json={"actor": "alice"})
    assert ok.status_code == 200
    assert_error(await env.client.post(approve_url(run_id, approval_id), json={"actor": "alice"}), 409,
                 "approval_not_pending")
    assert_error(await env.client.post(approve_url(run_id, approval_id, "reject"), json={"actor": "bob"}), 409,
                 "approval_not_pending")


async def test_concurrent_second_decision_is_409_not_a_crash(env):
    run_id, approval_id = await gated_run(env)
    first = await env.client.post(approve_url(run_id, approval_id), json={"actor": "alice"})
    second = await env.client.post(approve_url(run_id, approval_id, "reject"), json={"actor": "bob"})
    assert first.status_code == 202
    assert_error(second, 409, "approval_not_pending")  # in flight, though not yet applied
    await env.supervisor.wait(run_id, WAIT_S)
    assert (await env.client.get(f"/runs/{run_id}")).json()["state"] == "succeeded"


# ------------------------------------------------------------------ stop
async def test_stop_a_paused_run(env):
    run_id, _ = await gated_run(env)
    r = await env.client.post(f"/runs/{run_id}/stop")
    assert r.status_code == 200
    view = r.json()
    assert view["state"] == "stopped" and view["stop_requested"] is True
    assert view["pending_approvals"] == []
    assert {s["id"]: s["state"] for s in view["stages"]}["ship"] == "skipped"


async def test_stop_a_running_run_lets_inflight_stage_finish(env):
    r = await start(env, "slow", wait=False)
    run_id = r.json()["run_id"]
    for _ in range(200):  # wait until the slow stage is really running
        await asyncio.sleep(0.01)
        if (await env.client.get(f"/runs/{run_id}")).json()["state"] == "running":
            break
    stop = await env.client.post(f"/runs/{run_id}/stop")
    assert stop.status_code == 200 and stop.json()["stop_requested"] is True
    GATE.set()
    await env.supervisor.wait(run_id, WAIT_S)
    view = (await env.client.get(f"/runs/{run_id}")).json()
    stages = {s["id"]: s["state"] for s in view["stages"]}
    assert view["state"] == "stopped" and stages == {"slow": "succeeded", "after": "skipped"}


async def test_stop_a_finished_run_is_409_and_unknown_is_404(env):
    run_id = (await start(env, "plain")).json()["run_id"]
    assert_error(await env.client.post(f"/runs/{run_id}/stop"), 409, "run_already_finished")
    assert_error(await env.client.post("/runs/run-nope/stop"), 404, "run_not_found")


# ------------------------------------------------------------------ trace / dag
async def test_trace_has_audit_events_and_decision_lineage(env):
    run_id, approval_id = await gated_run(env)
    await env.client.post(approve_url(run_id, approval_id), params={"wait": "true"},
                          json={"actor": "alice", "comment": "ship it"})
    trace = (await env.client.get(f"/runs/{run_id}/trace")).json()
    assert trace["run_id"] == run_id
    assert {e["run_id"] for e in trace["events"]} == {run_id}
    assert {"start", "stage", "artifact", "approval", "finish"} <= {e["event"] for e in trace["events"]}
    assert {"ts", "stage_id", "event", "details"} <= set(trace["events"][0])
    approved = [d for d in trace["decisions"] if d["kind"] == "approval" and d["outcome"] == "approved"]
    assert approved[0]["inputs"]["actor"] == "alice" and approved[0]["rationale"] == "ship it"
    assert trace["decisions"][-1]["kind"] == "finish" and trace["decisions"][-1]["outcome"] == "succeeded"


async def test_trace_unknown_run_is_404(env):
    assert_error(await env.client.get("/runs/run-nope/trace"), 404, "run_not_found")


async def test_dag_shape_and_node_states(env):
    run_id, _ = await gated_run(env)
    dag = (await env.client.get(f"/runs/{run_id}/dag")).json()
    assert dag["run_id"] == run_id and dag["state"] == "waiting_approval"
    nodes = {n["id"]: n for n in dag["nodes"]}
    assert {k: v["state"] for k, v in nodes.items()} == {"a": "succeeded", "ship": "waiting_approval",
                                                         "after": "pending"}
    assert nodes["ship"]["requires_approval"] and nodes["ship"]["approval_kind"] == "public_api_change"
    assert sorted((e["source"], e["target"]) for e in dag["edges"]) == [("a", "ship"), ("ship", "after")]


async def test_dag_unknown_run_is_404(env):
    assert_error(await env.client.get("/runs/run-nope/dag"), 404, "run_not_found")


# ------------------------------------------------------------------ metrics
async def test_metrics_json_reflects_finished_runs(env):
    await start(env, "plain")
    await start(env, "broken")
    r = await env.client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    m = r.json()
    assert (m["runs_started"], m["runs_succeeded"], m["runs_failed"]) == (2, 1, 1)
    assert m["success_rate"] == 0.5


async def test_metrics_prometheus_text(env):
    await start(env, "plain")
    r = await env.client.get("/metrics", params={"format": "prometheus"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "# TYPE orchestrator_runs_started gauge" in r.text
    assert "orchestrator_runs_started 1" in r.text.splitlines()
    assert_error(await env.client.get("/metrics", params={"format": "xml"}), 422, "validation_error")


# ------------------------------------------------------------------ robustness
async def test_background_crash_is_logged_and_recorded_as_failed_finish(env, monkeypatch, caplog):
    async def crash(run):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(env.engine, "_drive", crash)
    r = await start(env, "plain", wait=False)
    run_id = r.json()["run_id"]
    await env.supervisor.wait(run_id, WAIT_S)

    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["state"] == "failed" and view["metrics"]["ended_at"] is not None
    trace = (await env.client.get(f"/runs/{run_id}/trace")).json()
    finish = [e for e in trace["events"] if e["event"] == "finish"]
    assert finish and finish[-1]["details"]["outcome"] == "failed" and "engine bug" in finish[-1]["details"]["rationale"]
    assert trace["decisions"][-1]["kind"] == "finish" and trace["decisions"][-1]["outcome"] == "failed"
    assert "crashed" in caplog.text
    assert (await env.client.get("/metrics")).json()["runs_failed"] == 1   # telemetry sees it too


async def test_crash_while_waiting_for_approval_cancels_pending_approval(env, monkeypatch):
    run_id, approval_id = await gated_run(env)

    async def crash(run):
        raise RuntimeError("resume failed")

    monkeypatch.setattr(env.engine, "_drive", crash)
    r = await env.client.post(approve_url(run_id, approval_id), json={"actor": "alice"})
    assert r.status_code == 202
    await env.supervisor.wait(run_id, WAIT_S)
    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["state"] == "failed" and view["pending_approvals"] == []


async def test_unknown_route_and_wrong_method_use_error_format(env):
    assert_error(await env.client.get("/nope"), 404, "not_found")
    assert_error(await env.client.delete("/runs"), 405, "method_not_allowed")


async def test_shutdown_cancels_background_tasks_and_closes_engine():
    GATE.clear()
    telemetry = Telemetry()
    engine = Engine(AGENTS, store=InMemoryRunStore(), telemetry=telemetry, sleep=no_sleep)
    closed = []
    real_close = engine.close
    engine.close = lambda: (closed.append(True), real_close())
    app = create_orchestrator_app(engine, telemetry, SCENARIOS, on_shutdown=lambda: closed.append("hook"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            await client.post("/runs", json={"scenario": "slow"})
            await asyncio.sleep(0.05)
            assert app.state.ctx.supervisor.active == 1
    GATE.set()
    assert app.state.ctx.supervisor.active == 0 and closed == [True, "hook"]


async def test_run_survives_restart_via_shared_store(env):
    """A paused run can be approved through a brand-new app sharing the store."""
    run_id, approval_id = await gated_run(env)
    engine2 = Engine(AGENTS, store=env.engine.store, telemetry=Telemetry(), sleep=no_sleep)
    app2 = create_orchestrator_app(engine2, Telemetry(), SCENARIOS)
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as c2:
            r = await c2.post(approve_url(run_id, approval_id), params={"wait": "true"}, json={"actor": "alice"})
            assert r.status_code == 200 and r.json()["state"] == "succeeded"


# ------------------------------------------------------------------ openapi
async def test_openapi_documents_every_endpoint(env):
    paths = (await env.client.get("/openapi.json")).json()["paths"]
    assert {"/health", "/scenarios", "/runs", "/runs/{run_id}", "/runs/{run_id}/stop", "/runs/{run_id}/trace",
            "/runs/{run_id}/dag", "/metrics", "/runs/{run_id}/approvals/{approval_id}/approve",
            "/runs/{run_id}/approvals/{approval_id}/reject"} <= set(paths)
    json.dumps(paths)
