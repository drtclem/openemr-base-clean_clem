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

**This is a named next step for Final Submission, not an oversight.**
Building the 30-100 actual behavioral cases is out of scope for today --
this section exists to document the distinction and commit to the plan, not
to implement it. When it's built, the eval runner's terminal output (see
below) already has a dedicated "Behavioral Coverage" section ready to report
its results as a second, separately-interpreted number once real cases
exist.

## Near-term next steps

Both deferred deliberately (2026-09-16), not oversights -- noted here with
enough specificity to pick back up without re-deriving the plan.

**Behavioral Coverage** (above): design and write 30-100 cases spanning a
much wider range of phrasings and scenarios per USERS.md use case, run
per-release rather than per-commit, evaluated for *patterns* of weakness
rather than pass/fail correctness.

**Process for building this, not just the target size:** the right way to
build this set is not to generate 30-100 cases from a generic checklist.
It's to read real conversation traces one at a time, write down in plain
language what went wrong (if anything) with no taxonomy in front of you,
and only then cluster those notes into named failure-mode categories
specific to this product. A downloaded or AI-generated taxonomy is useful
for checking coverage after the fact, but useless as a starting point --
this agent will fail in ways specific to clinical cross-coverage that no
generic list would name. This process requires real usage data (or, before
that exists, a deliberate red-teaming session using the same real-trace-
reading discipline) -- it is not something to shortcut by having an LLM
invent scenarios directly.

**Langfuse Datasets/Experiments wiring:** register the Golden Set's 8 cases
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
