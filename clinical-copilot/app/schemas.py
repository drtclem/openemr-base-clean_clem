"""Pydantic schemas for each tool, per ARCHITECTURE.md 7.3: these are the
source of truth for each tool's input/output shape. A tool implementation that
can't populate one of these validly must raise, not return malformed data --
see tools.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- get_patient_snapshot ----------------------------------------------------


class GetPatientSnapshotInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class ConditionFact(BaseModel):
    text: str
    category: Literal["problem-list-item", "health-concern", "unknown"]
    clinical_status: str | None
    source_resource: str  # e.g. "Condition/<fhir-id>", for source-attribution checks


class MedicationFact(BaseModel):
    text: str
    dosage_text: str | None
    status: str | None
    source_resource: str


class AllergyFact(BaseModel):
    text: str
    reaction: str | None
    is_coded: bool = Field(
        ..., description="False if this only survived in narrative text.div (Finding 11)."
    )
    source_resource: str


class DuplicatePatientWarning(BaseModel):
    other_patient_id: str
    matched_on: str  # e.g. "name+birthdate"


class GetPatientSnapshotOutput(BaseModel):
    patient_id: str
    name: str
    birth_date: str | None
    gender: str | None
    conditions: list[ConditionFact]
    medications: list[MedicationFact]
    allergies: list[AllergyFact]
    chart_is_empty: bool
    duplicate_warnings: list[DuplicatePatientWarning]
    partial_failures: list[str] = Field(
        default_factory=list,
        description="Which sub-resource fetches failed this call, e.g. 'AllergyIntolerance: timeout'.",
    )


# --- check_allergy_conflict --------------------------------------------------


class CheckAllergyConflictInput(BaseModel):
    patient_id: str
    medication_name: str = Field(..., description="Medication name to check, as free text.")


class CheckAllergyConflictOutput(BaseModel):
    patient_id: str
    medication_name: str
    conflict_found: bool
    matched_allergy_text: str | None
    cross_reactive_class: str | None = Field(
        default=None,
        description=(
            "Set to the shared drug-class name (e.g. 'penicillins') when the conflict was "
            "found via the curated cross-reactivity table (app/clinical_reference.py) rather "
            "than a direct/substring match against the allergy text itself -- e.g. amoxicillin "
            "flagged against a documented penicillin allergy. None for a direct name match, "
            "and always None when conflict_found is False. That table is a scoped stand-in for "
            "a production drug-interaction database, not general clinical decision support -- "
            "see its module docstring."
        ),
    )
    checked_allergy_count: int
    low_confidence: bool = Field(
        ..., description="True if the match relied on uncoded/narrative-only allergy data."
    )
    source_resources: list[str]


# --- get_recent_encounters (Phase 6, CLAUDE_CODE_BUILD_INSTRUCTIONS.md) -----


class GetRecentEncountersInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class EncounterFact(BaseModel):
    text: str  # encounter type/reason display
    status: str | None
    period_start: str | None
    source_resource: str  # e.g. "Encounter/<fhir-id>"


class GetRecentEncountersOutput(BaseModel):
    patient_id: str
    encounters: list[EncounterFact]
    sensitivity_filtered_count: int = Field(
        ...,
        description=(
            "How many encounters the compensating sensitivity filter (app/sensitivity.py, "
            "ARCHITECTURE.md 3.3) excluded before reaching `encounters` above -- never silently "
            "zero, so a resident/log reader can tell filtering is active even when nothing this "
            "call happened to exclude anything."
        ),
    )
    partial_failures: list[str] = Field(default_factory=list)


# --- get_recent_observations (UC2, USERS.md) --------------------------------
#
# Deliberately NOT run through app/sensitivity.py's compensating filter, per
# explicit build instruction: that filter is specifically for Encounter-type
# data (ARCHITECTURE.md 3.3's own scope is the FHIR Encounter resource, which
# has no sensitivity field at all). Observation carries a different risk
# profile -- it's not the resource type that compensating control exists for
# -- so this tool is not gated behind it. If a future finding shows
# Observation data needs its own compensating control, that's a new,
# separate gap to evaluate on its own terms, not an oversight here.


class GetRecentObservationsInput(BaseModel):
    patient_id: str = Field(..., description="OpenEMR FHIR Patient resource id (UUID).")


class ObservationFact(BaseModel):
    text: str  # what was measured, e.g. "Blood Pressure", "Potassium"
    value: str | None = Field(
        None, description="Formatted value + unit if present, e.g. '5.2 mEq/L' or '120/80 mmHg'."
    )
    status: str | None
    effective_datetime: str | None
    source_resource: str  # e.g. "Observation/<fhir-id>"


class GetRecentObservationsOutput(BaseModel):
    patient_id: str
    observations: list[ObservationFact]
    partial_failures: list[str] = Field(default_factory=list)


# --- shared tool-failure envelope (ARCHITECTURE.md Section 2, 4) ------------


class ToolFailure(BaseModel):
    tool: str
    reason: str = Field(..., description="Safe to show a resident directly, no raw exception text.")
    detail_code: Literal[
        "timeout", "http_error", "not_found", "malformed_response", "invalid_input", "patient_mismatch"
    ]
