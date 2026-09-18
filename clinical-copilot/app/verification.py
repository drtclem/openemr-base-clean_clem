"""Verification layer: ARCHITECTURE.md Section 3, simplified per the Early
Submission scope note.

Two independent checks run on every drafted response, after the model
drafts it and before it reaches the resident:

1. Source attribution (3.1) -- simplified: a curated clinical-vocabulary
   scan for medication/allergy/condition-shaped terms in the draft, each
   checked by substring match against a "grounded vocabulary" built from this
   turn's actual tool outputs. A term that doesn't match anything the tools
   actually returned is stripped and logged as a verification failure. This
   is deliberately NOT full per-claim source tagging (out of scope for this
   pass, see early-submission-build-prompt.md) -- it will miss a clinical
   claim phrased in words outside CLINICAL_VOCAB, and it can't tell a
   positive assertion ("patient is on X") from a negation ("no record of
   X"). Both are documented, honest gaps, not assumed away.

2. Domain constraint enforcement (3.2) -- a hard, code-level check: for
   every medication-shaped term in the draft, `check_allergy_conflict` is
   re-run server-side regardless of whether the model already called it.
   If a conflict exists and isn't clearly stated, the verification layer
   forces the warning into the response rather than trusting the model
   noticed its own tool result.

Also enforces two ARCHITECTURE.md Section 4 failure modes the same way,
since a wrong answer here is exactly the kind of silent-degradation risk
Section 4 treats as a verification failure, not an acceptable fallback:
duplicate patient records, and an empty chart presented as "nothing to report."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.fhir_client import FhirClient
from app.schemas import CheckAllergyConflictOutput, GetPatientSnapshotOutput, GetRecentEncountersOutput
from app.tools import check_allergy_conflict

# Curated clinical-term vocabulary used ONLY to decide "this looks like a
# claim worth checking" -- not to prove correctness. Deliberately modest;
# expanding this is the cheapest lever for improving recall later.
_MEDICATION_TERMS = [
    "metformin", "lisinopril", "warfarin", "aspirin", "ibuprofen", "amoxicillin",
    "penicillin", "atorvastatin", "simvastatin", "metoprolol", "amlodipine",
    "losartan", "omeprazole", "albuterol", "insulin", "heparin", "vancomycin",
    "ciprofloxacin", "azithromycin", "prednisone", "furosemide", "hydrochlorothiazide",
    "clopidogrel", "gabapentin", "sertraline", "fluoxetine", "levothyroxine",
]
_ALLERGY_TERMS = [
    "penicillin", "sulfa", "latex", "peanut", "shellfish", "iodine", "aspirin",
    "nsaid", "codeine", "morphine", "eggs", "soy",
]
_CONDITION_TERMS = [
    "diabetes", "hypertension", "hypotension", "pneumonia", "sepsis", "asthma",
    "copd", "ckd", "cirrhosis", "afib", "atrial fibrillation", "stroke", "cancer",
]

_ALL_TERMS = sorted(set(_MEDICATION_TERMS + _ALLERGY_TERMS + _CONDITION_TERMS), key=len, reverse=True)
_DOSE_RE = re.compile(r"\b([A-Z][a-zA-Z]+)\s+\d+(\.\d+)?\s*(mg|mcg|g|units?)\b")

# Common clinical abbreviations that won't literally substring-match the
# expanded form a tool returns (e.g. a model saying "COPD" against a
# Condition.text of "Chronic obstructive pulmonary disease"). Caught this via
# eval case pid2_normal_snapshot flagging a true, grounded fact as
# unverified -- a false positive is exactly as bad as a missed hallucination
# for this metric, so this is a correctness fix, not just documented as a gap.
_CLINICAL_ALIASES = {
    "copd": "chronic obstructive pulmonary disease",
    "htn": "hypertension",
    "afib": "atrial fibrillation",
    "atrial fibrillation": "afib",
    "ckd": "chronic kidney disease",
    "dm": "diabetes",
    "t2dm": "diabetes",
    "mi": "myocardial infarction",
    "chf": "heart failure",
}


def _is_grounded(term: str, grounded: set[str]) -> bool:
    t = term.lower()
    if t in grounded or any(t in g for g in grounded):
        return True
    alias = _CLINICAL_ALIASES.get(t)
    if alias and (alias in grounded or any(alias in g or g in alias for g in grounded)):
        return True
    return False

_EMPTY_CHART_CUES = ["no recorded", "nothing recorded", "no problems", "no medications",
                      "no allergies", "empty chart", "no active problems"]
_DUPLICATE_CUES = ["duplicate", "another record", "two records", "more than one record",
                    "two matching", "multiple records"]


@dataclass
class ToolCallRecord:
    tool_name: str
    patient_id: str
    output: object  # GetPatientSnapshotOutput | CheckAllergyConflictOutput


@dataclass
class VerificationOutcome:
    passed_source_attribution: bool
    passed_domain_constraint: bool
    final_response: str
    flagged_claims: list[str] = field(default_factory=list)
    enforced_warnings: list[str] = field(default_factory=list)


def _grounded_vocabulary(records: list[ToolCallRecord]) -> set[str]:
    vocab: set[str] = set()
    for rec in records:
        if isinstance(rec.output, GetPatientSnapshotOutput):
            snap = rec.output
            vocab.update(part.lower() for part in snap.name.split())
            for c in snap.conditions:
                vocab.add(c.text.lower())
            for m in snap.medications:
                vocab.add(m.text.lower())
                vocab.add(m.text.split()[0].lower())  # bare drug name, e.g. "metformin"
            for a in snap.allergies:
                vocab.add(a.text.lower())
                if a.reaction:
                    vocab.add(a.reaction.lower())
        elif isinstance(rec.output, CheckAllergyConflictOutput):
            conf = rec.output
            vocab.add(conf.medication_name.lower())
            if conf.matched_allergy_text:
                vocab.add(conf.matched_allergy_text.lower())
        elif isinstance(rec.output, GetRecentEncountersOutput):
            # Phase 6: an encounter's reason/type text can legitimately
            # overlap with _ALL_TERMS' condition vocabulary (e.g. a visit
            # reason of "diabetes follow-up") -- ground it the same way
            # snapshot conditions are, so a real, sourced fact from this
            # tool isn't falsely flagged as unverified.
            for enc in rec.output.encounters:
                vocab.add(enc.text.lower())
    return vocab


def _find_candidate_terms(text: str) -> list[str]:
    lowered = text.lower()
    found = [term for term in _ALL_TERMS if term in lowered]
    found += [m.group(1) for m in _DOSE_RE.finditer(text)]
    # de-dupe, case-insensitive
    seen: set[str] = set()
    unique = []
    for term in found:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def _check_source_attribution(
    draft_response: str, grounded: set[str]
) -> tuple[str, list[str]]:
    candidates = _find_candidate_terms(draft_response)
    unverified = [term for term in candidates if not _is_grounded(term, grounded)]
    if not unverified:
        return draft_response, []

    # Strip whole sentences containing an unverified term, rather than
    # surgically excising the word (which could leave a misleading fragment).
    sentences = re.split(r"(?<=[.!?])\s+", draft_response)
    kept = []
    for sentence in sentences:
        s_lower = sentence.lower()
        if any(term.lower() in s_lower for term in unverified):
            continue
        kept.append(sentence)
    final = " ".join(kept).strip()
    final += (
        f"\n\n[Verification note: I removed {len(unverified)} detail(s) I could not "
        f"confirm against this patient's chart: {', '.join(unverified)}.]"
    )
    return final, unverified


def _check_domain_constraint(
    draft_response: str,
    grounded_records: list[ToolCallRecord],
    patient_id: str,
    fhir: FhirClient,
) -> tuple[str, list[str], bool]:
    already_checked = {
        rec.output.medication_name.lower()
        for rec in grounded_records
        if isinstance(rec.output, CheckAllergyConflictOutput)
    }
    med_candidates = [t for t in _find_candidate_terms(draft_response) if t.lower() in _MEDICATION_TERMS]

    enforced: list[str] = []
    passed = True
    response = draft_response
    for med in med_candidates:
        if med.lower() in already_checked:
            continue
        result = check_allergy_conflict(fhir, {"patient_id": patient_id, "medication_name": med})
        if not isinstance(result, CheckAllergyConflictOutput):
            continue  # tool failure here is logged separately by the caller
        if result.conflict_found:
            mentions_conflict = any(
                cue in response.lower() for cue in ["allerg", "conflict", "do not give", "contraindicat"]
            )
            if not mentions_conflict:
                passed = False
                confidence_note = " (based on an uncoded/narrative allergy entry, lower confidence)" if result.low_confidence else ""
                response += (
                    f"\n\n[HARD STOP -- allergy conflict: {result.matched_allergy_text} "
                    f"conflicts with {med}{confidence_note}. This was not stated in the "
                    f"drafted response and has been added by the verification layer.]"
                )
                enforced.append(med)
    return response, enforced, passed


def _enforce_duplicate_and_empty_chart(
    response: str, grounded_records: list[ToolCallRecord]
) -> tuple[str, list[str]]:
    enforced = []
    lowered = response.lower()
    for rec in grounded_records:
        if not isinstance(rec.output, GetPatientSnapshotOutput):
            continue
        snap = rec.output
        if snap.duplicate_warnings and not any(cue in lowered for cue in _DUPLICATE_CUES):
            other_ids = ", ".join(w.other_patient_id for w in snap.duplicate_warnings)
            response += (
                f"\n\n[Verification note: there are {len(snap.duplicate_warnings)} other "
                f"patient record(s) matching this patient's name and birthdate "
                f"({other_ids}). This is a real ambiguity in the chart -- do not assume "
                f"they're the same visit or that data hasn't diverged between them.]"
            )
            enforced.append("duplicate_patient")
            lowered = response.lower()
        if snap.chart_is_empty and not any(cue in lowered for cue in _EMPTY_CHART_CUES):
            response += (
                "\n\n[Verification note: this patient's chart has no recorded problems, "
                "medications, or allergies -- that's an empty/incomplete chart, not "
                "confirmation there's nothing to report.]"
            )
            enforced.append("empty_chart")
            lowered = response.lower()
    return response, enforced


def _effective_domain_check_patient_id(
    current_patient_id: str | None, turn_records: list[ToolCallRecord]
) -> str | None:
    """The patient the hard-coded allergy-conflict backstop should run
    against. Prefers the caller-declared active patient; if the caller
    omitted it, falls back to whichever single patient this turn's own tool
    calls actually grounded data for -- an omitted request field is not a
    reason to skip a code-level safety check (ARCHITECTURE.md 3.2's "a wall,
    not a request"). Concretely: `patient_id: null` on /chat previously
    disabled this check entirely (passed_domain_constraint stayed True with
    no [HARD STOP] even for a real, documented conflict) purely because
    `if current_patient_id:` was falsy -- confirmed live against pid1's real
    penicillin allergy. Returns None only when genuinely ambiguous (more than
    one distinct patient touched this turn), matching this system's already-
    documented single-active-patient-per-turn assumption (README.md Known
    Gaps) rather than guessing which one to check.
    """
    if current_patient_id:
        return current_patient_id
    record_patient_ids = {r.patient_id for r in turn_records if r.patient_id}
    if len(record_patient_ids) == 1:
        return next(iter(record_patient_ids))
    return None


def verify_response(
    draft_response: str,
    turn_records: list[ToolCallRecord],
    current_patient_id: str | None,
    fhir: FhirClient,
) -> VerificationOutcome:
    grounded = _grounded_vocabulary(turn_records)
    after_source_check, flagged = _check_source_attribution(draft_response, grounded)

    domain_enforced: list[str] = []
    passed_domain = True
    effective_patient_id = _effective_domain_check_patient_id(current_patient_id, turn_records)
    if effective_patient_id:
        after_source_check, domain_enforced, passed_domain = _check_domain_constraint(
            after_source_check, turn_records, effective_patient_id, fhir
        )

    final_response, structural_enforced = _enforce_duplicate_and_empty_chart(
        after_source_check, turn_records
    )

    return VerificationOutcome(
        passed_source_attribution=not flagged,
        passed_domain_constraint=passed_domain,
        final_response=final_response,
        flagged_claims=flagged,
        enforced_warnings=domain_enforced + structural_enforced,
    )
