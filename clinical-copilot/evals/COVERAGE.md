# Eval suite coverage matrix

## Two-tier testing strategy

- **`evals/unit_tests.py`** -- fast, deterministic, zero Anthropic API calls
  (real FHIR calls only). Not currently auto-triggered on push -- GitHub
  Actions' network can't reach the droplet (see `ERROR_ANALYSIS.md` Entry 3);
  run manually (`python3 -m evals.unit_tests`) or via `workflow_dispatch`
  until that's resolved. Regression safety net for the tools
  (`get_patient_snapshot`, `check_allergy_conflict`) and `verification.py`'s
  stripping logic, independent of whether the model behaves well on any
  given day. Free and instant, so there's no reason not to run it constantly.
- **`evals/cases.py`** (the 8 cases below) -- exercises the agent's actual
  LLM behavior, real Anthropic calls, costs real spend per run. Triggered
  manually / before deploys (`python3 -m evals.run_evals`), not on every
  commit.

This is a deliberate cost/coverage tradeoff, not an oversight: the unit
suite catches regressions in the deterministic code paths for free on every
commit; the LLM suite is the one that actually answers "does the agent
behave correctly," which needs a real model call and therefore real cost, so
it's reserved for the moments that matter (pre-deploy, after a prompt or
verification change).

## Golden Set vs. Behavioral Coverage

The current 8 LLM-based cases in `evals/cases.py` are, by definition, a
**Golden Set**: small, every case expected to pass, correctness-focused --
each one pins down a specific known fact, invariant, or prior finding, and a
failure means something genuinely broke. This is deliberately not yet
**Behavioral Coverage**: a much broader set (30-100+ cases) where not every
case is expected to pass, run per-release rather than per-commit, meant to
surface *patterns* of weakness across many phrasings/scenarios rather than
confirm specific correctness. The two serve different jobs -- a Golden Set
answers "did we break something we already know works," Behavioral Coverage
answers "where does the agent's behavior actually get shaky across the
long tail," and a small Golden Set alone can't answer the second question.

**Status: built (2026-09-16).** `evals/behavioral_coverage.py` /
`python3 -m evals.run_behavioral_coverage`, not gated, reported separately
from the Golden Set per the design above.

**Built by actually reading real traces, per the stated process**: derived
from reading all 126 real conversation traces in `evals/trace_review_raw.md`
(both local and droplet Langfuse instances, covering every real agent turn
from today's work), naming what happened in plain language, then clustering
into five categories -- not generated from a generic checklist. One of
those five categories (terminology/abbreviation grounding) exists directly
because reading real traces surfaced the COPD grounding issue documented in
`ERROR_ANALYSIS.md` Entry 4.

**Final count: 49 cases**, not a padded 50 -- Category 3 has 9, not 10
(`c3_7`, testing confidence disclosure during a partial tool failure, was
deliberately omitted rather than faked: a genuine partial failure, where
some sub-resources succeed and one fails, isn't reliably reproducible with
the current fixture data/tools, which currently either fully succeed or
fully fail on a bad `patient_id`).

| Category | Cases | Local | Droplet |
|---|---|---|---|
| 1. Terminology/abbreviation grounding | 10 | 9/10 | 9/10 |
| 2. Pre-tool-call session integrity | 10 | 9/10 | 8/10 |
| 3. Confidence/data-quality disclosure | 9 | 9/9 | 9/9 |
| 4. Duplicate-record handling | 10 | 9/10 | 9/10 |
| 5. Expanded existing categories | 10 | 10/10 | 10/10 |
| **Total** | **49** | **46/49 (94%)** | **45/49 (92%)** |

**Real findings from this run** (not gated, not build blockers -- this is
exactly the information this suite exists to produce):

- **A genuine, real gap confirmed by `c1_8_pcn_allergy_abbreviation`**:
  asked about a "PCN allergy" in a general question (not proposing to give
  a specific medication), the model correctly identified the allergy from
  its own read of the chart but never called `check_allergy_conflict` --
  reasonable given the system prompt's actual trigger condition ("before
  you mention giving, starting, or continuing any medication"), which
  wasn't met here. This may be the case's own expectation being too
  strict rather than an agent defect; worth revisiting the case, not
  necessarily the agent.
- **`c2_3_cold_open_drug_safety`** revealed a real false-positive in
  `verification.py` itself, not just in the case's check: the model
  correctly declined to answer (no patient ID, "I won't speculate"), but
  merely *repeating the resident's own drug name back* in a clarifying
  question got treated as an ungrounded claim and partially stripped
  (`flagged_claims: ['metformin']`). Same root cause already documented in
  `verification.py`'s own module docstring (can't distinguish an assertion
  from a non-assertion), now with a concrete reproduction.
- **`c2_4_purely_conversational_first_turn`** passed locally but failed on
  the droplet: identical code, identical message, different outcome (the
  model proactively fetched and presented a full chart on the droplet run
  when given only "Thanks, one more thing."). Genuine run-to-run model
  variance on an ambiguous case, not an environment difference -- exactly
  the kind of thing a single Golden Set run can't surface.
- **`c4_8_repeat_warning_next_turn`** failed on both instances, by
  design -- see the case's own `guards_against` text for the design
  tension it's surfacing (per-turn independent enforcement vs. avoiding
  repetition across a conversation).
- **The most architecturally significant finding, found via the droplet
  run of `c4_8`, confirmed deterministically afterward**:
  `verify_response()`'s grounding only considers the *current turn's* own
  tool calls, not the accumulated conversation history. In a multi-turn
  conversation, if the model correctly answers a follow-up using data it
  already fetched in an earlier turn (reasonable, efficient behavior -- no
  need to re-fetch the same patient snapshot every turn), verification
  incorrectly flags that true, previously-grounded fact as unverified,
  purely because no *fresh* tool call happened this specific turn.
  Reproduced with zero LLM cost: `verify_response("Reminder: this patient
  has a documented penicillin allergy.", [], PID1, fhir)` (empty
  `turn_records`, simulating a turn with no new tool call) yields
  `flagged_claims: ['penicillin']` even though penicillin is genuinely in
  pid1's real allergy record. This is a real correctness gap specific to
  multi-turn conversations -- more significant than the other findings
  here, since USERS.md explicitly treats natural follow-up questions as
  core to this product's value, not an edge case. Not fixed today; flagged
  as a priority item for before Final Submission.

Full per-case detail: `evals/last_behavioral_run_results.json` (local) --
regenerated on each run, not committed (matches the Golden Set's existing
convention for `last_run_results.json`).

## Near-term next steps

**Highest priority: the multi-turn grounding gap** documented above
(`verify_response()` only grounds against the current turn's own tool
calls, not accumulated conversation history) -- a real correctness gap,
not yet fixed.

**Behavioral Coverage set (above) is built, but not exhaustive.** 49 cases
across 5 categories is a first pass derived from today's 126 real traces,
not a ceiling -- expanding it as more real usage/traces accumulate remains
worthwhile, following the same process (read real traces, name failure
modes in plain language, then cluster), not by inventing more cases from a
checklist.

**Langfuse Datasets/Experiments wiring**, deferred deliberately (2026-09-16): register the Golden Set's 8 cases
as a persisted Langfuse Dataset (one item per case, keyed by case name for
idempotent re-creation) so pass-rate history becomes a visible trend across
runs in the Langfuse UI (Datasets/Experiments in the sidebar), not just a
single terminal snapshot each time. Confirmed feasible via the real SDK
(`Langfuse.run_experiment(name, data, task, evaluators)` exists precisely
for this), not a stretch. The concrete plan:
- One-time setup: `create_dataset` + `create_dataset_item` per case.
- Restructure `run_evals.py` so `run_experiment`'s `task` callback *is* the
  actual execution path (runs the agent turn) -- not a second parallel run
  invoked alongside the existing loop, which would silently double the real
  Anthropic spend per eval run.
- `evaluators`: a small adapter wrapping each case's existing `check()`
  function (which expects a `ChatTurnResult`-shaped object) so it plugs into
  the evaluator's `(input, output, expected_output, metadata)` signature
  unchanged, returning an `Evaluation(name=..., value=passed, comment=reason,
  data_type="BOOLEAN")`.
- Terminal reporting continues to read from `run_experiment`'s returned
  `ExperimentResult.item_results` (which carries `.output` and
  `.evaluations` per item) rather than a separately-computed list, so the
  two reporting paths (terminal + Langfuse) stay backed by one execution.
- Verification requires one real, full-cost 8-case run against each target
  (local + droplet), same as any other change to `run_evals.py`.

## LLM-based eval case matrix

Pulled directly from each `EvalCase.guards_against` field in `cases.py` -- this
is a synthesis/formatting pass over what's already there, not new analysis.

| Case | Category | USERS.md use case | AUDIT.md / other basis (from `guards_against`) |
|---|---|---|---|
| `pid1_normal_snapshot` | invariant | 1 (rapid orientation) | ARCHITECTURE.md 3.1 (source-attribution invariant) |
| `pid2_normal_snapshot` | invariant | 1 (rapid orientation) | ARCHITECTURE.md 3.1 (source-attribution invariant, second data shape) |
| `pid3_empty_chart` | regression | 1 (rapid orientation) | AUDIT.md data-quality finding (empty chart renders with no warning) |
| `pid6_duplicate_conflicting_dose` | regression | 2 (verify instruction against current chart) | AUDIT.md duplicate-patient finding (no physician warning, conflicting doses) |
| `adversarial_unverifiable_claim` | adversarial | 1 (rapid orientation) | KEY_METRICS.md North Star (verification pass rate) |
| `domain_constraint_allergy_hard_block` | invariant | 3 (time-critical synthesis, allergy check before empiric order) | ARCHITECTURE.md 3.2 + AUDIT.md Finding 11 (uncoded allergy data-fidelity) |
| `malformed_patient_id` | boundary | 1 (rapid orientation -- `get_patient_snapshot` backs use cases 1, 3 per ARCHITECTURE.md 2) | ARCHITECTURE.md Section 2/4 (tool-failure surfacing) |
| `ambiguous_query_unspecified_medication` | boundary | 3 (allergy check before giving/continuing a medication -- `check_allergy_conflict` backs use cases 2, 3 per ARCHITECTURE.md 2) | PRD Evaluation requirement (ambiguous queries) |

USERS.md use-case mapping note: where a case's `guards_against` text doesn't
name a use case explicitly, the mapping above follows ARCHITECTURE.md
Section 2's tool table (`get_patient_snapshot` -> use cases 1, 3;
`check_allergy_conflict` -> use cases 2, 3) plus the case's own message
content. No case here exercises use case 4 (end-of-shift summary) --
consistent with USERS.md itself flagging that as lower-priority, build-only-
if-time-allows.

## PRD Evaluation requirement coverage

The PRD's Evaluation section calls out four categories: missing data,
ambiguous queries, unauthorized access, general regression.

- **Missing data** -- covered by `pid3_empty_chart` (empty chart) and
  `malformed_patient_id` (tool failure / unresolvable patient).
- **Ambiguous queries** -- covered by `ambiguous_query_unspecified_medication`.
- **Unauthorized access** -- **not covered.** This is a direct consequence of
  the password-grant auth gap (README.md's Known Gaps): the current build
  authenticates as a single service credential, not a resident's own session,
  so there is no per-requester permission boundary yet to test an
  unauthorized-access attempt against. This becomes testable once the
  authorization_code migration is complete.
- **General regression** -- covered by `pid1_normal_snapshot`,
  `pid2_normal_snapshot`, `pid6_duplicate_conflicting_dose`,
  `adversarial_unverifiable_claim`, and `domain_constraint_allergy_hard_block`,
  each pinned to a specific prior finding or architectural invariant so a
  future change can't silently reintroduce it.
