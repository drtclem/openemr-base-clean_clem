"""The tool-calling loop: a single Python service calling the Anthropic API
directly (ARCHITECTURE.md 1.2) -- no agent framework. Verification (Section 3)
is inserted as an explicit step between "model drafts a response" and
"response reaches the resident", which is exactly the property 1.2 says a
heavier framework would make harder to guarantee.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import anthropic

from app.config import Settings
from app.fhir_client import FhirClient
from app.observability import TurnObserver
from app.schemas import CheckAllergyConflictOutput, GetPatientSnapshotOutput, ToolFailure
from app.tools import check_allergy_conflict, get_patient_snapshot
from app.verification import ToolCallRecord, verify_response

SYSTEM_PROMPT = """\
You are the Clinical Co-Pilot, helping an overnight cross-covering resident \
get oriented to a patient they did not admit. You are a decision-SUPPORT tool, \
not a decision-maker: the resident remains responsible for clinical judgment \
and escalation.

Hard rules:
- Every clinical fact you state (a medication, an allergy, a condition, a \
  dose) must come from a tool call you made this turn. Never state a fact \
  you were not given by a tool.
- If a tool call fails or returns no data, say so plainly ("I couldn't \
  retrieve X" / "this chart has no recorded Y"). Never answer as if the \
  data were there when it wasn't, and never present an empty/missing \
  result as reassuring.
- If get_patient_snapshot reports more than one matching patient record \
  (a possible duplicate), say so explicitly rather than picking one.
- Before you mention giving, starting, or continuing any medication for \
  this patient, call check_allergy_conflict for that medication first.
- If you don't know something, say you don't know. Do not guess.
"""

TOOLS = [
    {
        "name": "get_patient_snapshot",
        "description": (
            "Get a synthesized snapshot of a patient's chart: demographics, "
            "active problems, current medications, and allergies, pulled live "
            "from OpenEMR via FHIR. Use this first for any unfamiliar patient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "OpenEMR FHIR Patient resource id (UUID).",
                }
            },
            "required": ["patient_id"],
        },
    },
    {
        "name": "check_allergy_conflict",
        "description": (
            "Hard, code-level check for whether a named medication conflicts "
            "with this patient's recorded allergies. Call this before "
            "mentioning any medication for the patient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "medication_name": {"type": "string"},
            },
            "required": ["patient_id", "medication_name"],
        },
    },
]

_TOOL_IMPLS = {
    "get_patient_snapshot": get_patient_snapshot,
    "check_allergy_conflict": check_allergy_conflict,
}

MAX_TOOL_ROUNDS = 6


@dataclass
class ChatTurnResult:
    correlation_id: str
    response_text: str
    verification_passed: bool
    flagged_claims: list[str]
    enforced_warnings: list[str]
    tool_calls: list[dict] = field(default_factory=list)
    updated_history: list[dict] = field(default_factory=list)


class ClinicalCopilotAgent:
    def __init__(self, settings: Settings, fhir: FhirClient, observer: TurnObserver | None = None):
        self._settings = settings
        self._fhir = fhir
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._observer = observer or TurnObserver(settings)

    def run_turn(
        self,
        history: list[dict],
        user_message: str,
        patient_id: str | None,
    ) -> ChatTurnResult:
        correlation_id = str(uuid.uuid4())
        trace = self._observer.start_turn(correlation_id, user_message, patient_id)

        # The API layer receives patient_id as separate structured "patient
        # context" (early-submission-build-prompt.md), but the Anthropic
        # Messages API has no side-channel for that -- it has to be part of
        # the turn's content, or the model has no way to know which patient
        # to call tools for. Prepended every turn (not just the first) so it
        # holds even if a caller changes patient_id mid-conversation.
        content_for_model = user_message
        if patient_id:
            content_for_model = f"[Active patient context: patient_id={patient_id}]\n\n{user_message}"

        messages = list(history) + [{"role": "user", "content": content_for_model}]
        turn_records: list[ToolCallRecord] = []
        tool_call_log: list[dict] = []

        for round_index in range(MAX_TOOL_ROUNDS):
            llm_handle = trace.start_llm_call(
                round_index=round_index,
                model=self._settings.anthropic_model,
                input_payload={"system": SYSTEM_PROMPT, "messages": _messages_to_jsonable(messages)},
            )
            response = self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            trace.finish_llm_call(
                llm_handle,
                output_payload=_blocks_to_jsonable(response.content),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                draft_text = "".join(b.text for b in response.content if b.type == "text")
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results_content = []
            for block in tool_use_blocks:
                tool_handle = trace.start_tool_call(tool_name=block.name, input_payload=block.input)
                output = self._call_tool(block.name, block.input)
                is_failure = isinstance(output, ToolFailure)
                trace.finish_tool_call(tool_handle, output_payload=_to_jsonable(output), failed=is_failure)
                latency = time.monotonic() - tool_handle.wall_t0

                tool_call_log.append(
                    {
                        "tool": block.name,
                        "input": block.input,
                        "failed": is_failure,
                        "latency_s": round(latency, 3),
                    }
                )
                if not is_failure:
                    turn_records.append(
                        ToolCallRecord(
                            tool_name=block.name,
                            patient_id=block.input.get("patient_id", patient_id or ""),
                            output=output,
                        )
                    )

                tool_results_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(_to_jsonable(output)),
                    }
                )
            messages.append({"role": "user", "content": tool_results_content})
        else:
            draft_text = (
                "I wasn't able to finish gathering this patient's information within "
                "the allowed number of tool calls. Please try a narrower question."
            )

        outcome = verify_response(draft_text, turn_records, patient_id, self._fhir)
        trace.log_verification(
            passed_source_attribution=outcome.passed_source_attribution,
            passed_domain_constraint=outcome.passed_domain_constraint,
            flagged_claims=outcome.flagged_claims,
            enforced_warnings=outcome.enforced_warnings,
        )
        trace.finish_turn(final_response=outcome.final_response)

        messages.append({"role": "assistant", "content": outcome.final_response})

        return ChatTurnResult(
            correlation_id=correlation_id,
            response_text=outcome.final_response,
            verification_passed=outcome.passed_source_attribution and outcome.passed_domain_constraint,
            flagged_claims=outcome.flagged_claims,
            enforced_warnings=outcome.enforced_warnings,
            tool_calls=tool_call_log,
            updated_history=messages,
        )

    def _call_tool(self, name: str, tool_input: dict):
        impl = _TOOL_IMPLS.get(name)
        if impl is None:
            return ToolFailure(tool=name, reason=f"Unknown tool '{name}'.", detail_code="invalid_input")
        return impl(self._fhir, tool_input)


def _to_jsonable(output) -> dict:
    if isinstance(output, (GetPatientSnapshotOutput, CheckAllergyConflictOutput, ToolFailure)):
        return output.model_dump()
    return {"error": "unexpected tool output type"}


def _blocks_to_jsonable(blocks) -> list[dict]:
    """Anthropic content blocks (TextBlock/ToolUseBlock, ...) -> plain dicts,
    for Langfuse trace input/output -- the SDK objects themselves aren't
    reliably JSON-serializable over the OTEL exporter."""
    out = []
    for b in blocks:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif b.type == "thinking":
            out.append({"type": "thinking", "thinking": b.thinking})
        elif b.type == "redacted_thinking":
            out.append({"type": "redacted_thinking"})
        else:
            out.append({"type": b.type})
    return out


def _messages_to_jsonable(messages: list[dict]) -> list[dict]:
    """Same as _blocks_to_jsonable, but for a full messages list where an
    assistant turn's content may be raw SDK content blocks (appended
    directly from response.content) rather than plain dicts."""
    out = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list) and content and hasattr(content[0], "type") and not isinstance(content[0], dict):
            content = _blocks_to_jsonable(content)
        out.append({"role": msg["role"], "content": content})
    return out
