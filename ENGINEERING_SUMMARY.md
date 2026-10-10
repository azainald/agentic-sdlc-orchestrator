# Final Engineering Summary

An agentic SDLC orchestrator: a small, explicit-DAG workflow engine that runs a chain of
(deterministic) agents with quality gates, human approvals, policy guardrails, retries,
rollback, an audit trail and metrics. It drives a URL-shortener product through three
scenarios: greenfield, brownfield and ambiguous.

This document is written for a reviewer. Where the system does less than the words
"agent", "implementation" or "release" might suggest, it says so (see Limitations).

## 1. Plan and rationale

**Goal.** Show that an agentic delivery pipeline can be controlled, observable and safe,
not only that it can produce output.

**Approach, in the order it was decided.**

1. **Make the workflow data, not code.** A scenario is a list of `StageSpec` objects
   (`orchestrator/models.py`): `depends_on`, `join` (`all` or `any`), `entry_gates`,
   `exit_gates`, `max_attempts`, `backoff_ms`, `fallback_agent`, `rollback_agent`,
   `requires_approval`, `approval_kind`, `timeout_ms`, `consumes`. The graph is validated
   (cycles, unknown or duplicate ids, bad joins) before a run is created. This keeps the
   DAG explicit and inspectable (`GET /scenarios`, `GET /runs/{id}/dag`).
2. **One small engine that owns all state changes.** `orchestrator/engine.py` (about 700
   lines) is a single asyncio loop. Agents are plain synchronous functions run in a thread
   pool so parallel stages really overlap, but they only return a result. Every mutation
   of the `Run` happens on the event-loop thread, so there are no locks in the engine.
3. **Gates check evidence; policy is a hard stop.** Gates are pure functions over
   artifacts, stage states and approvals, and fail closed (an unknown or crashing gate
   counts as a failure). Policy rules are separate: a `PolicyViolation` is never retried,
   never falls back, never rolls back, and safe-stops the rest of the run.
4. **Humans are a first-class pause, not an exception.** A gated stage moves to
   `WAITING_APPROVAL`, the whole `Run` is saved as JSON, and the process can exit. Any later
   process can approve or reject and resume (SQLite store, CLI `resume`, HTTP API).
5. **Make every decision auditable.** Each gate result, retry, fallback, rollback,
   approval, re-plan, stop and policy decision becomes a `DecisionRecord` on the run (with a
   rationale) and a telemetry event (optional append-only JSONL file).
6. **Prove it on a real product.** The shortener is real, tested code. The agents derive
   artifacts from the requirement text and from the real code: `ast`-based impact analysis,
   probes that call real functions, a real pytest subprocess, and an in-process smoke test.
7. **Deterministic agents on purpose.** Behaviour is reproducible, tests need no network or
   API key, and the engine, which is the thing under evaluation, can be tested without LLM
   noise. The agent contract (`AgentContext -> AgentResult`) is the seam where a real LLM
   agent would plug in (see Next steps).

**Why a custom engine.** The semantics wanted (per-stage entry and exit gates, `join=any`,
re-plan when an input artifact's hash changes, approvals that survive restart, fail-closed
policy) are specific enough that they would have been written on top of a framework anyway.
Writing them directly produced something small enough to read and defend end to end. The
price is that durability, distribution and UI are not provided (see Limitations).

**The three scenarios** (`scenarios/greenfield.py`, `brownfield.py`, `ambiguous.py`;
shared tail in `scenarios/common.py`):

| Scenario | Stages | Human approvals | What it shows |
|---|---|---|---|
| greenfield | 9 | `production_release` | clear requirement; three parallel `impl_*` checks join in `sync`; `testing` and `documentation` run in parallel; approved release |
| brownfield | 11 | `public_api_change`, `production_release` | impact analysis over the real source; human review of public API changes before implementation |
| ambiguous ("Make links safer") | 11 | `requirement_interpretation`, `production_release` | vagueness detected; bounded interpretations proposed; nothing is designed until a human approves and the `ambiguity_resolved` gate passes |

## 2. Artifacts delivered

| Artifact | Path | Purpose |
|---|---|---|
| Workflow engine | `orchestrator/engine.py` | DAG scheduling, parallel + join, gates, retry with backoff, fallback, rollback, safe-stop, approvals, re-plan, timeouts, metrics |
| Data model | `orchestrator/models.py` | `StageSpec`, `Run`, `Artifact`, `Approval`, `DecisionRecord`, `AutoApprovePolicy`; everything is a pydantic model so a run is JSON |
| Graph helpers | `orchestrator/graph.py` | Validation, Kahn topological order, `downstream`, `join` logic |
| Gates | `orchestrator/gates.py` | 9 evidence gates (requirements, ambiguity, architecture, impact, implementation, tests green, docs, release ready, policy exceptions) |
| Policy engine | `orchestrator/policy.py` | Secret scanner, unsafe-URL scanner, stage rules (high impact needs approval, release not autonomous, no fallback on approval stages), reserved approver names |
| Persistence | `orchestrator/store.py` | SQLite (WAL) and in-memory run stores; a run is stored as its own JSON |
| Telemetry | `orchestrator/telemetry.py` | Bounded event log, JSONL audit file, run metrics (success rate, retries, MTTR, latency, p95), Prometheus text |
| HTTP API | `orchestrator/api.py` | Start runs, approve/reject, stop, DAG, trace, metrics; background supervisor |
| Agents | `orchestrator/agents/` | Requirements, normalize, apply_interpretation, impact (ast), architecture, 9 `impl_*` verifiers, `sync`, testing (+fallback, +rollback), documentation, release (+rollback), api_change_review |
| Scenarios and CLI | `scenarios/` | The three DAGs, registry, CLI (`python -m scenarios ...`), plain-text reports |
| Product under orchestration | `shortener/` | URL shortener API: create, alias, ttl, redirect, analytics, idempotency, rate limit, SSRF guard |
| Tests | `tests/orchestrator`, `tests/scenarios`, `tests/shortener` | 806 tests (see section 5) |
| Machine-readable contracts | `docs/openapi.orchestrator.json`, `docs/openapi.shortener.json`, `scripts/export_openapi.py` | OpenAPI for both services |
| Overview and run instructions | `README.md` | How to install, run, demo |
| Architecture | `ARCHITECTURE.md` | Design, state machine, diagrams |
| Scenario walkthroughs | `docs/SCENARIOS.md` | Each scenario, expected output |
| Packaging and CI | `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `.github/workflows/ci.yml` | Container, install metadata, tests on Python 3.10 and 3.12 plus a scenario smoke run |
| This document | `ENGINEERING_SUMMARY.md` | Reviewer-facing summary |
| Presenter notes | `INTERVIEW_NOTES.md` | Walkthrough guide |

## 3. How AI assistance was used

The system was built with AI assistance. Described plainly:

- **Parallel subagents with strict file ownership.** Work was split so that each agent owned
  a disjoint set of files (engine, policy and gates, store and telemetry, API, agents,
  scenarios and CLI, shortener, tests). No two agents edited the same file, which removed
  merge conflicts and made each piece reviewable on its own.
- **A shared contracts file.** `orchestrator/contracts.py` defines the interfaces between
  components (`RunStore`, `TelemetrySink`, and the engine's public surface) together with
  the data model in `models.py`. Agents built against these instead of against each other's
  code, so the pieces fit when assembled.
- **An independent, read-only review agent.** After the build, a separate agent that had not
  written the code reviewed it without the ability to edit. It reported **12 defects; 9
  were reproduced.** They were then fixed, each with a regression test. The review found
  real problems that the build agents, and passing tests, had not caught. The notable ones:

| Defect class (as found) | Fix now in code | Regression tests (examples) |
|---|---|---|
| **`auto_approve` bypass.** Approval could be switched off by something a client controls. | Auto-approval is an engine constructor argument (`AutoApprovePolicy`), never a run parameter. The API rejects `params.auto_approve` with 422, and the engine would ignore it anyway. The machine actor names are reserved. | `test_client_cannot_enable_auto_approve_through_run_params`, `test_run_params_can_never_enable_auto_approve`, `test_reserved_actor_names_cannot_approve_or_reject_over_http` |
| **Policy bypasses.** Secrets and unsafe URLs could slip past the scanners (key-name variants, nesting, header forms, scheme tricks); the approval kind could dodge high-impact rules by case or whitespace; a fallback agent could run a different action on a stage a human had approved. | Scanners walk every scalar with its key path and normalise names (`clientSecret`, `Client-Secret`); schemes are checked after stripping whitespace and control characters, also inside `href=`/`src=` text; `approval_kind` is normalised on the spec; stages that need approval may not define a fallback. | `test_secret_bypasses_are_rejected_without_echoing_the_secret`, `test_unsafe_schemes_rejected_in_href_and_src_attributes_inside_strings`, `test_approval_kind_case_cannot_dodge_high_impact_handling`, `test_create_run_rejects_a_fallback_on_a_stage_that_needs_approval` |
| **Stale-input re-plan.** A stage that was still running when its input changed finished on stale data and was never re-run. | `_replan_if_self_stale` checks, when a stage finishes, whether any consumed artifact's hash changed while it ran. | `test_running_consumer_that_finishes_on_stale_input_is_replanned` |
| **Entry gates not re-checked after approval.** Evidence could change while a stage waited for a human, and the stage still launched. | An approved stage goes through the entry gates again before it starts; if they now fail it is BLOCKED. | `test_entry_gates_are_rechecked_after_approval` |
| **Stop semantics.** A stop request did not reliably stop retries, the fallback agent or the rollback agent, and the final state was wrong. | After stop: in-flight attempt finishes, no further attempts, no fallback, no rollback launch; unstarted stages are SKIPPED; pending approvals are cancelled; the run ends STOPPED (not FAILED). | `test_stop_skips_remaining_retries_and_the_run_ends_stopped_not_failed`, `test_stop_skips_the_fallback_agent`, `test_stop_does_not_launch_the_rollback_agent_and_the_stage_counts_as_failed` |

  Other hardening that also has regression tests: per-attempt timeouts, a hostile `run_id`
  cannot escape `docs/generated/`, an unreadable pytest summary never counts as green, and
  double or concurrent approval returns 409.

- **Who decided what.** The AI assistant proposed the architecture (explicit DAG, custom
  engine, deterministic agents, SQLite), the three-scenario plan, and the independent-review
  step. The candidate reviewed and approved the plan before any code was written, chose the
  product (the URL shortener named in the brief), the no-paid-API-key constraint, and
  accepted or rejected each result. Humans own the approvals, the scope and the final
  acceptance; the AI did most of the typing. Passing tests written by the same agents that
  wrote the code were not treated as proof, which is why an independent review was added.

## 4. Risks, trade-offs and failure scenarios

| Risk | Impact | Mitigation in code | Residual |
|---|---|---|---|
| An agent raises, returns garbage, or fails its exit gate | Stage fails | Bounded retries with exponential backoff (`backoff_ms * 2^(n-1)`), then optional fallback agent, then rollback agent; a non-`AgentResult` return is just a failed attempt; artifacts are published only if the whole attempt (agent, policy, exit gates) succeeds | Rollback agents are mostly symbolic (see Limitations) |
| An agent hangs | Stage stalls | `timeout_ms` per attempt (testing stage: 120 s); timeout counts as a failed attempt | A Python thread cannot be killed: the worker keeps running and holds a pool slot; its late result is discarded but its side effects remain |
| An agent leaks a secret or emits a `javascript:` URL | Unsafe artifact reaches later stages or docs | `no_secrets` and `safe_destination_urls` run on every artifact before publish; violation is a hard stop with no retry or fallback; messages name the field and secret kind, never the value | Pattern-based scanner; the generated markdown files on disk are built from artifacts and the manifest but are not re-scanned |
| Something approves its own release | Unreviewed production release | `AutoApprovePolicy` only via engine constructor; API refuses `auto_approve`; reserved actor names rejected; high-impact stages must require approval; release may not be `autonomy=full`; agents get a deep-copied read-only context and have no handle to the engine | No authentication: the approver name is whatever the caller supplies; cannot enforce requester differs from approver |
| Process dies mid-run | Lost progress | The run is saved after every scheduling turn; a RUNNING stage is re-queued on resume (`_recover_interrupted`); approvals survive restart | In-flight work is redone; a re-queued approval-gated stage asks for a new approval; nothing restarts a crashed run automatically (CLI `resume <run_id>`; the HTTP API has no resume endpoint) |
| Two processes resume the same run | Last writer wins, corrupted history | A single-process guard (`_live`, API `resolving` set) | No compare-and-swap in the store: unsafe across processes |
| Upstream artifact changes after a consumer ran | Silent stale results | Artifacts are hashed; consumers record input hashes; changed input resets the consumer and everything downstream (re-plan) | Re-plans are capped at 3 per source stage; on exceeding it, stale stages FAIL loudly |
| Re-plan loops forever | Run never ends | Hash comparison (re-producing identical content is not a change), `MAX_REPLANS = 3` per source stage counted from persisted decisions, and a loud failure instead of continuing stale | Total work is bounded but could still be a few multiples of the graph |
| Metrics mislead | Wrong operational picture | `approval_wait_ms` separates human waiting from system time; `active_ms` reported next to end-to-end latency | MTTR and `active_ms` are approximations (see Limitations) |
| Shortener abuse (SSRF targets, floods, retries) | Unsafe redirects, duplicate links | Destination guard (scheme allow-list, no credentials, private/loopback/link-local/reserved IPs including decimal, hex, octal and IPv4-mapped IPv6, blocked local suffixes); sliding-window rate limit; idempotency keys with body fingerprint | No DNS resolution (rebinding not covered); limiter and idempotency store are in memory and per process |

## 5. Validation approach and guardrails

**Test pyramid (806 tests, `python -m pytest -q -W ignore`, about 25 seconds; parametrised
cases are counted individually).**

| Layer | Tests | What it covers |
|---|---|---|
| Unit | 529 | graph (18), gates (13), policy (320, mostly parametrised bypass attempts), run stores (23), telemetry (16), shortener validation (106), store (13), rate limiter (6), manifest-vs-code (14) |
| Engine component | 68 | stub agents: parallel overlap and join, `join=any`, retry/backoff, fallback, rollback, stop, approvals, restart with a new `Engine`, re-plan and its cap, timeouts, auto-approve rules, metrics |
| HTTP | 122 | orchestrator API (56) and shortener API (66) through `TestClient`: status codes, error format, 409 on double approval, restart through a shared store, shutdown behaviour |
| Agents on real code | 38 | each agent against the real shortener; a regression in the real code fails the part; no artifact contains secrets or unsafe URLs |
| Scenario integration | 32 | all three scenarios end to end, pause and resume on new engines, parallel overlap, decisions all carry a rationale |
| CLI | 17 | process-level behaviour, exit codes, resume across invocations, JSON output |

One test (`test_real_pytest_subprocess_path_once`, marked `slow`) launches the real pytest
subprocess; the others skip it with `skip_pytest`. CI (`.github/workflows/ci.yml`) runs the
suite on Python 3.10 and 3.12 and then a greenfield scenario smoke run.

**Failure injection** is built into the product, not bolted on: run parameter
`inject_failure` (CLI `--inject-failure`) with three modes. `testing` fails the first attempt
only (retry succeeds), `testing_hard` fails every primary attempt (fallback succeeds),
`testing_all` fails the fallback too (rollback, release skipped, run ends ROLLED_BACK, CLI
exit code 1). The engine tests add timeouts, garbage returns, crashing rollback agents and
exit-gate failures.

**Policy tests** (320 cases) attack the scanners with key-name variants, nesting, header
forms, scheme obfuscation and approval-kind tricks, and assert that the violation message
never echoes the secret.

**Review.** The independent read-only review described in section 3, with every finding
turned into a regression test.

**Guardrails at runtime.** Policy hard stops; fail-closed gates; entry gates re-checked after
approval; no unreviewed fallback on approval stages; reserved approver names; read-only
agent context; bounded retries, timeouts and re-plans; audit trail for every decision.

## 6. Assumptions

- Agents are trusted, in-process Python functions. The trust boundary is the process, not the
  agent.
- One orchestrator process owns a database at a time (SQLite file, single writer).
- The shortener already exists and is correct enough to verify; "implementation" in this
  system means verifying real code with evidence, not writing it.
- Requirements are short English text. Feature detection uses a fixed table of nine features.
- Wall-clock time is monotonic enough for latency metrics.
- Approvers are identified by a name supplied by a trusted front end or operator.
- The demo auto-approve switch is for demos and tests only.

## 7. Limitations

Stated candidly; most are also documented in code docstrings.

- **Agents are deterministic, not LLM-backed.** They derive artifacts from keyword tables and
  from the real code. No agent writes product code; the `impl_*` agents call real functions
  with sample inputs and record evidence. `impl_audit` and `impl_retention` are design-only
  and say so (`design_only: true`, `verified: false`).
- **No authentication for approvers.** The `actor` is caller-supplied. The engine blocks
  blank and reserved names but cannot prove who someone is, nor that the approver differs
  from the requester.
- **Single-process safety.** There is no compare-and-swap in the store, so resuming one run
  from two processes at once is unsafe (last writer wins).
- **Restart re-requests approval.** A stage that was RUNNING when the process died is
  re-queued; if it needed approval it asks for a new one although an earlier one was granted.
  There is also no automatic restart of crashed runs, and the HTTP API has no resume
  endpoint (the CLI `resume` command re-enters the run).
- **MTTR is an approximation.** It is measured from the first retry, fallback or rollback
  decision to the end of the run, for runs that succeeded. The failure timestamp is not
  stored, the run end includes any later human approval wait, and runs that never recovered
  contribute nothing.
- **`active_ms` under-reports parallel work.** It is end-to-end time minus the time any
  approval was pending, so work done by other branches while an approval waits is subtracted
  too.
- **No DNS resolution in the SSRF guard.** Hostnames that resolve to private addresses and
  DNS rebinding are not detected. The shortener only redirects, it never fetches; the guard
  protects whatever later follows the link.
- **In-memory rate limiter and idempotency store.** Per process; idempotency keys are global
  (not per client) and never expire. The JSON file store rewrites the whole file on change.
- **Requirement understanding is keyword-based.** A request that matches no feature and no
  "vague" word gets generic questions.
- **The call graph is name-based.** `x.add()` matches every `add`: an over-approximation, fine
  for "what might this touch", not a type checker.
- **Timeouts cannot kill threads.** A timed-out agent keeps running; with six worker threads
  (default), repeated hangs would starve the pool.
- **Rollback agents are mostly symbolic.** `rollback_release` deletes the checklist file and
  `rollback_testing` records that nothing was published. Nothing is deployed in this system,
  so there is little real to undo.
- **Some model fields are informational.** `parallel_group` and the `full` versus
  `supervised` autonomy levels do not change scheduling (only `human_gated` and the release
  rule do). The `no_policy_exceptions` gate is real but no shipped scenario requests a
  `security_exception`, so there it always passes.
- **Live trace events are bounded and in memory.** `GET /runs/{id}/trace` returns events from
  the last 5000 in memory (older ones are in the JSONL file); the decisions stored on the run
  are complete.

## 8. Next steps

1. **A real LLM agent behind the same contract**: strict schema validation of the output, the
   existing policy check on the result, retries and a deterministic agent as the fallback,
   and an evaluation harness (golden requirements, recorded responses in CI, live runs on a
   schedule). Keep humans on high-impact stages.
2. **Authentication and approver separation**: put an identity provider in front of the API,
   record the requester, and enforce requester != approver for `production_release`.
3. **Durable multi-process execution**: a Postgres store with a version column (compare and
   swap), a lease per run, and workers pulling from a queue; or adopt a durable-execution
   system and keep this model as the definition layer.
4. **Reuse granted approvals after a restart** when the stage's inputs have not changed.
5. **Real isolation for agents**: run them as subprocesses or containers so a timeout can
   kill them and a rollback can undo real side effects.
6. **Better metrics**: store a "first failure at" timestamp per stage, compute MTTR per stage,
   and track critical-path time rather than active time.
7. **Shortener**: resolve DNS at redirect or fetch time with pinning, move idempotency keys
   and rate limiting to a shared store with expiry and per-client scope.
8. **An API resume endpoint** and a startup scan that offers to resume runs left RUNNING.
