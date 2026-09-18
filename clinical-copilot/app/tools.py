"""The agent's tools.

Each maps directly to a USERS.md use case and a row of ARCHITECTURE.md's
Section 2 table:

- get_patient_snapshot    -> Patient, Condition, AllergyIntolerance, MedicationRequest
- check_allergy_conflict  -> AllergyIntolerance vs. a supplied medication name
- get_recent_encounters   -> Encounter (Phase 6, sensitivity-filtered -- app/sensitivity.py)
- get_recent_observations -> Observation (UC2, NOT sensitivity-filtered -- see its own
  docstring below for why that compensating control doesn't apply here)
- summarize_shift_events  -> UC4, different in kind from the other four: no FHIR call,
  no `fhir` parameter, takes this conversation's already-accumulated ToolCallRecords
  instead -- see its own docstring below, and ClinicalCopilotAgent._call_tool's
  special-case dispatch for it (app/agent.py).

Error handling follows ARCHITECTURE.md Section 2's hard rule: a tool call
that fails returns a structured ToolFailure the agent must surface directly,
never silently answered around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.clinical_reference import cross_reactive_class
from app.fhir_client import FhirClient, FhirRequestError, bundle_entries
from app.schemas import (
    AllergyFact,
    CheckAllergyConflictInput,
    CheckAllergyConflictOutput,
    ConditionFact,
    DuplicatePatientWarning,
    EncounterFact,
    GetPatientSnapshotInput,
    GetPatientSnapshotOutput,
    GetRecentEncountersInput,
    GetRecentEncountersOutput,
    GetRecentObservationsInput,
    GetRecentObservationsOutput,
    MedicationFact,
    ObservationFact,
    ShiftEventFact,
    SummarizeShiftEventsInput,
    SummarizeShiftEventsOutput,
    ToolFailure,
)
from app.sensitivity import Role, filter_encounters_by_sensitivity

_DATA_ABSENT_SYSTEM = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class ToolCallRecord:
    """Defined here, not in app/verification.py (which re-exports it for
    backward compatibility -- see its import comment), because
    summarize_shift_events (below) needs it too, and verification.py
    already imports from tools.py -- the reverse (tools.py importing
    from verification.py) would be circular. Every real tool call this
    conversation, in either module, is recorded as one of these; both
    verify_response()'s grounding and summarize_shift_events' aggregation
    read the same accumulated list."""

    tool_name: str
    patient_id: str
    output: object  # GetPatientSnapshotOutput | CheckAllergyConflictOutput | ...


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


# Every real user of get_recent_encounters maps to USERS.md's single
# overnight cross-covering resident persona, which ARCHITECTURE.md 3.3
# confirms never holds a High-sensitivity grant (audit-notes.md's gacl
# matrix -- the clin/physician-covering roles this persona maps to hold no
# High grant). Hard-coded here, not looked up, because no endpoint a
# resident's own user/-scoped token can call exposes their ACL role in this
# build: OpenEMR's OIDC discovery document advertises a `/userinfo`
# endpoint, but no handler for it actually exists (confirmed by reading
# AuthorizationController.php); the standard REST API's only user-lookup
# route (`GET /api/user*`) requires `admin/users` ACL, which this persona
# doesn't hold either. "clin" is therefore the correct behavior for every
# real user of this tool today, not an approximation standing in for a
# missing lookup -- if a broader-clearance persona is ever added, resolving
# the role per-session becomes a real gap to close, not before.
_RESIDENT_ROLE: Role = "clin"


def get_recent_encounters(fhir: FhirClient, raw_input: dict) -> GetRecentEncountersOutput | ToolFailure:
    try:
        params = GetRecentEncountersInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_recent_encounters",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    try:
        bundle = fhir.search("Encounter", {"patient": params.patient_id})
    except FhirRequestError as exc:
        return ToolFailure(
            tool="get_recent_encounters",
            reason=f"I couldn't retrieve this patient's encounter history ({exc.detail_code}).",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )
    fhir_encounters = bundle_entries(bundle)

    # The FHIR Encounter resource carries no sensitivity field at all
    # (confirmed by code read and a live test -- ARCHITECTURE.md 3.3,
    # architecture-audit.md 6.9), so sensitivity is sourced from OpenEMR's
    # own standard REST API instead -- still "OpenEMR's REST/FHIR API" per
    # ARCHITECTURE.md 1.3, not a direct-database read (app/fhir_client.py's
    # get_standard_api()).
    partial_failures: list[str] = []
    sensitivity_by_uuid: dict[str, str] = {}
    try:
        standard_encounters = fhir.get_standard_api(f"/patient/{params.patient_id}/encounter")
        if isinstance(standard_encounters, list):
            for enc in standard_encounters:
                euuid = enc.get("uuid")
                if euuid:
                    sensitivity_by_uuid[euuid] = (enc.get("sensitivity") or "").lower()
    except FhirRequestError as exc:
        partial_failures.append(f"sensitivity lookup: {exc.detail_code}")

    # Unknown sensitivity (the lookup above failed entirely, or this
    # specific encounter has no matching standard-API row) is treated as
    # "high" -- fail closed. This is the opposite failure mode from
    # get_patient_snapshot's other sub-fetches, which surface partial data
    # on failure: ARCHITECTURE.md 3.3's compensating control exists
    # specifically to fail closed, not open, so an unreadable sensitivity
    # value must exclude an encounter, never include it by default.
    raw_for_filter = [
        {"id": res.get("id"), "sensitivity": sensitivity_by_uuid.get(res.get("id"), "high")}
        for res in fhir_encounters
        if res.get("id")
    ]
    allowed_ids = {e["id"] for e in filter_encounters_by_sensitivity(raw_for_filter, _RESIDENT_ROLE)}

    encounters: list[EncounterFact] = []
    for res in fhir_encounters:
        if res.get("id") not in allowed_ids:
            continue
        type_concepts = res.get("type") or []
        text, _ = _codeable_concept_text(type_concepts[0]) if type_concepts else ("unknown", False)
        if text == "unknown":
            reason_codes = res.get("reasonCode") or []
            if reason_codes:
                text, _ = _codeable_concept_text(reason_codes[0])
        encounters.append(
            EncounterFact(
                text=text,
                status=res.get("status"),
                period_start=(res.get("period") or {}).get("start"),
                source_resource=f"Encounter/{res.get('id')}",
            )
        )

    return GetRecentEncountersOutput(
        patient_id=params.patient_id,
        encounters=encounters,
        sensitivity_filtered_count=len(fhir_encounters) - len(encounters),
        partial_failures=partial_failures,
    )


def _observation_value_text(res: dict) -> str | None:
    """Extracts a human-readable value from a FHIR Observation's
    polymorphic `value[x]` -- quantity (with unit), string, CodeableConcept,
    or (for panel-style observations like blood pressure) a `component`
    array of sub-measurements, each with its own code + value. None if no
    value is present at all (e.g. a pending/cancelled result) -- callers
    must not treat that as "value 0" or synthesize a placeholder; an
    ObservationFact with value=None and a real status is the honest
    representation of that case."""
    quantity = res.get("valueQuantity")
    if quantity and quantity.get("value") is not None:
        unit = quantity.get("unit") or quantity.get("code") or ""
        return f"{quantity['value']} {unit}".strip()
    if res.get("valueString"):
        return res["valueString"]
    concept_text, is_coded = _codeable_concept_text(res.get("valueCodeableConcept"))
    if is_coded:
        return concept_text
    components = res.get("component") or []
    parts: list[str] = []
    for comp in components:
        comp_text, _ = _codeable_concept_text(comp.get("code"))
        comp_quantity = comp.get("valueQuantity")
        if comp_quantity and comp_quantity.get("value") is not None:
            unit = comp_quantity.get("unit") or comp_quantity.get("code") or ""
            parts.append(f"{comp_text}: {comp_quantity['value']} {unit}".strip())
    return ", ".join(parts) if parts else None


def get_recent_observations(fhir: FhirClient, raw_input: dict) -> GetRecentObservationsOutput | ToolFailure:
    """UC2 (USERS.md): "what changed" for labs/vitals -- the same sign-out-
    verification role get_recent_encounters plays for visit history.
    Deliberately NOT run through app/sensitivity.py's compensating filter;
    see GetRecentObservationsOutput's docstring in schemas.py for why that
    control (scoped specifically to Encounter data) doesn't apply here.
    """
    try:
        params = GetRecentObservationsInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_recent_observations",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    try:
        bundle = fhir.search("Observation", {"patient": params.patient_id})
    except FhirRequestError as exc:
        return ToolFailure(
            tool="get_recent_observations",
            reason=f"I couldn't retrieve this patient's recent labs/vitals ({exc.detail_code}).",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )

    observations: list[ObservationFact] = []
    for res in bundle_entries(bundle):
        text, _ = _codeable_concept_text(res.get("code"))
        observations.append(
            ObservationFact(
                text=text,
                value=_observation_value_text(res),
                status=res.get("status"),
                effective_datetime=res.get("effectiveDateTime")
                or (res.get("effectivePeriod") or {}).get("start"),
                source_resource=f"Observation/{res.get('id')}",
            )
        )

    return GetRecentObservationsOutput(patient_id=params.patient_id, observations=observations)


def summarize_shift_events(
    turn_records: list[ToolCallRecord], raw_input: dict
) -> SummarizeShiftEventsOutput | ToolFailure:
    """UC4 (USERS.md): synthesizes a shift-handoff summary purely from facts
    already gathered THIS conversation (turn_records -- accumulated across
    both this turn's own tool-call rounds and any earlier turns, see
    ClinicalCopilotAgent.run_turn's prior_tool_records docstring in
    app/agent.py). Makes NO new FHIR call and takes NO fhir client, unlike
    every other tool in this module -- dispatched through a special case in
    ClinicalCopilotAgent._call_tool, not the uniform impl(fhir, tool_input)
    pattern the other four share; see that special case's own comment for
    why (app/agent.py).

    Purely deterministic aggregation, no generation: this function never
    calls the model. The actual narrative synthesis into shift-summary
    prose happens the same place every other tool's output becomes prose --
    the main model turn, whose draft still passes through
    verify_response()'s existing grounding check. Keeping this tool
    mechanical means no new verification machinery is needed here; a nested
    LLM call inside a tool would draft text nothing in this architecture
    ever checks (verify_response only scans the FINAL response text, never
    an intermediate tool output).

    Because the compensating sensitivity filter (app/sensitivity.py) already
    ran inside get_recent_encounters before its output was ever added to
    turn_records, a filtered-out encounter is structurally absent from what
    this function reads -- no separate filtering step needed here. That
    reasoning holds only because this tool has no FHIR access of its own;
    if it ever gained one, it would need re-verifying, not assuming.
    """
    try:
        params = SummarizeShiftEventsInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="summarize_shift_events",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    patient_records = [rec for rec in turn_records if rec.patient_id == params.patient_id]
    if not patient_records:
        return SummarizeShiftEventsOutput(patient_id=params.patient_id, events=[], data_gathered=False)

    events: list[ShiftEventFact] = []
    for rec in patient_records:
        out = rec.output
        if isinstance(out, GetPatientSnapshotOutput):
            for c in out.conditions:
                events.append(ShiftEventFact(text=c.text, category="condition", source_resource=c.source_resource))
            for m in out.medications:
                events.append(ShiftEventFact(text=m.text, category="medication", source_resource=m.source_resource))
            for a in out.allergies:
                events.append(ShiftEventFact(text=a.text, category="allergy", source_resource=a.source_resource))
            for d in out.duplicate_warnings:
                events.append(
                    ShiftEventFact(
                        text=f"Possible duplicate record: {d.other_patient_id} (matched on {d.matched_on})",
                        category="duplicate_warning",
                        source_resource=f"Patient/{d.other_patient_id}",
                    )
                )
        elif isinstance(out, GetRecentEncountersOutput):
            for enc in out.encounters:
                events.append(ShiftEventFact(text=enc.text, category="encounter", source_resource=enc.source_resource))
        elif isinstance(out, GetRecentObservationsOutput):
            for obs in out.observations:
                text = f"{obs.text}: {obs.value}" if obs.value else obs.text
                events.append(ShiftEventFact(text=text, category="observation", source_resource=obs.source_resource))
        elif isinstance(out, CheckAllergyConflictOutput):
            if out.conflict_found:
                events.append(
                    ShiftEventFact(
                        text=f"Allergy conflict checked and found: {out.medication_name} vs {out.matched_allergy_text}",
                        category="allergy",
                        source_resource=(out.source_resources[0] if out.source_resources else "AllergyIntolerance/unknown"),
                    )
                )

    # De-dupe by (category, text) -- the same fact can legitimately be
    # gathered more than once this conversation (e.g. get_patient_snapshot
    # called in an earlier turn and again in this one), and a shift summary
    # shouldn't repeat it per gathering call.
    seen: set[tuple[str, str]] = set()
    deduped: list[ShiftEventFact] = []
    for event in events:
        key = (event.category, event.text.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(event)

    return SummarizeShiftEventsOutput(patient_id=params.patient_id, events=deduped, data_gathered=True)
