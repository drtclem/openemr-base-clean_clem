# Error analysis log

A running log, not a one-time document. After any real eval run -- not just
the entries seeded below -- add an entry here for anything unexpected
observed, whether or not it was a "failure" in the pass/fail sense. The
point is catching patterns over time, not just tracking a pass rate.

Format per entry: date, what was observed, root cause, fix/resolution (or
status, if not yet fixed).

---

## Entry 1 — Langfuse span timing bug (2026-09-16)

**Observed:** In the Langfuse trace view, `llm_call_0` (and every other
`llm_call_*` / `tool:*` observation) showed `Latency: 0.00s`, `Input: null`,
`Output: undefined`, despite the parent trace and the token-usage numbers
being correct.

**Root cause:** `TurnTrace.log_llm_call` / `log_tool_call` in
`app/observability.py` called `span.start_observation(...)` and
`child.end()` back-to-back, *after* the actual Anthropic API call or tool
call had already completed. Since both the start and end timestamps were
stamped at essentially the same instant, every span had zero-width
duration. `input`/`output` were never passed to either call -- only
`usage_details`, which is why token counts alone survived.

**Fix:** Restructured to `start_llm_call()` / `finish_llm_call()` (and the
tool-call equivalents) that bracket the *real* work: the span opens before
`self._client.messages.create(...)` (or the tool call) and closes after,
with the actual request/response content serialized and passed as
`input`/`output` at close. Also fixed a related gap found while touching
this: `thinking` content blocks were being serialized as bare
`{"type": "thinking"}` with the reasoning text dropped.

**Verification:** Re-ran the full eval suite (8/8) both locally and against
the droplet after the fix. Queried ClickHouse directly on both instances for
a fresh trace and confirmed real, non-zero, sequential durations that sum
correctly to the parent span's total, with real `input`/`output` content at
every step.

---

## Entry 2 — `ambiguous_query_unspecified_medication` eval flakiness (2026-09-16)

**Observed:** The same eval case, same code, same patient/message, passed on
one run and would plausibly fail on another depending on the model's exact
phrasing (e.g. "which medication" vs. "which specific medication" -- the
latter doesn't substring-match the former).

**Root cause:** The check (`_check_ambiguous_query` in `cases.py`) relies on
substring-matching a fixed list of clarification phrases
(`clarification_cues`). This makes it sensitive to model wording variance
rather than testing the actual underlying behavior (did the agent ask for
clarification, in any phrasing, rather than guess) robustly.

**Status:** Not yet fixed. Flagged as a known limitation of this specific
check, worth revisiting with a more semantic/flexible check (e.g. a cheap
LLM-judge pass, or a broader token-level heuristic) before Final Submission.
This is the same class of fragility already documented for the
source-attribution vocabulary match in `app/verification.py`.
