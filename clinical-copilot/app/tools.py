"""The two Early Submission tools.

Both map directly to a USERS.md use case and a row of ARCHITECTURE.md's
Section 2 table:

- get_patient_snapshot   -> Patient, Condition, AllergyIntolerance, MedicationRequest
- check_allergy_conflict -> AllergyIntolerance vs. a supplied medication name

Error handling follows ARCHITECTURE.md Section 2's hard rule: a tool call
that fails returns a structured ToolFailure the agent must surface directly,
never silently answered around.
"""

from __future__ import annotations

import re

from app.clinical_reference import cross_reactive_class
from app.fhir_client import FhirClient, FhirRequestError, bundle_entries
from app.schemas import (
    AllergyFact,
    CheckAllergyConflictInput,
    CheckAllergyConflictOutput,
    ConditionFact,
    DuplicatePatientWarning,
    GetPatientSnapshotInput,
    GetPatientSnapshotOutput,
    MedicationFact,
    ToolFailure,
)

_DATA_ABSENT_SYSTEM = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(narrative_div: str) -> str:
    return _HTML_TAG_RE.sub("", narrative_div).strip()


def _codeable_concept_text(concept: dict | None) -> tuple[str, bool]:
    """Returns (text, is_coded). Falls back to narrative when the structured
    `code` is a data-absent-reason -- ARCHITECTURE.md 3.1 / Finding 11."""
    if not concept:
        return ("unknown", False)
    coding = concept.get("coding") or []
    is_data_absent = any(c.get("system") == _DATA_ABSENT_SYSTEM for c in coding)
    if not is_data_absent:
        if concept.get("text"):
            return (concept["text"], True)
        if coding and coding[0].get("display"):
            return (coding[0]["display"], True)
    return ("unknown", False)


def _patient_display(patient: dict) -> tuple[str, str | None, str | None]:
    names = patient.get("name") or []
    if names:
        given = " ".join(names[0].get("given", []))
        family = names[0].get("family", "")
        name = f"{given} {family}".strip()
    else:
        name = "Unknown"
    return name, patient.get("birthDate"), patient.get("gender")


def _find_duplicates(fhir: FhirClient, patient: dict) -> list[DuplicatePatientWarning]:
    names = patient.get("name") or []
    birth_date = patient.get("birthDate")
    if not names or not birth_date:
        return []
    family = names[0].get("family")
    if not family:
        return []
    try:
        bundle = fhir.search(
            "Patient", {"family": family, "birthdate": birth_date}
        )
    except FhirRequestError:
        # Duplicate detection is a compensating safety check, not the primary
        # request; if it fails we say nothing rather than block the snapshot,
        # but we do NOT claim "no duplicates" either -- see partial_failures.
        return []
    matches = []
    for entry in bundle_entries(bundle):
        if entry.get("id") == patient.get("id"):
            continue
        matches.append(
            DuplicatePatientWarning(other_patient_id=entry["id"], matched_on="name+birthdate")
        )
    return matches


def get_patient_snapshot(fhir: FhirClient, raw_input: dict) -> GetPatientSnapshotOutput | ToolFailure:
    try:
        params = GetPatientSnapshotInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_patient_snapshot",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    try:
        patient = fhir.read("Patient", params.patient_id)
    except FhirRequestError as exc:
        return ToolFailure(
            tool="get_patient_snapshot",
            reason=f"I couldn't retrieve this patient's chart ({exc.detail_code}).",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )

    name, birth_date, gender = _patient_display(patient)
    partial_failures: list[str] = []

    conditions: list[ConditionFact] = []
    try:
        bundle = fhir.search("Condition", {"patient": params.patient_id})
        for res in bundle_entries(bundle):
            text, _ = _codeable_concept_text(res.get("code"))
            category_codes = [
                c.get("code")
                for cat in (res.get("category") or [])
                for c in (cat.get("coding") or [])
            ]
            category = "unknown"
            if "problem-list-item" in category_codes:
                category = "problem-list-item"
            elif "health-concern" in category_codes:
                category = "health-concern"
            conditions.append(
                ConditionFact(
                    text=text,
                    category=category,  # type: ignore[arg-type]
                    clinical_status=(res.get("clinicalStatus", {}).get("coding", [{}])[0].get("code")),
                    source_resource=f"Condition/{res.get('id')}",
                )
            )
    except FhirRequestError as exc:
        partial_failures.append(f"Condition: {exc.detail_code}")

    medications: list[MedicationFact] = []
    try:
        bundle = fhir.search("MedicationRequest", {"patient": params.patient_id})
        for res in bundle_entries(bundle):
            text, _ = _codeable_concept_text(res.get("medicationCodeableConcept"))
            dosage = res.get("dosageInstruction") or [{}]
            medications.append(
                MedicationFact(
                    text=text,
                    dosage_text=dosage[0].get("text"),
                    status=res.get("status"),
                    source_resource=f"MedicationRequest/{res.get('id')}",
                )
            )
    except FhirRequestError as exc:
        partial_failures.append(f"MedicationRequest: {exc.detail_code}")

    allergies: list[AllergyFact] = []
    try:
        bundle = fhir.search("AllergyIntolerance", {"patient": params.patient_id})
        for res in bundle_entries(bundle):
            text, is_coded = _codeable_concept_text(res.get("code"))
            reaction = None
            if not is_coded:
                narrative = (res.get("text") or {}).get("div")
                if narrative:
                    text = _strip_html(narrative)
            reactions = res.get("reaction") or []
            if reactions:
                manifestations = reactions[0].get("manifestation") or []
                if manifestations:
                    reaction, _ = _codeable_concept_text(manifestations[0])
            allergies.append(
                AllergyFact(
                    text=text,
                    reaction=reaction,
                    is_coded=is_coded,
                    source_resource=f"AllergyIntolerance/{res.get('id')}",
                )
            )
    except FhirRequestError as exc:
        partial_failures.append(f"AllergyIntolerance: {exc.detail_code}")

    duplicate_warnings = _find_duplicates(fhir, patient)

    return GetPatientSnapshotOutput(
        patient_id=params.patient_id,
        name=name,
        birth_date=birth_date,
        gender=gender,
        conditions=conditions,
        medications=medications,
        allergies=allergies,
        chart_is_empty=not (conditions or medications or allergies),
        duplicate_warnings=duplicate_warnings,
        partial_failures=partial_failures,
    )


def check_allergy_conflict(fhir: FhirClient, raw_input: dict) -> CheckAllergyConflictOutput | ToolFailure:
    try:
        params = CheckAllergyConflictInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="check_allergy_conflict",
            reason="I couldn't process that request -- missing a patient ID or medication name.",
            detail_code="invalid_input",
        )

    try:
        bundle = fhir.search("AllergyIntolerance", {"patient": params.patient_id})
    except FhirRequestError as exc:
        return ToolFailure(
            tool="check_allergy_conflict",
            reason=f"I couldn't retrieve this patient's allergy list ({exc.detail_code}), "
            "so I can't confirm whether this medication is safe to give.",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )

    entries = bundle_entries(bundle)
    med_norm = params.medication_name.strip().lower()
    matched_text: str | None = None
    matched_cross_reactive_class: str | None = None
    low_confidence = False
    source_resources: list[str] = []

    for res in entries:
        text, is_coded = _codeable_concept_text(res.get("code"))
        if not is_coded:
            narrative = (res.get("text") or {}).get("div")
            if narrative:
                text = _strip_html(narrative)
        allergy_norm = text.strip().lower()
        if not allergy_norm or allergy_norm == "unknown":
            continue
        # (a) Simple substring match against the allergy text itself.
        # (b) A small, curated drug-class cross-reactivity table
        # (app/clinical_reference.py) -- e.g. "amoxicillin" against a
        # documented "penicillin" allergy. That table is a scoped stand-in
        # for a production drug-interaction database (First Databank/
        # Medi-Span/Multum-style), not general clinical decision support;
        # see its module docstring. Neither (a) nor (b) is a drug-class
        # knowledge base beyond what's curated there -- a class this table
        # doesn't cover is still a known limitation, not assumed away.
        direct_match = med_norm in allergy_norm or allergy_norm in med_norm
        matched_class = None if direct_match else cross_reactive_class(allergy_norm, med_norm)
        if direct_match or matched_class:
            matched_text = text
            matched_cross_reactive_class = matched_class
            if not is_coded:
                low_confidence = True
            source_resources.append(f"AllergyIntolerance/{res.get('id')}")

    return CheckAllergyConflictOutput(
        patient_id=params.patient_id,
        medication_name=params.medication_name,
        conflict_found=matched_text is not None,
        matched_allergy_text=matched_text,
        cross_reactive_class=matched_cross_reactive_class,
        checked_allergy_count=len(entries),
        low_confidence=low_confidence,
        source_resources=source_resources,
    )
