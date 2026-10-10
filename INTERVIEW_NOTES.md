# Interview walkthrough guide

A preparation sheet for presenting this project. Everything here is checked against the code
in this repository. Where an answer depends on a limit, the limit is stated. Facts that are
easy to get wrong in the room are marked **(know this)**.

Commands use PowerShell and the project virtual environment. In the room, it is easiest to
set `$py` once.

```powershell
$py = ".\.venv\Scripts\python.exe"
$env:PYTHONWARNINGS = "ignore"   # hides a harmless Starlette deprecation warning
```

## 1. Two-minute elevator pitch

> I built an orchestrator for an agentic software-delivery pipeline, and I built it so you
> can trust what it does, not only watch it run.
>
> A workflow is an explicit graph of stages, written as data. Each stage names its agent, its
> dependencies, the quality gates it must pass before it starts and before it can finish, how
> many retries it gets, which fallback and rollback agents exist, and whether a human must
> approve it. The engine is one small asyncio loop: it starts every stage whose dependencies
> are met, runs the agents in threads so parallel stages truly overlap, and waits for events.
>
> Three things matter most. First, guardrails: a policy engine hard-stops on secrets and
> unsafe URLs, high-impact stages must have a human approval, and a release can never be fully
> autonomous. Second, the human pause is real: when a stage needs approval the run is saved as
> JSON and the process can exit; days later a different process approves it and the run
> continues. Third, everything is auditable: every gate result, retry, fallback, rollback,
> approval and re-plan is a decision record with a reason, and metrics such as success rate,
> retries and MTTR come from the same data.
>
> I drove a real URL shortener through three scenarios: a clear greenfield request, a
> brownfield change with impact analysis and an API-change approval, and an ambiguous request,
> "make links safer", that has to be interpreted and approved before any design happens. The
> agents are deterministic on purpose, so the engine is testable: 806 tests, with failure
> injection built in. The build was AI-assisted, and I used an independent review agent that
> found 12 defects, 9 reproduced, all fixed with regression tests. I will also tell you what
> it does not do: the agents are not LLMs, there is no approver authentication, and it is
> single-process.

## 2. Ten-minute demo script

Setup before the interview (not in the clock):

```powershell
cd C:\Users\ahmed\Desktop\job-agentic
Remove-Item data\*.db*, data\*.jsonl -ErrorAction SilentlyContinue
Remove-Item -Recurse docs\generated -ErrorAction SilentlyContinue
& $py -m pytest -q -W ignore          # expect: 806 passed (about 25 s). Do this earlier, not live.
```

Run ids are random (`run-xxxxxxxx`). Each command prints the exact next command with the id
filled in; copy it. Durations in milliseconds vary between runs; the states and counts below do not.

### Step 1 (1 min): the happy path

```powershell
& $py -m scenarios greenfield --auto-approve --skip-pytest
```

Say: "Greenfield, nine stages. `--auto-approve` is a loud demo switch, see the banner; it is an
engine setting, not something a client can send."

Expected: banner `*** DEMO MODE: all approvals auto-granted ***`, then
`state=SUCCEEDED`, a stage table with 9 stages `succeeded`, the three `impl_*` stages and
then `testing` and `documentation` with start times that overlap, and for `release`:
`production_release: approved by demo-auto-approver`. Last line:
`Metrics: ... stages_ok=9, stages_failed=0, retries=0, ... approvals=1`.

Point out: gate columns (`entry 2/2, exit 1/1`), artifacts with versions and hashes.

### Step 2 (3 min): human approval that survives restart (brownfield)

```powershell
& $py -m scenarios brownfield --skip-pytest
```

Say: "No auto-approve. It stopped by itself at the public API change."

Expected: `state=WAITING_APPROVAL`; `api_change_review waiting_approval`; the later stages `pending`;
a `PAUSED:` block listing exactly what the human is approving (alias adds 409/422 codes, ttl,
rate limit adds 429 with Retry-After), then ready-made `approve:` and `reject:` commands.
Exit code is 0 (a pause is not a failure).

Try the attack first:

```powershell
& $py -m scenarios resume <RUN_ID> --approve api_change_review-1 --actor engine
```

Expected: `error: actor 'engine' is reserved for the engine and cannot make human decisions`.
Say: "A machine name cannot approve. In this version the actor is a name I trust the caller
for; there is no login. I will come back to that."

Now approve, as a person:

```powershell
& $py -m scenarios resume <RUN_ID> --approve api_change_review-1 --actor alice
```

Say: "This is a brand new process. It loaded the run from SQLite and continued."

Expected: the three `impl_*` stages, `sync`, `testing`, `documentation` now `succeeded`;
`release waiting_approval` with `production_release: pending`, and a review block
(`tests: 6 passed, 0 failed`, `documentation: 3 file(s)`; the number of tests is 6 because
`--skip-pytest` leaves only the smoke probes).

```powershell
& $py -m scenarios resume <RUN_ID> --approve release-1 --actor bob
& $py -m scenarios resume <RUN_ID> --approve release-1 --actor bob
```

Expected: first `state=SUCCEEDED`, `release: production_release: approved by bob`; second
`error: approval 'release-1' is already approved` (an approval can be decided once).

```powershell
& $py -m scenarios trace <RUN_ID>
```

Say: "This is the decision lineage: every gate, approval and start, with a reason." Scroll to
the `approval requested ... approved by alice` rows.

### Step 3 (2 min): failure handling

```powershell
& $py -m scenarios greenfield --auto-approve --skip-pytest --inject-failure testing
& $py -m scenarios greenfield --auto-approve --skip-pytest --inject-failure testing_hard
& $py -m scenarios greenfield --auto-approve --skip-pytest --inject-failure testing_all
$LASTEXITCODE
```

Expected, in order:

1. `testing` `succeeded`, ATTEMPTS `2`, `retries=1`: transient failure, exponential backoff, retry.
2. `testing` `succeeded`, ATTEMPTS `4 (fallback)`, `retries=2, fallbacks=1`: three failed primary
   attempts, then the cheaper fallback agent.
3. `state=ROLLED_BACK`, `testing rolled_back`, `release skipped`, `Errors: testing: injected fallback
   failure`, `rollbacks=1`. `$LASTEXITCODE` is `1`.

Say: "Notice `release` never started. The release gate needs a green `test_report`, and nothing
publishes it unless the whole attempt passed."

### Step 4 (1 min): ambiguity

```powershell
& $py -m scenarios ambiguous --skip-pytest
```

Expected: `WAITING_APPROVAL` at `apply_interpretation` (`requirement_interpretation: pending`);
design stages still `pending`; the `PAUSED` block shows each open question with the proposed
answer (four questions: scope with SSRF first, accountability with audit as design only, privacy
with 90-day retention, compatibility with backward compatible). Say: "The agents propose, a human decides, and the `ambiguity_resolved` gate
keeps architecture closed until then." Leave it paused or approve it.

### Step 5 (1 min): metrics

```powershell
& $py -m scenarios metrics
```

Expected: a table with `runs_started`, `success_rate`, `retries_total`, `fallbacks_total`,
`rollback_rate`, `mttr_ms`, `avg_e2e_latency_ms`, `avg_active_ms`, `p95_e2e_latency_ms`.
Say: "`avg_e2e` includes human waiting, `avg_active` removes it. MTTR is approximate, and I can
explain how."

### Step 6 (2 min): the same engine over HTTP

Terminal A: `& $py -m orchestrator` (serves http://127.0.0.1:8090). Open `/docs`.

Terminal B (PowerShell 5.1 quoting; if it misbehaves, do the same in the Swagger UI at `/docs`):

```powershell
curl.exe -s -X POST "http://127.0.0.1:8090/runs?wait=true" -H "content-type: application/json" -d '{\"scenario\":\"brownfield\",\"params\":{\"skip_pytest\":true}}'
curl.exe -s -X POST "http://127.0.0.1:8090/runs" -H "content-type: application/json" -d '{\"scenario\":\"brownfield\",\"params\":{\"auto_approve\":true}}'
```

Expected: first returns a run view with `"state":"waiting_approval"` and a pending
`api_change_review-1`; second returns
`{"error":{"code":"auto_approve_not_allowed","message":"params.auto_approve is not allowed over the API"}}`
(HTTP 422). Then approve with `actor` `alice` at
`POST /runs/<id>/approvals/api_change_review-1/approve?wait=true` (body
`{"actor":"alice"}`); with `engine` as actor you get `invalid_actor` (422). Show
`GET /runs/<id>/dag` and `GET /metrics?format=prometheus`.

Stop the server with Ctrl+C. Clean up afterwards with the two `Remove-Item` lines from the setup.

## 3. Tour of the repo, in the order to open things

1. `ENGINEERING_SUMMARY.md`, then `ARCHITECTURE.md`: the story and the map.
2. `orchestrator/models.py`: `StageSpec` (the whole vocabulary of a workflow), `Run`,
   `AutoApprovePolicy`. Everything is pydantic, so a `Run` is JSON.
3. `scenarios/brownfield.py` and `scenarios/common.py`: a real DAG as data. See `join="all"` in
   `sync`, `parallel_group`, `approval_kind="production_release"`, `timeout_ms` on testing.
4. `orchestrator/engine.py`: read the module docstring first (it lists the known limitations),
   then `_drive` (the loop), `_advance` and `_admit` (what may start), `_attempt` (agent, then
   policy, then exit gates, then publish), `_execute` (retry, fallback, policy hard stop),
   `_fail` (rollback), `_replan` and `_stale_inputs`, `_settle` (final run state).
5. `orchestrator/gates.py` and `orchestrator/policy.py`: gates are evidence checks; policy is the
   hard stop. Look at `run_gates` (fail closed), `STAGE_RULES` and `ARTIFACT_RULES`.
6. `orchestrator/graph.py`: `validate_graph`, `topological_order`, `dependency_status`.
7. `orchestrator/store.py` and `orchestrator/telemetry.py`: durability and metrics; the MTTR
   definition is in the telemetry module docstring.
8. `orchestrator/api.py`: routes are thin; the `Supervisor` owns the background tasks.
9. `orchestrator/agents/impact.py` (ast call graph over real code) and `implementation.py`
   (probes: `{check, input, expected, actual, ok}` evidence).
10. `shortener/validation.py` and `shortener/service.py`: SSRF guard and idempotency.
11. `tests/orchestrator/test_engine.py`, then `tests/scenarios/test_scenarios.py`: the tests that
    read like the spec.

## 4. The twelve most likely questions

### 1. Why a custom engine instead of LangGraph (or Temporal, or Airflow)?

The semantics I needed are specific: entry and exit gates per stage, `join=any`, re-plan when an
input artifact's hash changes, approvals that pause the run and survive a restart, and
fail-closed policy. I would have had to write those on top of a framework anyway, and I
wanted something small enough (about 700 lines) that I can explain every line and test it
deterministically (injected `sleep`, plain functions as agents). The cost is that I own
durability, distribution and UI. If this went to production I would look at a durable-execution
system and keep this model as the definition layer; the `RunStore` and `TelemetrySink`
protocols in `contracts.py` are the seams.
Files: `orchestrator/engine.py` (docstring), `orchestrator/contracts.py`.

### 2. How do you stop an agent approving its own release?

Layers, from the outside in:
- Auto-approval is an **engine constructor argument** (`AutoApprovePolicy`), not a run
  parameter. The API rejects `params.auto_approve` with 422, and `test_run_params_can_never_enable_auto_approve`
  proves the engine ignores it even if it gets in.
- Agents receive a deep-copied, read-only `AgentContext` and return only artifacts. They have
  no reference to the engine, so they cannot call `approve`.
- Policy at `create_run`: high-impact kinds (`schema_change`, `public_api_change`,
  `production_release`, `security_exception`) must have `requires_approval`; a release stage
  cannot be `autonomy=full`; a stage that needs approval cannot define a fallback agent (a
  fallback is a different action nobody approved).
- Machine actor names (`auto-approver`, `demo-auto-approver`, `engine`, `system`) cannot make
  human decisions, in the engine, CLI and API.
- Entry gates are re-checked after approval, so evidence that changed during the wait still
  blocks the stage.

What it does not do **(know this)**: the actor is a string the caller supplies, there is no
login, and the engine cannot enforce that the approver is not the requester. That needs an
identity provider in front of the API (stated in `validate_human_actor`).
Files: `orchestrator/policy.py`, `engine.py` (`_request_approval`, `_human`), `api.py` (`create_run`, `decide`).

### 3. What happens if the process dies mid-run?

The whole `Run` (stage states, artifacts, approvals, decisions) is saved as JSON to SQLite at
every scheduling turn. After a restart, a new `Engine` on the same database loads it. Stages
that were `RUNNING` have no task behind them, so `_recover_interrupted` puts them back to
`PENDING` and they run again (agents are re-runnable; artifacts are only published on
success, so there is no half-published state). A run paused for approval is simply loaded and
resumed by `approve()`.

Limits **(know this)**: work in flight is redone; if the redone stage needs approval it asks
again (a documented limitation); nothing restarts a crashed run by itself (CLI `resume
<run_id>`; the API has no resume endpoint); and two processes resuming the same run is unsafe
because the store has no compare-and-swap.
Tests: `test_approval_pauses_run_and_resumes_after_restart_with_new_engine`,
`test_interrupted_running_stage_is_requeued_on_resume`, `test_run_survives_restart_via_shared_store`,
`test_resume_walks_a_run_through_its_approvals_across_invocations`.
Files: `engine.py` (`_recover_interrupted`), `store.py`.

### 4. How does re-plan avoid infinite loops?

Re-plan means: when a stage publishes an artifact whose content hash changed, any stage that
already succeeded using an older version (or ran before it existed) is reset to pending,
together with everything downstream of it. Three protections:
1. It only fires when the **hash differs**. Re-producing identical content is not a change.
2. A cap: `MAX_REPLANS = 3` **per source stage**, counted from the persisted decision records,
   so a restart does not reset the budget.
3. When the cap is hit the engine does not carry on quietly: the stale stages are marked
   `FAILED` with `stale_after_replan_limit`, and a `limit_exceeded` decision is recorded.
It also covers a stage that was still running when its input changed (`_replan_if_self_stale`).
No stage is reset if a stop was requested.

Honest note: none of the three shipped scenarios triggers a re-plan (`replans=0` in every demo
run); it is exercised by the engine tests. Total work is bounded (3 per source stage) but is
not constant.
Tests: `test_replan_resets_downstream_when_upstream_artifact_changes`,
`test_replan_limit_fails_the_run_loudly_instead_of_continuing_with_stale_work`,
`test_replan_cap_is_per_source_stage`, `test_running_consumer_that_finishes_on_stale_input_is_replanned`.
Files: `engine.py` (`_maybe_replan`, `_replan`, `_stale_inputs`).

### 5. What is MTTR here, and why is it approximate?

For a run that ended `SUCCEEDED` after some trouble: `run.ended_at` minus the time of the
**first** retry, fallback or rollback decision. `mttr_ms` is the mean of those samples. Runs
that never needed recovery, and runs that did not succeed, give no sample.

Why approximate **(know this)**: (a) the engine does not store a "failure happened at" time;
the first recovery decision is recorded when the engine reacts, slightly later; (b) it
measures recovery of the whole run, not of the one stage; (c) the run end includes anything
after recovery, including a human waiting to approve the release; (d) failures that never
recover are invisible (survivorship). The `contracts.py` docstring says MTTR is for failed
runs; the implementation measures runs that recovered; trust the implementation and the
module docstring in `telemetry.py`.
Files: `orchestrator/telemetry.py` (top docstring, `run_finished`), `tests/orchestrator/test_telemetry.py::test_mttr_only_counts_succeeded_runs_that_needed_recovery`.

### 6. Why are the agents deterministic?

So that what I am evaluating, the orchestration, is testable and reproducible: no network, no
API key, same inputs give the same artifacts, and a failure in a test means the engine or a
rule is wrong, not that a model had a bad day. The agents still do real work: they parse the
shortener with `ast` and build a call graph, call the real functions as probes, run the real
test suite in a subprocess and drive the live app with a test client. The agent contract is a
single function type, so an LLM agent is a drop-in at the seam (question 7).

Be upfront **(know this)**: no agent writes product code. "Implementation" here means
verifying the existing shortener with evidence. Audit and retention are marked
`design_only` and `verified: false`.
Files: `orchestrator/agents/__init__.py`, `implementation.py`, `impact.py`, `testing.py`.

### 7. How would you add a real LLM agent safely?

Same signature, `AgentContext -> AgentResult`, then:
1. **Schema-validate the output.** Define a pydantic model per artifact; parse the model's
   JSON into it; raise on failure. The engine already treats an exception as a failed
   attempt, so retries and backoff apply.
2. **Policy on the result.** Already automatic: every artifact goes through `check_artifact`
   (secrets, unsafe URLs) before publish, and a violation is a hard stop with no retry.
3. **Exit gates on evidence**, not on the model's own claim (for example `tests_green` reads
   a real test report). Keep deterministic probes to verify what the model proposes.
4. **Fallback** to the existing deterministic agent for non-approval stages (policy forbids a
   fallback on approval-gated stages, on purpose).
5. **Bounds**: `timeout_ms`, `max_attempts`, token and cost budgets, low temperature; the
   model gets no tools that mutate anything and no handle to the engine. Treat requirement
   text as untrusted input (prompt injection): it can only change the artifact, and the
   artifact is checked.
6. **Record provenance** in the decision lineage (model, version, prompt hash).
7. **Eval harness**: a golden set of requirements with expected features and criteria, the
   deterministic agents as a baseline oracle, recorded responses in CI, live evals on a
   schedule, and a regression threshold.
8. Humans stay on high-impact stages.
Files: `orchestrator/models.py` (`AgentContext`, `AgentResult`), `engine.py` (`_attempt`), `policy.py`.

### 8. How does it scale, and what are the trade-offs of asyncio plus threads?

Design: one asyncio loop owns every state mutation; agents are sync functions in a
`ThreadPoolExecutor` (6 workers by default) that only return results. That gives genuine
overlap for I/O-bound or subprocess-bound agents with no locks in the engine, and simple
reasoning. Trade-offs **(know this)**: a thread cannot be killed, so a timed-out agent
lingers and holds a worker slot (repeated hangs would starve the pool); CPU-bound agents are
limited by the GIL; the loop is single-process.

Scaling path: the model is already serialisable, so the changes are in storage and
execution: Postgres store with a version column for compare-and-swap, a lease per run so only
one worker drives it, a queue and a pool of workers, agents as subprocesses or containers
(killable), an append-only event store instead of the in-memory deque, and saving deltas
instead of rewriting the full run JSON each turn (today the whole run is rewritten at every
scheduling turn, which is fine at demo size and O(size of run) per save).
Files: `engine.py` (module docstring, `_drive`, `_attempt`), `store.py`.

### 9. What is the security posture of the shortener (SSRF), and how does idempotency work?

SSRF guard, in `validate_destination`: only `http` and `https`; no whitespace, control
characters or backslashes (parser-confusion tricks); no `user:pass@`; 2048 characters max;
non-ASCII hosts rejected; `localhost` and `.localhost`, `.internal`, `.local` blocked; a
fully qualified name is required; and **IP literals in every encoding** (`2130706433`,
`0x7f000001`, `0177.0.0.1`, `127.1`, IPv6, IPv4-mapped IPv6) are parsed and rejected unless
globally routable. Cloud metadata address `169.254.169.254` is rejected as link-local.
Limit **(know this)**: no DNS resolution, so a public-looking hostname that resolves to a
private address, or DNS rebinding, is not caught. The shortener only redirects (it never
fetches), so the guard protects whatever later follows the link, and a fetcher must check
again at connect time.

Idempotency: `POST /api/v1/urls` accepts an `Idempotency-Key`. The service hashes the request
body (`url`, `alias`, `ttl_seconds`); the same key and same body returns the original
record (HTTP 200 instead of 201); the same key with a different body is 409
`idempotency_key_conflict`. The check and the insert run under a lock so two concurrent
retries cannot both create. Limits: keys are global (not per client), never expire, and the
store is per process. Other controls: sliding-window rate limit per `X-API-Key` hash or IP
(429 with `Retry-After`), salted truncated hash for visitors (raw IP is never stored), the
socket address is used, not `X-Forwarded-For`.
Orchestrator side: an approval can be decided once (second attempt is 409/`already approved`),
and the API blocks a concurrent second decision.
Files: `shortener/validation.py`, `shortener/service.py` (`create`, `_replay`), `shortener/app.py`.

### 10. What is the testing strategy?

A pyramid, 806 tests, about 25 seconds: 529 unit (policy has 320 mostly parametrised bypass
attempts; validation 106), 68 engine component tests with stub agents (parallelism, join,
retry, fallback, rollback, stop, restart with a new `Engine`, re-plan, timeouts), 122 HTTP
tests across both APIs, 38 agent tests against the real shortener, 32 scenario integration
tests, 17 CLI tests. Failure injection is part of the product (`inject_failure`). One slow test
runs the real pytest subprocess. CI runs Python 3.10 and 3.12 and a scenario smoke run.
Determinism: injected `sleep` and clocks, so no test waits on real time except where overlap is
being proven. Beyond that, an independent review caught what tests written alongside the code
had missed (question 11).
Files: `tests/orchestrator/test_engine.py`, `tests/scenarios/test_scenarios.py`, `pytest.ini`, `.github/workflows/ci.yml`.

### 11. What did the AI get wrong that you caught?

I did not rely on the builders' own tests. An independent read-only review agent reported 12
defects; 9 were reproduced; all were fixed with regression tests. The ones to talk about:
- **auto_approve bypass**: approvals could be switched off by something a client controls. Now
  engine-only, API-rejected, reserved actor names.
- **Policy bypasses**: secrets and unsafe URLs getting past the scanners through key-name
  variants and nesting; approval kind dodging high-impact rules by case; a fallback agent
  running an unapproved action on an approved stage.
- **Stale-input re-plan**: a stage running while its input changed finished on stale data.
- **Entry gates after approval**: evidence could change during the wait.
- **Stop semantics**: retries, fallback and rollback launching after a stop; wrong end state.
The lesson I would draw: agent-written code and agent-written tests share blind spots, so a
separate reviewer with a different brief, and a rule that every finding becomes a failing test
first, is what gave confidence. Be ready to open one fix and its test, for example
`test_entry_gates_are_rechecked_after_approval` and `_admit` in `engine.py`.

### 12. What would you do differently with more time?

In order: (1) a real LLM agent behind the same contract with an eval harness (question 7);
(2) authentication and requester-versus-approver separation; (3) a Postgres store with
compare-and-swap and run leases, so more than one process can safely drive runs; (4) reuse a
granted approval after restart when inputs are unchanged; (5) run agents in subprocesses so
timeouts really kill and rollbacks can undo real side effects; (6) store a failure timestamp
to make MTTR exact; (7) DNS-aware destination checks and a shared, expiring idempotency and
rate-limit store; (8) an API endpoint to resume runs left `RUNNING`.

## 5. Glossary (plain English)

- **DAG (directed acyclic graph)**: stages as boxes, dependencies as arrows, no loops. A stage
  starts only when the boxes pointing at it are done. Here it is a list of `StageSpec`.
- **Stage**: one step of the workflow, run by one named agent.
- **Agent**: a function that reads a read-only snapshot of the run and returns artifacts.
  Here: deterministic Python, not an LLM.
- **Artifact**: a named JSON result of a stage (for example `requirements`, `test_report`),
  versioned and hashed.
- **Gate**: a pass/fail check on evidence. An **entry gate** must pass before a stage starts;
  an **exit gate** must pass before its result is accepted. Unknown or crashing gates fail.
- **Policy**: a hard rule (no secrets, no `javascript:` URLs, high-impact needs a human).
  Breaking it stops the run's remaining work; there is no retry.
- **Join**: how a stage waits for several dependencies. `all`: every one must succeed.
  `any`: one success is enough.
- **Parallel**: independent stages running at the same time (the three `impl_*` stages).
- **Retry and backoff**: try again a limited number of times, waiting longer each time
  (50 ms, 100 ms, ...).
- **Fallback**: a second, different agent tried once after retries are used up.
- **Rollback**: an agent that undoes or withdraws what a failed stage left behind. Here the
  effects are small, so rollbacks are mostly symbolic.
- **Safe-stop**: stop starting new work; let what is running finish; skip the rest; end as
  `STOPPED`. Triggered by a person or automatically by a policy violation.
- **Approval**: a pause where a named human says yes or no. The run is saved and can resume
  in another process.
- **Re-plan**: when an input changed after someone used it, re-run that stage and everything
  after it (capped at 3 per source).
- **Idempotency**: doing the same request twice has the same effect as doing it once. In the
  shortener: same `Idempotency-Key` and body returns the first link instead of making another.
- **SSRF (server-side request forgery)**: tricking a server into requesting internal
  addresses (`127.0.0.1`, cloud metadata, private networks). The guard rejects such
  destinations when links are created.
- **DNS rebinding**: a hostname that first resolves to a public address and later to a private
  one. Not covered here because the guard does not resolve DNS.
- **MTTR (mean time to recovery)**: the average time from the first sign of trouble to the
  run succeeding anyway. Approximate here (question 5).
- **Audit trail**: the permanent record of what happened and why: decision records on the run
  plus an append-only JSONL file.
- **Active time**: end-to-end time minus time spent waiting for humans.

## 6. Five honest weaknesses to volunteer

Say these before they are found. Pattern: what it is, why I accepted it, what I would do.

1. **The agents are deterministic, not LLMs, and nothing writes product code.**
   "I chose deterministic agents so the engine could be proven rather than hoped. The
   `impl_*` agents verify the existing shortener with real probes; two parts, audit and
   retention, are design only and labelled so. The agent contract is one function type, so the
   next step is an LLM agent with schema validation, the same policy checks and an eval
   harness."
2. **There is no authentication for approvers.**
   "The approver is a name the caller supplies. The engine blocks blank and machine names and
   records who decided, but it cannot prove identity or that the approver differs from the
   requester. That belongs to an identity provider in front of the API; I documented it in
   `validate_human_actor` instead of pretending."
3. **It is single-process and only as durable as one SQLite writer.**
   "There is no compare-and-swap, so two processes resuming one run is unsafe. After a
   restart, an in-flight approval-gated stage asks for approval again, and the HTTP API has no
   resume endpoint for crashed runs. All three are in the engine docstring. The fix is a
   versioned store with a lease per run."
4. **Some metrics are approximations.**
   "MTTR runs from the first recovery decision to the end of the run and can include a later
   human wait; `active_ms` also removes parallel work that happened during an approval wait.
   They are directionally right and clearly labelled; exact numbers need a stored
   failure timestamp and critical-path tracking."
5. **The analysis and guards are heuristics.**
   "Requirement understanding is a keyword table, the impact call graph matches calls by
   name (an over-approximation), the secret scanner is pattern-based, and the SSRF guard does
   not resolve DNS. Each is tested against bypass attempts, but none is complete; they are the
   first layer, and the manifest and docstrings list the known gaps."

Also be ready to say, if asked about gaps in the model: `parallel_group` and the `full` versus
`supervised` autonomy levels are informational, and the `no_policy_exceptions` gate always
passes in the shipped scenarios because none requests a security exception.
