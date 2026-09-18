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
from app.schemas import (
    CheckAllergyConflictOutput,
    CompareSignoutToChartOutput,
    DuplicatePatientWarning,
    GetPatientSnapshotOutput,
    GetRecentEncountersOutput,
    GetRecentObservationsOutput,
    SummarizeShiftEventsOutput,
)
from app.tools import ToolCallRecord, check_allergy_conflict  # noqa: F401 -- ToolCallRecord re-exported,
# see its docstring in app/tools.py for why it lives there now, not here

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

# Structural (shape-based) candidate detection, not enumeration -- the same
# principle _DOSE_RE already applies to medication doses, extended to
# lab/vital values. Added 2026-09-18, investigating compare_signout_to_chart:
# this is a real, if partial, fix for the recurring "_ALL_TERMS has no
# vocabulary for tool X's facts" gap (get_recent_observations, then this
# tool) -- a fabricated lab/vital value now becomes a candidate needing
# grounding WITHOUT "potassium" or any lab name ever needing to be added to
# a fixed list, because the match is on the number+unit shape, not a name.
# Deliberately does NOT close the other half of that recurring gap (a bare
# NAME-shaped claim, e.g. a fabricated condition or encounter type, has no
# numeric shape to match on) -- no cheap structural fix exists for that
# half; see COVERAGE.md's note on why expanding _ALL_TERMS incrementally
# remains the accepted approach there, not a structural fix.
_LAB_VALUE_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(mEq/L|mg/dL|mmHg|mmol/L|mcg/mL|ng/mL|g/dL|mIU/L|U/L|bpm|°C|°F|/min)\b",
    re.IGNORECASE,
)

# Excludes a lab/vital value from candidacy when it's the upper bound of a
# stated "X-Y unit" range (e.g. "normal range (~3.5-5.0 mEq/L)") -- found
# live, the same day _LAB_VALUE_RE itself was added: a model correctly
# citing a normal reference range alongside the real patient value got its
# OWN reference-range mention stripped as an "unverified claim", since a
# range's upper bound is exactly as number+unit-shaped as a genuine
# patient-specific value and nothing distinguished them. A reference range
# is general medical knowledge, not a claim about this patient, and
# shouldn't be held to the same "must come from a tool" bar. Unlike a
# fabricated medication name, a lab value's harm surface leans toward
# false positives (stripping real, useful clinical context) rather than
# false negatives (missing a fabricated value) being the primary risk --
# this exclusion accepts a small amount of the latter to avoid the former.
_LAB_VALUE_RANGE_PREFIX_RE = re.compile(r"\d+(\.\d+)?\s*[-‐-―]\s*$")

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


_LAB_VALUE_FORMATTING_RE = re.compile(r"[\s°]")


def _normalize_lab_value(s: str) -> str:
    """Strips whitespace and the degree symbol so a lab/vital value
    compares equal regardless of cosmetic formatting differences between
    how a tool stored it (app/tools.py's _observation_value_text formats
    as "<value> <unit>", e.g. "38.9 C") and how the model naturally
    writes it in prose (e.g. "38.9°C", no space, real degree symbol).
    Found live: _LAB_VALUE_RE's new candidate detection caught a real,
    tool-sourced temperature that then failed grounding on this exact
    formatting mismatch, stripping a true fact from the response."""
    return _LAB_VALUE_FORMATTING_RE.sub("", s)


def _is_grounded(term: str, grounded: set[str]) -> bool:
    t = term.lower()
    if t in grounded or any(t in g for g in grounded):
        return True
    alias = _CLINICAL_ALIASES.get(t)
    if alias and (alias in grounded or any(alias in g or g in alias for g in grounded)):
        return True
    t_norm = _normalize_lab_value(t)
    if t_norm != t and any(t_norm == _normalize_lab_value(g) or t_norm in _normalize_lab_value(g) for g in grounded):
        return True
    return False

# Fixed 2026-09-18, found live via audit: bare "no problems"/"no
# medications" collided with unrelated uses ("no problems accessing this
# data"), silently suppressing the empty-chart safety caveat below --
# _enforce_duplicate_and_empty_chart is production enforcement, not an
# eval check, so a false match here means the hard "never present empty
# as reassuring" guarantee silently doesn't fire. Fixed two ways: a few
# highly specific standalone phrases stay as bare substrings (low
# collision risk), and the generic "no X" shapes are replaced with a
# structural pattern requiring "no"/"none" to actually be followed by
# "recorded"/"documented"/"on file"/"noted" within a short window --
# catches real phrasings ("no conditions, medications, or allergies are
# recorded", "Conditions: none recorded") while excluding "no problems
# accessing this data", which mentions none of those words at all.
_EMPTY_CHART_CUES = ["empty chart", "chart is empty", "essentially empty", "genuinely empty",
                      "no active problems"]
_EMPTY_CHART_RECORDED_RE = re.compile(
    r"\b(?:no|none)\b.{0,60}?\b(?:recorded|documented|on file|noted)\b",
    re.IGNORECASE | re.DOTALL,
)


def _mentions_empty_chart(response: str) -> bool:
    lowered = response.lower()
    if any(cue in lowered for cue in _EMPTY_CHART_CUES):
        return True
    return bool(_EMPTY_CHART_RECORDED_RE.search(response))


# Fixed 2026-09-18, same audit, same risk shape and same severity: bare
# "two records"/"another record"/"multiple records" collided with
# unrelated mentions ("multiple records of prior vaccinations"), silently
# suppressing the duplicate-patient-record warning -- the exact "never
# silently drop" guarantee c4_7_explicit_suppress_request's own
# guards_against text describes. Narrowed to phrases that tie the
# duplicate/matching language to the patient/chart/record context
# specifically, rather than any generic "records" mention; the literal
# other_patient_id is also checked directly (see _mentions_duplicate_
# warning below) since a UUID has effectively zero collision risk.
_DUPLICATE_CUES = [
    "duplicate patient", "duplicate record", "duplicate chart", "duplicate-record",
    "possible duplicate", "duplicate warning", "another patient record",
    "two patient records", "matching patient record", "matches on name",
    "matched on name", "matching on name", "same name and birthdate",
    "same name + birthdate", "flagged a duplicate", "flagged another patient",
]


def _mentions_duplicate_warning(response: str, warnings: list[DuplicatePatientWarning]) -> bool:
    lowered = response.lower()
    if any(w.other_patient_id.lower() in lowered for w in warnings):
        return True
    return any(cue in lowered for cue in _DUPLICATE_CUES)


@dataclass
class VerificationOutcome:
    passed_source_attribution: bool
    passed_domain_constraint: bool
    final_response: str
    flagged_claims: list[str] = field(default_factory=list)
    enforced_warnings: list[str] = field(default_factory=list)


def _ground_snapshot(vocab: set[str], snap: GetPatientSnapshotOutput) -> None:
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


def _ground_encounters(vocab: set[str], enc_output: GetRecentEncountersOutput) -> None:
    # Phase 6: an encounter's reason/type text can legitimately overlap with
    # _ALL_TERMS' condition vocabulary (e.g. a visit reason of "diabetes
    # follow-up") -- ground it the same way snapshot conditions are, so a
    # real, sourced fact from this tool isn't falsely flagged as unverified.
    for enc in enc_output.encounters:
        vocab.add(enc.text.lower())


def _ground_observations(vocab: set[str], obs_output: GetRecentObservationsOutput) -> None:
    # UC2: grounds an observation's measured-thing text (e.g. "potassium")
    # and its formatted value (e.g. "5.2 mEq/L") so a real, sourced fact
    # from this tool isn't falsely flagged. Also reachable now via
    # _LAB_VALUE_RE's structural candidate detection (added alongside
    # compare_signout_to_chart), which doesn't need the measured-thing name
    # in _ALL_TERMS at all -- but the bare name (e.g. "potassium") is still
    # only a candidate if it happens to already be in _ALL_TERMS, which it
    # generally isn't; that half of the gap is unchanged, see this module's
    # docstring.
    for obs in obs_output.observations:
        vocab.add(obs.text.lower())
        if obs.value:
            vocab.add(obs.value.lower())


def _grounded_vocabulary(records: list[ToolCallRecord]) -> set[str]:
    vocab: set[str] = set()
    for rec in records:
        if isinstance(rec.output, GetPatientSnapshotOutput):
            _ground_snapshot(vocab, rec.output)
        elif isinstance(rec.output, CheckAllergyConflictOutput):
            conf = rec.output
            vocab.add(conf.medication_name.lower())
            if conf.matched_allergy_text:
                vocab.add(conf.matched_allergy_text.lower())
        elif isinstance(rec.output, GetRecentEncountersOutput):
            _ground_encounters(vocab, rec.output)
        elif isinstance(rec.output, GetRecentObservationsOutput):
            _ground_observations(vocab, rec.output)
        elif isinstance(rec.output, SummarizeShiftEventsOutput):
            # UC4: mostly redundant with the branches above, since the
            # underlying GetPatientSnapshotOutput/GetRecentEncountersOutput/
            # GetRecentObservationsOutput records this tool read from are
            # still in turn_records themselves and already grounded their
            # own text. Grounded anyway, defense in depth, and because the
            # duplicate-record event text is newly synthesized here (not a
            # verbatim field on any underlying Fact) -- without this branch
            # that specific phrasing wouldn't be grounded by anything else.
            for event in rec.output.events:
                vocab.add(event.text.lower())
        elif isinstance(rec.output, CompareSignoutToChartOutput):
            # UC2: grounds the fresh data this tool fetched internally,
            # which -- unlike every other tool -- never becomes its own
            # separate ToolCallRecord (get_patient_snapshot/get_recent_
            # encounters/get_recent_observations run *inside*
            # compare_signout_to_chart's own function body, invisible to
            # run_turn's dispatch loop). Without this branch, anything the
            # model restates from the fresh fetch beyond the diff itself
            # (e.g. "current potassium is 5.8") would be ungroundable.
            # Reuses the exact same per-type grounding logic as the real
            # top-level tools, applied to the nested current_* fields.
            cmp = rec.output
            for discrepancy in cmp.discrepancies:
                vocab.add(discrepancy.text.lower())
            if cmp.current_snapshot:
                _ground_snapshot(vocab, cmp.current_snapshot)
            if cmp.current_encounters:
                _ground_encounters(vocab, cmp.current_encounters)
            if cmp.current_observations:
                _ground_observations(vocab, cmp.current_observations)
    return vocab


def _find_candidate_terms(text: str) -> list[str]:
    lowered = text.lower()
    found = [term for term in _ALL_TERMS if term in lowered]
    found += [m.group(1) for m in _DOSE_RE.finditer(text)]
    found += [
        m.group(0)
        for m in _LAB_VALUE_RE.finditer(text)
        if not _LAB_VALUE_RANGE_PREFIX_RE.search(text[: m.start()])
    ]
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


# Fixed 2026-09-18, found live via audit, the most severe of the three:
# bare "conflict" collided with unrelated uses ("a scheduling conflict",
# "the two records conflict on her DOB") -- ARCHITECTURE.md 3.2 calls this
# mechanism "a wall, not a request", and a false match here silently skips
# appending the HARD STOP override, no signal to the resident at all.
# "conflict" alone is dropped; the remaining safe cues ("allerg",
# "contraindicat", "do not give", or the specific matched allergy text)
# are checked in a window around each mention of the medication itself,
# not the whole response -- so an unrelated "conflict" elsewhere (e.g.
# about duplicate-record data) can't satisfy this, and a multi-medication
# response can't have one drug's allergy mention satisfy a different
# drug's check.
_CONFLICT_SAFE_CUES = ["allerg", "contraindicat", "do not give"]


def _mentions_conflict_near_medication(response: str, medication: str, matched_allergy_text: str | None, window: int = 200) -> bool:
    lowered = response.lower()
    med_lower = medication.lower()
    safe_cues = _CONFLICT_SAFE_CUES + ([matched_allergy_text.lower()] if matched_allergy_text else [])
    idx = lowered.find(med_lower)
    while idx != -1:
        window_text = lowered[max(0, idx - window) : idx + len(med_lower) + window]
        if any(cue in window_text for cue in safe_cues):
            return True
        idx = lowered.find(med_lower, idx + 1)
    return False


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
        result = check_allergy_conflict(fhir, {"patient_id": patient_id, "medication_name": med}, [])
        if not isinstance(result, CheckAllergyConflictOutput):
            continue  # tool failure here is logged separately by the caller
        if result.conflict_found:
            mentions_conflict = _mentions_conflict_near_medication(response, med, result.matched_allergy_text)
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
    for rec in grounded_records:
        if not isinstance(rec.output, GetPatientSnapshotOutput):
            continue
        snap = rec.output
        if snap.duplicate_warnings and not _mentions_duplicate_warning(response, snap.duplicate_warnings):
            other_ids = ", ".join(w.other_patient_id for w in snap.duplicate_warnings)
            response += (
                f"\n\n[Verification note: there are {len(snap.duplicate_warnings)} other "
                f"patient record(s) matching this patient's name and birthdate "
                f"({other_ids}). This is a real ambiguity in the chart -- do not assume "
                f"they're the same visit or that data hasn't diverged between them.]"
            )
            enforced.append("duplicate_patient")
        if snap.chart_is_empty and not _mentions_empty_chart(response):
            response += (
                "\n\n[Verification note: this patient's chart has no recorded problems, "
                "medications, or allergies -- that's an empty/incomplete chart, not "
                "confirmation there's nothing to report.]"
            )
            enforced.append("empty_chart")
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
