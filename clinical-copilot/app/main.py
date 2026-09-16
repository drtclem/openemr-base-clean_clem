"""HTTP interface: POST /chat.

Deliberately minimal per early-submission-build-prompt.md's scope note: "a
basic HTTP endpoint that takes a message and patient context, returns a
response." A real UI/OpenEMR module (ARCHITECTURE.md 1.1) is future work,
noted in README.md, not built here.

Conversation state is an in-memory dict keyed by conversation_id -- fine for
a single-process demo tonight; a real deployment needs a shared store
(ARCHITECTURE.md doesn't specify one yet, this is a new gap this build
surfaced, see README).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI
from pydantic import BaseModel

from app.agent import ClinicalCopilotAgent
from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.observability import TurnObserver
from app.verification import ToolCallRecord

app = FastAPI(title="Clinical Co-Pilot (Early Submission)")

_settings = get_settings()
_tokens = OAuthTokenProvider(_settings)
_fhir = FhirClient(_settings, _tokens)
_observer = TurnObserver(_settings)
_agent = ClinicalCopilotAgent(_settings, _fhir, _observer)


@dataclass
class _ConversationState:
    history: list[dict] = field(default_factory=list)
    # Every real tool result fetched anywhere in this conversation so far --
    # must travel with the conversation the same way history does, or a
    # follow-up turn loses grounding for facts fetched in an earlier turn
    # (ERROR_ANALYSIS.md Entry 5).
    tool_records: list[ToolCallRecord] = field(default_factory=list)


_conversations: dict[str, _ConversationState] = {}


class ChatRequest(BaseModel):
    message: str
    patient_id: str | None = None
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    correlation_id: str
    response: str
    verification_passed: bool
    flagged_claims: list[str]
    enforced_warnings: list[str]
    tool_calls: list[dict]


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    conversation_id = request.conversation_id or str(uuid.uuid4())
    state = _conversations.get(conversation_id, _ConversationState())

    result = _agent.run_turn(
        state.history, request.message, request.patient_id, state.tool_records
    )
    _conversations[conversation_id] = _ConversationState(
        history=result.updated_history, tool_records=result.accumulated_tool_records
    )

    return ChatResponse(
        conversation_id=conversation_id,
        correlation_id=result.correlation_id,
        response=result.response_text,
        verification_passed=result.verification_passed,
        flagged_claims=result.flagged_claims,
        enforced_warnings=result.enforced_warnings,
        tool_calls=result.tool_calls,
    )
