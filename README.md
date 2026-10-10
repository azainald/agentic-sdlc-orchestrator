# Agentic SDLC Orchestrator

A small, explicit workflow engine that drives a software change (requirement to reviewed release candidate) through a graph of specialised agents, with quality gates, human approvals, bounded failure handling, a policy layer, and an audit trail. The product being "built" is a URL shortener (`shortener/`, port 8080); the system under review is the orchestrator (`orchestrator/`, port 8090).

- **What it is:** a custom DAG engine (about 840 lines, `orchestrator/engine.py`) plus 22 deterministic agent functions (requirements, impact, architecture, implementation checks, testing, documentation, release, and their fallback and rollback agents) and three scenarios (greenfield, brownfield, ambiguous). Four more agents, `requirements_llm`, `normalize_llm`, `architecture_llm` and `documentation_llm`, are optional (see below).
- **What it controls:** entry and exit gates per stage, parallel branches with a join, retry with backoff, fallback agent, rollback agent, safe-stop, approvals that survive a process restart, and a governed re-plan when a person amends the requirement of a run that is not finished.
- **What it records:** every decision with a rationale (decision lineage), a JSONL audit file, and reliability metrics (success rate, retry and rollback frequency, MTTR, end-to-end and active latency).
- **LLM use:** deterministic by default. No network, no key, no model call unless you opt in. With the opt-in (`--llm`, or `params.llm` over the API, or `LLM_ENABLED=1`) four of the artifacts are written by a Gemini model instead of the rule-based agents; the deterministic agent stays as the fallback, so a missing key, a rate limit or garbage output degrades to the offline result instead of failing the run. The model never approves, routes or reads run state. Principle: **LLM proposes; deterministic tools verify; gates and humans decide.** Details, real eval numbers and limits: [docs/LLM.md](docs/LLM.md).
- **Dynamic re-plan:** `amend` changes the requirement of an unfinished run; only the stages whose inputs actually changed run again, pending approvals on re-derived evidence are withdrawn and asked again. Algorithm, real output and limits: [docs/REPLAN.md](docs/REPLAN.md).
- **What it does not do:** the plan itself (the graph) is authored, not generated; nothing writes product code; there is no authentication. See [ARCHITECTURE.md](ARCHITECTURE.md), section "Agent design".
- **How much to trust it:** 996 automated tests pass and 2 live-model tests are skipped (about 40 s, offline). Limitations are listed honestly in [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md).

## The problem and the approach

Letting agents change software is easy to demo and hard to defend. The questions a reviewer asks are: what stops an agent from shipping something nobody approved, what happens when a step fails, can we explain afterwards why each decision was made, and can the run survive a restart while it waits for a person.

The approach is to keep the agents simple and make the control plane strict:

| Concern | Mechanism | Where |
|---|---|---|
| Order of work | Explicit dependency graph (`StageSpec.depends_on`, `join`), validated for cycles | `orchestrator/graph.py` |
| Evidence before progress | Entry gates and exit gates, pure functions over artifacts, fail closed | `orchestrator/gates.py` |
| Human control | Approval checkpoints; run pauses, persists, resumes in another process | `orchestrator/engine.py` (`_request_approval`, `_resolve`) |
| Failure | Retry with backoff, then fallback agent, then rollback agent, then safe-stop | `orchestrator/engine.py` (`_execute`, `_attempts`, `_fail`) |
| Guardrails | Policy rules on stage definitions and on every artifact; violations are hard stops | `orchestrator/policy.py` |
| Explainability | Decision lineage on the run, JSONL audit file, metrics | `orchestrator/models.py`, `orchestrator/telemetry.py` |
| Changing requirements | `amend`: named actor, policy-scanned text, lazy re-plan driven by artifact hashes, approvals asked again | `orchestrator/engine.py` (`amend_requirement`, `_replan`), [docs/REPLAN.md](docs/REPLAN.md) |
| Model output (optional) | Schema-validated JSON, size caps, policy scan, exit gates, deterministic fallback, provenance on every artifact | `orchestrator/agents/llm_agents.py`, `orchestrator/llm/`, [docs/LLM.md](docs/LLM.md) |

## Quick start

Requires Python 3.10 or newer. All commands run from the repository root.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q -W ignore            # 996 passed, 2 skipped, about 40 s
```

If PowerShell blocks `Activate.ps1` (execution policy), skip activation and prefix every command below with `.\.venv\Scripts\python.exe` instead of `python`.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q -W ignore            # 996 passed, 2 skipped, about 40 s
```

`-W ignore` only hides a third-party deprecation warning that `fastapi.testclient` prints on import. The tests pass with or without it. The 2 skipped tests are `@pytest.mark.live` tests that call the real model; they run only with `RUN_LIVE_LLM=1` and a key. Every other test is offline: `tests/conftest.py` forces `LLM_ENABLED=0` whatever your `.env` says.

### Run the scenarios (CLI)

The commands are identical on every platform. State is stored in `data/runs.db` (SQLite) and the audit trail in `data/audit.jsonl`; both are git-ignored.

```text
python -m scenarios greenfield --auto-approve        # DEMO MODE: the engine grants approvals itself
python -m scenarios brownfield                       # pauses at the first human approval
python -m scenarios ambiguous                        # pauses on the interpretation of "Make links safer"
```

A paused run prints exactly how to continue it, for example:

```text
PAUSED: run run-99285371 is waiting for approval 'api_change_review-1' (public_api_change) at stage 'api_change_review'.
  - [alias] POST /api/v1/urls accepts an optional 'alias'; adds 409 alias_taken, 422 invalid_alias and reserved_alias
  ...
  approve: python -m scenarios resume run-99285371 --approve api_change_review-1 --actor you
  reject:  python -m scenarios resume run-99285371 --reject api_change_review-1 --actor you
  amend the requirement instead: python -m scenarios amend run-99285371 --requirement "..." --actor you
```

If you passed a non-default `--db` or `--audit`, the printed commands repeat them, so they always point at the same store.

Each `resume` is a new process: it loads the run from SQLite and continues. Typical brownfield session (two approvals):

```text
python -m scenarios brownfield
python -m scenarios resume <run_id> --approve api_change_review-1 --actor alice --comment "API deltas are additive"
python -m scenarios resume <run_id> --approve release-1 --actor bob
python -m scenarios status                           # list runs and pending approvals
python -m scenarios status <run_id>                  # one run; a RUNS column appears once a stage has run more than once
python -m scenarios trace <run_id>                   # decision lineage, one line per decision
python -m scenarios metrics                          # aggregate reliability metrics over all stored runs
python -m scenarios metrics <run_id>                 # one run (add --json for machine output)
```

### Change the requirement of a run that is not finished (dynamic re-plan)

```text
python -m scenarios brownfield --skip-pytest
python -m scenarios resume <run_id> --approve api_change_review-1 --actor alice     # now paused at release-1
python -m scenarios amend <run_id> --requirement "..." --actor alice --comment "product also wants analytics"
python -m scenarios resume <run_id> --approve api_change_review-2 --actor bob       # asked again: the evidence changed
python -m scenarios resume <run_id> --approve release-2 --actor carol
```

`amend` validates first (named human actor, run not finished, text non-empty, at most 2000 characters, different from the current one, and the same secret and unsafe-URL scan as artifacts); on any refusal the run is left untouched and the exit code is 2. Otherwise it records a `replan / requirement_amended` decision, re-queues the root stage, withdraws the pending approvals on evidence that is about to be re-derived (the table shows them as `withdrawn by engine`), and lets the normal hash comparison decide the rest: only stages that consumed a changed artifact run again. It prints a table of which stages re-ran (`RUNS 1 -> 2`) and which were kept, plus the decision lineage it wrote. In the real run in [docs/REPLAN.md](docs/REPLAN.md) the `testing` stage kept its result because the implementation artifact it reads did not change. The same operation over HTTP is `POST /runs/{id}/amend`.

Useful flags:

| Flag | Effect |
|---|---|
| `--auto-approve` | DEMO ONLY. The engine grants every approval itself as actor `demo-auto-approver` and prints a banner. Operator-level engine setting; the HTTP API never enables it. |
| `--skip-pytest` | The testing stage normally runs the 205-test shortener suite in a subprocess (about 6 s) plus 6 in-process smoke checks. This flag keeps only the smoke checks. |
| `--inject-failure testing` | The testing agent fails on attempt 1 only: retry succeeds. |
| `--inject-failure testing_hard` | The testing agent always fails: retries are exhausted, the fallback agent succeeds. |
| `--inject-failure testing_all` | The fallback fails too: rollback, release skipped, run ends `rolled_back` (exit code 1). |
| `--requirement "..."` | Override the scenario's requirement text. |
| `--llm` | Optional. Use the LLM-backed agents where they exist (needs `GEMINI_API_KEY`); the deterministic agent is the fallback. Prints a banner and a `Generators:` line saying who wrote each artifact. See [docs/LLM.md](docs/LLM.md). |
| `--db PATH` / `--audit PATH` | Use another SQLite file or audit file. |

Exit codes: `0` succeeded or paused for approval, `1` failed, rolled back or stopped, `2` refused or usage error (unknown run or approval, a finished run, an invalid or policy-violating amendment).

### Optional: the LLM layer

Nothing above needs a key or a network. To try the model-backed agents, put this in a `.env` file in the repository root (it is git-ignored, and so is any key in it) or in real environment variables:

```text
GEMINI_API_KEY=your-key
LLM_ENABLED=1        # opt-in: without it, plain runs stay deterministic
```

```text
python -m scenarios greenfield --auto-approve --skip-pytest --llm     # model writes requirements, architecture, documentation
python -m evals                                                       # deterministic agents on 10 labelled texts (offline)
python -m evals --llm --pause 4                                       # the same texts through the model (network, free-tier pacing)
```

Without a key, `--llm` does not crash: each LLM stage fails twice, the engine falls back to the deterministic agent, and the run finishes with `retries=3 fallbacks=3` and `Generators: ... (fallback)` (real output in [docs/SCENARIOS.md](docs/SCENARIOS.md) section 6). `python -m evals --llm` without a key exits with code 2. If your `.env` sets `LLM_ENABLED=1`, plain scenario commands use the model too; set `LLM_ENABLED=0` in the environment to force an offline run. To test the no-key path on a machine that has a key, set `LLM_PROVIDER=none` in the environment. Only Gemini is implemented and live-tested.

### Run the two HTTP services

```text
python -m shortener        # URL shortener on http://127.0.0.1:8080  (OpenAPI at /docs)
python -m orchestrator     # orchestrator   on http://127.0.0.1:8090  (OpenAPI at /docs)
```

The orchestrator API persists runs to `data/orchestrator.db` and appends to `data/audit.jsonl`. A run can be started, approved, amended and stopped over HTTP; approvals and amendments need a named actor.

macOS / Linux / Git Bash (curl):

```bash
# 1. Start a brownfield run and wait until it pauses for approval (?wait=true blocks until settled).
curl -s -X POST "http://127.0.0.1:8090/runs?wait=true" \
  -H "Content-Type: application/json" \
  -d '{"scenario":"brownfield","params":{"skip_pytest":true}}'
# -> {"run_id":"run-...","state":"waiting_approval","pending_approvals":[{"id":"api_change_review-1", ...}], ...}

# 2. Approve as a named human. "actor" is mandatory; engine names such as "system" are rejected (422).
curl -s -X POST "http://127.0.0.1:8090/runs/<run_id>/approvals/api_change_review-1/approve?wait=true" \
  -H "Content-Type: application/json" \
  -d '{"actor":"alice@example.com","comment":"reviewed API deltas"}'
# -> state is still "waiting_approval": now pending is "release-1". Repeat with that id to finish.

# 3. Optionally change the requirement while the run is unfinished (see docs/REPLAN.md). 202 and background re-plan, or add ?wait=true.
curl -s -X POST "http://127.0.0.1:8090/runs/<run_id>/amend?wait=true" \
  -H "Content-Type: application/json" \
  -d '{"requirement":"Extend the existing URL shortener: add custom aliases only.","actor":"alice@example.com","comment":"narrow the scope"}'
# -> the pending approval is asked again under a new id (api_change_review-2): the old one was withdrawn.

# 4. Inspect, or stop.
curl -s  http://127.0.0.1:8090/runs/<run_id>/trace      # telemetry events + decision lineage
curl -s  http://127.0.0.1:8090/runs/<run_id>/dag        # nodes with current state, dependency edges
curl -s  "http://127.0.0.1:8090/metrics?format=prometheus"
curl -s -X POST http://127.0.0.1:8090/runs/<run_id>/stop
```

Windows PowerShell:

```powershell
$body = @{ scenario = "brownfield"; params = @{ skip_pytest = $true } } | ConvertTo-Json -Depth 5
$run  = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8090/runs?wait=true" -ContentType "application/json" -Body $body
$run.state                                   # waiting_approval
$approval = $run.pending_approvals[0].id     # api_change_review-1

$decision = @{ actor = "alice@example.com"; comment = "reviewed API deltas" } | ConvertTo-Json
$next = Invoke-RestMethod -Method Post -ContentType "application/json" -Body $decision `
  -Uri "http://127.0.0.1:8090/runs/$($run.run_id)/approvals/$approval/approve?wait=true"
$next.pending_approvals[0].id                # release-1
```

Things the API refuses, on purpose (all verified against a running server):

```text
POST /runs  {"scenario":"brownfield","params":{"auto_approve":true}}   -> 422 auto_approve_not_allowed
POST .../approve  {"actor":"system"}                                   -> 422 invalid_actor
POST .../approve  {}                                                   -> 422 validation_error (actor required)
POST .../approve  (second time)                                        -> 409 approval_not_pending
POST .../amend    {"requirement":"...","actor":"system"}               -> 422 invalid_actor
POST .../amend    (same text as the current requirement)               -> 422 invalid_requirement
POST .../amend    (text containing password=...)                       -> 422 policy_violation, run untouched
POST .../amend    (finished run)                                       -> 409 run_already_finished
POST /runs        {"scenario":"brownfield","params":{"llm":true}}      -> 422 llm_not_configured (no GEMINI_API_KEY)
```

The amend lines and the `llm_not_configured` line were run against a real server on a scratch database with no key set. `params.llm: true` with a key in place swaps in the LLM-backed graph for that run; it is the only LLM switch a client has, and it cannot change what the model is allowed to do.

## What to look at first (reviewer map)

If you have ten minutes:

1. Run `python -m scenarios brownfield --skip-pytest`, then follow the printed resume commands. This shows a pause, a restart-safe resume, gates, parallel stages and a join. At the second pause, try `python -m scenarios amend <run_id> --requirement "..." --actor you` to see the re-plan.
2. Read [docs/SCENARIOS.md](docs/SCENARIOS.md) for real output of all three scenarios and the failure paths.
3. Read [ARCHITECTURE.md](ARCHITECTURE.md) for the state machines and the design trade-offs, [docs/REPLAN.md](docs/REPLAN.md) for the re-plan and [docs/LLM.md](docs/LLM.md) for the optional model layer.
4. Skim `orchestrator/engine.py` (the whole engine), then `orchestrator/gates.py` and `orchestrator/policy.py`.

Which file proves which claim:

| If you want to see | Read | Then confirm with |
|---|---|---|
| The whole scheduling loop and failure ladder | `orchestrator/engine.py` | `tests/orchestrator/test_engine.py` (69 tests) |
| Gates fail closed | `orchestrator/gates.py` (`run_gates`) | `test_unknown_gate_name_fails_closed`, `test_run_gates_fails_closed_when_a_gate_raises` |
| A paused run resumes in a new process | `engine._resolve`, `orchestrator/store.py` | `test_approval_pauses_run_and_resumes_after_restart_with_new_engine`, `test_resume_walks_a_run_through_its_approvals_across_invocations` |
| Clients cannot bypass approvals | `AutoApprovePolicy` in `models.py`, `api.py::create_run` | `test_run_params_can_never_enable_auto_approve`, `test_client_cannot_enable_auto_approve_through_run_params` |
| Policy guardrails | `orchestrator/policy.py` | `tests/orchestrator/test_policy.py` (320 cases), `test_secret_in_artifact_is_a_hard_stop_with_no_retry` |
| Codebase reasoning (brownfield) | `orchestrator/agents/impact.py` | `test_impact_cites_real_files_and_traces_the_call_path` |
| Honest "design only" output | `orchestrator/agents/implementation.py` (`impl_audit`, `impl_retention`) | `test_design_only_agents_are_honest_about_it` |
| Metrics and the MTTR definition | `orchestrator/telemetry.py` (module docstring) | `tests/orchestrator/test_telemetry.py` |
| A requirement can change mid-run, and the engine re-runs only what depends on it | `engine.amend_requirement`, `_replan`, `_inputs_settled` | `tests/orchestrator/test_amend.py` (31), `tests/scenarios/test_amend.py` (5, real agents) |
| A model cannot approve, route or smuggle fields; failure degrades to the deterministic agent | `orchestrator/agents/llm_agents.py`, `llm_schemas.py`, `scenarios/common.py` (`llm_graph`) | `tests/llm/` (116, with a scripted fake: no network), e.g. `test_prompt_injection_that_obeys_the_attacker_hits_the_policy_hard_stop`, `test_unknown_keys_are_dropped_so_a_model_cannot_add_fields` |

## Requirement-to-evidence table

"Demo" means a command in this README or in [docs/SCENARIOS.md](docs/SCENARIOS.md) shows it. Test names are functions under `tests/`.

| Requirement | Implementation | Evidence |
|---|---|---|
| **Requirement understanding** | `orchestrator/agents/requirements.py` and the feature table in `agents/features.py`: structured problem, goals, Given/When/Then acceptance criteria, non-goals, assumptions, open questions, ambiguity flag. Rule-based (regex feature table) by default. Optionally `requirements_llm` (behind `--llm`) understands paraphrases the table cannot match; its output has the same shape, passes the same gates and carries `generator: llm:<model>`. | `tests/scenarios/test_agents.py::test_clear_requirements_are_structured_and_not_ambiguous`, `::test_vague_requirement_is_flagged_with_unresolved_questions`; LLM: `tests/llm/test_agents.py::test_requirements_llm_has_the_deterministic_shape_and_passes_the_real_gates`; demo: SCENARIOS.md "Requirement understanding" per scenario; eval numbers: [docs/LLM.md](docs/LLM.md) |
| **Task decomposition** | One explicit graph per scenario: `scenarios/greenfield.py`, `brownfield.py`, `ambiguous.py`, shared stages in `scenarios/common.py`. The graph is authored, not generated by an agent (stated plainly in ARCHITECTURE.md). Exposed at `GET /scenarios`. | `tests/scenarios/test_scenarios.py::test_graph_is_explicit_valid_and_policy_clean`, `::test_graph_shape_parallelism_and_safety_settings`; Mermaid graphs in SCENARIOS.md |
| **Codebase reasoning (brownfield)** | `agents/impact.py` parses every `shortener/*.py` with `ast`, builds a name-based call graph, and traces each HTTP handler to the code a feature touches. Reports impacted modules, call traces, regression risks, tests to re-run, public API deltas. | `test_agents.py::test_impact_cites_real_files_and_traces_the_call_path`, `::test_impact_with_no_codebase_maps_nothing_and_fails_its_gate`; demo: brownfield, artifact `impact` (5 modules, 9 call traces, 63 functions scanned) |
| **Workflow: explicit DAG** | `orchestrator/graph.py` (`validate_graph`, `topological_order`, `dependency_status`), `StageSpec` in `models.py` | `tests/orchestrator/test_graph.py` (18); `test_engine.py::test_invalid_graph_rejected_at_create_run` |
| **Entry and exit gates** | `orchestrator/gates.py` (9 gates), applied in `engine._admit` (entry) and `engine._attempt` (exit, evaluated on the artifacts a stage is about to publish) | `tests/orchestrator/test_gates.py` (13); `test_engine.py::test_entry_gate_failure_blocks_stage_and_skips_downstream`, `::test_exit_gate_failure_counts_as_failed_attempt_and_retries`, `::test_entry_gates_are_rechecked_after_approval` |
| **Sequential and parallel execution with sync** | `engine._advance` launches every ready stage as an asyncio task; agents run in a thread pool; `join="all"` or `"any"`; `sync` stage merges the parallel `impl_*` results | `test_engine.py::test_parallel_branches_overlap_and_join_waits_for_all`, `::test_join_any_starts_as_soon_as_one_dependency_succeeds`; `test_scenarios.py::test_parallel_stages_overlap_in_time` |
| **Cross-stage context and decision lineage** | Versioned, hashed artifacts (`Artifact.sha256`, `version`, `produced_by`); each stage records the input hashes it ran on (`StageRuntime.input_hashes`); every decision is a `DecisionRecord` with rationale | `test_scenarios.py::test_every_decision_has_a_rationale`, `::test_greenfield_artifacts_are_grounded`; demo: `python -m scenarios trace <run_id>` |
| **Human approval checkpoints** | `engine._request_approval`, `_resolve`, `_apply_resolution`; brownfield pauses at `public_api_change` and `production_release`, ambiguous at `requirement_interpretation` and `production_release`; approvals persist with the run | `test_engine.py::test_approval_pauses_run_and_resumes_after_restart_with_new_engine`, `::test_reject_fails_stage_skips_downstream_and_ends_failed`; `test_scenarios.py::test_brownfield_pauses_at_api_review_then_release_and_resumes_on_new_engines`; `test_api.py::test_run_survives_restart_via_shared_store` |
| **Bounded retries** | `engine._attempts`: `max_attempts` per stage, exponential backoff `backoff_ms * 2^(n-1)`, per-attempt `timeout_ms` | `test_engine.py::test_retry_with_exponential_backoff_then_success`, `::test_agent_timeout_fails_the_attempt_and_counts_toward_retries`; demo: `--inject-failure testing` |
| **Fallback** | `StageSpec.fallback_agent`, tried once after retries are spent (`engine._execute`) | `test_engine.py::test_retries_exhausted_then_fallback_succeeds`; `test_scenarios.py::test_persistent_failure_uses_the_fallback_agent`; demo: `--inject-failure testing_hard` |
| **Rollback** | `StageSpec.rollback_agent`, run once when a stage finally fails (`engine._fail`); stage becomes `rolled_back` | `test_engine.py::test_fallback_fails_then_rollback_and_downstream_skipped`, `::test_failing_rollback_leaves_stage_failed`; `test_scenarios.py::test_total_failure_rolls_back_and_the_release_never_runs`; demo: `--inject-failure testing_all` |
| **Safe-stop** | `Engine.stop` and `POST /runs/{id}/stop`: in-flight stages finish, nothing new starts (no retry, fallback or rollback launches), unstarted stages are skipped, pending approvals cancelled. A policy violation triggers it automatically. | `test_engine.py::test_stop_lets_inflight_stage_finish_and_skips_the_rest`, `::test_stop_a_paused_run_cancels_pending_approval`, `::test_policy_violation_safe_stops_other_branches`; `test_api.py::test_stop_a_running_run_lets_inflight_stage_finish` |
| **Policy guardrails** | `orchestrator/policy.py`: secret detection and unsafe-URL-scheme rules on every artifact; stage rules checked at run creation (high-impact needs approval, release is never fully autonomous, a stage needing approval cannot have a fallback) | `tests/orchestrator/test_policy.py`; `test_engine.py::test_secret_in_artifact_is_a_hard_stop_with_no_retry`, `::test_create_run_rejects_policy_violating_stage`; `test_scenarios.py::test_every_published_artifact_passes_policy` |
| **Audit-grade observability** | `Run.decisions` (persisted lineage), `Telemetry` bounded event log plus append-only JSONL (`data/audit.jsonl`), `GET /runs/{id}/trace`, `python -m scenarios trace` | `tests/orchestrator/test_telemetry.py::test_jsonl_audit_file_written`; `test_cli.py::test_audit_trail_is_written_as_jsonl`; `test_api.py::test_trace_has_audit_events_and_decision_lineage` |
| **Reliability metrics** | `orchestrator/telemetry.py`: success rate, retry rate, rollback rate, MTTR, average and p95 end-to-end latency, active time excluding human waits; `GET /metrics` (JSON or Prometheus text), `python -m scenarios metrics` | `test_telemetry.py::test_aggregation_over_mixed_runs`, `::test_mttr_only_counts_succeeded_runs_that_needed_recovery`; `test_engine.py::test_metrics_aggregate_across_a_messy_run`, `::test_metrics_split_human_waiting_from_active_time` |
| **Dynamic re-plan** | Two triggers, one mechanism. (1) A changed artifact: `engine._maybe_replan`, `_replan_if_self_stale`, `_replan` compare recorded input hashes with current artifact hashes and reset only the stages that consumed the old version (lazy cascade: a reset stage that reproduces the same artifact invalidates nothing further; a stage does not start while an ancestor is being re-run). Capped at 3 per source stage, and fails the run loudly beyond the cap. (2) A human amends the requirement: `engine.amend_requirement`, `POST /runs/{id}/amend`, `python -m scenarios amend`; named actor, policy-scanned text, pending approvals withdrawn and asked again. The three default scenarios still show `replans=0`, because nothing changes mid-run unless someone amends; `amend` is how a scenario now triggers it. Details and a real run: [docs/REPLAN.md](docs/REPLAN.md). | Amend: `tests/orchestrator/test_amend.py` (31), `tests/scenarios/test_amend.py::test_amend_at_the_release_gate_reruns_only_what_the_new_requirement_touches`, `::test_a_wording_only_amendment_rederives_the_text_but_keeps_every_approved_decision`, `::test_amend_survives_a_restart_and_the_amended_run_finishes`. Lazy cascade and cap: `test_engine.py::test_replan_resets_downstream_when_upstream_artifact_changes`, `::test_replan_is_selective_a_reset_stage_that_reproduces_its_output_resets_nothing_downstream`, `::test_no_replan_when_consumer_already_saw_current_artifact`, `::test_replan_limit_fails_the_run_loudly_instead_of_continuing_with_stale_work`, `::test_running_consumer_that_finishes_on_stale_input_is_replanned`; `tests/orchestrator/test_amend.py::test_replan_cap_fails_the_run_loudly_when_a_requirement_is_amended_too_often`; API: `test_api.py::test_amend_a_paused_run_replans_in_the_background_and_asks_again` |
| **Optional LLM agents** | `orchestrator/llm/` (client interface, Gemini REST client, settings, scripted fake), `orchestrator/agents/llm_agents.py` (four agents), `scenarios/common.py` (`llm_graph`: swaps the agent in and makes the deterministic one the fallback), `evals/`. Opt-in only; the model has no tools, its JSON is schema-projected (unknown keys dropped), policy-scanned and exit-gated like any artifact, and approval stages are never LLM stages. | `tests/llm/test_scenarios_llm.py::test_garbage_output_is_retried_then_falls_back_to_the_deterministic_agent`, `::test_a_model_cannot_approve_anything_or_alter_routing`, `::test_llm_stages_are_never_the_approval_stages`; `tests/orchestrator/test_api.py::test_llm_param_without_a_configured_key_is_422`; real eval numbers and an honest reading of them: [docs/LLM.md](docs/LLM.md) |
| **Engineering output generation** | Architecture with ADRs, impact analysis, verification evidence, a test report, generated docs under `docs/generated/<run_id>/` (CHANGELOG, API, DESIGN), and a release checklist with rollback plan | `test_agents.py::test_documentation_writes_real_files_only_under_docs_generated`, `::test_release_builds_a_go_checklist_and_rollback_undoes_the_file`, `::test_architecture_derives_components_and_adrs_from_features` |
| **Validation and risk control** | Implementation agents probe the real shortener code and record `{check, input, expected, actual, ok}`; `sync` fails if any part fails; testing runs the real pytest suite plus a live smoke test; release gate requires green tests and no failed stage; risks are listed in `impact` and the release checklist | `test_agents.py::test_verifying_agents_run_real_code_and_it_checks_out`, `::test_a_regression_in_the_real_code_fails_the_part`; `test_gates.py::test_release_ready_needs_green_tests_and_no_bad_stage` |
| **Controlled autonomy** | Three autonomy levels, human approval kinds, a high-impact set, reserved machine actors, and the demo-mode switch that is deliberately not a run parameter | `test_engine.py::test_run_params_can_never_enable_auto_approve`, `::test_reserved_or_blank_actors_cannot_make_human_decisions`; `test_api.py::test_reserved_actor_names_cannot_approve_or_reject_over_http`; `test_cli.py::test_auto_approve_is_a_loud_demo_mode_with_a_machine_actor` |
| **Final summary** | Run summary table, artifacts with hashes and metrics printed by the CLI (`scenarios/report.py`); engineering write-up in `ENGINEERING_SUMMARY.md`; generated release checklist per run | `test_cli.py::test_greenfield_auto_approve_succeeds_and_prints_the_stage_table`, `::test_json_output_is_machine_readable` |

## Testing approach

`python -m pytest -q -W ignore` collects 998 tests: **996 pass and 2 are skipped** (about 40 s, run in the project venv on 2026-10-10). The 2 skipped are `@pytest.mark.live` tests that call the real model (`RUN_LIVE_LLM=1` plus a key to run them); they have not been part of this count.

| Suite | Tests | What it covers |
|---|---|---|
| `tests/orchestrator/` | 568 | engine behaviour (69), requirement amendment and the lazy re-plan at engine level (31), HTTP API (78, including amend and the `llm` param), gates (13), graph (18), policy (320, mostly parametrised secret and URL cases), SQLite store (23), telemetry and metrics (16) |
| `tests/scenarios/` | 109 | each agent in isolation (38), full scenario runs through the engine including pause and resume on new engine instances, failure injection, stop and reject (32), the CLI (34, including `amend`, `--llm` and the resume hint), amendment end to end on the brownfield scenario with real agents (5) |
| `tests/llm/` | 116 (114 run, 2 live skipped) | the optional LLM layer, all against a scripted fake model: Gemini client behaviour and error handling (34), settings and `.env` parsing (27), the four LLM agents and their schemas (21), whole scenarios through the retry and fallback ladder, prompt injection, and "a model cannot approve" (14), the eval harness (18) |
| `tests/shortener/` | 205 | the URL shortener product: API (66), validation (106), store (13), rate limiter (6), manifest consistency (14) |

How the tests are built:

- Time and sleeping are injected (`Engine(sleep=...)`, a manual clock in the shortener), so retry and backoff tests do not sleep and expiry tests do not wait.
- Restart behaviour is tested by creating a second `Engine` on the same store.
- One test (`test_real_pytest_subprocess_path_once`, marker `slow`) exercises the real subprocess path of the testing agent; every other scenario test sets `skip_pytest`. Deselect it with `-m "not slow"`.
- When the testing stage runs for real, the run reports 211 passed: 205 from `pytest tests/shortener` plus 6 in-process smoke checks.
- The suite never touches the network. `tests/conftest.py` sets `LLM_ENABLED=0` before anything is imported, and the LLM tests use `ScriptedLLM`, a queue of canned answers and exceptions that also records every prompt, so prompts are asserted on too.

## Repository layout

```text
.
|-- README.md                  this file
|-- ARCHITECTURE.md            design, state machines, decisions, scaling
|-- ENGINEERING_SUMMARY.md     what was built, limitations, honest assessment
|-- INTERVIEW_NOTES.md         talking points
|-- docs/
|   |-- SCENARIOS.md           real output of the three scenarios and failure paths
|   |-- REPLAN.md              requirement amendment and the lazy, governed re-plan
|   |-- LLM.md                 the optional Gemini layer: design, safety model, real eval numbers
|   |-- openapi.orchestrator.json, openapi.shortener.json   (scripts/export_openapi.py)
|   `-- generated/<run_id>/    written by the documentation agent at run time (git-ignored)
|-- orchestrator/              the control plane
|   |-- models.py              StageSpec, Run, Artifact, DecisionRecord, Approval, states
|   |-- engine.py              scheduling loop, failure ladder, approvals, re-plan, metrics
|   |-- graph.py               validation, topological order, join logic
|   |-- gates.py               entry/exit gate registry (fail closed)
|   |-- policy.py              guardrail rules, human-actor validation
|   |-- store.py               SQLite and in-memory run stores
|   |-- telemetry.py           event log, JSONL audit, reliability metrics
|   |-- contracts.py           Protocols: RunStore, TelemetrySink, Orchestrator
|   |-- api.py                 FastAPI app (port 8090) and background-task supervisor
|   |-- __main__.py            python -m orchestrator
|   |-- llm/                   optional: LLMClient interface, Gemini REST client, settings, scripted fake
|   `-- agents/                requirements, impact, architecture, implementation,
|                              testing, documentation, release (+ features.py, common.py);
|                              llm_agents.py and llm_schemas.py are the optional LLM-backed agents
|-- scenarios/                 greenfield / brownfield / ambiguous graphs, CLI (incl. amend), report
|-- evals/                     python -m evals: deterministic vs LLM agents on 10 labelled texts
|-- shortener/                 the product: FastAPI URL shortener (port 8080) + manifest.py
|-- scripts/export_openapi.py
|-- tests/
|   |-- orchestrator/          8 files, 568 tests
|   |-- scenarios/             4 files, 109 tests
|   |-- llm/                   6 files, 116 tests (2 live, skipped by default)
|   `-- shortener/             5 files, 205 tests
|-- data/                      runtime state (SQLite, audit.jsonl), git-ignored
|-- .env                       optional, git-ignored: GEMINI_API_KEY, LLM_ENABLED=1
|-- .github/workflows/ci.yml   tests on Python 3.10 and 3.12, then a greenfield smoke run
|-- Dockerfile, docker-compose.yml   present, NOT validated (see below)
|-- pytest.ini, pyproject.toml, requirements.txt
```

## Honest limitations (short version)

The full list, with reasoning, is in [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md). The ones a reviewer should know before reading the code:

- Deterministic by default. The default "understanding" of a requirement is a regex-driven feature table; anything it cannot match becomes an explicit ambiguity that a human must resolve. The optional Gemini agents do better on paraphrases, but on 10 self-written eval cases (so a smoke test, not a benchmark), they are nondeterministic, rate limited on the free tier (a 429 falls back to the deterministic agent), only Gemini is implemented and live-tested, and model prose can be fluent but wrong (one run labelled an existing endpoint as an API addition). See [docs/LLM.md](docs/LLM.md).
- The scenario graphs are authored by hand. What is dynamic is execution (retries, fallback, rollback, re-running stale stages), not the shape of the plan.
- The shortener already existed, so "implementation" stages verify the real code with evidence; `audit` and `retention` are flagged `design_only` because no code exists for them.
- Single process, SQLite, no authentication. Approver names are recorded but not authenticated, and two processes resuming the same run at once is unsafe (no compare-and-swap).
- Re-plan limits (details in [docs/REPLAN.md](docs/REPLAN.md)): an artifact is hashed as a whole, so a stage that reads one field of an artifact re-runs when any field changes; a stage that is running cannot be interrupted, so one amendment can cost one wasted execution; a withdrawn approval is stored as `rejected` by actor `engine` (there is no `cancelled` status); `actor` is an unauthenticated string, so "the amender is not the approver" cannot be enforced.
- Runtime details a reviewer might trip over: after an API restart the telemetry half of `/trace` is empty until new events arrive (`/metrics` is rebuilt from the stored runs at start-up; the decision lineage and the JSONL file keep the full history); `--auto-approve` exists for demos only.
- Docker: a `Dockerfile` and `docker-compose.yml` exist, but they were never validated by a build or a container run. The comment at the bottom of the `Dockerfile` about the services binding to loopback is out of date (`HOST` and `PORT` are read by both `__main__` modules), which is one more reason to treat them as untested.
