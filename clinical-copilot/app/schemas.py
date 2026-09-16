"""Pydantic schemas for the two tools, per ARCHITECTURE.md 7.3: these are the
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
    checked_allergy_count: int
    low_confidence: bool = Field(
        ..., description="True if the match relied on uncoded/narrative-only allergy data."
    )
    source_resources: list[str]


# --- shared tool-failure envelope (ARCHITECTURE.md Section 2, 4) ------------


class ToolFailure(BaseModel):
    tool: str
    reason: str = Field(..., description="Safe to show a resident directly, no raw exception text.")
    detail_code: Literal["timeout", "http_error", "not_found", "malformed_response", "invalid_input"]
