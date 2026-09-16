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

---

## Entry 3 — GitHub Actions cannot reach the droplet (2026-09-16)

**Observed:** `.github/workflows/tests.yml`, once wired up to run
`evals/unit_tests.py` against the droplet's OpenEMR on every push, failed
every run (0/6 tests) with `detail_code='http_error'` on every FHIR/OAuth
call -- but the suite itself ran cleanly to completion (no crash, no
missing-config traceback), so this was never a missing-secrets problem.

**Investigation, in order:**
1. Ruled out missing/misconfigured GitHub secrets -- if `ANTHROPIC_API_KEY`
   or the `OPENEMR_*` secrets were unset, `app/config.py`'s `get_settings()`
   would raise `RuntimeError` before any test ran. It didn't; all 6 tests
   executed and failed identically.
2. Tested `https://...:9300` vs. `http://...:8300` (TLS/cert isolation
   test) -- identical failure on both ports, ruling out a TLS/self-signed-
   certificate-specific cause.
3. Confirmed via the DigitalOcean control panel that no Cloud Firewall is
   assigned to the droplet -- ruled out.
4. Checked for `fail2ban` on the droplet -- not installed.
5. Checked `iptables -L -n -v` -- nothing beyond the standard `ufw` +
   Docker chains, no exotic blocking rule.
6. Checked `ufw status verbose` -- both 8300 and 9300 correctly
   `ALLOW IN Anywhere`; confirmed both ports actually listening
   (`ss -tlnp`).
7. Checked `ufw`'s own packet log for the exact failure window -- full of
   ordinary internet background-noise port-scanning traffic, but **zero**
   blocked entries for port 8300 or 9300 (a `ufw`-level block would have
   logged here; it didn't).
8. Checked OpenEMR/Apache access logs for the same window -- **completely
   empty**. Not one request, on any port, from anywhere.

**Root cause:** Not fully determined, but narrowed precisely: nothing on
the droplet (`ufw`, `iptables`, `fail2ban`, the app itself) is blocking or
even seeing this traffic, yet it also never arrives. That combination means
the traffic is being dropped upstream of the droplet entirely -- before it
reaches `eth0` -- most likely DigitalOcean's automatic network-edge/
anti-abuse protection (a layer separate from, and not disabled by leaving
off, the optional Cloud Firewall product) filtering traffic from GitHub
Actions' datacenter IP ranges, or a routing/peering issue specific to
GitHub's network and this droplet's datacenter (NYC1). Normal traffic
(this session's own SSH-based testing, browser access, etc.) reaches the
droplet with no issue -- this is specific to GitHub Actions' network path.

**Status:** Not fixed; deliberately deprioritized (2026-09-16) to focus on
the demo video, since it doesn't block anything else. `.github/workflows/
tests.yml`'s automatic `push` trigger was removed (kept as
`workflow_dispatch`-only) so it stops failing on every commit; the LLM-free
unit suite goes back to fully manual (`python3 -m evals.unit_tests`) for
now, same as the LLM-based eval suite already is.

**Flagged next step, with its tradeoff:** a self-hosted GitHub Actions
runner installed directly on the droplet would sidestep the external-
reachability question entirely -- the runner would hit `localhost:8300`/
`9300`, exactly like this session's own successful SSH-based testing did
throughout. The tradeoff: a self-hosted runner means GitHub-orchestrated
code execution happens directly on a machine that also runs the droplet's
real (if fixture) OpenEMR/Langfuse services, which is a materially larger
trust boundary than GitHub's own ephemeral hosted runners -- worth deciding
deliberately, not defaulting into, before Final Submission.
