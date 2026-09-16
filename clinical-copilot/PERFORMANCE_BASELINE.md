# PERFORMANCE_BASELINE.md

Load/stress test results per ARCHITECTURE.md §7.9, establishing the baseline for §7.8. Run
2026-09-16, both locally and against the droplet. **These numbers are the baseline going
forward** — future performance work (caching, model tiering, infrastructure changes) should be
measured as an improvement against this document, not against assumptions.

## Methodology

**Tool:** Locust 2.46 (`load_test/locustfile.py`), matching ARCHITECTURE.md §7.9's original plan.

**Scenario — a realistic conversation, not a single repeated request.** Each simulated resident
runs one bounded session against `/chat`, carrying its own `conversation_id` across turns like a
real multi-turn conversation:

1. A normal orientation request (patient randomly chosen between pid1/Alice and pid2/Bob).
2. One natural follow-up in the *same* conversation — "Given her allergy history, is amoxicillin
   safe to start empirically?" — chosen specifically to exercise the multi-turn grounding fix
   (ERROR_ANALYSIS.md Entry 5, which threads `tool_records` across turns) under real concurrent
   load, not just the single-user testing it was originally verified with.
3. For roughly 1 in 10 simulated users: a third message with a malformed patient ID, so the load
   includes real error-path traffic, not only the happy path.

Every user sends exactly 2 or 3 messages total, then stops (`StopUser`) — this is a load test
proving the system holds up under concurrency, not an open-ended soak test.

**Cost estimate (checked before running, per the task's own checkpoint):** ~10×2 + 50×2 +
10×2 ≈ 140 baseline calls, +~10% for the malformed-ID branch ≈ 150-160 real `/chat` calls total
across all stages, at COST_ANALYSIS.md's ~$0.03/interaction estimate ≈ **$4.50-$4.80**, well
within "a few dollars." Actual real calls sent, counted directly from Locust's own request
counters (not estimated after the fact): 21 (local, 10 users) + 109 (local, 50 users) + 20
(droplet, 10 users) = **150 real `/chat` calls total**, matching the estimate closely.

## Results

### Stage 1 — 10 concurrent users, local

| Metric | Value |
|---|---|
| Requests | 21 (10 orientation, 10 follow-up, 1 malformed-ID) |
| Error rate | 0 / 21 (0%) |
| Throughput | 0.75 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 10.0s / 16.0s / 17.0s |
| p50 / p95 / max — orientation | 15.0s / 17.0s / 16.71s* |
| p50 / p95 / max — follow-up | 8.2s / 10.0s / 10.18s |
| Peak container CPU | `development-easy-openemr-1` 116.0%, `langfuse-worker` 63.2%, `mysql` 17.7%, `clickhouse` 16.5% |
| Peak container memory | `clickhouse` 2.24 GiB, `openemr` 769 MiB, `langfuse-web` 748 MiB, `langfuse-worker` 501 MiB |

\* Locust's percentile bucketing occasionally rounds a bucket above the true max; the true
measured max is used here in place of the rounded bucket value.

*(Our own service's host-level CPU/memory was not separately sampled in this stage — see the
honest note below. Only Stage 2 and Stage 3 sample the `uvicorn` process directly.)*

### Stage 2 — 50 concurrent users, local

| Metric | Value |
|---|---|
| Requests | 109 (50 orientation, 50 follow-up, 9 malformed-ID) |
| Error rate | 0 / 109 (0%) |
| Throughput | 1.39 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 21.0s / 61.0s / 65.0s |
| p50 / p95 / max — orientation | 53.0s / 65.0s / 64.58s |
| p50 / p95 / max — follow-up | 16.0s / 22.0s / 24.02s |
| Peak container CPU | `openemr-1` 156.3%, `clickhouse` 45.0%, `mysql` 16.7%, `minio` 15.9%, `langfuse-worker` 12.3% |
| Peak container memory | `openemr` 2.29 GiB, `clickhouse` 2.16 GiB, `langfuse-web` 804 MiB, `langfuse-worker` 501 MiB |
| Our own service (`uvicorn`, host process) | Peak 9.1% CPU, peak 103 MB RSS |

**Reading this honestly:** latency roughly doubled from 10 to 50 concurrent users (p95: 16s →
61s), and `development-easy-openemr-1` — OpenEMR's own FHIR backend, not our agent service — is
consistently the single most CPU-loaded container at both concurrency levels (116% → 156% on a
multi-core host). Our own service's CPU/memory footprint is trivial by comparison (peak 9.1% CPU,
~103 MB) because it is I/O-bound, waiting on Anthropic and FHIR round-trips, not doing real
compute itself. This matches ARCHITECTURE.md §6's already-documented tradeoff: FHIR access is the
latency-relevant bottleneck under load, not our own agent code, and caching or bulk-export (both
already named there) is the correct lever, not scaling the agent service itself.

### Stage 3 — droplet confirmatory pass

**Pre-flight check:** droplet load average was 3.46/2.62/2.50 (healthy — nowhere near the ~175
load average seen during the earlier resource-contention incident in COVERAGE.md), and Selenium /
CouchDB / Mailpit were **not** running, so nothing needed stopping this time (unlike before).

**10 concurrent users — run:**

| Metric | Value |
|---|---|
| Requests | 20 (10 orientation, 10 follow-up, 0 malformed-ID — 1-in-10 odds over only 10 users legitimately landed on zero) |
| Error rate | 0 / 20 (0%) |
| Throughput | 0.12 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 81.0s / 154.0s / 154.0s |
| p50 / max — orientation | 82.0s / 154.19s |
| p50 / max — follow-up | 73.0s / 72.61s |
| Peak container CPU | `clickhouse` **165.8%**, `langfuse-web` 93.9%, `openemr` 68.0%, `langfuse-worker` 31.4% |
| Peak container memory | `langfuse-web` 981 MiB, `openemr` 824 MiB, `clickhouse` 801 MiB, `langfuse-worker` 626 MiB |
| Our own service (`uvicorn`, host process) | Peak 0.8% CPU, ~79 MB RSS |

**50 concurrent users — deliberately skipped, stated honestly.** The 10-user droplet result
already shows severe degradation relative to both the local result and the droplet's own
single-user baseline (ARCHITECTURE.md §7.7's measured p50≈12.8s/p95≈19.7s/max≈25.8s from 218 real
traces) — median latency alone (81s) is already 4-6x the droplet's normal single-user experience,
and `clickhouse` alone is consuming more CPU than one full core on this 2-vCPU box. Scaling to 50
concurrent users on top of that would almost certainly produce multi-minute waits or outright
timeouts without teaching us anything the 10-user result hasn't already shown, while risking real
disruption to the shared droplet's other workloads (it also runs the actual OpenEMR dev stack) for
no added diagnostic value. This is a judgment call, stated plainly rather than silently
substituting a smaller number as if it were the full test.

## A significant finding this load test surfaced, not smoothed over

On the droplet specifically (not reproduced locally — see below), the load test surfaced a real
gap in this project's own observability, not just a capacity limit:

- **Locust measured** (client-side, real wall-clock round trip): p95 = 154.0s, max = 154.19s across
  the 20 completed requests.
- **Langfuse's own recorded `AGENT`-span duration** for the same time window, queried directly from
  the droplet's ClickHouse: only **5** `AGENT` spans were recorded for those 20 real completed
  requests (also only 4 `TOOL` spans, where closer to 15-20 would be expected), with p95 = 17.2s
  and max = 17.7s — nowhere close to what the client actually experienced.
- **Locally, this discrepancy does not occur.** The same ClickHouse query against local's own
  instance for its 130 real requests (stage 1 + stage 2 combined) shows exactly 130 `AGENT` spans
  and 130 `TOOL` spans — a clean 1:1 match — with span-level latency tracking the client-observed
  latency reasonably closely at both concurrency levels.

**Most likely explanation:** on the resource-constrained droplet, severe CPU contention
(`clickhouse` alone peaked at 165.8% of a 2-vCPU box) delays the request well before our own
instrumented code path even begins — time the client genuinely waits, but that our own `AGENT`
span never measures, since the span timer only starts once `run_turn()` begins executing. Under
that same contention, a real fraction of this project's own span-create calls to the self-hosted
Langfuse ingestion endpoint likely fail or are dropped outright — consistent with
`app/observability.py`'s deliberate design of treating the observability pipeline as best-effort
(a Langfuse outage should never break the actual resident-facing response), which is the right
tradeoff for availability, but means telemetry itself becomes unreliable exactly when the system
is under the load conditions where accurate telemetry matters most.

**Why this matters for the alerts just configured:** the droplet's p95-latency Monitor
(ARCHITECTURE.md §7.7, threshold 30,000 ms) read `severity: OK` throughout this entire load test —
it never fired, because it evaluates Langfuse's own `AGENT`-span data, which this test proves can
itself go quiet under exactly the kind of real contention the alert exists to catch. A 154-second
real resident wait produced no alert. This is a genuine blind spot, not a rounding error, and it is
the single most important finding of this exercise — more important than any individual latency
number above. It is noted here as a concrete next step (a client-observable or reverse-proxy-level
latency signal, independent of the agent's own internal instrumentation, would close this gap),
not fixed in this session, which was scoped to running and honestly reporting the load test.

## Connecting back to KEY_METRICS.md and the configured alerts

KEY_METRICS.md names p95 latency as a supporting metric specifically to guard against
"verification rigor quietly making the agent too slow for its most time-critical use case." This
load test is the first real evidence bearing on that question under concurrency, and the honest
answer is: **yes, these numbers exceed the alert thresholds already configured**, substantially, at
both stages:

- Local: Stage 2's p95 (61.0s) is already ~3x the local alert threshold (20,000 ms / 20s).
- Droplet: Stage 3's p95 (154.0s) is over **5x** the droplet's alert threshold (30,000 ms / 30s) —
  and yet, per the finding above, the alert did not fire, because the underlying telemetry itself
  degraded under the same contention.

Both alert thresholds were deliberately set from *single-user, uncontended* baseline traffic
(ARCHITECTURE.md §7.7) specifically to catch *regressions from that normal case* — they were never
claimed to represent a validated concurrency ceiling, and this test shows they aren't one. The
system remains correct under concurrency (0 errors across all 150 real requests, every one of them
eventually returned a real, complete response) but is **not currently fast enough at 50 concurrent
local users or even 10 concurrent droplet users** to meet the sub-30-second bound the architecture
document aspires to for its most time-critical use case. That gap is FHIR/infrastructure-bound
(Stage 1 vs 2's dominant CPU consumer was consistently OpenEMR itself, not our agent), matching
ARCHITECTURE.md §6's and COST_ANALYSIS.md's existing guidance that caching, bulk-export, and
eventually horizontal scaling — not agent-code changes — are the correct next levers, not something
newly discovered here, but now backed by a real number instead of a guess.
