"""Observability: ARCHITECTURE.md Section 5 + 7.4, KEY_METRICS.md.

Every invocation gets a correlation ID at the moment the resident's message
arrives; it's used as the seed for the Langfuse trace id, so the same ID
threads through structured logs AND the Langfuse UI -- either one alone is
enough to reconstruct what happened for a given request (Section 5's "a full
trace is reconstructable from logs alone" is met by the local structured
logs; Langfuse is the dashboard on top, not the only record).

If LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY aren't set, this module still logs
everything locally -- it just skips sending to Langfuse. That keeps the
service runnable before the Langfuse container is up.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings

logger = logging.getLogger("clinical_copilot")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _log(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str))


class TurnObserver:
    def __init__(self, settings: Settings):
        self._client = None
        if settings.observability_enabled:
            from langfuse import Langfuse  # local import: optional dependency at runtime

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )

    def start_turn(self, correlation_id: str, user_message: str, patient_id: str | None) -> "TurnTrace":
        _log(
            "turn_started",
            correlation_id=correlation_id,
            patient_id=patient_id,
            message=user_message,
        )
        span = None
        if self._client is not None:
            trace_id = self._client.create_trace_id(seed=correlation_id)
            span = self._client.start_observation(
                trace_context={"trace_id": trace_id},
                name="chat_turn",
                as_type="agent",
                input={"message": user_message, "patient_id": patient_id},
                metadata={"correlation_id": correlation_id},
            )
        return TurnTrace(client=self._client, span=span, correlation_id=correlation_id)


@dataclass
class TurnTrace:
    client: Any
    span: Any
    correlation_id: str

    def log_llm_call(self, *, round_index: int, latency_s: float, input_tokens: int, output_tokens: int) -> None:
        _log(
            "llm_call",
            correlation_id=self.correlation_id,
            round_index=round_index,
            latency_s=round(latency_s, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if self.span is not None:
            child = self.span.start_observation(
                name=f"llm_call_{round_index}",
                as_type="generation",
                usage_details={"input": input_tokens, "output": output_tokens},
            )
            child.end()

    def log_tool_call(self, *, tool_name: str, input: dict, latency_s: float, failed: bool) -> None:
        _log(
            "tool_call",
            correlation_id=self.correlation_id,
            tool=tool_name,
            input=input,
            latency_s=round(latency_s, 3),
            failed=failed,
        )
        if self.span is not None:
            child = self.span.start_observation(
                name=f"tool:{tool_name}",
                as_type="tool",
                input=input,
                level="ERROR" if failed else "DEFAULT",
            )
            child.end()

    def log_verification(
        self,
        *,
        passed_source_attribution: bool,
        passed_domain_constraint: bool,
        flagged_claims: list[str],
        enforced_warnings: list[str],
    ) -> None:
        overall_pass = passed_source_attribution and passed_domain_constraint
        _log(
            "verification",
            correlation_id=self.correlation_id,
            passed_source_attribution=passed_source_attribution,
            passed_domain_constraint=passed_domain_constraint,
            verification_pass=overall_pass,  # North Star metric, KEY_METRICS.md
            flagged_claims=flagged_claims,
            enforced_warnings=enforced_warnings,
        )
        if self.span is not None:
            self.span.score_trace(
                name="verification_pass_rate",
                value=1.0 if overall_pass else 0.0,
                data_type="BOOLEAN",
            )
            self.span.score_trace(
                name="source_attribution_pass",
                value=1.0 if passed_source_attribution else 0.0,
                data_type="BOOLEAN",
            )
            self.span.score_trace(
                name="domain_constraint_pass",
                value=1.0 if passed_domain_constraint else 0.0,
                data_type="BOOLEAN",
            )

    def finish_turn(self, *, final_response: str) -> None:
        _log("turn_finished", correlation_id=self.correlation_id, final_response=final_response)
        if self.span is not None:
            self.span.update(output=final_response)
            self.span.end()
            self.client.flush()
