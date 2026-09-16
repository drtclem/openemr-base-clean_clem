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

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agent import ClinicalCopilotAgent
from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.observability import TurnObserver
from app.verification import ToolCallRecord

_READINESS_TIMEOUT_S = 2.5
# The FHIR capability statement (/metadata) is real and unauthenticated, but
# not as cheap to compute as a plain resource fetch -- measured latency on
# the droplet is ~2.3-3.5s (vs ~0.6-0.9s locally, consistent with its more
# modest resources, a pattern seen throughout this project's droplet work),
# right at or over the general 2.5s budget. A dedicated, more generous
# timeout for this one check, not a blanket increase for the others.
_OPENEMR_READINESS_TIMEOUT_S = 5.0

app = FastAPI(title="Clinical Co-Pilot (Early Submission)")

_REQUEST_TIMING_LOG_PATH = Path(__file__).parent.parent / "request_timing.log"


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    """Independent request-timing log -- plain file I/O, zero dependency on
    Langfuse being configured or reachable (ERROR_ANALYSIS.md Entry 6).

    PERFORMANCE_BASELINE.md's droplet load test found Langfuse's own
    telemetry can itself go quiet under real contention: only 5 of 20 real
    requests produced an AGENT span there, with the span duration it did
    record far below what the client actually experienced. This starts
    timing at the ASGI/middleware layer -- before routing reaches the
    /chat handler, and therefore before any Langfuse span is opened --
    so real request latency is captured even when Langfuse's own
    instrumentation degrades under load.
    """
    start = time.monotonic()
    response = await call_next(request)
    duration_s = time.monotonic() - start
    line = (
        f"{datetime.now(timezone.utc).isoformat()},{request.method},"
        f"{request.url.path},{duration_s:.3f},{response.status_code}\n"
    )
    with _REQUEST_TIMING_LOG_PATH.open("a") as f:
        f.write(line)
    return response


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


@app.get("/health")
def health() -> dict:
    """Returns 200 if the process itself is running -- no dependency
    checks. Should never fail unless the process is genuinely down."""
    return {"status": "ok"}


def _check_openemr() -> bool:
    """Reuses the FhirClient's own configured base URL/TLS setting; hits
    the FHIR capability statement, which per FHIR_README.md needs no auth
    -- a real, lightweight, unauthenticated connectivity check, not a new
    heavy call invented for this."""
    try:
        resp = httpx.get(
            f"{_settings.openemr_fhir_base_url}/metadata",
            timeout=_OPENEMR_READINESS_TIMEOUT_S,
            verify=_settings.verify_tls,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _check_anthropic() -> bool:
    """A real API call that validates the key and reachability without
    burning a completion -- models.list() is a metadata endpoint, not a
    message generation."""
    try:
        client = anthropic.Anthropic(api_key=_settings.anthropic_api_key, timeout=_READINESS_TIMEOUT_S)
        client.models.list(limit=1)
        return True
    except Exception:  # noqa: BLE001 -- any failure here means "not ready", full stop
        return False


def _check_langfuse() -> bool:
    """If Langfuse isn't configured at all, that's a deliberate,
    supported configuration (app/observability.py degrades gracefully to
    local-only logging) -- not a failed dependency, so it doesn't count
    against readiness. If it IS configured, actually check its real
    public health endpoint."""
    if not _settings.observability_enabled:
        return True
    try:
        resp = httpx.get(f"{_settings.langfuse_host}/api/public/health", timeout=_READINESS_TIMEOUT_S)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


@app.get("/ready")
def ready() -> JSONResponse:
    """Actively checks each real dependency, per-dependency breakdown so a
    failure is diagnosable from the response itself, not just a single
    true/false (ARCHITECTURE.md 7.6)."""
    checks = {
        "openemr": _check_openemr(),
        "anthropic": _check_anthropic(),
        "langfuse": _check_langfuse(),
    }
    all_ready = all(checks.values())
    return JSONResponse(status_code=200 if all_ready else 503, content={"ready": all_ready, "checks": checks})


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
