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

from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.schemas import CheckAllergyConflictOutput, GetPatientSnapshotOutput
from app.tools import check_allergy_conflict, get_patient_snapshot
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


TESTS: list[tuple[str, Callable[[FhirClient], tuple[bool, str]]]] = [
    ("get_patient_snapshot_pid1", test_get_patient_snapshot_pid1),
    ("get_patient_snapshot_pid2", test_get_patient_snapshot_pid2),
    ("check_allergy_conflict_positive", test_check_allergy_conflict_positive),
    ("check_allergy_conflict_negative", test_check_allergy_conflict_negative),
    ("verification_strips_ungrounded_claim", test_verification_strips_ungrounded_claim),
    ("verification_passes_grounded_claim", test_verification_passes_grounded_claim),
]


def main() -> int:
    settings = get_settings()
    fhir = FhirClient(settings, OAuthTokenProvider(settings))

    results = []
    for name, fn in TESTS:
        try:
            passed, reason = fn(fhir)
        except Exception as exc:  # noqa: BLE001 -- a test must record a failure, not crash the suite
            passed, reason = False, f"raised an exception: {exc!r}"
        results.append((name, passed, reason))

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
