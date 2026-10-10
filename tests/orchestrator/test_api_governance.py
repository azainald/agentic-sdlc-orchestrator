"""API side of the governance hardening: strict ``params.llm``, the initial requirement and comments are scanned."""
import pytest

from .test_api import approve_url, assert_error, env, gated_run, start  # noqa: F401  (env is a fixture)

SECRET = "hunter2hunter2"


@pytest.mark.parametrize("value", ["0", "false", "no", "", 0, 1, 2.5, None, [], {}, ["true"]])
async def test_params_llm_must_be_a_real_boolean(env, value):
    """Regression: '0' and 'false' are truthy strings and used to switch the LLM layer ON."""
    r = await start(env, "plain", params={"llm": value})
    assert_error(r, 422, "invalid_llm_param")
    assert (await env.client.get("/runs")).json() == []  # nothing was created


async def test_params_llm_false_is_accepted(env):
    r = await start(env, "plain", params={"llm": False})
    assert r.status_code == 200 and r.json()["params"]["llm"] is False


async def test_a_requirement_with_a_secret_is_refused_over_http(env):
    r = await start(env, "plain", requirement=f"ship it, password={SECRET}")
    assert_error(r, 422, "policy_violation")
    assert SECRET not in r.text and (await env.client.get("/runs")).json() == []


async def test_an_oversized_requirement_is_refused_over_http(env):
    r = await start(env, "plain", requirement="x" * 2001)
    assert_error(r, 422, "validation_error")
    assert (await env.client.get("/runs")).json() == []


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_a_decision_comment_with_a_secret_is_a_clean_422_and_the_approval_stays_pending(env, verb):
    run_id, approval_id = await gated_run(env)
    r = await env.client.post(approve_url(run_id, approval_id, verb),
                              json={"actor": "alice", "comment": f"token={SECRET}abc"})
    assert_error(r, 422, "policy_violation")
    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["state"] == "waiting_approval" and [a["id"] for a in view["pending_approvals"]] == [approval_id]
    assert all(SECRET not in str(a) for a in view["approvals"])  # nothing stored: neither on the approval ...
    trace = (await env.client.get(f"/runs/{run_id}/trace")).json()
    assert SECRET not in str(trace)                              # ... nor in the decisions or audit events


async def test_an_amend_comment_with_a_secret_is_a_422(env):
    run_id, _ = await gated_run(env)
    r = await env.client.post(f"/runs/{run_id}/amend",
                              json={"requirement": "something else", "actor": "alice",
                                    "comment": f"secret_key={SECRET}"})
    assert_error(r, 422, "policy_violation")
    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["requirement"] == "ship a thing" and view["state"] == "waiting_approval"
