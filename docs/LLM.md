# The optional LLM layer

> **LLM proposes; deterministic tools verify; gates and humans decide.**

The orchestrator is deterministic by default: no network, no key, 1,000+ tests that need neither (1,067 pass and 2 live-model tests are skipped by default). This document
describes an **opt-in** layer that lets a language model write four of the artifacts instead of the rule-based
agents, while the control plane (gates, policy, approvals, retries, audit) stays exactly as it was.

It was added to prove the design claim in `ARCHITECTURE.md` ("an LLM-backed agent can replace any agent behind
`AgentFn`") with working code, not to make the system depend on a model.

## What it is

| Part | Where | Notes |
|---|---|---|
| `LLMClient` interface | `orchestrator/llm/client.py` | One method: `generate_json(system, prompt, schema) -> schema instance`. Text in, validated JSON out. No tools. |
| Gemini client | `orchestrator/llm/gemini.py` | Plain REST via `httpx` (no SDK). Key in the `x-goog-api-key` header only, never in a URL or a message. |
| Scripted fake | `orchestrator/llm/fake.py` | `ScriptedLLM`: a queue of answers or exceptions, records every prompt. Used by all tests. |
| Settings | `orchestrator/llm/config.py` | Environment, then a git-ignored `.env`, parsed by a 20-line parser. |
| LLM agents | `orchestrator/agents/llm_agents.py`, `llm_schemas.py` | `requirements_llm`, `normalize_llm`, `architecture_llm`, `documentation_llm`. |
| Wiring | `scenarios/common.py` (`llm_graph`, `with_llm`, `resolve_llm`), `scenarios/registry.py` | A graph transform, so the offline graphs are untouched. |
| Evals | `evals/` | `python -m evals [--llm]`: deterministic vs LLM on 10 labelled texts. |

Not LLM-backed, on purpose: `impact` (reads the real code with `ast`: ground truth), `apply_interpretation` and
`api_change_review` (they run only after a human approved), implementation checks, testing, release.

## Design

```
requirement text (untrusted DATA)
        |
        v
  <name>_llm  --- fails? ---> engine retry (max_attempts >= 2) ---> fallback_agent = deterministic <name>
        |                                 (all of it recorded in the audit trail)
        v  schema-validated JSON (size-capped, unknown keys dropped)
  engine policy scan (secrets, unsafe URLs)  --- violation ---> HARD STOP (no retry, no fallback, safe-stop)
        |
        v
  exit gate (requirements_complete, architecture_present, docs_present)
        |
        v
  artifact published, with  "generator": "llm:<model>"  (or "deterministic")
```

* **Same artifact shapes.** An LLM agent returns the keys the deterministic agent returns, so the existing gates and
  downstream agents (impact, implementation checks, release) work unchanged. A test asserts key-set equality.
* **Grounded.** Prompts carry real facts: the feature catalogue, the shortener `MANIFEST` (endpoints, data fields,
  known limitations) and, for brownfield, the `ast`-derived impact analysis. The model's vocabulary is closed:
  `detected_features` and `open_questions[].topic` are `Literal` types over the product's real feature keys and
  question topics, so the model cannot invent a feature that downstream code has no row for.
* **Deterministic parts stay deterministic.** The architecture agent always keeps the core components and the
  grounded component of each feature even if the model forgot them; `constraints` are copied from the MANIFEST; the
  documentation agent still writes the markdown files from facts only (the model's narrative lives in the artifact,
  never on disk).
* **Provenance.** Every requirements / normalized_requirements / architecture / documentation artifact carries
  `generator`: `llm:<model that answered>` or `deterministic`. The deterministic agents were given the same field.
  `scenarios/report.py` prints a banner and `Generators: requirements=llm:..., architecture=deterministic (fallback)`
  for runs that opted in. Offline output is byte-for-byte unchanged.

### The four agents

| Agent | Does | Guard rails |
|---|---|---|
| `requirements_llm` | Understands intent from raw text (paraphrases included), lists testable Given/When/Then criteria, **must** raise `open_questions` for vague text and none for clear text. | `open_questions[].resolution` is forced to `""` by the code, whatever the model wrote. No feature and no question is rejected. A clear request that the model flags is stopped by the real `ambiguity_resolved` entry gate. **A model may not call an explicitly vague request clear** (narrow rule below): that answer is invalid, so the engine retries and then runs the deterministic agent, which asks the human. |
| `normalize_llm` | Proposes one bounded resolution per question. | Status stays `proposed`. Every question must be answered. The human approval stage and `apply_interpretation` are unchanged. |
| `architecture_llm` | Components, data model, API deltas, ADR-style decisions. | Grounded components re-added if missing; ids assigned by code; `constraints` from MANIFEST. |
| `documentation_llm` | A narrative (overview, highlights, caveats) on top of the deterministic documentation facts. | Narrative is policy-scanned as part of the artifact; files on disk are written from facts only. |

## Safety model

1. **The requirement is untrusted data.** It is truncated to 2000 characters, wrapped in
   `<untrusted_requirement>...</untrusted_requirement>` (a forged closing tag inside the text is removed), and the
   system prompt says to treat it as data and ignore instructions in it.
2. **The model has no tools.** It returns JSON. It cannot read or write run state, create or resolve approvals, or
   change routing. Output is projected through a pydantic schema: only declared fields are read, so an
   `"approved": true` or `"skip_stages": [...]` in the answer is dropped (tested).
3. **Schemas cap sizes**: strings 200 to 1200 characters, lists of 1 to 12 items. Over the cap is a failed attempt.
   **No control characters**: model text ends up in terminals, reports and documents, so an answer containing any
   control character (NUL, ESC, carriage return, backspace, the C1 range 0x7f to 0x9f) is invalid. Single-line fields
   allow none at all; only the multi-line documentation narrative may use newline and tab. Invalid means a failed
   attempt: retry, then the deterministic fallback (`tests/llm/test_hardening.py`). Separately, everything the CLI prints
   goes through `safe()`, which shows a control character as a visible `\x1b`-style escape.
   **Vague requests (a heuristic cross-check, not a guarantee).** After parsing, `requirements_llm` checks the
   deterministic verdict (`is_explicitly_vague`): if the request has no deterministic feature match, contains an
   explicit vague word (`safe`, `secure`, `protect`, `harden`, `better`, `improve`, `robust`, `reliable`, with their
   endings: safer, secured, hardening, improved, ...) and the model returned zero `open_questions`, the answer is
   invalid (`request is vague by the deterministic check but the model raised no open question`). This is deliberately
   narrow. A vague request that uses none of those words (a paraphrase) is not covered: recognising those is what the
   model is for, and a model that wrongly calls one clear skips the interpretation step and is only caught, if at all, by the later human
   approvals (API change, release).
   The rule also says nothing about whether the questions the model does raise are good ones.
4. **Every artifact is policy-scanned by the engine**, like any other agent's output. A planted
   `password=hunter2hunter2` that an obedient model echoes is a `secret_detected` `PolicyViolation`: hard stop, no
   retry, no fallback, safe-stop of the run, and the value never appears in the decision log
   (`test_prompt_injection_that_obeys_the_attacker_hits_the_policy_hard_stop`).
5. **Exit gates and approvals decide.** The LLM stages are never approval stages (policy forbids a fallback on a
   stage that needs a human, so those stages stay deterministic). A model cannot approve anything: approvals come
   only from `Engine.approve` / `reject` or the engine's own opt-in demo policy.
6. **Low temperature** (0.2) and a fixed token cap; thinking is set to minimal so output tokens are not eaten.
7. **Secrets hygiene.** The key travels in a header; error text built from provider responses has the key replaced
   by `<redacted>`; `repr` of settings and clients never shows it; `.env` is git-ignored. Tests assert a fake key
   never appears in any error, even when the provider echoes it back.

## How to enable

```
# .env in the repo root (git-ignored), or real environment variables
GEMINI_API_KEY=your-key
LLM_ENABLED=1            # opt-in; without it every run stays deterministic
# optional: LLM_MODEL=gemini-3.5-flash-lite   LLM_PROVIDER=gemini|none
```

* Python API: `get_scenario("greenfield", llm=True)` (or `llm=None` to follow `LLM_ENABLED`), then
  `engine.create_run(s.name, s.requirement, s.graph, {**s.params, ...})` with an engine built from
  `build_agent_registry()`. `s.params` already contains `llm: true` for the banner.
* `python -m evals --llm` evaluates the model (`--cases N`, `--json`, `--pause SECONDS`).
* Tests: `python -m pytest -q` never touches the network. `RUN_LIVE_LLM=1 python -m pytest tests/llm/test_live.py`
  runs the `@pytest.mark.live` tests against the real model.
* CLI: `python -m scenarios <greenfield|brownfield|ambiguous> --llm` (wired in `scenarios/cli.py` through
  `get_scenario(name, llm=True)`; a plain run follows `LLM_ENABLED`).
* HTTP API: `POST /runs` with `"params": {"llm": true}` (wired in `orchestrator/api.py::create_run` through
  `resolve_llm(params)` and `with_llm(scenario)`). `params.llm` must be the JSON value `true` or `false`; any other type
  is `422 invalid_llm_param` (the strings `"0"` and `"false"` are truthy, so an earlier version would have switched the
  model on for them). `true` without a configured key is `422 llm_not_configured`. The param only picks between two
  versions of the same graph; it cannot reach the approval logic.

If the key is missing, wrong, rate limited, or the model returns garbage, nothing breaks: the stage's attempts fail,
the engine retries, and then runs the deterministic agent. Demonstrated with a deliberately invalid key against the
real API (a `400 API key not valid` from Google, nothing mocked):

```
state=succeeded retries=3 fallbacks=3
Generators: requirements=deterministic (fallback), architecture=deterministic (fallback), documentation=deterministic (fallback)
retry     failed     attempt 1/2 with 'requirements_llm' failed: gemini-3.1-flash-lite: HTTP 400 API key not valid...
retry     scheduled  retrying in 50 ms
retry     failed     attempt 2/2 with 'requirements_llm' failed: ...
fallback  attempted  retries exhausted; trying fallback agent 'requirements'
```

That capture was taken before the circuit breaker described next existed. With the final code, the first refused call
(HTTP 401, 403, or 400 "API key not valid") marks the key as refused for that client: no other model is tried, and every
later call fails immediately with `... (the API key was refused earlier; not calling the API again)` without a network
request. So a wrong key costs one request for a whole run, not one per attempt and model. The agent registry holds one
client, so in the CLI and in the API process the breaker stays open until the process restarts (fix the key, start a new
process). The breaker is covered by offline tests (`tests/llm/test_client.py`, `tests/llm/test_hardening.py`); it was not
re-checked against the real API. Other client errors (a missing model, a 5xx, a rate limit) do not open it.

## Provider and model notes

* Default `gemini-3.5-flash-lite`, then `gemini-flash-lite-latest`, then `gemini-3.1-flash-lite`: the client hands over
  to the next model on a missing model (404), a rate limit that outlasts its retries, a 5xx or a blocked answer.
* `gemini-2.5-*` is closed to new API users. `gemini-3.5-flash` returned empty text with small token caps because
  thinking tokens count against `maxOutputTokens`; the client therefore sends `thinkingConfig.thinkingLevel =
  "minimal"` (verified to work on the lite models; `thinkingBudget: 0` is rejected with a 400 by them) and uses
  generous output limits.
* The JSON schema is given in the prompt and the answer is validated locally; the API's `responseSchema` is not
  used (it does not accept the `$ref`s pydantic emits). `responseMimeType: application/json` is set. Markdown fences
  are stripped anyway.
* 429 / 5xx get at most 2 retries per model, honouring `Retry-After` up to 20 s (longer is not worth blocking a stage
  and goes to the next model). One `generate_json` call has a 100 s budget; each HTTP call times out at 45 s; each
  LLM stage has a 120 s engine timeout.
* **Untested providers.** Only Gemini is implemented, and it is the only provider exercised live. Groq / Ollama
  (OpenAI-compatible) clients were deliberately not written: they would be untested code. The `LLMClient` protocol is
  one method, so adding one is a small, isolated change.

## Evals

`python -m evals --llm --pause 4` (real run, `gemini-3.5-flash-lite`, 10 labelled texts: two clear, two vague, an
SSRF text, two paraphrases that the keyword matcher cannot see, a multi-feature text, an injection attempt and the
one-word request "stuff"). Deterministic agents and the LLM agents are scored on the same texts, each through the
engine's checks (agent, then policy scan, then exit gate). Latency excludes the deliberate rate-limit pauses.

| Family | Mode | N | Schema valid | Gate | Precision | Recall | Vague/clear correct | Policy violations | Avg ms |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| requirements | deterministic | 10 | 100% | 90% | 65% | 65% | 70% | 1 | 1 |
| requirements | **llm** | 10 | 100% | 100% | 100% | 100% | 100% | 0 | 1228 |
| normalize | deterministic | 5 | 100% | 100% | - | - | - | 0 | 1 |
| normalize | **llm** | 3 | 100% | 100% | - | - | - | 0 | 795 |
| architecture | deterministic | 4 | 100% | 100% | - | - | - | 0 | 3 |
| architecture | **llm** | 7 | 100% | 100% | - | - | - | 0 | 1518 |
| documentation | deterministic | 4 | 100% | 100% | - | - | - | 0 | 6 |
| documentation | **llm** | 7 | 100% | 100% | - | - | - | 0 | 812 |

Reading it honestly:

* The deterministic misses are the expected ones: both paraphrases ("how many times each link was opened and which
  pages sent people there", "stop any single client from flooding the service") match no keyword, so they are
  mis-flagged as vague; "delete old click data" is a false `analytics` hit and "delete ... after 90 days" misses
  `retention`; and the injection text is echoed into `problem`, which trips the policy scan (the deterministic agent
  would hard-stop that run, which is the safe outcome but not a useful one).
* The model got all 10 right, including leaving the injection text out of its output. **That is 10 cases written by
  the author.** Treat it as a smoke test and a demonstration, not as a benchmark: no confidence interval, no held-out
  set, one run per case.
* `N` differs for downstream families because they run only when the requirements agent produced something to work
  on (questions for normalize; features for architecture and documentation).
* Nondeterminism is real. In an earlier run of the same command, `normalize_llm` left out the `topic` field of one
  proposal on "Make links safer" (schema failure, 67% for that family). I tightened the prompt and the next full run
  had no such failure, but that is one sample, not proof. In the engine such a failure is a retry and then the
  deterministic fallback, not a failed run.

## Real artifacts, side by side

Request: `Make links safer`. Deterministic `requirements` (trimmed) asks four fixed questions:

```json
{"problem": "Stakeholders asked: \"Make links safer\". No concrete capability could be identified, so the intent is ambiguous (vague terms: safer).",
 "open_questions": [{"topic": "scope", "question": "Which kind of harm should the change protect against: unsafe destinations (SSRF, dangerous schemes), abuse of link creation, or visitor privacy?", "resolution": ""},
                    {"topic": "accountability", "...": "..."}, {"topic": "privacy", "...": "..."}, {"topic": "compatibility", "...": "..."}],
 "detected_features": [], "generator": "deterministic"}
```

LLM `requirements` (real output, trimmed) asks what it judged necessary and names the vague term:

```json
{"problem": "The request is too vague to determine specific feature requirements for making links safer.",
 "assumptions": ["The user wants to improve link safety but did not specify whether this means destination validation, rate limiting, or data protection."],
 "open_questions": [{"topic": "scope", "question": "What specific safety measures should be applied to links (e.g., preventing internal network access, rate limiting creation, or adding expiration)?", "resolution": ""}],
 "detected_features": [], "ambiguity": {"is_ambiguous": true, "vague_terms": ["safer"]},
 "generator": "llm:gemini-3.5-flash-lite"}
```

The next stage, `normalize_llm`, proposed (real, left as `status: proposed` until the human approves):
`"Implement destination safety (SSRF guard) to refuse destinations pointing at internal infrastructure or dangerous schemes."`
with `adds_feature: "ssrf"` and the rationale "This directly addresses link safety by preventing the shortener from being
used to access internal network resources."

Greenfield `architecture_llm` (real, trimmed to one decision; the components, `constraints` and ids are as described above):

```json
{"id": "ADR-001", "title": "In-Memory Analytics Tracking", "context": "Need to track clicks and unique visitors securely.",
 "decision": "Store anonymous click records with hashed visitor identifiers.",
 "consequence": "Enables robust analytics while preserving privacy.", "feature": "analytics"}
```

The deterministic ADRs are more specific to this codebase (they quote `hash_visitor(ip, salt)` and the restart caveat); the
model's are fluent and plausible but shallower. Also visible in that run: the model labelled `GET /{code}` an "Add"
API delta although it already exists. That is why the model's output is a proposal that gates and humans review, not
a source of truth.

Real runs (`skip_pytest`, in-memory store, engine auto-approve for the demo; model `gemini-3.5-flash-lite`):
greenfield succeeded in 3.8 s end to end with 0 retries and 0 fallbacks, ambiguous succeeded in 4.2 s (both
approvals auto-granted by the engine's demo policy, actor `auto-approver`).

## Limitations

* **Free-tier rate limits.** The eval sleeps between calls; a burst of runs can get 429s. The client retries
  briefly and then the engine falls back, so runs finish, but they finish deterministic.
* **Nondeterminism.** Temperature 0.2 is not zero; two runs differ, so artifact hashes differ and a re-plan triggered by
  a changed upstream artifact can fire for an LLM stage where it never would for a deterministic one. LLM runs cannot be
  compared byte for byte; the deterministic default exists for that.
* **The model can be fluently wrong.** Schemas and gates check shape and a few semantics (e.g. that vague requests keep
  questions unresolved), not truth. The `impact` agent and the implementation checks are what keep the pipeline tied
  to the real code; the model's architecture text is not verified against it beyond the grounded component names.
* **The vague-request check is a keyword heuristic.** It forces a model to raise questions when the request is explicitly
  vague by the deterministic definition (no feature matched and a word such as safe, secure, protect, harden, better,
  improve, robust or reliable present). It does not catch other vague requests and does not judge the questions the model
  raises.
* **A refused key needs a restart.** After one refusal the client makes no further calls (circuit breaker above), including
  after the key is fixed in `.env`, until the process restarts.
* **Policy false positives apply to model prose too.** The secret scanner is conservative; an innocent sentence in
  the shape `password: <6+ non-letters>` would hard-stop a run. Not seen in the real runs above.
* **Prompt injection is mitigated, not solved.** Delimiters and instructions reduce it; the guarantee comes from the
  model having no authority (no tools, schema-projected output, policy scan, gates, human approvals). A cleverly
  manipulated model can still produce a misleading but harmless-looking requirement; a human reviews the approval stages.
* **One provider, tested live only on Gemini**, only the lite models, small inputs. Latency numbers are single
  samples from one network.
* **Cost/privacy.** The requirement text and the product facts are sent to Google. Requirements are policy-scanned
  (secrets, unsafe URLs) when the run is created, before any model call, and a requirement that fails the scan is
  refused; text that passes the scan IS sent to Google. Do not enable this for requirements that must not leave the
  machine.
* **Model names change.** The defaults reflect what worked for a new free-tier key at the time of writing.
