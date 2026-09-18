#!/usr/bin/env python3
"""Fast, deterministic, LLM-free tests: no Anthropic API calls, so free and
instant enough to run on every commit (see .github/workflows/tests.yml).

Different from evals/cases.py, which exercises the agent's actual LLM
behavior against real Anthropic calls and costs real spend per run -- those
stay manually-triggered/pre-deploy, not run on every commit. These tests
instead check the parts of the system that don't need a model call at all:
the two tools directly (real FHIR calls, no LLM), and verification.py's
stripping logic against a hand-constructed fake draft (no FHIR chart
lookups involved in the draft itself, though the domain-constraint check
inside verify_response still makes a real, deterministic FHIR call --
that's the same LLM-free/network-allowed distinction as (a) and (b)).

Follows the same lightweight runner convention as run_evals.py (no pytest
dependency added) rather than introducing a second test-runner pattern.
"""

from __future__ import annotations

import sys
from typing import Callable

from app.agent import ClinicalCopilotAgent
from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.schemas import (
    CheckAllergyConflictOutput,
    GetPatientSnapshotOutput,
    SummarizeShiftEventsOutput,
    ToolFailure,
)
from app.sensitivity import filter_encounters_by_sensitivity, has_high_sensitivity_clearance
from app.tools import check_allergy_conflict, get_patient_snapshot, summarize_shift_events
from app.verification import ToolCallRecord, verify_response
from evals import fixtures as f
from evals.golden_facts import GOLDEN_FACTS


def _contains_all(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return all(term.lower() in lowered for term in terms)


# --- (a) get_patient_snapshot, tested directly against real FHIR data ------


def test_get_patient_snapshot_pid1(fhir: FhirClient) -> tuple[bool, str]:
    result = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE})
    if not isinstance(result, GetPatientSnapshotOutput):
        return False, f"expected a snapshot, got a tool failure: {result}"
    facts = GOLDEN_FACTS["pid1_alice"]
    all_text = " ".join(c.text for c in result.conditions) + " " + \
        " ".join(m.text for m in result.medications) + " " + \
        " ".join(a.text for a in result.allergies)
    expected = facts["conditions"] + facts["medications"] + facts["allergies"]
    if not _contains_all(all_text, expected):
        return False, f"snapshot is missing one of {expected}, got: {all_text!r}"
    return True, "snapshot contains all golden facts for pid1"


def test_get_patient_snapshot_pid2(fhir: FhirClient) -> tuple[bool, str]:
    result = get_patient_snapshot(fhir, {"patient_id": f.PID2_BOB})
    if not isinstance(result, GetPatientSnapshotOutput):
        return False, f"expected a snapshot, got a tool failure: {result}"
    facts = GOLDEN_FACTS["pid2_bob"]
    all_text = " ".join(c.text for c in result.conditions) + " " + \
        " ".join(m.text for m in result.medications) + " " + \
        " ".join(a.text for a in result.allergies)
    expected = facts["conditions"] + facts["medications"] + facts["allergies"]
    if not _contains_all(all_text, expected):
        return False, f"snapshot is missing one of {expected}, got: {all_text!r}"
    return True, "snapshot contains all golden facts for pid2"


# --- (b) check_allergy_conflict, tested directly ---------------------------


def test_check_allergy_conflict_positive(fhir: FhirClient) -> tuple[bool, str]:
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "penicillin"})
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if not result.conflict_found:
        return False, "pid1 has a documented penicillin allergy; expected conflict_found=True"
    return True, "known conflict (pid1 + penicillin) correctly flagged"


def test_check_allergy_conflict_negative(fhir: FhirClient) -> tuple[bool, str]:
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "metformin"})
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if result.conflict_found:
        return False, "pid1's only allergy is penicillin; expected conflict_found=False for metformin"
    return True, "known non-conflict (pid1 + metformin) correctly not flagged"


def test_check_allergy_conflict_cross_reactive(fhir: FhirClient) -> tuple[bool, str]:
    """Phase 5 (app/clinical_reference.py): pid1's documented penicillin
    allergy must also flag amoxicillin -- a different drug in the same
    curated 'penicillins' class -- even though "amoxicillin" never appears
    in the allergy text itself, so this can only pass via the cross-
    reactivity table, not the pre-existing direct/substring match."""
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "amoxicillin"})
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if not result.conflict_found:
        return False, "pid1's penicillin allergy should cross-flag amoxicillin (same drug class); got conflict_found=False"
    if result.cross_reactive_class != "penicillins":
        return False, f"expected cross_reactive_class='penicillins', got {result.cross_reactive_class!r}"
    return True, "amoxicillin correctly flagged via the curated penicillin cross-reactivity table"


def test_check_allergy_conflict_cross_reactive_scoped_to_curated_classes(fhir: FhirClient) -> tuple[bool, str]:
    """The cross-reactivity table is a scoped stand-in, not a general
    drug-class knowledge base -- a medication in no curated class, and not
    a direct/substring match, must still come back as no conflict rather
    than the table silently over-firing."""
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "metformin"})
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if result.conflict_found:
        return False, "metformin is in no curated drug class and isn't a substring match; expected conflict_found=False"
    if result.cross_reactive_class is not None:
        return False, f"expected cross_reactive_class=None for a non-conflict, got {result.cross_reactive_class!r}"
    return True, "medication outside every curated class correctly reported no conflict"


# --- (c) verification.py's stripping logic, no LLM involved ----------------


def test_verification_strips_ungrounded_claim(fhir: FhirClient) -> tuple[bool, str]:
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE})
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    fake_draft = (
        "Alice Testpatient has type 2 diabetes and hypertension, and is on Metformin "
        "and Lisinopril. She is also on Warfarin 5 mg daily, which was adjusted last week."
    )
    outcome = verify_response(fake_draft, [record], f.PID1_ALICE, fhir)

    if "warfarin" not in [c.lower() for c in outcome.flagged_claims]:
        return False, f"expected 'warfarin' to be flagged, got flagged_claims={outcome.flagged_claims}"
    if "warfarin" in outcome.final_response.lower().split("[verification note")[0].lower():
        return False, "the fabricated Warfarin claim survived into the final response body"
    if outcome.passed_source_attribution:
        return False, "passed_source_attribution should be False when a claim was stripped"
    return True, "fabricated Warfarin claim (not in real tool output) correctly flagged and stripped"


def test_verification_passes_grounded_claim(fhir: FhirClient) -> tuple[bool, str]:
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE})
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    grounded_draft = (
        "Alice Testpatient has type 2 diabetes and hypertension. She is on Metformin "
        "500 mg and Lisinopril 10 mg."
    )
    outcome = verify_response(grounded_draft, [record], f.PID1_ALICE, fhir)

    if outcome.flagged_claims:
        return False, f"expected no flagged claims for an all-grounded draft, got {outcome.flagged_claims}"
    if not outcome.passed_source_attribution:
        return False, "passed_source_attribution should be True when nothing was stripped"
    if grounded_draft not in outcome.final_response:
        return False, "the grounded draft text should pass through unmodified (verification notes may be appended)"
    return True, "an all-grounded draft passed through untouched"


# --- (c) app/sensitivity.py -- pure functions, no FHIR/network needed at all,
# but kept in this LLM-free suite (not evals/cases.py) since they need no
# model call either. Phase 1's empirical finding this guards: a real
# authorization_code `user/`-scoped token's FHIR Encounter search does NOT
# filter high-sensitivity encounters (confirmed live against pid4's known
# sensitivity='high' test encounter -- ARCHITECTURE.md 3.3 / architecture-
# audit.md 6.6) -- this filter is the only thing that does, so it must
# actually exclude what the platform doesn't. ------------------------------


def test_sensitivity_filter_excludes_high_for_clin(fhir: FhirClient) -> tuple[bool, str]:
    encounters = [
        {"id": "1", "sensitivity": "normal"},
        {"id": "2", "sensitivity": "high"},
        {"id": "3", "sensitivity": ""},
    ]
    filtered = filter_encounters_by_sensitivity(encounters, "clin")
    ids = [e["id"] for e in filtered]
    if "2" in ids:
        return False, f"clin (no High grant per audit-notes.md) must not see a high-sensitivity encounter, got ids={ids}"
    if ids != ["1", "3"]:
        return False, f"expected non-high encounters to pass through unchanged, got ids={ids}"
    return True, "clin correctly excluded the high-sensitivity encounter, kept the others"


def test_sensitivity_filter_allows_high_for_doc(fhir: FhirClient) -> tuple[bool, str]:
    encounters = [{"id": "1", "sensitivity": "high"}]
    filtered = filter_encounters_by_sensitivity(encounters, "doc")
    if len(filtered) != 1:
        return False, f"doc holds a High-sensitivity grant per audit-notes.md's gacl matrix, expected it kept, got {filtered}"
    if has_high_sensitivity_clearance("clin"):
        return False, "clin must not report High-sensitivity clearance (audit-notes.md: no grant)"
    if not has_high_sensitivity_clearance("admin"):
        return False, "admin must report High-sensitivity clearance (audit-notes.md: full write grant)"
    return True, "doc's High-sensitivity grant is respected; clin/admin clearance flags match the gacl matrix"


# --- (d) Two invariants found via THREAT_MODEL.md-prompted re-verification
# (2026-09-17) against the post-Phase-1 code, confirmed live, then fixed.
# Both are deterministic reproductions with no LLM in the loop -- exactly
# what this suite is for, unlike evals/cases.py's LLM-behavior cases.


def test_domain_constraint_backstop_survives_missing_patient_id(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a real bug: verify_response() skipped the
    hard-coded allergy-conflict backstop entirely whenever a /chat caller
    omitted `patient_id`, because `_check_domain_constraint` only ran
    `if current_patient_id:`. Live-confirmed against pid1's real, documented
    penicillin allergy before the fix, with a drafted response mentioning
    penicillin but never stating the conflict:

        | patient_id declared | passed_domain_constraint | HARD STOP | verification_passed |
        |---|---|---|---|
        | supplied             | False                     | yes       | False                |
        | omitted              | True                      | no        | True                 |

    Omitting patient_id from the request silently disabled a hard,
    code-level safety check -- exactly the thing ARCHITECTURE.md 3.2 says a
    prompt instruction alone can't be trusted for. Fix: verify_response()
    now falls back to the single patient this turn's own tool calls
    grounded data for when the caller omits patient_id
    (`app/verification.py::_effective_domain_check_patient_id`), rather than
    skipping the check outright.
    """
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE})
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)
    fake_draft = "Penicillin should be fine to give for this infection."

    outcome = verify_response(fake_draft, [record], None, fhir)  # patient_id OMITTED, as /chat allows
    if outcome.passed_domain_constraint:
        return False, (
            "domain-constraint backstop was skipped even though patient_id was omitted -- "
            "regression of the 2026-09-17 fix"
        )
    if "HARD STOP" not in outcome.final_response:
        return False, "expected a [HARD STOP] warning injected for the real, documented penicillin conflict"
    if "penicillin" not in outcome.enforced_warnings:
        return False, f"expected 'penicillin' in enforced_warnings, got {outcome.enforced_warnings}"
    return True, "domain-constraint backstop correctly fired even though patient_id was omitted from the request"


def test_cross_patient_tool_call_blocked(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a real bug: `_call_tool()` dispatched straight to
    the real FHIR read for whatever `patient_id` a tool call carried, with
    no check against the conversation's declared active patient anywhere in
    the call path. Live-confirmed before the fix: a `get_patient_snapshot`
    call for pid4 executed and returned real data (Dan Otherprovider, his
    real conditions/medications) while the turn's declared active patient
    was pid1 -- nothing intercepted the mismatch before the network call.
    The only patient_id filtering that existed (`records_for_active_patient`
    in `run_turn`) ran *after* the fetch, and only affected grounding, not
    whether the read happened.

    Fix: `_call_tool()` now takes the turn's `active_patient_id` and rejects
    a mismatched tool-call `patient_id` with a `ToolFailure` (detail_code
    `patient_mismatch`) before dispatch (`app/agent.py`).
    """
    agent = ClinicalCopilotAgent(get_settings())

    blocked = agent._call_tool("get_patient_snapshot", {"patient_id": f.PID4_DAN}, fhir, f.PID1_ALICE, [])
    if not isinstance(blocked, ToolFailure):
        return False, f"expected the mismatched patient_id to be blocked before dispatch, got: {blocked!r}"
    if blocked.detail_code != "patient_mismatch":
        return False, f"expected detail_code='patient_mismatch', got {blocked.detail_code!r}"

    # Control: a matching patient_id must still work normally -- this isn't
    # supposed to block all tool calls, only mismatched ones.
    ok = agent._call_tool("get_patient_snapshot", {"patient_id": f.PID1_ALICE}, fhir, f.PID1_ALICE, [])
    if not isinstance(ok, GetPatientSnapshotOutput):
        return False, f"a matching patient_id should still succeed normally, got: {ok!r}"

    return True, "cross-patient tool call was blocked before any FHIR read; matching patient_id still works"


def test_shift_summary_reports_nothing_gathered_yet(fhir: FhirClient) -> tuple[bool, str]:
    """UC4 boundary condition, LLM-free by design rather than an eval case:
    summarize_shift_events' `data_gathered=False` path is a Python-level
    contract (what does the function return when turn_records has no
    records for this patient), not really a question of model behavior --
    a compliant model, following SYSTEM_PROMPT's "gather first" nudge,
    should rarely if ever hit this path in a real conversation, which
    makes it an unreliable thing to force via a natural LLM eval message.
    Testing the tool's own contract directly is both more rigorous and
    more deterministic for this specific case than trying to coax an LLM
    into skipping its own gathering step. The companion boundary condition
    -- data was gathered, and it's genuinely empty (data_gathered=True,
    events=[]) -- is `shift_summary_empty_honest_report` in evals/cases.py,
    exercised against pid3's real empty chart; this test is deliberately
    the other honest-failure condition, not a duplicate of it.
    """
    result = summarize_shift_events([], {"patient_id": f.PID1_ALICE})
    if not isinstance(result, SummarizeShiftEventsOutput):
        return False, f"expected a SummarizeShiftEventsOutput, got: {result!r}"
    if result.data_gathered is not False:
        return False, f"expected data_gathered=False when turn_records has nothing for this patient, got {result.data_gathered!r}"
    if result.events:
        return False, f"expected an empty events list when nothing was gathered, got {result.events!r}"

    # Control: a real record for a DIFFERENT patient must not leak in as
    # "gathered" for this patient -- data_gathered is per-patient, not
    # "anything exists in turn_records at all".
    other_patient_record = ToolCallRecord(
        tool_name="get_patient_snapshot",
        patient_id=f.PID2_BOB,
        output=GetPatientSnapshotOutput(
            patient_id=f.PID2_BOB, name="Bob Testpatient", birth_date=None, gender=None,
            conditions=[], medications=[], allergies=[], chart_is_empty=True,
            duplicate_warnings=[], partial_failures=[],
        ),
    )
    scoped_result = summarize_shift_events([other_patient_record], {"patient_id": f.PID1_ALICE})
    if scoped_result.data_gathered is not False:
        return False, "a different patient's gathered data incorrectly counted as 'gathered' for this patient"

    return True, "correctly reports data_gathered=False, scoped per-patient, when nothing has been gathered yet"

    return True, "cross-patient tool call was blocked before any FHIR read; matching patient_id still works"


TESTS: list[tuple[str, Callable[[FhirClient], tuple[bool, str]]]] = [
    ("get_patient_snapshot_pid1", test_get_patient_snapshot_pid1),
    ("get_patient_snapshot_pid2", test_get_patient_snapshot_pid2),
    ("check_allergy_conflict_positive", test_check_allergy_conflict_positive),
    ("check_allergy_conflict_negative", test_check_allergy_conflict_negative),
    ("check_allergy_conflict_cross_reactive", test_check_allergy_conflict_cross_reactive),
    ("check_allergy_conflict_cross_reactive_scoped_to_curated_classes", test_check_allergy_conflict_cross_reactive_scoped_to_curated_classes),
    ("verification_strips_ungrounded_claim", test_verification_strips_ungrounded_claim),
    ("verification_passes_grounded_claim", test_verification_passes_grounded_claim),
    ("sensitivity_filter_excludes_high_for_clin", test_sensitivity_filter_excludes_high_for_clin),
    ("sensitivity_filter_allows_high_for_doc", test_sensitivity_filter_allows_high_for_doc),
    ("domain_constraint_backstop_survives_missing_patient_id", test_domain_constraint_backstop_survives_missing_patient_id),
    ("cross_patient_tool_call_blocked", test_cross_patient_tool_call_blocked),
    ("shift_summary_reports_nothing_gathered_yet", test_shift_summary_reports_nothing_gathered_yet),
]


def run_all_tests() -> list[tuple[str, bool, str]]:
    """Runs every test and returns (name, passed, reason) tuples, no
    printing -- the reusable core, shared by main() and evals/run_all.py."""
    settings = get_settings()
    fhir = FhirClient(settings, OAuthTokenProvider(settings))

    results = []
    for name, fn in TESTS:
        try:
            passed, reason = fn(fhir)
        except Exception as exc:  # noqa: BLE001 -- a test must record a failure, not crash the suite
            passed, reason = False, f"raised an exception: {exc!r}"
        results.append((name, passed, reason))
    return results


def main() -> int:
    results = run_all_tests()

    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    print(f"\n{'=' * 70}")
    print(f"Clinical Co-Pilot unit tests (LLM-free): {passed_count}/{total} passed")
    print(f"{'=' * 70}\n")
    for name, passed, reason in results:
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {name}")
        print(f"       {reason}\n")

    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
