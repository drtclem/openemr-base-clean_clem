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
    DuplicatePatientWarning,
    GetPatientSnapshotOutput,
    GetRecentObservationsOutput,
    ObservationFact,
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
    result = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
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
    result = get_patient_snapshot(fhir, {"patient_id": f.PID2_BOB}, [])
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
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "penicillin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if not result.conflict_found:
        return False, "pid1 has a documented penicillin allergy; expected conflict_found=True"
    return True, "known conflict (pid1 + penicillin) correctly flagged"


def test_check_allergy_conflict_negative(fhir: FhirClient) -> tuple[bool, str]:
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "metformin"}, [])
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
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "amoxicillin"}, [])
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
    result = check_allergy_conflict(fhir, {"patient_id": f.PID1_ALICE, "medication_name": "metformin"}, [])
    if not isinstance(result, CheckAllergyConflictOutput):
        return False, f"expected a conflict result, got a tool failure: {result}"
    if result.conflict_found:
        return False, "metformin is in no curated drug class and isn't a substring match; expected conflict_found=False"
    if result.cross_reactive_class is not None:
        return False, f"expected cross_reactive_class=None for a non-conflict, got {result.cross_reactive_class!r}"
    return True, "medication outside every curated class correctly reported no conflict"


# --- (c) verification.py's stripping logic, no LLM involved ----------------


def test_verification_strips_ungrounded_claim(fhir: FhirClient) -> tuple[bool, str]:
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
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
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
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


def test_lab_value_grounding_tolerates_formatting(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a bug caught and fixed live during compare_
    signout_to_chart's build (2026-09-18) -- at the time it was only
    verified with a one-off interactive check, not a permanent test; this
    is that missing test. _LAB_VALUE_RE (app/verification.py) extracts a
    lab/vital value from the model's own drafted prose, but the model
    doesn't always format it identically to how
    ObservationFact.value/_observation_value_text() (app/tools.py) stored
    it -- e.g. the model writing "38.9°C" (the real degree symbol, no
    space) against a stored "38.9 C" (a space, no degree symbol). Without
    _is_grounded()'s normalization fallback, this exact mismatch strips a
    true, tool-sourced value from the response.

    Confirms both directions that matter: the equivalent-but-differently-
    formatted value IS treated as grounded (the bug that was actually
    found), and -- the control that proves this isn't just loosened
    grounding in general -- a genuinely different value is NOT swept in as
    a false match by the same normalization.
    """
    observations = GetRecentObservationsOutput(
        patient_id=f.PID1_ALICE,
        observations=[
            ObservationFact(
                text="Temperature",
                value="38.9 C",
                status="final",
                effective_datetime="2026-09-18T01:00:00Z",
                source_resource="Observation/fixture-temp-1",
            )
        ],
    )
    record = ToolCallRecord(tool_name="get_recent_observations", patient_id=f.PID1_ALICE, output=observations)

    matching_draft = "Her most recent temperature was 38.9°C, which is febrile."
    outcome = verify_response(matching_draft, [record], f.PID1_ALICE, fhir)
    if any("38.9" in c for c in outcome.flagged_claims):
        return False, (
            "the real temperature (38.9 C, as stored) was incorrectly stripped when the model "
            f"wrote it as 38.9°C -- got flagged_claims={outcome.flagged_claims}"
        )
    if "38.9" not in outcome.final_response:
        return False, "the real, matching temperature value did not survive into the final response"

    wrong_draft = "Her most recent temperature was 39.5°C, which is febrile."
    wrong_outcome = verify_response(wrong_draft, [record], f.PID1_ALICE, fhir)
    if not any("39.5" in c for c in wrong_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely different temperature (39.5°C, not in the tool "
            f"output) should still be flagged as ungrounded, got flagged_claims={wrong_outcome.flagged_claims}"
        )

    return True, (
        "a real lab value survives despite cosmetic formatting differences, and a genuinely "
        "different value is still caught"
    )


def test_lab_value_range_mention_not_flagged(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for a second live bug, found on the very next
    full-suite run after test_lab_value_grounding_tolerates_formatting's
    fix landed (2026-09-18): _LAB_VALUE_RE's candidate detection also
    caught the upper bound of a stated reference range ("...above the
    typical normal range (~3.5-5.0 mEq/L)") and stripped it as an
    "unverified claim" -- even though a reference range is general medical
    knowledge, not a claim about this patient, and shouldn't need a tool
    call to back it. Confirms the range exclusion
    (_LAB_VALUE_RANGE_PREFIX_RE) works end-to-end through verify_response():
    a reference-range mention survives untouched, and -- the control that
    proves this isn't a blanket weakening -- a genuinely fabricated value
    elsewhere in the same response is still caught.
    """
    observations = GetRecentObservationsOutput(
        patient_id=f.PID1_ALICE,
        observations=[
            ObservationFact(
                text="Potassium",
                value="5.8 mEq/L",
                status="final",
                effective_datetime="2026-09-18T02:00:00Z",
                source_resource="Observation/fixture-potassium-1",
            )
        ],
    )
    record = ToolCallRecord(tool_name="get_recent_observations", patient_id=f.PID1_ALICE, output=observations)

    draft = (
        "Her most recent potassium was 5.8 mEq/L, which is elevated -- above the typical "
        "normal range (~3.5-5.0 mEq/L). This should be correlated clinically."
    )
    outcome = verify_response(draft, [record], f.PID1_ALICE, fhir)
    if outcome.flagged_claims:
        return False, (
            "the reference-range mention (5.0 mEq/L, the range's upper bound) was incorrectly "
            f"flagged as an unverified patient-specific claim, got flagged_claims={outcome.flagged_claims}"
        )
    if draft not in outcome.final_response:
        return False, "the reference-range context was stripped from the response instead of surviving untouched"

    fabricated_draft = (
        "Her most recent potassium was 5.8 mEq/L, and her sodium was 200 mEq/L, which is "
        "critically abnormal."
    )
    fabricated_outcome = verify_response(fabricated_draft, [record], f.PID1_ALICE, fhir)
    if not any("200" in c for c in fabricated_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely fabricated value (200 mEq/L sodium, not in the tool "
            f"output, not part of a range) should still be flagged, got "
            f"flagged_claims={fabricated_outcome.flagged_claims}"
        )

    return True, "a reference-range mention survives untouched, and a genuinely fabricated value elsewhere is still caught"


def test_allergy_hard_stop_not_silenced_by_unrelated_conflict_word(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the most severe of three bugs found in a
    systematic audit (2026-09-18) of every regex/substring-match mechanism
    in verification.py, prompted after the two _LAB_VALUE_RE bugs: the
    domain-constraint HARD STOP's "already mentioned" check used a bare
    "conflict" substring cue, which collides with unrelated uses ("a
    scheduling conflict", "the two records conflict on her DOB") --
    confirmed live via audit before this fix. Silently skipping the HARD
    STOP append is exactly what ARCHITECTURE.md 3.2 calls this mechanism a
    "wall" specifically to prevent; a false match here means no signal at
    all reaches the resident. Fixed by dropping bare "conflict" and
    checking the remaining safe cues in a window around the specific
    medication mention, not the whole response
    (_mentions_conflict_near_medication).

    Uses pid1's real, documented penicillin allergy (same fixture as
    test_domain_constraint_backstop_survives_missing_patient_id) so the
    conflict is genuine, not a stub.
    """
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    unrelated_conflict_draft = (
        "There is a scheduling conflict for tomorrow's follow-up that the resident should know "
        "about. Penicillin should be fine to give for this infection."
    )
    outcome = verify_response(unrelated_conflict_draft, [record], f.PID1_ALICE, fhir)
    if outcome.passed_domain_constraint:
        return False, (
            "an unrelated mention of 'conflict' (a scheduling conflict) incorrectly satisfied "
            "the allergy-conflict check and silenced the HARD STOP for a real penicillin conflict"
        )
    if "HARD STOP" not in outcome.final_response:
        return False, "expected a [HARD STOP] warning injected for the real, documented penicillin conflict"

    already_stated_draft = "She has a documented penicillin allergy, so penicillin should not be given."
    already_stated_outcome = verify_response(already_stated_draft, [record], f.PID1_ALICE, fhir)
    if "HARD STOP" in already_stated_outcome.final_response:
        return False, "a response that already correctly stated the conflict should not get a redundant HARD STOP appended"

    return True, "an unrelated 'conflict' mention no longer silences the HARD STOP, and a genuine self-correction is still respected"


def test_duplicate_warning_not_silenced_by_unrelated_records_mention(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the second of three bugs from the same audit:
    the duplicate-patient-record caveat's "already mentioned" check used
    bare substrings like "two records"/"multiple records", which collide
    with unrelated mentions ("multiple records of prior vaccinations") --
    confirmed live via audit. This is the exact "never silently drop"
    guarantee c4_7_explicit_suppress_request's own guards_against text
    describes; a false match here silently skips the caveat entirely.
    Fixed by requiring the duplicate/matching language to be tied to the
    patient/chart/record context specifically, or by checking the literal
    other_patient_id directly (_mentions_duplicate_warning).
    """
    snapshot = GetPatientSnapshotOutput(
        patient_id=f.PID1_ALICE, name="Alice Testpatient", birth_date="1972-03-14", gender="female",
        conditions=[], medications=[], allergies=[], chart_is_empty=False,
        duplicate_warnings=[DuplicatePatientWarning(other_patient_id="fixture-other-id-1", matched_on="name+birthdate")],
        partial_failures=[],
    )
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    unrelated_records_draft = "Her chart shows multiple records of prior vaccinations. She has type 2 diabetes."
    outcome = verify_response(unrelated_records_draft, [record], f.PID1_ALICE, fhir)
    if "duplicate_patient" not in outcome.enforced_warnings:
        return False, (
            "an unrelated mention of 'multiple records' (vaccination history) incorrectly "
            "satisfied the duplicate-warning check and silenced the caveat for a real duplicate "
            f"patient record, got enforced_warnings={outcome.enforced_warnings}"
        )

    already_stated_draft = (
        "Possible duplicate record: this patient matches on name + birthdate with another "
        "patient record."
    )
    already_stated_outcome = verify_response(already_stated_draft, [record], f.PID1_ALICE, fhir)
    if "duplicate_patient" in already_stated_outcome.enforced_warnings:
        return False, "a response that already correctly stated the duplicate should not get a redundant caveat appended"

    return True, "an unrelated 'records' mention no longer silences the duplicate caveat, and a genuine self-correction is still respected"


def test_empty_chart_caveat_not_silenced_by_unrelated_no_problems_mention(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the third of three bugs from the same audit:
    the empty-chart caveat's "already mentioned" check used bare
    substrings like "no problems"/"no medications", which collide with
    unrelated uses ("no problems accessing this data") -- confirmed live
    via audit. A false match here silently skips the caveat that exists
    specifically so an empty chart never gets presented as reassuring
    (ARCHITECTURE.md Section 4). Fixed by keeping a few highly specific
    standalone phrases and replacing the generic ones with a structural
    pattern requiring "no"/"none" to actually be followed by "recorded"/
    "documented"/"on file"/"noted" (_mentions_empty_chart).
    """
    snapshot = GetPatientSnapshotOutput(
        patient_id=f.PID3_CAROL, name="Carol Emptychart", birth_date="1990-07-21", gender="female",
        conditions=[], medications=[], allergies=[], chart_is_empty=True,
        duplicate_warnings=[], partial_failures=[],
    )
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID3_CAROL, output=snapshot)

    unrelated_draft = "I had no problems accessing this data for the patient."
    outcome = verify_response(unrelated_draft, [record], f.PID3_CAROL, fhir)
    if "empty_chart" not in outcome.enforced_warnings:
        return False, (
            "an unrelated mention of 'no problems' (data access) incorrectly satisfied the "
            f"empty-chart check and silenced the caveat, got enforced_warnings={outcome.enforced_warnings}"
        )

    already_stated_draft = "This chart shows no conditions, medications, or allergies are recorded."
    already_stated_outcome = verify_response(already_stated_draft, [record], f.PID3_CAROL, fhir)
    if "empty_chart" in already_stated_outcome.enforced_warnings:
        return False, "a response that already correctly described the empty chart should not get a redundant caveat appended"

    return True, "an unrelated 'no problems' mention no longer silences the empty-chart caveat, and a genuine self-correction is still respected"


def test_dose_candidate_finds_real_drug_not_preceding_verb(fhir: FhirClient) -> tuple[bool, str]:
    """Regression test for the fourth bug from the same audit as the three
    "mentions" fixes above, and the same class as the two _LAB_VALUE_RE
    bugs: _DOSE_RE assumed the capitalized word immediately BEFORE a dose
    is always the drug name -- found live, "Started 10 mg Lisinopril
    daily" captured "Started", not "Lisinopril", stripping the whole
    (correct) sentence. Fixed structurally: checks both sides of the dose
    pattern and prefers a real drug name found immediately after the
    dose+unit when one exists, falling back to the preceding word only
    when it doesn't look verb-shaped (see _dose_candidates' own docstring
    for the honestly-disclosed limit of pure morphology here -- "Given"
    and "Day" needed a small supplementary exclusion, not the same kind
    of fix as the fabrication-phrase keyword lists elsewhere in this
    file).

    Uses the exact three collision sentences found during the audit, plus
    a control confirming detection isn't blanket-weakened.
    """
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
    if not isinstance(snapshot, GetPatientSnapshotOutput):
        return False, f"setup failed: couldn't fetch pid1 snapshot ({snapshot})"
    record = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=f.PID1_ALICE, output=snapshot)

    # A real drug (Lisinopril, pid1's actual medication) present, with a
    # verb -- not the drug name -- immediately preceding the dose.
    real_drug_draft = "Started 10 mg Lisinopril daily for her hypertension."
    outcome = verify_response(real_drug_draft, [record], f.PID1_ALICE, fhir)
    if any("started" in c.lower() for c in outcome.flagged_claims):
        return False, (
            "'Started' was incorrectly treated as the drug-name candidate instead of "
            f"'Lisinopril', got flagged_claims={outcome.flagged_claims}"
        )
    if real_drug_draft not in outcome.final_response:
        return False, "the real, grounded Lisinopril sentence was stripped instead of surviving"

    # No real drug present at all in either sentence -- the false-positive
    # word (Given / Day) must not be flagged, since there's nothing here
    # to fabricate a claim about.
    for draft in ["Given 4 g IV push.", "On Day 3 units of blood were transfused."]:
        no_drug_outcome = verify_response(draft, [record], f.PID1_ALICE, fhir)
        if no_drug_outcome.flagged_claims:
            return False, (
                f"expected no flagged claims for {draft!r} (no real drug name present), got "
                f"flagged_claims={no_drug_outcome.flagged_claims}"
            )

    # Control: a genuinely fabricated drug name (not in the tool output)
    # in the same "verb dose unit DRUG" shape must still be caught -- the
    # fix prefers the following word, it doesn't stop checking it.
    fabricated_draft = "Started 20 mg Fakenstatin daily for her cholesterol."
    fabricated_outcome = verify_response(fabricated_draft, [record], f.PID1_ALICE, fhir)
    if not any("fakenstatin" in c.lower() for c in fabricated_outcome.flagged_claims):
        return False, (
            "control failed: a genuinely fabricated drug name (Fakenstatin, not in the tool "
            f"output) should still be flagged, got flagged_claims={fabricated_outcome.flagged_claims}"
        )

    return True, (
        "the real drug name (not the preceding verb) is correctly extracted and grounds, the "
        "no-drug-present sentences don't get a false-positive word flagged, and a genuinely "
        "fabricated drug name in the same shape is still caught"
    )


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
    snapshot = get_patient_snapshot(fhir, {"patient_id": f.PID1_ALICE}, [])
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
    result = summarize_shift_events(fhir, {"patient_id": f.PID1_ALICE}, [])
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
    scoped_result = summarize_shift_events(fhir, {"patient_id": f.PID1_ALICE}, [other_patient_record])
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
    ("lab_value_grounding_tolerates_formatting", test_lab_value_grounding_tolerates_formatting),
    ("lab_value_range_mention_not_flagged", test_lab_value_range_mention_not_flagged),
    ("allergy_hard_stop_not_silenced_by_unrelated_conflict_word", test_allergy_hard_stop_not_silenced_by_unrelated_conflict_word),
    ("duplicate_warning_not_silenced_by_unrelated_records_mention", test_duplicate_warning_not_silenced_by_unrelated_records_mention),
    ("empty_chart_caveat_not_silenced_by_unrelated_no_problems_mention", test_empty_chart_caveat_not_silenced_by_unrelated_no_problems_mention),
    ("dose_candidate_finds_real_drug_not_preceding_verb", test_dose_candidate_finds_real_drug_not_preceding_verb),
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
