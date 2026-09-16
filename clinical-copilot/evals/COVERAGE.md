# Eval suite coverage matrix

## Two-tier testing strategy

- **`evals/unit_tests.py`** -- fast, deterministic, zero Anthropic API calls
  (real FHIR calls only). Runs automatically on every push to `main`
  (`.github/workflows/tests.yml`). Regression safety net for the tools
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
