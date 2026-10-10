# Final Engineering Summary

An agentic SDLC orchestrator: a small, explicit-DAG workflow engine that runs a chain of
agents with quality gates, human approvals, policy guardrails, retries, rollback, an audit
trail and metrics. The agents are deterministic by default; four of them have an optional
Gemini-backed twin behind a flag. It drives a URL-shortener product through three
scenarios: greenfield, brownfield and ambiguous, and it can re-plan a run when a person
amends the requirement mid-flight.

This document is written for a reviewer. Where the system does less than the words
"agent", "implementation" or "release" might suggest, it says so (see Limitations).

## 1. Plan and rationale

**Goal.** Show that an agentic delivery pipeline can be controlled, observable and safe,
not only that it can produce output.

**Approach, in the order it was decided.**

1. **Make the workflow data, not code.** A scenario is a list of `StageSpec` objects
   (`orchestrator/models.py`): `depends_on`, `join` (`all` or `any`), `entry_gates`,
   `exit_gates`, `max_attempts`, `backoff_ms`, `fallback_agent`, `rollback_agent`,
   `requires_approval`, `approval_kind`, `timeout_ms`, `consumes`, `reads_requirement`. The graph is validated
   (cycles, unknown or duplicate ids, bad joins) before a run is created. This keeps the
   DAG explicit and inspectable (`GET /scenarios`, `GET /runs/{id}/dag`).
2. **One small engine that owns all state changes.** `orchestrator/engine.py` (869
   lines including docstrings) is a single asyncio loop. Agents are plain synchronous functions run in a thread
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
   approval, re-plan, requirement amendment, stop and policy decision becomes a
   `DecisionRecord` on the run (with a rationale) and a telemetry event (optional
   append-only JSONL file).
6. **Prove it on a real product.** The shortener is real, tested code. The agents derive
   artifacts from the requirement text and from the real code: `ast`-based impact analysis,
   probes that call real functions, a real pytest subprocess, and an in-process smoke test.
7. **Deterministic agents on purpose, an LLM behind the same seam as an opt-in.** Behaviour
   is reproducible, tests need no network or API key, and the engine, which is the thing
   under evaluation, can be tested without LLM noise. The agent contract
   (`AgentContext -> AgentResult`) is the seam; it was later used for four optional
   Gemini-backed agents (step 9).
8. **Re-plan lazily, and let a human trigger it.** Every stage remembers the hash of each
   artifact it consumed. When an artifact changes, only the stages that consumed the old
   version are reset; their consumers are reset later, and only if the new output differs.
   A stage does not start while an ancestor is being re-run, so nobody is asked to approve
   evidence that is about to be replaced. `Engine.amend_requirement` (CLI `amend`,
   `POST /runs/{id}/amend`) is the human trigger: named actor, policy-scanned text and
   comment, pending approvals withdrawn and asked again, stages that read the raw
   requirement text (`reads_requirement`) re-run even when their artifacts are unchanged,
   bounded by the existing re-plan cap (the fourth re-plan from one source stage fails the run
   by design). See
   [docs/REPLAN.md](docs/REPLAN.md).
9. **LLM proposes; deterministic tools verify; gates and humans decide.** The optional layer
   (`orchestrator/llm/`, `orchestrator/agents/llm_agents.py`) writes `requirements`,
   `normalized_requirements`, `architecture` and `documentation` instead of the rule-based
   agents. The model has no tools; its JSON is schema-projected (unknown keys dropped),
   policy-scanned and exit-gated like any artifact; output containing a terminal control
   character is invalid; a model cannot answer "clear" for an explicitly vague request (a
   keyword cross-check, a heuristic and not a guarantee); a refused API key trips a circuit
   breaker so no further calls are made; approval stages are never LLM stages;
   any failure goes through the engine's retry, then fallback ladder to the deterministic
   agent; every artifact records its `generator`. See [docs/LLM.md](docs/LLM.md).

**Why a custom engine.** The semantics wanted (per-stage entry and exit gates, `join=any`,
re-plan when an input artifact's hash changes, approvals that survive restart, fail-closed
policy) are specific enough that they would have been written on top of a framework anyway.
Writing them directly produced something small enough to read and defend end to end (the engine
is 869 lines including docstrings). The
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
| Workflow engine | `orchestrator/engine.py` | DAG scheduling, parallel + join, gates, retry with backoff, fallback, rollback, safe-stop, approvals, lazy re-plan, requirement amendment (`amend_requirement`), timeouts, metrics |
| Data model | `orchestrator/models.py` | `StageSpec`, `Run`, `Artifact`, `Approval`, `DecisionRecord`, `AutoApprovePolicy`; everything is a pydantic model so a run is JSON |
| Graph helpers | `orchestrator/graph.py` | Validation, Kahn topological order, `downstream`, `join` logic |
| Gates | `orchestrator/gates.py` | 9 evidence gates (requirements, ambiguity, architecture, impact, implementation, tests green, docs, release ready, policy exceptions) |
| Policy engine | `orchestrator/policy.py` | Secret scanner, unsafe-URL scanner, stage rules (high impact needs approval, release not autonomous, no fallback on approval stages), reserved approver names. The same scanners are applied to the requirement (capped at 2000 characters) and to every approve, reject and amend comment |
| Persistence | `orchestrator/store.py` | SQLite (WAL) and in-memory run stores; a run is stored as its own JSON |
| Telemetry | `orchestrator/telemetry.py` | Bounded event log, JSONL audit file, run metrics (success rate, retries, MTTR, latency, p95), Prometheus text |
| HTTP API | `orchestrator/api.py` | Start runs (optionally `params.llm`, which must be a JSON boolean and needs a configured key), approve/reject, amend the requirement, stop, DAG, trace, metrics; background supervisor. No authentication |
| Agents | `orchestrator/agents/` | 22 deterministic: requirements, normalize, apply_interpretation, impact (ast), architecture, 9 `impl_*` verifiers, `sync`, testing (+fallback, +rollback), documentation, release (+rollback), api_change_review |
| Optional LLM layer | `orchestrator/llm/`, `orchestrator/agents/llm_agents.py`, `llm_schemas.py` | `LLMClient` interface, Gemini REST client, settings (environment, then git-ignored `.env`), scripted fake for tests, four LLM agents with provenance (`generator`) |
| Evals | `evals/` | `python -m evals [--llm]`: deterministic vs LLM agents on 10 labelled requirement texts |
| Scenarios and CLI | `scenarios/` | The three DAGs, registry, CLI (`python -m scenarios ...` incl. `amend`, `--llm`), plain-text reports |
| Product under orchestration | `shortener/` | URL shortener API: create, alias, ttl, redirect, analytics, idempotency, rate limit, SSRF guard |
| Tests | `tests/orchestrator`, `tests/scenarios`, `tests/llm`, `tests/shortener`, `tests/test_packaging.py` | 1,069 collected: 1,067 pass, 2 live-model tests skipped by default (see section 5) |
| Machine-readable contracts | `docs/openapi.orchestrator.json`, `docs/openapi.shortener.json`, `scripts/export_openapi.py` | OpenAPI for both services |
| Overview and run instructions | `README.md` | How to install, run, demo |
| Architecture | `ARCHITECTURE.md` | Design, state machine, diagrams |
| Scenario walkthroughs | `docs/SCENARIOS.md` | Each scenario, real output, the amend demo and the `--llm` fallback demo |
| Re-plan and LLM pages | `docs/REPLAN.md`, `docs/LLM.md` | Algorithm, governance, real runs and limits of the amendment feature; design, safety model, real eval numbers and limits of the LLM layer |
| Packaging and CI | `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `requirements-lock.txt`, `.github/workflows/ci.yml` | Container, install metadata, the 6 direct dependencies pinned (not their transitive ones), tests on Python 3.10 and 3.12 plus a scenario smoke run with a read-only CI token. Compose publishes both ports on `127.0.0.1` only because the API has no authentication. The Docker image was never built (no Docker daemon was available) and the CI workflow could not be run here; only text-level checks exist (`tests/test_packaging.py`). |
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
  written the code reviewed it without the ability to edit. It reported **12 findings; 9
  were reproduced by running code and 3 were suspected from reading it.** Ten were fixed,
  each with a regression test, and two were documented as known limitations instead
  (multi-process resume has no compare-and-swap; a restart re-requests approval for an
  in-flight approved stage). The review found
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

- **Two late features, same working rules.** The requirement amendment with its lazy re-plan,
  and the optional Gemini layer, were added after the first build. They were developed against
  tests that need no network: the LLM tests use a scripted fake model (`ScriptedLLM`) that
  also records every prompt, and `tests/conftest.py` forces `LLM_ENABLED=0` so the suite is
  offline whatever a developer's `.env` says. What was run against the real model is limited
  and recorded in `docs/LLM.md`: the 10-case eval, two full scenario runs, and a run with a
  deliberately invalid key to see the fallback path against the real API. Nothing in this
  repository records these live runs being repeated after the final hardening round below
  (the new control-character, vague-request and circuit-breaker behaviour is covered by
  offline tests only). The independent
  review described above was done on the original build; this document does not claim it
  was repeated, with the same method, for these two features.
- **A final hardening round.** A last review of the finished system (the commit is titled
  "Fix final-review findings"; this document does not record how many findings or who raised
  them) led to one more set of fixes, each with its own regression tests (`test_governance.py`,
  `test_api_governance.py`, `test_report_safety.py`, `test_hardening.py`, `test_packaging.py`):
  the requirement and every approve, reject and amend comment are policy-scanned and bounded
  before use (a refusal creates no run, or leaves the run unchanged and audited); the approver's
  review text shows what approving applies (`adds feature`, `rationale`) and warns on an empty
  review; every terminal print neutralises control characters, and model output containing one is
  rejected (retry, then the deterministic fallback); a model cannot call an explicitly vague
  request clear (a keyword heuristic, not a guarantee); `params.llm` must be a JSON boolean and
  needs a configured key; amendment decisions keep the old and new text and approval requests
  keep the hashes of the evidence shown; stages that read the requirement text re-run on an
  amendment; a refused API key trips a circuit breaker; compose (image never built) publishes on loopback only, the CI
  token is read-only and the 6 direct dependencies are pinned. As before, passing tests written
  alongside the fixes are not proof that nothing else is wrong.
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
| Something approves its own release | Unreviewed production release | `AutoApprovePolicy` only via engine constructor; API refuses `auto_approve`; reserved actor names rejected; high-impact stages must require approval; release may not be `autonomy=full`; agents get a deep-copied read-only context and have no handle to the engine | No authentication anywhere: the API is open to whoever can reach the port (loopback by default, and compose publishes on loopback only), and the approver name is whatever the caller supplies; cannot enforce requester differs from approver |
| Process dies mid-run | Lost progress | The run is saved after every scheduling turn; a RUNNING stage is re-queued on resume (`_recover_interrupted`); approvals survive restart | In-flight work is redone; a re-queued approval-gated stage asks for a new approval; nothing restarts a crashed run automatically (CLI `resume <run_id>`; the HTTP API has no resume endpoint) |
| Two processes resume the same run | Last writer wins, corrupted history | A single-process guard (`_live`, API `resolving` set) | No compare-and-swap in the store: unsafe across processes |
| Upstream artifact changes after a consumer ran | Silent stale results | Artifacts are hashed; consumers record input hashes; a changed input resets the stale consumer, and its own consumers are reset later only if its new output differs (lazy cascade); a stage waits while an ancestor is being re-run | Whole-artifact hashing re-runs more than strictly needed (a stage that reads one field re-runs when any field changes); re-plans are capped at 3 per source stage, and on exceeding it stale stages FAIL loudly |
| Re-plan loops forever | Run never ends | Hash comparison (re-producing identical content is not a change), `MAX_REPLANS = 3` per source stage counted from persisted decisions, and a loud failure instead of continuing stale | Total work is bounded but could still be a few multiples of the graph |
| A requirement is changed mid-run by the wrong person, or with a secret in it | Unreviewed change of what is being built, or a leaked credential | `amend_requirement`: mandatory non-reserved actor, run must be unfinished, text bounded to 2000 characters and policy-scanned (refusal leaves the run untouched and is audited), pending approvals on re-derived evidence are withdrawn and asked again, every amendment is a `replan / requirement_amended` decision with old and new text and hashes. The initial requirement and every approve, reject and amend comment get the same bound and scan | `actor` is an unauthenticated string, so the amender is not proven and cannot be required to differ from the approver; one amendment while a stage is running can cost one wasted execution; the scan is a heuristic; the fourth re-plan from one source stage fails the run (a cap by design) |
| What the approver reads is misleading, or redraws the terminal | An approval granted on a false picture | The review text shows each proposed interpretation with the feature approving it adds and the rationale, and says `NOTHING TO REVIEW` when empty; approval requests record the hashes of the evidence shown; every terminal print neutralises control characters; model output containing one is rejected | The approver still has to read it; a fluent but wrong proposal passes |
| A model is wrong, manipulated or unavailable | Misleading artifact; or the run stalls | Model has no tools and no authority; output is schema-projected and size-capped, control characters rejected, policy-scanned, exit-gated; a model cannot answer "clear" for an explicitly vague request (keyword cross-check); a refused key trips a circuit breaker; approval stages are never LLM stages; failures go retry, then deterministic fallback; provenance on every artifact | Prompt injection is mitigated, not solved; a model can be fluently wrong in ways gates do not check; the vague-request check only covers requests that contain an explicit vague word; requirement text leaves the machine when the layer is enabled |
| A run is started or approved by someone who should not | Unreviewed release | Loopback bind by default; compose (image never built) publishes on loopback only; `params.auto_approve` refused; reserved actor names | There is no authentication or authorisation on any endpoint, and the JSONL audit file is append-only by convention only (editable by anyone with file access) |
| Metrics mislead | Wrong operational picture | `approval_wait_ms` separates human waiting from system time; `active_ms` reported next to end-to-end latency | MTTR and `active_ms` are approximations (see Limitations) |
| Shortener abuse (SSRF targets, floods, retries) | Unsafe redirects, duplicate links | Destination guard (scheme allow-list, no credentials, private/loopback/link-local/reserved IPs including decimal, hex, octal and IPv4-mapped IPv6, blocked local suffixes); sliding-window rate limit; idempotency keys with body fingerprint | No DNS resolution (rebinding not covered); limiter and idempotency store are in memory and per process |

## 5. Validation approach and guardrails

**Test pyramid (1,069 collected, 1,067 pass and 2 live-model tests are skipped by default;
`python -m pytest -q -W ignore`, 48 seconds on 2026-10-10; parametrised cases are counted individually).**

| Layer | Tests | What it covers |
|---|---|---|
| Unit | 623 | graph (18), gates (13), policy (320, mostly parametrised bypass attempts), run stores (23), telemetry (16), shortener validation (106), store (13), rate limiter (6), manifest-vs-code (14), plus the LLM layer's client including the refused-key circuit breaker (41), settings and `.env` parsing (35) and eval scoring (18) |
| Engine component | 110 | stub agents: parallel overlap and join, `join=any`, retry/backoff, fallback, rollback, stop, approvals, restart with a new `Engine`, re-plan and its cap, timeouts, auto-approve rules, metrics (69); requirement amendment, lazy cascade and its edge cases such as amending while a stage runs, two concurrent amendments, restart (31); governance: scanned requirements and comments, old and new text and evidence hashes in the audit trail, `reads_requirement` re-runs (10) |
| HTTP | 161 | orchestrator API (78, including `amend` and the `llm` param), API governance: strict `params.llm`, scanned requirement and comments (17), and shortener API (66) through `TestClient`: status codes, error format, 409 on double approval, restart through a shared store, shutdown behaviour |
| Agents on real code | 38 | each agent against the real shortener; a regression in the real code fails the part; no artifact contains secrets or unsafe URLs |
| LLM agents on a scripted fake | 36 | same artifact shape, schemas, dropped unknown keys, untrusted-text delimiters, a failing model is an ordinary exception (21); hardening: control characters rejected anywhere, the vague-request cross-check, a refused key across a whole run, amendment re-running documentation (15) |
| Scenario integration | 52 | all three scenarios end to end, pause and resume on new engines, parallel overlap, decisions all carry a rationale (32); amendment end to end with real agents (5); whole scenarios with a scripted model: fallback ladder, prompt injection hitting the policy stop, "a model cannot approve" (15) |
| CLI and report | 41 | process-level behaviour, exit codes, resume across invocations, JSON output, `amend`, `--llm`, resume hint (34); report safety: control characters neutralised, approver review text, refused requirements and comments (7) |
| Deployment files | 6 | `tests/test_packaging.py`: compose ports on loopback only, entry points read `HOST` and `PORT`, Dockerfile data directory, git-ignored `data/`, read-only CI token, lock file pins exactly the direct dependencies. Text-level only: the image was never built |
| Live model | 2 | `@pytest.mark.live`: real Gemini calls; skipped unless `RUN_LIVE_LLM=1` and a key; not part of the 1,067 |

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

**Evals.** `python -m evals` scores the deterministic agents offline; `python -m evals --llm`
adds the model. The numbers in `docs/LLM.md` (model 10 of 10 on requirements, deterministic
65% precision and recall) are from 10 cases written by the author, one run each. That is a
smoke test and a demonstration, not a benchmark: no confidence interval, no held-out set.

**Guardrails at runtime.** Policy hard stops; fail-closed gates; entry gates re-checked after
approval; no unreviewed fallback on approval stages; reserved approver names; read-only
agent context; bounded retries, timeouts and re-plans; audit trail for every decision;
requirements (new and amended) and approval comments bounded and policy-scanned before use;
approver review text that shows what approving applies; terminal output with control
characters neutralised; model output schema-projected, control-character-free, policy-scanned
and gated; loopback-only publishing of the unauthenticated API.

## 6. Assumptions

- Agents are trusted, in-process Python functions. The trust boundary is the process, not the
  agent.
- One orchestrator process owns a database at a time (SQLite file, single writer).
- The shortener already exists and is correct enough to verify; "implementation" in this
  system means verifying real code with evidence, not writing it.
- Requirements are short English text (at most 2000 characters, enforced for a new run, an amendment and
  the text sent to a model).
  Default feature detection uses a fixed table of seven features (`alias`, `ttl`, `analytics`,
  `rate_limit`, `ssrf`, `audit`, `retention`).
- Wall-clock time is monotonic enough for latency metrics.
- Approvers are identified by a name supplied by a trusted front end or operator.
- The demo auto-approve switch is for demos and tests only.
- The LLM layer is opt-in and a reviewer may not want requirement text sent to Google: nothing
  is sent unless a run opts in and a key is configured.

## 7. Limitations

Stated candidly; most are also documented in code docstrings.

- **Agents are deterministic by default.** They derive artifacts from keyword tables and
  from the real code. No agent writes product code; the `impl_*` agents call real functions
  with sample inputs and record evidence. `impl_audit` and `impl_retention` are design-only
  and say so (`design_only: true`, `verified: false`). Four artifacts can optionally be
  written by a model (below).
- **LLM layer: free-tier rate limits.** A burst of runs can get 429s. The client retries
  briefly, then the engine falls back, so runs finish, but they finish deterministic.
- **LLM layer: nondeterminism.** Temperature 0.2 is not zero. Two runs differ, so artifact
  hashes differ, and a re-plan can fire for an LLM stage where it never would for a
  deterministic one. LLM runs cannot be compared byte for byte; an earlier eval run had one
  schema failure (a missing `topic` field) that a tightened prompt then avoided in the next run,
  which is one sample, not proof.
- **LLM layer: only Gemini is implemented and live-tested**, only the lite models, small inputs.
  A second provider was deliberately not written because it would be untested code. Model
  names change; the defaults reflect what worked for a new free-tier key at the time.
- **LLM layer: 10 self-written eval cases.** 100% on them is a smoke test, not a benchmark.
- **LLM layer: the model can be fluently wrong.** In a real run it labelled the existing
  `GET /{code}` endpoint as an API addition. Gates check shape and a few semantics, not truth.
  The `impact` agent and the implementation checks keep the pipeline tied to the real code; the
  model's architecture text is not verified against it beyond the grounded component names.
- **LLM layer: prompt injection is mitigated, not solved.** The guarantee comes from the model
  having no authority (no tools, schema-projected output, policy scan, gates, human approvals),
  not from the prompt wording. The policy scanner is conservative: an innocent model sentence
  shaped like `password: <6+ non-letters>` would hard-stop a run (not seen in the real runs).
- **LLM layer: cost and privacy.** The requirement text and product facts are sent to Google when
  a run opts in. Token and cost budgets are not implemented; only the model name is recorded
  (`generator`), not the prompt version or parameters.
- **Re-plan: whole-artifact hashing.** `consumes` is the dependency contract and an artifact is
  hashed as a whole, so a stage that reads one field of an artifact re-runs when any field
  changes, and a stage that reads an artifact without declaring it would be kept when it should
  re-run. A few `consumes` lists over-declare, which costs extra runs, never stale results.
- **Re-plan: one wasted execution is possible.** A stage that is running cannot be interrupted
  (a Python thread cannot be killed), so an amendment during a running stage lets it finish and
  then re-runs it if its input is outdated.
- **Re-plan: withdrawn approvals are stored as `rejected` by `engine`.** There is no `cancelled`
  status; the comment, the `approval / cancelled` decision and the CLI (`withdrawn by engine`)
  say why. Approvals already granted on a stage that is kept stay valid, by design.
- **Re-plan: `metrics.replans` is not a count of amendments.** It counts the amendment plus every
  applied re-plan pass, so one amendment shows `replans=3` or more.
- **Re-plan: the amender is an unauthenticated string,** like the approver. The engine refuses
  reserved machine names but cannot prove who the caller is or that the amender differs from the
  approver.
- **Docker is unvalidated.** `Dockerfile` and `docker-compose.yml` exist but the image was never
  built and no container was run (no Docker daemon was available). Only text-level checks exist
  (`tests/test_packaging.py`). The CI workflow could not be run here either; its commands were
  run locally.
- **No authentication anywhere.** No HTTP endpoint checks who is calling: anyone who can reach
  the orchestrator port can start, approve, reject, amend and stop runs. Both services bind to
  loopback by default and compose (image never built) publishes on loopback only; that is the whole protection.
  The `actor` is caller-supplied: the engine blocks blank and reserved names but cannot prove
  who someone is, nor that the approver differs from the requester. The audit trail therefore
  records who a caller claimed to be, and the JSONL file is append-only by convention only (it
  is not signed or write-protected).
- **Input scanning is a heuristic.** The secret and unsafe-URL scan on artifacts, requirements
  and comments is regular expressions plus key-name matching, with false positives and false
  negatives. The "model may not call a vague request clear" check is a keyword rule (no
  deterministic feature, an explicit vague word, zero open questions); it does not cover a
  vague request phrased without those words and it does not judge the quality of the
  questions the model raises.
- **A refused API key needs a restart.** After a 401, 403 or 400 "API key not valid" the Gemini
  client makes no further calls for the life of that client (the process, for the CLI and the
  API), so fixing the key means starting a new process.
- **Dependency pinning is partial.** `requirements-lock.txt` pins the 6 direct dependencies;
  their transitive dependencies are not pinned, there are no hashes, and CI and the
  `Dockerfile` install from `requirements.txt`.
- **Re-plan cap.** The fourth re-plan caused by the same source stage fails the run by design
  (`MAX_REPLANS = 3`), so a run can take about three amendments.
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
  are complete. After an API restart that buffer is empty, while `/metrics` is rebuilt from
  the stored runs at start-up (finished runs only).

## 8. Next steps

1. **Harden the LLM layer that now exists**: a larger, held-out eval set with a regression
   threshold (the current 10 cases are a smoke test), recorded responses in CI, live runs on a
   schedule, prompt version and model parameters in the decision lineage, token and cost
   budgets, a second provider, and a hosted or private model for requirements that must not
   leave the organisation. Keep humans on high-impact stages.
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
9. **Field-level dependencies** (`consumes` naming fields, not whole artifacts) so a re-plan
   re-runs only what a changed field affects, and a cancellable execution model so an amendment
   can stop a running stage instead of waiting for it.
