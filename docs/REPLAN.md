# Dynamic re-plan: changing a requirement mid-run

The assignment asks the orchestrator to "dynamically re-plan when upstream outputs change while maintaining
governance". This page explains how that works, how to trigger it, and what it does NOT do.

## In plain words

A run is a graph of stages. Each stage reads some artifacts (`StageSpec.consumes`) and publishes others. The engine
fingerprints (SHA-256) every artifact and remembers, per stage, the fingerprint of each artifact it consumed.

If an input changes after someone used it, the engine re-runs **only** the stages that used the old version. Stages
whose inputs did not change keep their results. Nothing is re-run "just in case".

There are two triggers:

| Trigger | Who | Example |
|---|---|---|
| **A changed artifact** (automatic) | the engine, comparing hashes | a stage that was running while its input changed finishes on stale data, so it is run again |
| **An amended requirement** (human) | a named person | "add custom aliases" becomes "add custom aliases and expiring links" while the run waits at an approval |

The second one feeds the first: amending the requirement re-queues the root stage, and the normal hash comparison
decides what else must run. That is why the human trigger needs no special graph logic.

## The algorithm

`Engine.amend_requirement(run_id, requirement, actor, comment="")` (async, like `approve`):

1. **Validate, change nothing on failure.**
   * `actor` must be a real name: same rule as approvals (`validate_human_actor`; `auto-approver`, `engine`,
     `system` ... are refused).
   * the run must exist (`KeyError`) and not be finished or stopping (`RunNotAmendable`, a `ValueError`).
   * the text must be non-empty, at most 2000 characters, and different from the current one (a whitespace-only
     difference counts as identical). `comment` is at most 2000 characters.
   * the text passes the same **policy scan as artifacts** (`no_secrets`, `safe_destination_urls`). A violation
     raises `PolicyViolation`, the run is left exactly as it was, and the refusal is audited as a `policy`
     decision (it names the rule and field, never the value). This is *not* the artifact "hard stop": an
     operator typo must not kill a run, but a secret must never enter the requirement.
2. **Record the decision** `kind="replan"`, `outcome="requirement_amended"` with `actor`, `comment`, the SHA-256
   of the old and new text, the stages that were re-queued and the approvals that were withdrawn. A telemetry
   event is emitted and `metrics.replans` is incremented.
3. **Set `run.requirement`** and re-queue the root stages (stages without dependencies) plus every downstream
   stage that is not `succeeded`: waiting for approval, failed, blocked, skipped. Their verdict was about the old
   requirement. `succeeded` stages are **kept for now**.
4. **Withdraw every pending approval** of a re-queued stage. A human was asked about evidence that is about to be
   re-derived, so that question is void (status `rejected`, actor `engine`, comment "requirement amended by ...",
   one `approval / cancelled` decision; the CLI table shows it as `withdrawn by engine`). A later attempt to
   approve it fails with "already rejected". The approval is asked again, with a new id (`release-1` becomes
   `release-2`), once the new evidence exists. Approvals that were already granted stay in the history; a stage
   that has to run again will ask again.
5. **Resume.** A paused or restarted run is resumed (`engine.run`). A live run is woken up. A stage that is
   `RUNNING` right now is never touched: when it finishes it is checked, and run again if it consumed outdated
   input (for a root stage: if the requirement changed while it ran).
6. **The normal re-plan decides the rest.** The root re-runs and publishes. For every changed artifact
   (`_maybe_replan`), each `succeeded` stage that consumed an older version is reset to `pending`; stages that did
   not consume it keep their result. A reset stage that re-runs and reproduces the *same* artifact invalidates
   nothing further. This is the "selective" part.

### What exactly is re-run, and what is kept

* A stage is re-run **iff** it consumed an artifact whose fingerprint differs from the one it saw (or it is itself
  waiting / failed behind such a stage).
* `consumes` is therefore the contract: a stage that reads an artifact but does not declare it would be kept when it
  should re-run. The scenario stage lists were checked against what the agents actually read (see Limits).
* **The cascade is lazy.** Before this change a reset also reset *everything* downstream. Now only the stale stage
  is reset; its consumers are reset later, and only if its new output differs. (When the cap is hit the cascade is
  eager again: see below.)
* While any ancestor of a stage is not `succeeded` (it is being re-run), the stage does not start. Without this a
  waiting stage could ask a human to approve evidence that is seconds from being replaced.

### Bounds (fail loud)

* `MAX_REPLANS = 3` per **source stage**, counted from persisted `replan / applied` decisions (unchanged). Each
  amendment makes the root stage the source of one re-plan, so after three applied re-plans from the same root
  (typically three amendments) the **next** one does not silently continue: the stale stages and everything that rests
  on them become `failed` with `stale_after_replan_limit`, a `limit_exceeded` decision is written and the run ends
  `failed`. The amendment itself is applied (and audited) before that happens.
* The text is capped at 2000 characters.
* One amendment cannot start a loop: re-running is driven by hashes, and re-producing identical content is not a
  change.

## Governance, in one list

| Concern | How |
|---|---|
| Who changed it | mandatory `actor`, validated like an approval; recorded in the decision and in telemetry |
| Why | optional `comment`, recorded |
| What changed | SHA-256 of the old and new requirement in the decision (the text itself is in `run.requirement`) |
| Unsafe input | the same policy scan as artifacts; refusal is audited, run untouched |
| Humans stay in the loop | pending approvals are withdrawn; every gate that needs a decision on changed evidence asks again |
| Nothing is silently stale | hash comparison, running stages re-checked on finish, lazy cascade guarded by the "ancestors settled" rule |
| Bounded | `MAX_REPLANS`, text length cap, fails loud |
| Durable | state is saved after every change; works from a brand-new `Engine` on the same store |
| Still not enforceable here | that the amender differs from the original requester: that needs an identity provider in front of the API |

## How to use it

### CLI

```
python -m scenarios amend <run_id> --requirement "..." --actor you [--comment "..."] [--db ...] [--audit ...] [--json]
```

It loads the run from SQLite, applies the amendment, resumes it, and prints which stages re-ran. Exit codes are the
same as the other commands: `0` succeeded or waiting for a human, `1` failed/stopped/rolled back, `2` refused
(unknown run, finished run, identical or empty text, reserved actor, policy violation). `status`, `resume` and
`trace` show a `RUNS` column for re-planned runs.

### HTTP API

```
POST /runs/{id}/amend   {"requirement": "...", "actor": "alice", "comment": "optional"}
```

| Status | When |
|---|---|
| `202` | accepted; the run re-plans in the background (like `approve`). Body: `run_id`, `actor`, old/new requirement SHA-256, links |
| `200` | with `?wait=true`: the run view once it settled (or on timeout) |
| `404` `run_not_found` | unknown run |
| `409` `run_already_finished` / `run_stopping` | finished or stopping run |
| `422` `invalid_actor` / `invalid_requirement` / `policy_violation` / `validation_error` | bad actor, identical text, secret or unsafe URL, empty or too long text |

Everything is validated **before** the background task starts, so a bad request is a clean 4xx and can never mark
the run as crashed.

## Real output: brownfield, amended while waiting at the release gate

Run on a scratch SQLite file (`--skip-pytest`; paths shortened; long lines trimmed with `...`).

```
$ python -m scenarios brownfield --skip-pytest --requirement "Extend the URL shortener: add custom aliases for links" --db demo.db --audit demo.jsonl
  ... PAUSED at api_change_review-1
$ python -m scenarios resume run-e5bf9e31 --approve api_change_review-1 --actor alice --db demo.db --audit demo.jsonl
  ... PAUSED at release-1 (production_release)

$ python -m scenarios amend run-e5bf9e31 --requirement "Extend the URL shortener: add custom aliases and expiring links" --actor alice --comment "product also wants expiry" --db demo.db --audit demo.jsonl
Run run-e5bf9e31  scenario=brownfield  state=WAITING_APPROVAL
Requirement amended by alice:
  before: "Extend the URL shortener: add custom aliases for links"
  after:  "Extend the URL shortener: add custom aliases and expiring links"

Stages (RUNS = executions before -> now; RESULT = what this amendment did):
STAGE              STATE             RUNS    RESULT
-----------------  ----------------  ------  -----------------------------------
requirements       succeeded         1 -> 2  re-ran
impact             succeeded         1 -> 2  re-ran
architecture       succeeded         1 -> 2  re-ran
api_change_review  waiting_approval  1 -> 1  waiting for approval
impl_alias         pending           1 -> 1  queued to re-run
impl_ttl           pending           1 -> 1  queued to re-run
impl_ratelimit     pending           1 -> 1  queued to re-run
sync               succeeded         1 -> 1  kept so far (upstream not finished)
testing            succeeded         1 -> 1  kept so far (upstream not finished)
documentation      pending           1 -> 1  queued to re-run
release            pending           0 -> 0  not run yet

Re-plan lineage (decisions written by this command):
  replan   -                  requirement_amended  requirement amended by alice: product also wants expiry. Re-queued ['requirements', 'release']; ...
  approval release            cancelled            pending approval withdrawn: requirement amended by alice
  replan   requirements       applied              'requirements' changed ['requirements']: 'impact' consumed outdated ['requirements']; 'architecture' ...
  replan   impact             applied              'impact' changed ['impact']: 'api_change_review' consumed outdated ['impact']; 'impl_alias' ...
  approval api_change_review  requested            approval 'public_api_change' requested for stage 'api_change_review'
  approval -                  paused               run paused until pending approvals are resolved

Metrics: e2e=-, stages_ok=0, stages_failed=0, retries=0, fallbacks=0, rollbacks=0, approvals=3, replans=3

PAUSED: run run-e5bf9e31 is waiting for approval 'api_change_review-2' (public_api_change) at stage 'api_change_review'.
  - [alias] POST /api/v1/urls accepts an optional 'alias'; adds 409 alias_taken, 422 invalid_alias and reserved_alias
  - [ttl] POST and PATCH accept ttl_seconds; GET /{code} returns 410 expired after expiry
```

Read it like this. The release approval `release-1` was withdrawn. The API change approved a moment ago was about
an impact analysis that no longer exists, so a human is asked again, now seeing the new `[ttl]` change. The three
derived stages re-ran. `sync` and `testing` are only "kept so far": the implementation checks have not re-run yet
(they sit behind the approval), so it is not known yet whether their inputs change. Continuing:

```
$ python -m scenarios resume run-e5bf9e31 --approve api_change_review-2 --actor bob --db demo.db --audit demo.jsonl
  ... PAUSED at release-2
$ python -m scenarios resume run-e5bf9e31 --approve release-2 --actor carol --db demo.db --audit demo.jsonl
$ python -m scenarios status run-e5bf9e31 --db demo.db --audit demo.jsonl
Run run-e5bf9e31  scenario=brownfield  state=SUCCEEDED
Requirement: "Extend the URL shortener: add custom aliases and expiring links"

STAGE              STATE      MS   ATTEMPTS  RUNS  GATES (passed/total)  APPROVAL
-----------------  ---------  ---  --------  ----  --------------------  -------------------------------------------------------------------
requirements       succeeded  1    1         2     exit 1/1              -
impact             succeeded  35   1         2     entry 1/1, exit 1/1   -
architecture       succeeded  13   1         2     entry 2/2, exit 1/1   -
api_change_review  succeeded  0    1         2     entry 1/1             public_api_change: approved by alice; public_api_change: approved by bob
impl_alias         succeeded  19   1         2     entry 1/1             -
impl_ttl           succeeded  24   1         2     entry 1/1             -
impl_ratelimit     succeeded  116  1         2     entry 1/1             -
sync               succeeded  26   1         2     exit 1/1              -
testing            succeeded  109  1         1     entry 1/1, exit 1/1   -
documentation      succeeded  11   1         2     entry 1/1, exit 1/1   -
release            succeeded  16   1         1     entry 3/3             production_release: withdrawn by engine; production_release: approved by carol
...
Metrics: e2e=7775 ms, stages_ok=11, stages_failed=0, retries=0, fallbacks=0, rollbacks=0, approvals=4, replans=4
```

`testing` has `RUNS = 1`: the test-suite stage consumes only the merged `implementation` artifact, which is identical
after the amendment, so it was **kept**. Everything else ran twice because it genuinely depends on the requirement.
The decision lineage (`python -m scenarios trace run-e5bf9e31 ...`, filtered):

```
 4349  replan    -                  requirement_amended  requirement amended by alice: product also wants expiry. Re-queued ['requirements', 'release']; ...
 4349  approval  release            cancelled            pending approval withdrawn: requirement amended by alice
 4382  replan    requirements       applied              'requirements' changed ['requirements']: 'impact' consumed outdated ['requirements']; ...
 4418  replan    impact             applied              'impact' changed ['impact']: 'api_change_review' consumed outdated ['impact']; ...
 4431  approval  api_change_review  requested            approval 'public_api_change' requested for stage 'api_change_review'
 5970  approval  api_change_review  approved             approved by bob
 6026  replan    impl_ttl           applied              'impl_ttl' changed ['impl_ttl']: 'sync' consumed outdated ['impl_ttl']. Re-running ['sync']
 6160  approval  release            requested            approval 'production_release' requested for stage 'release'
 7725  approval  release            approved             approved by carol
```

Note line `6026`: only `impl_ttl` changed (it now finds `expiry_policy` in the architecture), `impl_alias` and
`impl_ratelimit` re-ran and reproduced the same evidence, so only `sync` was reset because of it, and `sync`
reproduced the same `implementation`, which is why `testing` was never touched.

A wording-only amendment is even more selective (covered by
`tests/scenarios/test_amend.py::test_a_wording_only_amendment_rederives_the_text_but_keeps_every_approved_decision`):
`impact` and `architecture` re-run but reproduce identical artifacts, so the approved API review, the implementation
checks, `sync` and `testing` are all kept, and only the release decision is asked again.

## Limits (honest)

* **`consumes` is the dependency contract and it is artifact-level.** An artifact is hashed as a whole. `impl_alias`
  consumes `architecture` (it reads one field, the component list), so it re-runs whenever *anything* in the
  architecture changes, even if its own output is unchanged. It cannot say "only if this field changes".
* A few `consumes` lists over-declare (the brownfield `impl_*` stages list `impact`, which they do not read; the
  brownfield documentation lists `impact`). That costs extra runs, never stale results. The release stage reads
  `impact` and the requirement artifacts without declaring them; this is harmless today because `release` can only
  be reached through `documentation`, `testing` and `sync`, which do declare what changes.
* A stage that is `RUNNING` cannot be interrupted (a Python thread cannot be killed). It finishes and is then re-run
  if its input is outdated, so one amendment can cost one wasted execution.
* After an amendment an approval that was already granted and whose stage is **kept** stays valid (its evidence did
  not change). Approvals on stages that re-run are asked again. This follows the hashes, not the amendment text.
* A withdrawn approval is stored as `rejected` by `engine` (there is no `cancelled` status); the comment and the
  `approval / cancelled` decision say why, and the CLI table prints `withdrawn`.
* `metrics.replans` counts the amendment itself plus every hash-triggered re-plan pass, so one amendment shows
  `replans=3` or more, not 1.
* Amendments are applied one after the other on the event loop. Two amendments at the same instant are both applied
  (the later text wins); two identical ones: the second is refused as "identical".
* No identity provider: `actor` is a name the caller states. The engine refuses reserved machine names but cannot
  prove who the caller is, and cannot enforce "the amender is not the approver".
* `GET /runs/{id}/trace` returns `events` from an in-memory buffer (lost on restart); `decisions` are stored with
  the run and are the durable lineage.
* The amended text is bounded and scanned, but it is still free text. Its effect on the design comes from the
  deterministic keyword agents (or an LLM agent), not from anything the engine understands.

## Where to look

| What | Where |
|---|---|
| Engine | `orchestrator/engine.py`: `amend_requirement`, `check_amendment`, `_apply_amendment`, `_requeue_if_requirement_changed`, `_replan`, `_inputs_settled` |
| API | `orchestrator/api.py`: `POST /runs/{run_id}/amend` (also in `docs/openapi.orchestrator.json`) |
| CLI | `scenarios/cli.py`: `cmd_amend`, `amendment_rows` |
| Engine tests | `tests/orchestrator/test_amend.py`, plus `test_engine.py::test_replan_*` |
| API tests | `tests/orchestrator/test_api.py` (section "amend") |
| CLI tests | `tests/scenarios/test_cli.py` (section "amend") |
| End to end | `tests/scenarios/test_amend.py` (brownfield, real agents) |
