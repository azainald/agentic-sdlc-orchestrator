# Architecture

This document describes what the code does today. Where a behaviour is a limitation or an approximation, it says so. File references are to the repository root; the entry point is [README.md](README.md), real run output is in [docs/SCENARIOS.md](docs/SCENARIOS.md).

Two features have their own pages with real output: the requirement amendment and re-plan ([docs/REPLAN.md](docs/REPLAN.md)) and the optional LLM layer ([docs/LLM.md](docs/LLM.md)). This document explains where they sit in the design.

Contents: 1 Components, 2 Orchestration model (stage and run state machines), 3 Control flow and pause/resume, 4 Gates, 5 Parallelism and join, 6 Failure-handling ladder, 7 Approvals and autonomy, 8 Policy engine, 9 Re-plan and requirement amendment, 10 Observability and metrics, 11 Persistence, 12 HTTP API, 13 Agent design (deterministic agents and the optional LLM layer), 14 Key decisions and alternatives, 15 Scaling to production.

## 1. Components

```mermaid
flowchart LR
    subgraph Clients
        CLI["CLI<br/>python -m scenarios"]
        HTTP["HTTP client<br/>curl / Invoke-RestMethod"]
    end

    subgraph Orchestrator["orchestrator/ (control plane)"]
        API["api.py<br/>FastAPI :8090<br/>+ Supervisor (background tasks)"]
        ENG["engine.py<br/>Engine: asyncio loop,<br/>scheduler, failure ladder"]
        GR["graph.py<br/>validate, topo order,<br/>join status"]
        GT["gates.py<br/>9 gates, fail closed"]
        PO["policy.py<br/>artifact + stage rules"]
        ST[("store.py<br/>SQLite: one JSON row per run")]
        TE["telemetry.py<br/>event log, JSONL audit,<br/>metrics"]
        AG["agents/<br/>22 deterministic AgentFn<br/>+ 4 optional LLM AgentFn"]
        LL["llm/ (optional)<br/>LLMClient, Gemini REST client"]
    end

    subgraph Product["shortener/ (the product under change)"]
        SH["FastAPI URL shortener :8080"]
    end

    SC["scenarios/<br/>3 explicit DAGs + CLI"]

    CLI --> SC
    CLI --> ENG
    HTTP --> API
    API --> ENG
    API --> SC
    ENG --> GR
    ENG --> GT
    ENG --> PO
    ENG -->|save_run after every turn| ST
    ENG -->|emit events, run_finished| TE
    ENG -->|thread pool| AG
    AG -->|probe, ast scan, pytest, TestClient| SH
    AG -.->|only with the LLM opt-in| LL
```

Two Python packages carry the system, and a third is the thing being worked on:

| Package | Role |
|---|---|
| `orchestrator/` | Generic engine. Knows stages, artifacts, gates, approvals, policy; knows nothing about URL shorteners (agents and scenarios are injected). |
| `scenarios/` | The three workflows as data (`list[StageSpec]`), the CLI, and plain-text reporting. |
| `evals/` | `python -m evals`: scores the deterministic agents and (optionally) the LLM agents on 10 labelled requirement texts. Not used by the engine. |
| `shortener/` | The product. Agents read it (`ast`), call it (in-process `TestClient`), and test it (`pytest tests/shortener`). `shortener/manifest.py` is a machine-readable description of it; a test keeps the manifest in sync with the code. |

`orchestrator/llm/` is the only place that knows about a model provider (an `LLMClient` interface with one method, a Gemini client over plain REST, settings, and a scripted fake used by tests). The engine itself never imports it: the model is reached only through agents registered under the usual `AgentFn` signature.

The engine depends only on three Protocols in `orchestrator/contracts.py` (`RunStore`, `TelemetrySink`, `Orchestrator`), so the store and telemetry are replaceable and testable in isolation. Defaults are an in-memory store and a null telemetry sink; the CLI and API wire in SQLite and the real `Telemetry`.

## 2. Orchestration model

A **run** is one execution of a **graph**: a `list[StageSpec]` (`orchestrator/models.py`). A stage is declarative:

| Field | Meaning |
|---|---|
| `id`, `title`, `agent` | identity, and the key of the agent function to call |
| `depends_on`, `join` | edges; `join="all"` (default) needs every dependency succeeded, `"any"` needs one |
| `entry_gates`, `exit_gates` | names of gates evaluated before and after the agent |
| `max_attempts`, `backoff_ms` | bounded retries (default 2 attempts, 50 ms base, doubling) |
| `fallback_agent`, `rollback_agent` | tried once after retries; run once when the stage finally fails |
| `requires_approval`, `approval_kind`, `autonomy` | human checkpoint, its category, autonomy level |
| `consumes` | artifact names the stage reads; drives re-plan |
| `reads_requirement` | `True` if the agent also reads the raw requirement text, not only artifacts (`documentation` quotes it in the changelog; `normalize` in the ambiguous scenario, because its LLM variant quotes it in the prompt). Root stages always read it. Such a stage runs again on an amendment even when every artifact it consumes is unchanged |
| `timeout_ms`, `parallel_group` | per-attempt wall-clock limit; informational label |

At run time each stage has a `StageRuntime`: state, attempts, error, `input_hashes` (the fingerprint of each artifact in `consumes` at the moment the stage started), `runs` (how many times it has executed, more than 1 after a re-plan) and `requirement_fp` (the fingerprint of the requirement text the stage's last attempt read; it lets the engine notice that the requirement was amended while a root stage or a `reads_requirement` stage was running).

`create_run` validates the graph (no duplicate ids, no unknown or self dependencies, valid `join`, `max_attempts >= 1`, no cycle), runs the stage-level policy rules over every spec, checks that every named agent is registered, and applies the requirement bounds and scan: the requirement must be text of at most 2000 characters and passes the artifact policy rules (secrets, unsafe URL schemes) before any agent or model can read it. A violation raises before any run exists, so there is no run to record the refusal on (the API answers 422 `invalid_graph`, `policy_violation` or, for an over-long requirement, `validation_error`).

### Stage state machine

States (`StageState`): `pending`, `ready`, `running`, `waiting_approval`, `succeeded`, `failed`, `rolled_back`, `skipped`, `blocked`. The last five are terminal for the stage (until a re-plan resets them).

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: deps ready, entry gates pass, no approval needed
    pending --> waiting_approval: deps ready, entry gates pass, human approval required
    pending --> blocked: an entry gate failed
    pending --> skipped: a dependency is dead, or stop requested
    waiting_approval --> ready: approved
    waiting_approval --> failed: rejected
    waiting_approval --> skipped: stop requested
    ready --> running: entry gates re-checked and pass
    ready --> blocked: entry gate fails after approval
    ready --> pending: dependencies changed while waiting
    waiting_approval --> pending: re-plan or amendment re-queued it (approval withdrawn)
    running --> succeeded: agent ok, policy ok, exit gates pass
    running --> failed: retries and fallback spent, no rollback agent, or policy violation
    running --> rolled_back: retries and fallback spent, rollback agent ran
    running --> skipped: stop arrived between attempts
    running --> pending: process restarted mid-run (re-queued)
    succeeded --> pending: re-plan reset (stale input) or root re-queued by an amendment
    succeeded --> failed: re-plan cap exceeded
    failed --> pending: re-plan reset or amendment re-queue of a downstream stage
    blocked --> pending: re-plan reset or amendment re-queue of a downstream stage
    rolled_back --> pending: re-plan reset or amendment re-queue of a downstream stage
    skipped --> pending: re-plan reset or amendment re-queue of a downstream stage
```

The "reset arrow" is one operation (`_reset_stage`): state back to `pending`, attempts, error and input hashes cleared, pending approvals on the stage withdrawn. `StageRuntime.runs` is kept, so it counts executions across re-plans. A `running` stage is never reset (a thread cannot be killed): it finishes and is then checked. A `pending` stage with every direct dependency `succeeded` still does not start while an ancestor further up is being re-run (the "ancestors settled" rule, section 9); it stays `pending`, and nothing is recorded for that wait.

"Dead" dependency states, used for skip propagation (`graph.DEAD_STATES`): `failed`, `rolled_back`, `blocked`, `skipped`. With `join="all"` one dead dependency makes the stage `skipped`; with `join="any"` the stage is skipped only when all dependencies are dead.

A rejected approval makes the stage `failed` (nothing ran, so there is nothing to roll back) and its dependents become `skipped`.

### Run state machine

States (`RunState`): `pending`, `running`, `waiting_approval`, `succeeded`, `failed`, `stopped`, `rolled_back`.

```mermaid
stateDiagram-v2
    [*] --> pending: create_run
    pending --> running: run() / resume
    running --> waiting_approval: nothing in flight, a stage awaits a human
    waiting_approval --> running: approve / reject / amend, then run() again
    running --> succeeded: every stage succeeded
    running --> failed: a stage failed or was blocked
    running --> rolled_back: no failed stage, at least one rolled back
    running --> stopped: stop requested, no failed stage
    waiting_approval --> stopped: stop() while paused
```

`Engine._settle` decides the terminal state with this precedence: any `failed` or `blocked` stage gives `failed`; otherwise any `rolled_back` stage gives `rolled_back`; otherwise all `succeeded` gives `succeeded`; otherwise `stopped` if a stop was requested; otherwise `failed`. A consequence: a policy violation, which triggers an automatic safe-stop, still ends the run as `failed`, because the offending stage is `failed`. `stopped` is reserved for stops that did not coincide with a stage failure.

## 3. Control flow of a run

`Engine.run(run_id)` executes until the run is terminal or paused for approval, then returns. It is one asyncio loop (`_drive`):

```text
_drive(run):
    first call: set metrics.started_at, telemetry.run_started   |  later calls: record "run resumed"
    state = RUNNING;  re-queue any stage left RUNNING by a crash (_recover_interrupted)
    loop:
        if stop_requested: skip every stage not yet started
        else: for stage in topological order:             # _advance
                  PENDING/READY stage with dependency status
                      DEAD  -> SKIPPED (cascades in the same pass)
                      READY but an ancestor is not settled (being re-run) -> keep waiting
                      READY -> evaluate entry gates -> BLOCKED on failure
                               -> approval needed? create Approval, WAITING_APPROVAL, no launch
                               -> otherwise launch as an asyncio task
        save_run(run)
        if no task in flight: break
        wait for FIRST_COMPLETED of (stage tasks, wake event)    # event-driven, no polling
        process finished stages in topological order:
            root or reads_requirement stage that read an outdated requirement -> run it again   (_requeue_if_requirement_changed)
            stage that finished on outdated inputs       -> reset it        (_replan_if_self_stale)
            stage that published a CHANGED artifact      -> reset stale consumers (_maybe_replan)
    _settle(run)  -> WAITING_APPROVAL or a terminal state;  save_run(run)
```

The `wake` event is how `approve()`, `stop()` and `amend_requirement()` interrupt a loop that is waiting on slow stages: they mutate the live run and set the event.

### Pause and resume across processes

```mermaid
sequenceDiagram
    participant H as Human
    participant P1 as Process 1
    participant DB as SQLite (runs table)
    participant P2 as Process 2 (new Engine)

    P1->>P1: run(): requirements, impact, architecture succeed
    P1->>P1: api_change_review needs approval: Approval created, stage WAITING_APPROVAL
    P1->>DB: save_run (state waiting_approval)
    P1-->>H: prints approval id and resume command, process exits
    Note over DB: Run JSON holds stages, artifacts, approvals, decisions, metrics
    H->>P2: resume --approve api_change_review-1 --actor alice
    P2->>DB: load_run
    P2->>P2: validate human actor, Approval approved, stage READY, decision recorded
    P2->>DB: save_run
    P2->>P2: run() again: entry gates re-checked, stage runs, 3 impl stages in parallel ...
    P2->>DB: save_run (state waiting_approval, next approval: release-1)
```

Everything the engine needs is inside the `Run` object (pydantic, serialised as one JSON document), so there is no in-memory state to lose. `started_at` and the metrics survive the pause, which is why `e2e_latency_ms` of a run includes the time it spent waiting for people (see section 10). The tests that cover this create a second `Engine` on the same store (`test_approval_pauses_run_and_resumes_after_restart_with_new_engine`), and the CLI does it for real because every `resume` is a new process.

What does not exist: a startup scan that resumes runs which were `running` when a server crashed. The engine can recover them (`_recover_interrupted` puts a `running` stage back to `pending`), but someone has to call `run()` again; the CLI does it with `resume <run_id>` and no flags. The HTTP API has no resume endpoint.

## 4. Gates

A gate is a pure function `GateContext -> GateResult` (`orchestrator/gates.py`). The context is a deep-copied snapshot: artifacts, stage states, run params, approvals. A gate checks evidence; it never acts.

| Gate | Passes when | Used as |
|---|---|---|
| `requirements_complete` | `problem`, `acceptance_criteria`, `assumptions` all non-empty | exit of `requirements`; entry of `architecture`, `impact`, `normalize` |
| `ambiguity_resolved` | every `open_questions` entry has a non-empty `resolution` (reads `normalized_requirements`, else `requirements`) | entry of `architecture`; exit of `apply_interpretation` |
| `impact_mapped` | impact analysis lists at least one impacted module | exit of `impact`; entry of brownfield `architecture` |
| `architecture_present` | at least one component | exit of `architecture`; entry of `api_change_review` and every `impl_*` |
| `implementation_complete` | `implementation.status == "ok"` | exit of `sync`; entry of `testing`, `documentation` |
| `tests_green` | `failed == 0` and `passed > 0` (an empty suite is not green) | exit of `testing` |
| `docs_present` | documentation lists at least one file | exit of `documentation`; entry of `release` |
| `release_ready` | tests green and no other stage is `failed`, `rolled_back` or `blocked` | entry of `release` |
| `no_policy_exceptions` | no `security_exception` approval is unapproved | entry of `release` (no scenario requests one; it is structural) |

Semantics:

- **Entry gates** run when a stage's dependencies are satisfied. A failing entry gate sets the stage `blocked` immediately (no retry, no waiting) and its dependents are skipped. After a human approves a stage, its entry gates are evaluated again, because evidence may have changed during the wait (`_admit(approved=True)`).
- **Exit gates** run inside the attempt, against the artifacts the agent is about to publish (an overlay on top of the run's artifacts). A failing exit gate makes the attempt fail, which feeds the retry ladder. Nothing is published unless policy and every exit gate pass, so a failed attempt leaves no partial artifacts.
- **Fail closed** (`run_gates`): a gate name that is not in the registry fails, and a gate that raises an exception fails. Neither can be read as a pass.
- Every gate evaluation is recorded as a decision (`kind="gate"`, with gate name, phase and details), pass or fail.

## 5. Parallelism and the join model

Stages with no unmet dependency are launched in the same scheduling pass, each as an `asyncio` task. In the scenarios: three `impl_*` stages run in parallel after `architecture` (or `api_change_review`), and `testing` and `documentation` run in parallel after `sync`.

```text
event loop thread                          worker threads (ThreadPoolExecutor, max_workers=6)
-----------------                          ---------------------------------------------------
build AgentContext (deep copy of artifacts)
run_in_executor(agent, ctx)  ------------> agent(ctx) -> AgentResult        (pure function of ctx)
await result  <---------------------------
policy.check_artifact, exit gates, publish artifacts, change stage state, append decisions, metrics
```

The rule that makes this safe without locks: agents only return a value. Every mutation of the `Run` (state transitions, artifact publication, decisions, metrics, approvals) happens on the event-loop thread, between awaits. Agents receive a deep copy, so an agent that mutates its context cannot affect the run (`test_agent_cannot_mutate_the_run_through_its_context`). The telemetry sink and SQLite store do take locks because they can be called from other contexts, but the engine itself does not.

Join: `sync` has `join="all"` and depends on all `impl_*` stages, so it starts only when every part has succeeded. If one part fails, `sync` is skipped and so is everything after it; independent branches keep going (`test_failure_in_one_branch_does_not_stop_independent_branch`). `join="any"` is implemented and tested but not used by the shipped scenarios.

Finished stages are processed in topological order, so scheduling is deterministic even though completion order is not.

Caveat: if more than six agent calls were ready at once, the extras would queue inside the executor, which the engine does not account for. The shipped graphs never exceed three.

## 6. Failure-handling ladder

Per stage, exactly this sequence (`_execute`, `_attempts`, `_attempt`, `_fail`):

```text
attempt (agent call, optional timeout) -> policy check on each artifact -> exit gates -> publish
   |
   | any failure (exception, wrong return type, timeout, exit gate fail)
   v
RETRY      up to max_attempts with the primary agent.
           delay before attempt n+1 = backoff_ms * 2^(n-1)   (50 ms, 100 ms, ...)
           metrics.retries += 1 for each retry that is scheduled
   |  all attempts failed
   v
FALLBACK   if fallback_agent is set: one attempt with it (same policy and gates).
           metrics.fallbacks += 1; stage.used_fallback = True
           (a failed fallback attempt is recorded as kind "fallback", outcome "failed"; a failed primary
           attempt is kind "retry", outcome "failed")
   |  fallback failed (or none defined)
   v
ROLLBACK   if rollback_agent is set: run it once with the failure in its params.
           success -> stage ROLLED_BACK, metrics.rollbacks += 1
           the rollback agent itself raises -> stage FAILED (error says "rollback also failed")
           no rollback agent -> stage FAILED
   |
   v
SAFE-STOP  dependents are SKIPPED (dead dependency); independent branches continue;
           run ends rolled_back or failed (section 2).
```

Details a reviewer should know:

- A policy violation skips the ladder entirely: no retry, no fallback, no rollback agent. The stage is `failed` and the run is put into safe-stop (section 8).
- An entry-gate failure is not retried; it blocks.
- `timeout_ms` bounds one attempt. Python cannot kill a thread, so on timeout the engine stops waiting and counts the attempt as failed, but the agent keeps running until it returns and its late result is discarded (documented in `engine.py` and `StageSpec`).
- A fallback is a different action nobody approved, so a stage that needs approval may not define one (policy rule `fallback_requires_own_approval`, enforced at `create_run`).
- Rollback here is deliberately modest and honest about what it undoes: `rollback_testing` records that no `test_report` was published (that absence keeps the release gate closed); `rollback_release` deletes the generated release checklist file and says that nothing was deployed. Neither pretends to revert a deployment, because there is none.
- In the scenarios only `testing` (3 attempts, `testing_fallback`, `rollback_testing`, 120 s timeout) and `release` (1 attempt, `rollback_release`) have the full ladder; other stages default to 2 attempts and fail directly.
- **Safe-stop** (`Engine.stop`, `POST /runs/{id}/stop`): the in-flight stages finish; between attempts the engine checks `stop_requested` and abandons remaining retries, the fallback and the rollback launch; stages not yet started become `skipped`; pending approvals are cancelled (status `rejected`, actor `engine`).

Failure injection for demonstration is a run param read by the testing agents: `testing` (attempt 1 fails), `testing_hard` (every primary attempt fails, fallback works), `testing_all` (fallback fails too). It is the only place agent code is aware of the ladder, and it exists so the ladder can be shown deterministically.

## 7. Approvals, autonomy, and demo mode

**Approval model.** A stage needs a human when `requires_approval` is set, or its autonomy is `human_gated`, or its `approval_kind` is in the high-impact set (`schema_change`, `public_api_change`, `production_release`, `security_exception`). The engine creates an `Approval` (id `<stage>-<n>`, kind, status `pending`) stored inside the run, marks the stage `waiting_approval`, and carries on with other branches. When nothing else is in flight the run is saved as `waiting_approval` and `run()` returns.

`approve` or `reject` takes an `actor` (mandatory, stripped, non-empty) and an optional comment. The comment is checked before anything is stored (`check_comment`): at most 2000 characters and the same secret and unsafe-URL scan as artifacts. A refusal raises (HTTP 422 `policy_violation`, CLI exit 2), the approval stays `pending`, and when the cause is the scan the refusal is written to the run's decisions as `policy / fail` (rule and field, never the value). Then:

- The request itself is recorded with the SHA-256 of every artifact the stage consumes (`evidence_hashes` on the `approval / requested` decision), so "what the approver was shown" can be reconstructed from the audit trail alone.
- Approved: stage becomes `ready`, entry gates are re-checked, the stage runs.
- Rejected: stage becomes `failed` with `approval rejected by <actor>: <comment>`; dependents are skipped; the run ends `failed`.
- Resolving an unknown or already-resolved approval raises (HTTP 404 or 409).
- If a run is paused for approval and is stopped, its pending approvals are cancelled.
- **Withdrawn approvals.** When a re-plan or a requirement amendment re-queues a stage that has a pending approval, the question is void: the evidence is about to be re-derived. The engine withdraws it: status `rejected`, actor `engine`, a comment saying why, and one decision `approval / cancelled`. (There is no `cancelled` status; the CLI table prints it as `withdrawn by engine`, and a later attempt to approve it fails with "already rejected".) When the stage is ready again a new approval is asked under a new id (`release-1` becomes `release-2`). Approvals that were already granted stay in the history; if the stage has to run again it asks again, and if the stage is kept (its inputs did not change) the earlier approval stands.
- The decision is recorded with actor, kind and comment in the decision lineage.

Machine actors (`auto-approver`, `demo-auto-approver`, `engine`, `system`, and the configured auto-approver's name) are reserved: a human decision cannot be attributed to them, case-insensitively, over the CLI, the engine or HTTP.

What the system cannot enforce: that the approver is a different person from whoever requested the change, and that the actor string is a real identity. Both need an identity provider in front of the API (section 15). **There is no authentication anywhere**: no HTTP endpoint checks who is calling, so anyone who can reach port 8090 can approve a release, and `approver` and `amender` in the trail are only the names the caller typed. The servers bind to `127.0.0.1` by default and `docker-compose.yml` publishes both ports on the host loopback only (`127.0.0.1:8080:8080`, `127.0.0.1:8090:8090`; the image itself was never built); that is the only protection.

What the approver is shown. `scenarios/report.py::review_lines` builds the text printed at a pause. For `requirement_interpretation` it lists, per open question, the question, the proposed resolution, the feature that approving it would add (`adds feature`, or `(none)`) and the `rationale`: approving does not only record a decision, it also adds that feature to the requirement, so the approver sees it. If there are no open questions it prints `NOTHING TO REVIEW - approving an empty interpretation` instead of a blank review. For `public_api_change` it lists each API change; for `production_release` the test counts and the number of documentation files. Everything printed to a terminal (this text, tables, error messages, the requirement) goes through `safe()`, which turns any control character other than newline and tab (ESC and ANSI sequences, carriage return, backspace, bell, the C1 range) into a visible `\x1b`-style escape, so text from a requirement, an artifact or a model cannot redraw or fake what a human is reviewing.

**Autonomy levels** (`Autonomy`):

| Level | Meaning | What the engine does with it |
|---|---|---|
| `full` | agent acts alone | nothing special, except policy forbids it on release stages |
| `supervised` (default) | agent acts, humans can watch and stop | same as `full` at runtime; the difference is intent, and the stop endpoint |
| `human_gated` | agent must wait for approval | stage needs a human before it runs |

To be plain about it: only `human_gated` (and `requires_approval`, and high-impact kinds) change engine behaviour. `full` versus `supervised` is a label that policy reads in one place. The controls that carry weight are the approval requirement, the policy rules, and gates.

**Demo mode (`--auto-approve`)** is an `AutoApprovePolicy(kinds, actor)` passed to the `Engine` constructor. The CLI builds it with `kinds={"*"}` and actor `demo-auto-approver`, and prints `*** DEMO MODE: all approvals auto-granted (no human review) ***`. With it, the engine still creates a real `Approval` and a decision record, marked approved by the machine actor, so the audit trail shows exactly who "approved".

It is operator configuration and not a run parameter on purpose. Everything in `run.params` is client-controlled input (the API merges request params into it, and agents read it). If a param could switch approvals off, any API caller could bypass the human checkpoints. So:

- The engine takes the switch only from its constructor; it never reads `run.params` for it (`test_run_params_can_never_enable_auto_approve`).
- `create_default_app` builds the engine with no auto-approve policy, so the HTTP service cannot be started in demo mode by a request.
- The API additionally rejects `params.auto_approve` with 422 `auto_approve_not_allowed`, so a client gets a clear error rather than a silently ignored key.
- The machine actor names are reserved, so a human cannot be impersonated by the demo identity, nor the reverse.

One client-controlled param does change behaviour on purpose: `params.llm`. It is consulted by `create_run` in the API (`resolve_llm`) only to choose between two versions of the same graph, the deterministic one and the LLM-backed one, which have identical gates, approval stages and policy checks; it must be the JSON value `true` or `false` (anything else, such as the truthy strings `"0"` or `"false"`, is 422 `invalid_llm_param`), and `true` needs a configured key (otherwise 422 `llm_not_configured`). It cannot reach the approval or auto-approve logic.

## 8. Policy engine

`orchestrator/policy.py`. Rules are small named functions that raise `PolicyViolation(rule, message)`; they sit in two lists, so adding a rule is one line. Messages name the rule and the field path, and never echo the value.

| Rule | Level | Effect |
|---|---|---|
| `no_secrets` | artifact | rejects credentials: AWS key ids, PEM private key blocks, JWT-shaped tokens, AWS-style 40-character secrets next to an `aws` or `secret` hint, credentials in `Authorization` headers, `password = "x"` style assignments inside text, and fields named like secrets (`password`, `api_key`, `client_secret`, `token`, ...) holding a literal. Placeholders (`${NAME}`, `<redacted>`, `***`, `REDACTED`, empty) are accepted. |
| `safe_destination_urls` | artifact | rejects `javascript:`, `data:`, `file:`, `vbscript:`, `blob:`, `about:` under any URL-like key (`url`, `href`, `redirect`, `target`, `callback`, ...) and in `href=` or `src=` attributes inside strings; whitespace and control characters inside the scheme are ignored, as browsers do |
| `high_impact_needs_approval` | stage | a stage with a high-impact `approval_kind` must set `requires_approval` |
| `release_not_fully_autonomous` | stage | a stage that looks like a release (id, agent or kind) may not use `autonomy=full` |
| `fallback_requires_own_approval` | stage | a stage that needs approval may not define a `fallback_agent` |

The same artifact rules also scan human-supplied text, in three places: the requirement of a new run (`create_run`, also capped at 2000 characters), an amended requirement (`Engine.amend_requirement`), and every approve, reject and amend comment (`check_comment`). A violation there is a refusal, not a hard stop. A new run with a refused requirement is never created (so there is no run to audit it on); an amendment or comment refusal leaves the run exactly as it was and is audited as a `policy / fail` decision that names the rule and field, never the value. A typo by an operator must not kill a run; a secret must not enter the requirement or the audit trail.

Stage rules run at `create_run`, so a violating graph never starts. Artifact rules run on every artifact of every attempt, before exit gates. An artifact violation is a **hard stop**: the stage is `failed` without retry, fallback or rollback; the engine sets `stop_requested`, so in-flight stages finish and everything unstarted is skipped; the run ends `failed`. A real example, from running the engine with an agent that emits `{"db": {"password": "..."}}`:

```text
policy  fail       literal secret value found in artifact 'config' field 'db.password'   {'rule': 'secret_detected'}
stop    requested  policy violation triggered an automatic safe-stop
stop    skipped    run is stopping; stage never started
```

The secret rules are heuristics (regular expressions and key-name matching), tuned against 320 test cases; they will have both false positives and false negatives, and are a guardrail, not a data-loss-prevention product. The same applies to the scan of requirements and comments: a secret phrased in a way no rule recognises passes.

## 9. Re-plan and requirement amendment

"Re-plan" here means invalidation and re-execution of work built on outdated input. It does not invent new stages. Full algorithm, real output and limits are in [docs/REPLAN.md](docs/REPLAN.md); this section is the design summary.

There are two triggers and one mechanism:

| Trigger | Who | Entry point |
|---|---|---|
| A changed artifact | the engine, comparing hashes | `_maybe_replan`, `_replan_if_self_stale` |
| An amended requirement | a named human | `Engine.amend_requirement`, `POST /runs/{id}/amend`, `python -m scenarios amend` |

The human trigger feeds the automatic one: amending re-queues the root stage(s), and the normal hash comparison decides what else must run. That is why it needs no special graph logic.

### The mechanism (hash comparison, lazy cascade)

1. When a stage starts, `_begin` records `input_hashes`: for each name in `spec.consumes`, the `sha256` of the artifact it sees (empty if the artifact does not exist yet), and `requirement_fp`, the fingerprint of the requirement text.
2. Artifacts are fingerprinted with `fingerprint()` (SHA-256 of canonical JSON, first 16 hex chars). When a stage publishes, each artifact gets a new version, and counts as *changed* only if its hash differs from the previous one. Republishing identical content triggers nothing.
3. When a stage succeeds with changed artifacts, every other `succeeded` stage whose recorded hash for one of those names differs from the current hash is **stale** (`_stale_inputs`).
4. **The cascade is lazy.** Only the stale stages are reset to `pending`, plus any downstream stage that had *not* succeeded (waiting for approval, failed, blocked, skipped, rolled back: its verdict was about the old world). Downstream stages that already `succeeded` are **kept for now**. If a reset stage re-runs and publishes a different artifact, the same comparison resets its consumers then; if it reproduces the same artifact, nothing further runs. (An earlier version of this engine reset everything downstream at once; that is no longer the behaviour. Tests: `test_replan_is_selective_a_reset_stage_that_reproduces_its_output_resets_nothing_downstream`.)
5. **Ancestors settled.** Because succeeded stages are kept optimistically, a stage can have all its direct dependencies `succeeded` while an ancestor higher up is being re-executed. `_advance` therefore does not start a stage unless its whole upstream chain is settled (`_inputs_settled`: every ancestor `succeeded`, any one for `join="any"`). Without this rule a kept stage could ask a human to approve evidence that is seconds from being replaced.
6. A stage that was `running` while its input changed is caught when it finishes: `_replan_if_self_stale` compares its recorded hashes with the current ones and resets it instead of accepting a result built on stale data. A *root* stage, or a stage declared `reads_requirement`, that finished while the requirement was amended is caught the same way through `requirement_fp` (`_requeue_if_requirement_changed`, recorded as `replan / requeued`).
7. `metrics.replans += 1` per applied pass, and a decision `kind="replan"`, outcome `applied`, records which stage consumed which outdated artifact (consumed hash versus current hash) and which stages were reset.
8. **Cap, fail loud:** each source stage may cause at most `MAX_REPLANS = 3` applied re-plans, counted from persisted decisions so a restart does not reset the budget. The next attempt does not silently continue: the stale `succeeded` stages are set to `failed` with error `stale_after_replan_limit`, a `replan / limit_exceeded` decision is recorded, and the run ends `failed`. Past the cap the cascade is **eager** again, as a safe default: everything that could rest on the stale stages goes down with them. Each amendment makes the root stage the source of one re-plan, so about three amendments of one run exhaust it, and the fourth amendment from the same source stage fails the run by design (a known cap, not a bug); the amendment itself is applied and audited before the run fails.

### The amend trigger

`Engine.amend_requirement(run_id, requirement, actor, comment="")`:

1. **Validate, change nothing on failure** (`_check_amendment`): the actor must be a real name (same rule as approvals: `auto-approver`, `engine`, `system` are refused); the run exists (`KeyError`) and is neither finished nor stopping (`RunNotAmendable`); the text is non-empty, at most 2000 characters, and different from the current one (whitespace-only differences count as identical); and the text passes the artifact policy scan (section 8); the `comment` gets the same length cap and scan. The HTTP API runs the same checks synchronously before it starts the background task (`check_amendment`), so a bad request is a clean 4xx and can never mark the run as crashed.
2. **Record** `replan / requirement_amended` with actor, comment, the old and new requirement text (`old_requirement`, `new_requirement`; the old text is cut to 2000 characters, and the new text is already capped there), their SHA-256, the stages re-queued and the approvals withdrawn; emit telemetry; `metrics.replans += 1`. The text is in the decision itself, so the trail shows what changed without needing the previous run state.
3. **Set `run.requirement`**, re-queue the root stages, every stage declared `reads_requirement` (they read the raw text, which the hash comparison cannot see) and every downstream stage that is not `succeeded` (waiting, failed, blocked, skipped), and **withdraw their pending approvals** (section 7). Other `succeeded` non-root stages are kept until the hash comparison proves their input changed.
4. **Resume.** A paused or restarted run is resumed (`engine.run`), like `approve`; a live run is woken up. A stage that is `running` right now is never touched.
5. The mechanism above does the rest.

In a real brownfield run amended while waiting at the release gate (docs/REPLAN.md), `requirements`, `impact` and `architecture` ran twice, the API-change approval was asked again (`api_change_review-2`), and `testing` kept its result (`RUNS = 1`) because the `implementation` artifact it reads was byte-identical. A wording-only amendment is more selective still: `impact` and `architecture` re-run but reproduce identical artifacts, so only the release decision is asked again.

### What it does not do

`consumes` is the dependency contract and it is **artifact-level**: a stage that reads one field of an artifact re-runs when any field changes, and a stage that reads an artifact without declaring it would be kept when it should re-run (the scenario stage lists were checked against what the agents read; a few over-declare, which costs extra runs, never stale results). A running stage cannot be interrupted, so one amendment can cost one wasted execution. A withdrawn approval is stored as `rejected` by `engine`. The `actor` is an unauthenticated string, so "the amender is not the approver" cannot be enforced here. LLM-backed stages are nondeterministic, so a re-run of one can publish a different artifact and trigger more re-plans than a deterministic stage would.

Honest status of the default scenarios: with no amendment, none of the three triggers a re-plan, because each artifact is published once (their runs show `replans=0`). The one exception to "published once" is `normalized_requirements` in the ambiguous scenario, which is republished by `apply_interpretation`; its only consumer is the stage that republishes it, so nothing is stale. `amend` is how a scenario run now triggers a re-plan. Tests: engine level `tests/orchestrator/test_amend.py` (31) and `test_engine.py::test_replan_*`; end to end with real agents `tests/scenarios/test_amend.py` (5). `consumes` only drives re-plan; it is not an access list, and agents can read every artifact in the run.

## 10. Observability

There are two complementary records, written by the same call (`Engine._record`):

| | Decision lineage | Telemetry trace and audit file |
|---|---|---|
| Object | `Run.decisions: list[DecisionRecord]` | `Telemetry` event log, and `data/audit.jsonl` |
| Stored | inside the run JSON, in the store | bounded in-memory deque (5000 events, per process) and an append-only JSONL file, flushed per event |
| Survives restart | yes, with the run | the JSONL file does; the in-memory part does not |
| Purpose | answer "why did the run do this?" for one run | operational trace and a durable, cross-run audit file |
| Shape | `ts, run_id, stage_id, kind, rationale, outcome, inputs` | `ts, run_id, stage_id, event, details{outcome, rationale, ...inputs}` |

Decision kinds in use, with the outcomes each can have:

| Kind | Outcomes |
|---|---|
| `start` | `created`, `running`, `resumed` |
| `stage` | `started`, `succeeded`, `failed`, `blocked`, `skipped`, `requeued` (approved, but its dependencies changed while it waited), `recovered` (found `running` after a restart and re-queued) |
| `gate` | `pass`, `fail` (inputs: gate name, phase, details) |
| `artifact` | `published` (inputs: sha256, version) |
| `retry` | `failed` (a primary-agent attempt), `scheduled` (backoff delay) |
| `fallback` | `attempted` (retries exhausted, fallback agent about to run), `failed` (a fallback attempt failed; it has its own kind so a reader can tell it from a primary failure) |
| `rollback` | `applied`, `fail`, `skipped` (stop requested) |
| `approval` | `requested` (inputs: approval id and kind, `evidence_hashes`: artifact name to SHA-256 of what the approver is shown), `approved`, `rejected`, `paused`, `cancelled` (pending approval withdrawn by the engine: stop, re-plan or amendment) |
| `policy` | `fail` (artifact hard stop, or an amendment or an approve, reject or amend comment refused by the scan; names rule and field, never the value) |
| `stop` | `requested`, `skipped` |
| `replan` | `requirement_amended` (a human changed the requirement), `requeued` (a root stage re-queued because the requirement changed while it ran), `applied` (stale stages reset), `limit_exceeded` (cap hit, run fails loudly) |
| `finish` | the terminal run state |

Every decision carries a rationale (`test_every_decision_has_a_rationale`). The `inputs` make the lineage checkable: gate decisions carry the gate name, phase and details; `artifact` decisions carry sha256 and version; `stage started` carries the input hashes the stage saw; approval requests carry the hashes of the evidence shown and approval decisions carry actor and kind; re-plan decisions carry consumed and current hashes; an amendment carries the actor, comment, the old and new requirement text (bounded) and their hashes. Together they answer "which evidence did stage X act on, who approved it, and which version of each artifact was it".

Two honest qualifiers on what "audit" means here. (1) `actor` is a string the caller supplies and nothing authenticates it, so the trail says who a caller claimed to be. (2) The JSONL file is opened in append mode and flushed per event, which makes it append-only by convention only: nothing signs, hashes or write-protects it, and anyone with file access can edit it. The decisions stored on a run are in the SQLite row, which is equally editable. Tamper evidence is a production item (section 15).

`python -m scenarios trace <run_id>` prints the decisions; `GET /runs/{id}/trace` returns both the telemetry events and the decisions. One consequence worth knowing: after an API restart the telemetry half of `/trace` only has events from the current process, while `decisions` is complete (it lives in the run); the JSONL file has everything.

### Metrics

Computed by `Telemetry.run_finished` when a run reaches a terminal state, and exposed as JSON or Prometheus text (`GET /metrics`, `python -m scenarios metrics`).

| Metric | Definition |
|---|---|
| `success_rate` | runs `succeeded` / runs finished (`succeeded + failed + stopped + rolled_back`) |
| `retries_total`, `retry_rate_per_run` | retries scheduled across runs; and that total divided by finished runs |
| `fallbacks_total`, `rollbacks_total` | counts of fallback attempts and rollback agent executions |
| `rollback_rate` | share of finished runs that ended `rolled_back` (not rollbacks per run) |
| `mttr_ms` | mean over runs that ended `succeeded` after at least one retry, fallback or rollback decision, of `run.ended_at - ts(first such decision)` |
| `avg_e2e_latency_ms`, `p95_e2e_latency_ms` | wall clock from first start to terminal state, mean and nearest-rank 95th percentile |
| `avg_active_ms` | mean of `e2e_latency_ms - approval_wait_ms` |
| `replans_total`, `approvals_requested_total` | counts |

Notes on interpretation, stated as plainly as the code states them (`orchestrator/telemetry.py` docstring):

- **MTTR is an approximation.** The first recovery decision is recorded when the engine reacts to a failure, slightly after the failure itself, and the measure covers recovery of the whole run, not of the single stage; the engine stores no separate "first failed at" timestamp. It also only samples runs that recovered, so a run that never recovered contributes nothing (the metric flatters). And because `ended_at` includes any human approval wait that happens after the recovery (for example the release approval), MTTR for human-gated runs includes that wait. For auto-approved runs it is a few hundred milliseconds; the injection runs in docs/SCENARIOS.md show 166 ms.
- **`e2e_latency_ms` includes human time.** It spans first start to final state, across pauses and process restarts; a brownfield run with two approvals showed e2e 10,164 ms of which 9,874 ms was waiting for a person. `approval_wait_ms` is the time during which at least one approval was pending (overlapping waits counted once) and `active_ms` is the remainder, which is the number to use when asking how long the system itself worked. `active_ms` still contains retry backoff sleeps.
- The in-memory aggregates are per process, but they are rebuilt from the stored runs (`replay_runs`) by `python -m scenarios metrics` and when the API starts, so a restart does not reset `GET /metrics`. Only finished runs count, and the event log (the telemetry half of `/trace`) is not replayed.
- `metrics.replans` counts the amendment itself plus every applied re-plan pass, so one amendment shows `replans=3` or more, not 1.

## 11. Persistence

`orchestrator/store.py`. The unit of persistence is the whole `Run`, serialised with `model_dump_json()` into one row of a SQLite table `runs(id, state, scenario, created_at, updated_at, body)`, upserted by `save_run`.

- The engine saves after each scheduling turn, after every approval, on stop, and when a run settles, so a crash loses at most the in-flight turn; a stage found `running` after a restart is re-queued (agents are re-runnable by design).
- A stored run is always a copy (loaded from JSON), so callers can never mutate persisted state by accident.
- File databases use WAL; one connection shared across threads with a re-entrant lock around every statement.
- The schema lives in one place, the pydantic models; the SQL columns exist only for listing and filtering.
- The CLI uses `data/runs.db`, the API uses `data/orchestrator.db`; both append to `data/audit.jsonl` by default. The shortener product can persist links to a JSON file (`SHORTENER_DATA_FILE`), which is a product concern, separate from run state.

Known limitation: there is no compare-and-swap. Two processes resuming the same run at the same time would be last-writer-wins. Inside one process the engine refuses a second `run()` on a live run, and the API returns 409 for an approval already being resolved. Another documented gap: if a stage that needed approval is interrupted mid-run by a restart, it is re-queued and asks for a new approval even though the earlier one was granted.

## 12. HTTP API surface

Orchestrator (`orchestrator/api.py`, port 8090, OpenAPI at `/docs` and in `docs/openapi.orchestrator.json`). Routes are `async def` and only translate HTTP to engine calls; a `Supervisor` owns the background tasks so long operations return `202` immediately.

| Method and path | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /scenarios` | scenarios with their full stage graph (the explicit DAG as data) |
| `POST /runs` | create and start a run in the background. Body: `scenario`, optional `requirement` (1 to 2000 characters, policy-scanned: `422 validation_error` if over-long, `422 policy_violation` if it holds a secret or unsafe URL; no run is created), optional `params` (merged over the scenario defaults; `params.llm` must be a JSON boolean, else `422 invalid_llm_param`; `true` opts this run into the LLM-backed agents and gives `422 llm_not_configured` if no key is configured; `params.auto_approve` is refused). `202` with links; with `?wait=true&timeout=N` blocks until the run is terminal or waiting for approval and returns the full view (`200`) |
| `GET /runs`, `GET /runs/{id}` | list (newest first, `?state=`) and detail (`?include_content=true` adds artifact bodies) |
| `POST /runs/{id}/approvals/{approval_id}/approve` and `/reject` | body `{actor, comment}`; `actor` mandatory, `comment` at most 2000 characters and policy-scanned (`422 policy_violation`, the approval stays pending); same `?wait=` behaviour |
| `POST /runs/{id}/amend` | change the requirement of an unfinished run and re-plan. Body `{requirement, actor, comment?}`; `actor` mandatory. Everything is validated before the background task starts. `202` with the old and new requirement hashes (`?wait=true` returns the settled run view with `200`); `404 run_not_found`; `409 run_already_finished` or `run_stopping`; `422 invalid_actor`, `invalid_requirement` (text identical to the current one), `policy_violation`, `validation_error` (missing, empty or over-long fields: requirement and comment at most 2000 characters) |
| `POST /runs/{id}/stop` | safe-stop; `409` if the run is already terminal |
| `GET /runs/{id}/trace` | telemetry events plus decision lineage |
| `GET /runs/{id}/dag` | nodes with current state, and dependency edges |
| `GET /metrics` | JSON snapshot, or Prometheus text with `?format=prometheus` |

Errors always use `{"error": {"code", "message"}}`: `unknown_scenario`, `auto_approve_not_allowed`, `invalid_llm_param`, `llm_not_configured`, `policy_violation`, `invalid_graph`, `invalid_actor`, `invalid_requirement`, `validation_error` (422); `run_not_found`, `approval_not_found`, `not_found` (404); `approval_not_pending`, `run_already_finished`, `run_stopping` (409). If a background task crashes, the `Supervisor` logs it and records the run as `failed` with a `finish` event and cancels pending approvals, instead of letting the failure vanish with the task.

The API has no authentication, authorisation or rate limiting. Every endpoint, including approve, reject, amend and stop, is open to anyone who can reach the port. Both services bind to `127.0.0.1` unless `HOST` is set, and `docker-compose.yml` publishes them on `127.0.0.1` only (the Docker image was never built, because no Docker daemon was available; `tests/test_packaging.py` checks the compose, Dockerfile and CI files as text only). `python -m orchestrator` stores runs in `data/orchestrator.db` (the CLI uses `data/runs.db`) and the `/metrics` aggregates are rebuilt from the stored runs when the API starts (finished runs only); the `events` half of `/trace` is an in-memory buffer that is empty after a restart.

The shortener (`shortener/app.py`, port 8080) exposes `GET /health`, `POST /api/v1/urls` (rate limited, idempotency key, optional alias and ttl), `GET`, `PATCH`, `DELETE /api/v1/urls/{code}` (soft delete), `GET /api/v1/urls/{code}/analytics`, and `GET /{code}` (302 redirect that records a click). Its error body uses the same `{error: {code, message}}` shape.

## 13. Agent design

An agent is a plain function with one signature (`orchestrator/models.py`):

```python
AgentFn = Callable[[AgentContext], AgentResult]
# AgentContext: run_id, stage_id, scenario, requirement, attempt, is_fallback, artifacts (deep copy), params
# AgentResult:  artifacts: dict[name, dict], summary: str
```

`build_agent_registry()` registers 26 agents: 22 deterministic ones (`orchestrator/agents/__init__.py`), which use no network, no model call and no API key and derive their output from the requirement text and the real shortener code, and 4 optional LLM-backed ones (`requirements_llm`, `normalize_llm`, `architecture_llm`, `documentation_llm`, described below). The LLM agents are always registered but only used when a run opts in.

| Agent | Produces | What it actually does |
|---|---|---|
| `requirements` | `requirements` | Detects features with a regex table (`agents/features.py`); emits problem, goals, Given/When/Then criteria, non-goals, assumptions, ambiguity flag. If no feature matches, it emits open questions instead of guessing. |
| `normalize`, `apply_interpretation` | `normalized_requirements` | Propose one bounded answer per open question; after human approval, write the answers in and add the features they imply. |
| `impact` | `impact` | Parses `shortener/*.py` with `ast`, builds a function index and a name-based call graph, finds the call path from each HTTP handler to the code a feature relies on, and lists impacted modules, regression risks, tests to re-run and public API deltas. Name-based matching over-approximates ("might touch"), and it says so. |
| `architecture` | `architecture` | Components (marking which already exist when impact is known), data model changes, API deltas, ADR-style decisions, constraints. |
| `api_change_review` | `api_change_review` | After approval, records exactly which changes were approved and whether they are breaking. |
| `impl_api`, `impl_store`, `impl_analytics`, `impl_alias`, `impl_ttl`, `impl_ratelimit`, `impl_ssrf` | `impl_<x>` | Call the real shortener code with sample inputs and record `{check, input, expected, actual, ok}` evidence. |
| `impl_audit`, `impl_retention` | `impl_<x>` | No code exists for these features, so they publish a design with `design_only: true`, `verified: false`; their only evidence is about the code they would build on. |
| `sync` | `implementation` | Merges the `impl_*` artifacts; raises if any part failed or none exist. |
| `testing`, `testing_fallback`, `rollback_testing` | `test_report` | Runs `pytest tests/shortener` in a subprocess plus a live in-process smoke test (6 checks); the fallback runs 3 smoke checks only; rollback records what it undid. |
| `documentation` | `documentation` | Writes `CHANGELOG.md`, `API.md`, `DESIGN.md` under `docs/generated/<run_id>/` (run id sanitised so it cannot escape the folder). |
| `release`, `rollback_release` | `release_checklist` | Builds a go/no-go checklist from the run's evidence, lists open risks, rollback plan and required approvals; writes it to disk and raises if no-go. Rollback deletes the file. |

Properties that matter to the engine:

- **Evidence-producing.** Agents publish what they checked, not just a verdict. Gates then check the evidence (for example `tests_green` reads the counts in `test_report`).
- **Idempotent and re-runnable.** Required because a stage may run again after a retry, a restart or a re-plan.
- **Pluggable, and now demonstrated.** The engine only calls `agents[name](ctx)`. The claim that an LLM-backed agent can replace an agent behind `AgentFn` is no longer only an argument: four agents have an LLM twin that is a function with exactly that signature, and the policy engine, exit gates, attempt timeout and retry ladder wrap its call like any other.

### The optional LLM layer

Deterministic by default; the layer is opt-in per run (CLI `--llm`, API `params.llm: true`, or `LLM_ENABLED=1` plus a key in `.env` or the environment). Full description, prompts, safety tests and real eval numbers: [docs/LLM.md](docs/LLM.md). Principle: **LLM proposes; deterministic tools verify; gates and humans decide.**

**Wiring.** `scenarios/common.py::llm_graph` is a graph transform. For each stage whose agent has an `_llm` twin and that does not require approval, it sets `agent = <name>_llm`, `fallback_agent = <name>` (the deterministic agent), `max_attempts >= 2` and a 120 s timeout. The offline graphs are untouched. Failure handling is therefore the engine's ordinary ladder (section 6): a missing key, a 429, an outage, malformed JSON or a failed exit gate is a failed attempt, then a retry, then the deterministic agent runs. Nothing special was added to the engine, and the whole sequence is in the audit trail. Real output with no key configured (2026-10-10): `retries=3 fallbacks=3` and `Generators: requirements=deterministic (fallback), architecture=deterministic (fallback), documentation=deterministic (fallback)`.

**Which agents.**

| Agent | Writes | Notes |
|---|---|---|
| `requirements_llm` | `requirements` | Paraphrases the keyword table cannot see; must raise open questions for vague text, none for clear text. `resolution` is forced to `""` by code. |
| `normalize_llm` | `normalized_requirements` | One bounded proposal per open question; status stays `proposed`. |
| `architecture_llm` | `architecture` | Grounded components re-added if the model forgot them; `constraints` copied from the product manifest, not from the model. |
| `documentation_llm` | `documentation` | A narrative on top of the deterministic facts; the markdown files on disk are written from facts only. |

Deliberately **not** LLM-backed: `impact` (reads the real code with `ast`: ground truth that is given to the model as input), `apply_interpretation` and `api_change_review` (they run only after a human approved), the implementation checks, testing and release. The approval stages stay deterministic because policy forbids a fallback on a stage that needs a human: a fallback is a different action nobody approved.

**Same shape, with provenance.** An LLM agent returns the same artifact keys as its deterministic twin, so gates and downstream agents are unchanged. Every `requirements`, `normalized_requirements`, `architecture` and `documentation` artifact carries `generator`: `llm:<model that answered>` or `deterministic` (the deterministic agents were given the field too). The CLI report prints a banner and a `Generators:` line for opted-in runs, with `(fallback)` where the deterministic agent ran instead.

**Safety model.**

1. *The requirement is untrusted data.* It is truncated to 2000 characters, wrapped in `<untrusted_requirement>` delimiters (a forged closing tag inside the text is removed), and the system prompt says to treat it as data and ignore instructions in it.
2. *No tool access.* The model returns JSON only; it cannot read or write run state, create or resolve approvals or change routing.
3. *Schema projection.* Its JSON is parsed through pydantic models with size caps; only declared fields are read, so an extra key such as `"approved": true` or `"skip_stages"` is dropped. Feature keys and question topics are `Literal` types over the product's real vocabulary.
4. *Policy scan.* The engine scans model output like any artifact. If an obedient model echoes a planted `password=...`, that is a hard stop (no retry, no fallback, safe-stop), and the value never reaches the decision log.
5. *Gates and humans.* Exit gates check shape and a few semantics; approvals come only from `Engine.approve` or `reject`. A model cannot approve anything.
6. Low temperature (0.2) and a fixed token cap.
7. *No control characters.* Model text ends up in terminals, reports and documents, so any answer containing a control character (ESC, carriage return, backspace, NUL, the C1 range) is invalid: in single-line fields none is allowed at all; only the multi-line documentation narrative may use newline and tab. An invalid answer is an ordinary failed attempt (retry, then the deterministic fallback). Independently of that, everything the CLI prints goes through `safe()` (section 7).
8. *A model may not call an explicitly vague request clear.* After parsing, `requirements_llm` cross-checks the deterministic verdict (`is_explicitly_vague`): if the request has no deterministic feature match and contains an explicit vague word (`safe`, `secure`, `protect`, `harden` and their forms, `better`, `improve`, `robust`, `reliable`) and the model returned zero `open_questions`, the answer is invalid (retry, then the deterministic fallback, which asks the human). This keeps a model from switching ambiguity detection off and skipping the human interpretation approval for a request like "Make links safer". It is a narrow keyword heuristic and not a guarantee: a vague request phrased without any of those words is not covered (recognising those is what the model is for), and it does not judge whether the model's questions are good ones.
9. *A refused key stops the traffic.* If Gemini answers 401, 403 or 400 "API key not valid", the client treats the key as permanently refused: it tries no other model, and every later call on that client fails at once without a network request (a small circuit breaker, `LLMAuthError`). The agent registry holds one client, so one bad key costs one request per process rather than several per stage; it takes a restart (or fixing the key and starting a new process) to try again, which is the intended trade-off. The error text never contains the key.

Prompt injection is mitigated, not solved: the guarantee comes from the model having no authority, not from the prompt wording. A manipulated model can still produce a misleading but harmless-looking artifact, which is why the human approval stages stay in place.

**Key handling.** The key is sent in a header (`x-goog-api-key`), never in a URL or a message; error text built from provider responses has it replaced by `<redacted>`; the `repr` of settings and clients never shows it; `.env` is git-ignored; and `tests/conftest.py` sets `LLM_ENABLED=0` so the suite is offline whatever the developer's `.env` says. Only Gemini is implemented and live-tested.

**What the evals say, and do not say.** `python -m evals --llm` on 10 labelled texts: the model got all 10 requirements cases right, the deterministic agents 65% precision and recall and 70% vague-versus-clear. Those are 10 cases written by the author, one run each: a smoke test, not a benchmark. docs/LLM.md also shows a case where the model's prose is fluent but wrong (it labelled an existing endpoint as an API addition).

### Why deterministic first

The subject under review is the control plane (gates, approvals, failure handling, audit), and deterministic agents make every scenario reproducible, runnable offline, testable in CI, and able to show exact failure behaviour via injection. The cost is real: the default "requirement understanding" is a regex feature table, so it recognises only the seven features it knows (`alias`, `ttl`, `analytics`, `rate_limit`, `ssrf`, `audit`, `retention`); the decomposition is authored rather than generated; and "implementation" verifies code that already exists rather than writing new code. The LLM layer addresses the first of those, for four artifacts only, and does not change the other two. What is still missing for production use of a model is in section 15: cost budgets, the prompt and model parameters in the decision lineage (today only the model name is recorded, as `generator`), a retry policy tuned for nondeterminism, and evaluation gating on prompt changes.

## 14. Key decisions and alternatives considered

| Decision | Chosen | Alternatives | Why this, and what it costs |
|---|---|---|---|
| Workflow engine | Custom, 869 lines (with docstrings), explicit DAG | LangGraph (agent graphs with checkpointing and interrupts); Temporal (durable workflow execution with signals and retry policies) | The assignment is about orchestration semantics, so they are written out where a reviewer can read them: gates, ladder, approvals, re-plan. No external service to run, and every behaviour has a test. Cost: no durable timers, no distributed workers, no battle-tested replay; for production the design maps onto Temporal's model (section 15) and I would not hand-roll that part. |
| Scheduling | `asyncio` loop, `wait(FIRST_COMPLETED)` plus a wake event | Polling loop with sleep; thread-per-stage with locks | Event-driven: no idle polling, no latency from a poll interval, deterministic ordering, and a single-threaded mutation rule that removes the need for locks. Cost: agents are synchronous and need a thread pool, and a timed-out thread cannot be killed. |
| Persistence | SQLite, one JSON document per run, WAL | Postgres with normalised tables; event sourcing | Zero setup, survives restart, schema in one place (pydantic). Cost: single node, whole-run rewrites on each save, no compare-and-swap, no queries across stage fields. |
| Agents | Deterministic, evidence-producing functions by default; optional LLM twins for four artifacts behind the same `AgentFn` seam, with the deterministic agent as the fallback | LLM agents everywhere; LLM only | Reproducible, offline, cheap to test and exact failure injection by default, and the LLM was added without touching the engine because the control plane already wraps every agent. Cost: shallow understanding by default (regex); LLM runs are nondeterministic, rate limited on the free tier, send the requirement to a third party and were evaluated on only 10 cases; "implementation" is still verification. |
| Deployment shape | In-process engine in the API process, background tasks | Queue plus worker fleet | Simple, debuggable, enough for the demo. Cost: the API process owns execution, no horizontal scale, no automatic resume after a crash. |
| Gates | Pure functions over artifacts, by name in a registry | Agents self-report quality; free-form checks inside stages | Independent of the producer, unit-testable, auditable, and they fail closed. Cost: gates only see what agents publish; a dishonest or buggy agent that publishes false evidence can pass them (the implementation agents mitigate by probing the code rather than claiming). |
| Approval bypass for demos | Engine constructor argument (`AutoApprovePolicy`) | A run param or environment variable | Run params are client input; a param would let API callers disable human review. Cost: the CLI needs its own flag. |
| Policy | Declarative rule lists, hard stop on violation | Soft warnings; per-stage ad hoc checks | Cannot be skipped by a stage author or an agent; one place to extend. Cost: heuristic detection, blunt response (the whole run safe-stops). |
| Rollback semantics | A rollback agent per stage, honest about what it can undo | Generic compensation framework | Only the effects that exist are undone (a file, an absent report); no illusion of reverting a deployment. Cost: rollback is shallow because the pipeline has few side effects. |
| Metrics | Computed from the run on completion, in-memory per process | Prometheus client library, OpenTelemetry | Keeps definitions inspectable in code and testable; Prometheus text is rendered for scraping. Cost: not durable in the API process; no histograms. |

## 15. How this would scale to production

| Area | Today | Production direction |
|---|---|---|
| Execution | one process runs the loop; agents on a six-thread pool | Queue and worker fleet: a coordinator persists state and enqueues stage executions; stateless workers run agents; an executor with real cancellation (processes or containers), so timeouts actually stop work. Alternatively adopt a durable-execution engine (Temporal) and keep the stage, gate and policy definitions. |
| State | SQLite, last writer wins | Postgres with optimistic locking (a version column checked on every `save_run`), so a second resumer fails instead of overwriting; a leader or lease per run; startup recovery of runs found `running`. Store artifacts and large payloads outside the run row. |
| Resume and recovery | manual `resume` after a crash | A reconciler that finds `running` and `waiting_approval` runs and resumes them; persist the approval that was granted so a restart does not ask again (the documented gap). |
| Observability | in-memory events, JSONL file, in-process metrics | OpenTelemetry traces (one trace per run, one span per attempt, gate and approval events as span events), metrics exported to a time-series store, audit log shipped to append-only storage with retention and tamper evidence. MTTR computed from explicit failure and recovery timestamps rather than from decisions. |
| AuthN and AuthZ | none: every endpoint is open to whoever can reach the port (loopback by default); `actor` is a free-text string, for approvals and for requirement amendments | Identity provider in front of the API; approver and amender identity taken from the token, not the body; roles per approval kind (release approvers differ from API reviewers) and a role for amending; enforce that the approver is not the requester or the amender; signed approval records. |
| Agents | deterministic functions; four optional Gemini-backed twins (schema-validated, policy-scanned, deterministic fallback, `generator` provenance on the artifact) | What is built: schema validation of model output, the policy engine on all output, the deterministic fallback, and a small eval harness. What is not: the prompt version, model parameters and token counts in the decision lineage (only the model name is recorded); token and cost budgets per stage; retries tuned for nondeterminism; a held-out evaluation set whose regression gates a prompt change like a code change; more than one provider; a hosted or private model for requirements that must not leave the organisation. |
| Secrets | the optional model key is read from the environment or a git-ignored `.env`, sent only in a request header and scrubbed from error text; policy rejects secrets in artifacts, requirements and comments (heuristically) | Secrets manager for model keys and tool credentials, injected into workers and never placed in `params` or artifacts; the secret-scan rule stays as a backstop. |
| Product side effects | agents verify and write files | Real changes through pull requests on a branch, CI as the testing stage, deployment through the existing pipeline; rollback becomes a real revert; the release approval gates a merge or deploy. |
| Policy and gates | in code | Versioned policy bundles loaded per environment, with change control of their own. |
| Dependencies, CI and packaging | `requirements-lock.txt` pins the 6 direct dependencies (not their transitive ones, no hash checking); CI installs from `requirements.txt` with a read-only token; the Dockerfile and compose file exist but the image was never built | A fully resolved, hash-pinned lock used by CI and the image; the image built and scanned in CI before anything is published. |
| Multi-tenancy and limits | none | per-tenant quotas, concurrency limits on runs and on agent calls, rate limiting on the orchestrator API. |

The design choices that make this migration cheap: state is one serialisable object, all effects go through `AgentFn` and two Protocols, mutations happen in one place, and every transition is already a recorded decision.
