"""Thin HTTP client for OpenEMR's FHIR API.

Data access is exclusively through this client -- never the database directly
-- per ARCHITECTURE.md 1.3 / 4.4. Every method here either returns a parsed
FHIR Bundle/dict or raises FhirRequestError; it never swallows a failure
silently (ARCHITECTURE.md Section 4's hard rule).
"""

from __future__ import annotations

import httpx

from app.auth import OAuthTokenProvider
from app.config import Settings


class FhirRequestError(Exception):
    def __init__(self, message: str, *, detail_code: str):
        super().__init__(message)
        self.detail_code = detail_code


class FhirClient:
    def __init__(self, settings: Settings, token_provider: OAuthTokenProvider, client: httpx.Client | None = None):
        self._settings = settings
        self._tokens = token_provider
        self._client = client or httpx.Client(
            timeout=settings.request_timeout_seconds, verify=settings.verify_tls
        )

    def search(self, resource_type: str, params: dict[str, str]) -> dict:
        """GET <fhir_base>/<resource_type>?params, returns the parsed Bundle."""
        url = f"{self._settings.openemr_fhir_base_url}/{resource_type}"
        try:
            response = self._client.get(
                url,
                params=params,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise FhirRequestError(
                f"Timed out calling {resource_type} search", detail_code="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(
                f"Network error calling {resource_type} search", detail_code="http_error"
            ) from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{resource_type} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(
                f"{resource_type} search returned HTTP {response.status_code}",
                detail_code="http_error",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FhirRequestError(
                f"{resource_type} search returned non-JSON body", detail_code="malformed_response"
            ) from exc
        if body.get("resourceType") != "Bundle":
            raise FhirRequestError(
                f"{resource_type} search did not return a FHIR Bundle", detail_code="malformed_response"
            )
        return body

    def read(self, resource_type: str, resource_id: str) -> dict:
        """GET <fhir_base>/<resource_type>/<id>, returns the parsed resource."""
        url = f"{self._settings.openemr_fhir_base_url}/{resource_type}/{resource_id}"
        try:
            response = self._client.get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise FhirRequestError(
                f"Timed out reading {resource_type}/{resource_id}", detail_code="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(
                f"Network error reading {resource_type}/{resource_id}", detail_code="http_error"
            ) from exc

        if response.status_code == 404:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} not found", detail_code="not_found"
            )
        if response.status_code != 200:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} returned HTTP {response.status_code}",
                detail_code="http_error",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise FhirRequestError(
                f"{resource_type}/{resource_id} returned non-JSON body",
                detail_code="malformed_response",
            ) from exc

    def get_standard_api(self, path: str) -> dict | list:
        """GET <openemr_base_url>/apis/default/api<path> -- OpenEMR's own
        standard REST API (`api:oemr` scope), used only where the FHIR API
        omits a field this app needs (Encounter.sensitivity -- see
        app/sensitivity.py's module docstring). Still "OpenEMR's REST/FHIR
        API" per ARCHITECTURE.md 1.3, not a direct-database read.
        """
        url = f"{self._settings.openemr_base_url}/apis/default/api{path}"
        try:
            response = self._client.get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise FhirRequestError(f"Timed out calling {path}", detail_code="timeout") from exc
        except httpx.HTTPError as exc:
            raise FhirRequestError(f"Network error calling {path}", detail_code="http_error") from exc

        if response.status_code == 404:
            raise FhirRequestError(f"{path} not found", detail_code="not_found")
        if response.status_code != 200:
            raise FhirRequestError(
                f"{path} returned HTTP {response.status_code}", detail_code="http_error"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FhirRequestError(f"{path} returned non-JSON body", detail_code="malformed_response") from exc
        return body.get("data", body) if isinstance(body, dict) else body

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.get_token()}",
            "Accept": "application/fhir+json",
        }


def bundle_entries(bundle: dict) -> list[dict]:
    """Extract the list of resource dicts from a FHIR Bundle, [] if none."""
    return [entry["resource"] for entry in bundle.get("entry", []) if "resource" in entry]
