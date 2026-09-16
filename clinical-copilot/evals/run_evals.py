#!/usr/bin/env python3
"""Eval runner: executes every case in evals/cases.py against the live agent
(real Anthropic API calls, real FHIR calls against the configured OpenEMR
instance) and reports pass/fail per case.

Usage:
    python3 -m evals.run_evals
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.agent import ClinicalCopilotAgent
from app.auth import OAuthTokenProvider
from app.config import get_settings
from app.fhir_client import FhirClient
from app.observability import TurnObserver
from evals.cases import ALL_CASES


def main() -> int:
    settings = get_settings()
    fhir = FhirClient(settings, OAuthTokenProvider(settings))
    observer = TurnObserver(settings)
    agent = ClinicalCopilotAgent(settings, fhir, observer)

    results = []
    for case in ALL_CASES:
        t0 = time.monotonic()
        try:
            turn_result = agent.run_turn([], case.message, case.patient_id)
            passed, reason = case.check(turn_result)
            error = None
        except Exception as exc:  # noqa: BLE001 -- an eval must record a failure, not crash the suite
            passed = False
            reason = f"eval case raised an exception: {exc!r}"
            turn_result = None
            error = repr(exc)
        elapsed = time.monotonic() - t0

        results.append(
            {
                "name": case.name,
                "category": case.category,
                "guards_against": case.guards_against,
                "patient_id": case.patient_id,
                "message": case.message,
                "passed": passed,
                "reason": reason,
                "response_text": turn_result.response_text if turn_result else None,
                "verification_passed": turn_result.verification_passed if turn_result else None,
                "flagged_claims": turn_result.flagged_claims if turn_result else None,
                "enforced_warnings": turn_result.enforced_warnings if turn_result else None,
                "elapsed_s": round(elapsed, 2),
                "error": error,
            }
        )

    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    pct = round(100 * passed_count / total) if total else 0

    print(f"\n{'=' * 70}")
    print("Golden Set vs. Behavioral Coverage -- see evals/COVERAGE.md")
    print(f"{'=' * 70}")
    print(f"Golden Set:          {passed_count}/{total} passed ({pct}%)")
    print("Behavioral Coverage:  not yet implemented (see evals/COVERAGE.md)")
    print(f"{'=' * 70}\n")

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['name']} ({r['category']}) -- {r['elapsed_s']}s")
        print(f"       guards against: {r['guards_against']}")
        print(f"       reason: {r['reason']}")
        if not r["passed"]:
            # Full concrete situation on every failure, not just the reason:
            # exactly what was sent, to which patient, and what came back.
            print(f"       patient_id: {r['patient_id']!r}")
            print(f"       message sent: {r['message']!r}")
            print(f"       response received: {r['response_text']!r}")
            if r["error"]:
                print(f"       exception: {r['error']}")
        print()

    out_path = Path(__file__).parent / "last_run_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"Full results written to {out_path}")

    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
