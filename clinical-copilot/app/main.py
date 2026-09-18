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
from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.agent import ClinicalCopilotAgent
from app.config import get_settings
from app.observability import TurnObserver
from app.oauth_session import SESSION_COOKIE_NAME, LoginError, SessionStore
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
_observer = TurnObserver(_settings)
_agent = ClinicalCopilotAgent(_settings, observer=_observer)
# No default_fhir: Phase 1 (CLAUDE_CODE_BUILD_INSTRUCTIONS.md) removed the
# single shared password-grant credential from the live path -- every
# /chat call below resolves and passes its own resident-scoped FhirClient
# explicitly. An accidental omission raises in run_turn() rather than
# silently falling back to a shared identity.
_sessions = SessionStore(_settings)


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


# pid1 (Alice Testpatient) -- a normal chart with a known duplicate record,
# see evals/fixtures.py / bruno/chat/01. Same UUID on local and droplet
# (both seeded from the same fixture data), so this default works
# out of the box against either instance.
_DEFAULT_PATIENT_ID = "98c4b82b-b07e-11f1-8334-022958ad0af8"

_UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clinical Co-Pilot</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 700px;
    margin: 2rem auto;
    padding: 0 1rem;
  }
  h1 { font-size: 1.25rem; }
  .patient-row { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 1rem; }
  .patient-row label { font-size: 0.85rem; opacity: 0.75; white-space: nowrap; }
  .patient-row input { flex: 1; }
  input {
    font: inherit;
    padding: 0.5rem;
    border: 1px solid #8888;
    border-radius: 6px;
  }
  #thread {
    border: 1px solid #8888;
    border-radius: 8px;
    height: 420px;
    overflow-y: auto;
    padding: 0.75rem;
    margin-bottom: 0.75rem;
  }
  .msg { margin-bottom: 1rem; }
  .msg .who { font-weight: 600; font-size: 0.85rem; margin-bottom: 0.15rem; }
  .msg .text { white-space: pre-wrap; }
  .msg .meta { font-size: 0.75rem; opacity: 0.6; margin-top: 0.25rem; }
  .msg.error .text { color: #c00; }
  .send-row { display: flex; gap: 0.5rem; }
  .send-row input { flex: 1; }
  button {
    font: inherit;
    padding: 0.5rem 1rem;
    border: none;
    border-radius: 6px;
    background: #2563eb;
    color: white;
    cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  .login-link {
    display: inline-block;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    background: #2563eb;
    color: white;
    text-decoration: none;
  }
  #resident { font-size: 0.85rem; opacity: 0.75; margin-bottom: 1rem; }
</style>
</head>
<body>
  <h1>Clinical Co-Pilot</h1>

  <div id="loginGate" style="display:none;">
    <p>Log in with your OpenEMR account to use the Co-Pilot -- Phase 1
       (CLAUDE_CODE_BUILD_INSTRUCTIONS.md) requires a real resident session,
       not anonymous access.</p>
    <a class="login-link" href="/login?next=/ui">Log in with OpenEMR</a>
  </div>

  <div id="chatApp" style="display:none;">
  <div id="resident"></div>
  <div class="patient-row">
    <label for="patientId">Patient ID</label>
    <input id="patientId" value="__DEFAULT_PATIENT_ID__">
  </div>
  <div id="thread"></div>
  <div class="send-row">
    <input id="message" placeholder="Ask about this patient..." autocomplete="off">
    <button id="send">Send</button>
  </div>
  </div>

<script>
let conversationId = null;
const loginGate = document.getElementById("loginGate");
const chatApp = document.getElementById("chatApp");
const residentLabel = document.getElementById("resident");
const thread = document.getElementById("thread");
const messageInput = document.getElementById("message");
const patientIdInput = document.getElementById("patientId");
const sendButton = document.getElementById("send");

async function checkSession() {
  try {
    const resp = await fetch("/me");
    if (resp.ok) {
      const data = await resp.json();
      chatApp.style.display = "";
      loginGate.style.display = "none";
      residentLabel.textContent = data.resident ? `Logged in as ${data.resident}` : "Logged in";
    } else {
      chatApp.style.display = "none";
      loginGate.style.display = "";
    }
  } catch (err) {
    loginGate.style.display = "";
  }
}
checkSession();

function appendMessage(who, text, meta, isError) {
  const div = document.createElement("div");
  div.className = "msg" + (isError ? " error" : "");
  const whoDiv = document.createElement("div");
  whoDiv.className = "who";
  whoDiv.textContent = who;
  const textDiv = document.createElement("div");
  textDiv.className = "text";
  textDiv.textContent = text;
  div.appendChild(whoDiv);
  div.appendChild(textDiv);
  if (meta) {
    const metaDiv = document.createElement("div");
    metaDiv.className = "meta";
    metaDiv.textContent = meta;
    div.appendChild(metaDiv);
  }
  thread.appendChild(div);
  thread.scrollTop = thread.scrollHeight;
}

async function sendMessage() {
  const message = messageInput.value.trim();
  if (!message) return;

  appendMessage("You", message);
  messageInput.value = "";
  sendButton.disabled = true;

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message,
        patient_id: patientIdInput.value.trim() || null,
        conversation_id: conversationId,
      }),
    });
    if (!resp.ok) {
      const body = await resp.text();
      appendMessage("Error", `HTTP ${resp.status}: ${body}`, null, true);
      return;
    }
    const data = await resp.json();
    conversationId = data.conversation_id;
    const flagged = data.flagged_claims && data.flagged_claims.length
      ? data.flagged_claims.join(", ")
      : "none";
    const meta = `verification_passed: ${data.verification_passed} · flagged_claims: ${flagged}`;
    appendMessage("Agent", data.response, meta);
  } catch (err) {
    appendMessage("Error", String(err), null, true);
  } finally {
    sendButton.disabled = false;
    messageInput.focus();
  }
}

sendButton.addEventListener("click", sendMessage);
messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendMessage();
});
</script>
</body>
</html>
""".replace("__DEFAULT_PATIENT_ID__", _DEFAULT_PATIENT_ID)


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    """Minimal, self-contained chat page -- a grader convenience, not a
    replacement for the real OpenEMR-embedded module (ARCHITECTURE.md
    1.1), which remains future work. No build step, no new dependency:
    plain HTML/CSS/JS served directly from this route. Whether the chat
    form or a "Log in with OpenEMR" prompt renders is decided client-side
    by the page's own call to GET /me -- see that route below."""
    return _UI_HTML


@app.get("/login")
def login(next: str = "/ui") -> RedirectResponse:
    """Starts the authorization_code flow: redirects the browser to
    OpenEMR's own login/authorize page. Phase 1 (CLAUDE_CODE_BUILD_
    INSTRUCTIONS.md) -- replaces the shared password-grant credential
    with a token bound to whichever resident actually logs in here,
    per ARCHITECTURE.md 1.3."""
    return RedirectResponse(_sessions.build_authorize_url(next_url=next))


@app.get("/callback")
def callback(code: str, state: str) -> RedirectResponse:
    """OpenEMR redirects here after the resident logs in. Exchanges the
    code for tokens, creates a session, sets the session cookie, and
    sends the browser on to wherever /login's `next` pointed (default
    /ui)."""
    try:
        session_id, next_url = _sessions.complete_login(code=code, state=state)
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = RedirectResponse(next_url)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        # The droplet serves plain HTTP on 8420 today (documented,
        # accepted-risk grading exposure) -- a blanket Secure flag would
        # silently break login there, since browsers drop Secure cookies
        # over a non-TLS connection. Revisit once a TLS-fronted deployment
        # exists (clinical-copilot/README.md Known Gaps).
        secure=_settings.copilot_base_url.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/me")
def me(request: Request) -> JSONResponse:
    """Used by /ui's own JS to decide whether to show the chat form or a
    login prompt -- also handy for a quick curl-based liveness check of
    whether a session cookie is still valid."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = _sessions.get(session_id)
    if session is None:
        return JSONResponse(status_code=401, content={"authenticated": False})
    return JSONResponse(content={"authenticated": True, "resident": session.resident_username})


@app.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    copilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> ChatResponse:
    session = _sessions.get(copilot_session)
    if session is None:
        # No more anonymous access and no shared fallback credential --
        # Phase 1's whole point. A curl caller now needs a real session
        # cookie from completing /login first; see README's updated
        # "Try it directly" section.
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Open /login (or /ui, which will prompt) to start an OpenEMR session first.",
        )

    conversation_id = request.conversation_id or str(uuid.uuid4())
    state = _conversations.get(conversation_id, _ConversationState())

    result = _agent.run_turn(
        state.history, request.message, request.patient_id, state.tool_records, fhir=session.fhir
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
