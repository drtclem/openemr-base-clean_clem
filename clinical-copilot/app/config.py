"""Runtime configuration, loaded from environment variables.

This is the one place `.env` values get parsed. Nothing else in the app
reads `os.environ` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    openemr_base_url: str
    openemr_fhir_base_url: str
    oauth_token_url: str
    oauth_authorize_url: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_username: str
    oauth_password: str
    oauth_scope: str

    # Where this Co-Pilot instance itself is reachable -- used to build the
    # `redirect_uri` for the authorization_code flow (app/oauth_session.py).
    # Must match a `redirect_uri` already registered on the OpenEMR OAuth
    # client (see clinical-copilot/README.md's setup section).
    copilot_base_url: str

    # A second, deliberately narrower-scoped credential used ONLY by the
    # oauth_scope_enforcement_denied eval case (evals/cases.py) to prove
    # OpenEMR's OAuth server actually rejects FHIR calls outside a token's
    # granted scope -- never used on the live /chat path. Optional so the
    # rest of the app/eval suite still runs without it configured; that one
    # eval case skips (not fails) if unset.
    oauth_scope_test_username: str | None
    oauth_scope_test_password: str | None

    anthropic_api_key: str
    anthropic_model: str

    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None

    request_timeout_seconds: float
    verify_tls: bool

    @property
    def observability_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


def _require(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(
            f"Missing required environment variable: {name}. See .env.example."
        )
    return value


@lru_cache
def get_settings() -> Settings:
    base_url = os.environ.get("OPENEMR_BASE_URL", "https://localhost:9300")
    return Settings(
        openemr_base_url=base_url,
        openemr_fhir_base_url=os.environ.get(
            "OPENEMR_FHIR_BASE_URL", f"{base_url}/apis/default/fhir"
        ),
        oauth_token_url=os.environ.get(
            "OPENEMR_OAUTH_TOKEN_URL", f"{base_url}/oauth2/default/token"
        ),
        oauth_authorize_url=os.environ.get(
            "OPENEMR_OAUTH_AUTHORIZE_URL", f"{base_url}/oauth2/default/authorize"
        ),
        oauth_client_id=_require("OPENEMR_OAUTH_CLIENT_ID"),
        oauth_client_secret=_require("OPENEMR_OAUTH_CLIENT_SECRET"),
        oauth_username=_require("OPENEMR_OAUTH_USERNAME"),
        oauth_password=_require("OPENEMR_OAUTH_PASSWORD"),
        oauth_scope=os.environ.get(
            "OPENEMR_OAUTH_SCOPE",
            "openid offline_access api:oemr api:fhir "
            "user/Patient.read user/Condition.read "
            "user/AllergyIntolerance.read user/MedicationRequest.read "
            "user/Encounter.read",
        ),
        copilot_base_url=os.environ.get("COPILOT_BASE_URL", "http://localhost:8420"),
        oauth_scope_test_username=os.environ.get("OPENEMR_SCOPE_TEST_USERNAME"),
        oauth_scope_test_password=os.environ.get("OPENEMR_SCOPE_TEST_PASSWORD"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        langfuse_host=os.environ.get("LANGFUSE_HOST", "http://localhost:3300"),
        request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15")),
        verify_tls=os.environ.get("OPENEMR_VERIFY_TLS", "true").lower() not in ("false", "0", "no"),
    )
