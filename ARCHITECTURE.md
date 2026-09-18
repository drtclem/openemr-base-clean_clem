# Executive Summary

The Clinical Co-Pilot will be built as an external service, not as code inside OpenEMR, registered
via OAuth2/SMART on FHIR the way OpenEMR's own platform already anticipates for AI decision-support
tools (`dsi_type='predictive'`, found in the architecture audit). It consumes patient data through
the REST/FHIR API, never the database directly, and presents itself inside the chart only through a
thin module that renders UI cards, not clinical logic.

This design is a direct consequence of the audit, not a default choice. The audit found OpenEMR's
legacy interface enforces authorization in a decentralized way, one check per page across 1,048
files, while the FHIR API has a single centralized authorization checkpoint every request passes
through. Building against anything else would inherit the same inconsistent enforcement that
produced two of the audit's highest-severity findings.

The target user is the overnight cross-covering resident (see `USERS.md`), and every capability
below traces to one of that document's four use cases: rapid orientation on a page, verifying a
sign-out instruction against the current chart, time-critical synthesis during an acute event, and
an end-of-shift summary. The agent is a single-agent, tool-calling system, not a multi-agent
framework, because every one of these use cases is one resident having one sequential conversation
with tools, not a task that benefits from multiple coordinating agents.

Data access uses tokens scoped to the logged-in resident, not a backend service credential with
standing access to every patient. This matters directly because of the audit's most consequential
correction: a system-scoped FHIR token does not apply the same sensitivity filtering the UI
enforces, meaning a backend-service agent could see restricted visits the resident themselves
cannot. Binding the agent's access to the resident's own session is the first line of defense
against this; because whether user-scoped tokens actually enforce sensitivity was never verified,
a second, explicit filter is built into the agent's own data-retrieval layer as a compensating
control, regardless of what the API does.

Verification is designed as two distinct layers, per the PRD's requirement. Source attribution
means every clinical claim in a response must be traceable to a specific retrieved record, checked
programmatically before the response reaches the resident, not assumed from the model's output.
Domain constraint enforcement means a small set of hard, rules-based checks, not model judgment,
for things like allergy conflicts, run independently of what the model concludes.

The riskiest known gap, carried over explicitly from the audit, is data fidelity: free-text
clinical entries can lose their content in structured FHIR fields, meaning the agent must read
narrative fallback text for uncoded entries and treat them as lower-confidence. This is documented
here as an open design constraint, not resolved, because measuring how common it is on real data
was never done and shouldn't be assumed away.

Failure handling follows one rule throughout: when a tool call fails or data is missing, the agent
states that plainly rather than answering around the gap. Given this project's stated concern that
a confident wrong answer is worse than an admitted unknown, silent degradation is treated as a
verification failure, not an acceptable fallback.

---

# 1. System shape

## 1.1 Where the agent lives

An external service, not in-process OpenEMR code. Two reasons, both from the audit:

- **Blast radius.** A module running inside OpenEMR inherits ambient database access and sits
  inside a codebase where per-file authorization is demonstrably unreliable (audit-notes.md,
  Findings 1 and 2). An external client is constrained by whatever OAuth2 scopes it's granted,
  centrally enforced, the one part of this codebase where that guarantee actually holds.
- **It's the intended pattern, not a workaround.** OpenEMR's `dsi_type='predictive'` client
  registration and `dsi_source_attributes` table (architecture-audit.md §5.2) exist specifically
  for external predictive decision-support tools to register themselves with declared source
  attributes. Building the Co-Pilot this way uses a mechanism the platform already has, rather
  than inventing a new integration pattern.

A thin OpenEMR module, subscribing to `CardRenderEvent`/`MenuEvent` (the same mechanism OpenEMR's
own demographics screen uses for its dashboard cards), is used only to surface a chat entry point
inside the chart UI. It contains no clinical logic and makes no direct data queries, it exists
purely so the resident doesn't have to leave OpenEMR to reach the Co-Pilot.

## 1.2 Framework

A single Python service calling the Anthropic API directly with tool-calling (function calling),
not a multi-agent framework (LangGraph, CrewAI, or similar).

**Why not a multi-agent framework:** every use case in `USERS.md` is structurally the same shape,
one resident, one conversation, a sequence of tool calls against FHIR endpoints, one response.
Nothing in the four use cases requires multiple agents coordinating, delegating subtasks to each
other, or running in parallel. Adding that machinery here would add real complexity (more moving
parts to secure, log, and verify) with no corresponding requirement to justify it. If a future use
case genuinely needs parallel specialized agents, that's a reason to add the complexity then, not
now.

**Why direct tool-calling over a lighter agent framework:** a thin, directly-controlled tool-calling
loop makes the verification layer (Section 3) straightforward to insert as an explicit step between
"model drafts a response" and "response reaches the resident." A heavier abstraction layer would
make it harder to guarantee that insertion point is never skipped.

## 1.3 Data access and authorization

Data is retrieved exclusively through OpenEMR's REST/FHIR API (never the database directly), using
an OAuth2 **authorization_code** flow bound to the logged-in resident's own OpenEMR session,
`user/`-scoped tokens, not `system/`-scoped backend credentials.

**Why this specific choice:** the audit's live FHIR verification found that a `system/`-scoped
token bypasses the sensitivity filtering the UI enforces (Finding 12), a backend-service agent
would see restricted visits the resident themselves cannot open. Binding the agent's token to the
resident's own authenticated session means the agent's access ceiling is, at minimum, no higher
than what that resident could already reach by clicking around OpenEMR directly.

**Resolved (Phase 1, 2026-09-17):** architecture-audit.md §6.6 flagged whether a `user/`-scoped
token enforces sensitivity filtering as untested. It's now tested, live, against pid4's known
`sensitivity='high'` test encounter (audit-notes.md's "AUDIT TEST: encounter created by dr_1,
attributed to admin, sensitivity high", form_encounter id 1 / encounter 7): a real
`authorization_code`-obtained `user/`-scoped token's `GET /Encounter?patient=...` returned it in
full, identical to Finding 12's `system/`-scope result. **`user/` scope does not enforce
sensitivity filtering either** -- this was never a UI-only gap that a better grant type would
close on its own. Section 3.3's compensating control is not a defensive no-op; it is the only
thing standing between a restricted visit and the model's context, confirmed necessary rather than
assumed necessary.

# 2. Tool design

Each tool maps directly to a `USERS.md` use case. No tool exists that doesn't trace back to one.

| Tool | FHIR resource(s) | Use case(s) |
|---|---|---|
| `get_patient_snapshot` | Patient, Condition, AllergyIntolerance, MedicationRequest | 1, 3 |
| `get_recent_encounters` | Encounter (filtered per 3.3) | 1, 2, 4 |
| `get_recent_observations` | Observation, DiagnosticReport | 1, 2, 3 |
| `compare_signout_to_chart` | Encounter, Condition, MedicationRequest (diff against a supplied sign-out text) | 2 |
| `check_allergy_conflict` | AllergyIntolerance, MedicationRequest | 2, 3 (backs the domain-constraint layer, Section 3.2) |
| `summarize_shift_events` | aggregates tool calls/results already made this session | 4 |

**Mock vs. real data during development:** early development should run against the audit's own
existing fabricated test patients (pid 1-6, per `audit-notes.md`) rather than inventing new fixtures,
they already include the specific edge cases that matter (a duplicate patient, an empty chart, an
uncoded allergy, conflicting medication doses across duplicate records), which makes them
genuinely useful eval material, not just placeholder data.

**Error handling per tool:** every tool call that fails (timeout, 401, 404, malformed response)
returns a structured failure the agent must surface to the resident directly, "I couldn't retrieve
recent labs for this patient", rather than the agent continuing to answer as if the call had
succeeded with no data. This is a hard rule, not a suggestion, because of Section 4.

# 3. Verification system

Two distinct layers, run after the model drafts a response and before the resident sees it.

## 3.1 Source attribution

Every clinical claim in a drafted response is checked against the actual tool call results from
that same conversation turn before release. Concretely: the drafting step must tag each factual
claim with which tool result it came from; a separate, non-generative check confirms that tagged
source data actually contains what's being claimed. A claim that can't be matched to a real tool
result is either stripped from the response or explicitly flagged as unverified rather than shown
as fact.

**Known limitation, inherited directly from the audit's Finding 11:** an uncoded allergy or problem
can lose its actual content in FHIR's structured fields, surviving only in a narrative `text.div`
field. The agent must read that narrative fallback whenever a structured field returns
`data-absent-reason`, and must mark anything sourced from narrative text as lower-confidence than a
properly coded field. How often real OpenEMR data is uncoded, and therefore how often this
fallback path is actually exercised, is unmeasured, this is called out as an open risk, not
assumed to be rare.

## 3.2 Domain constraint enforcement

A small set of rules-based checks run independently of the model's own reasoning, not as a prompt
instruction the model might or might not follow:

- **Allergy conflict check:** before the agent surfaces or suggests anything involving a
  medication, `check_allergy_conflict` runs as a hard, code-level check against the patient's
  allergy list, not a judgment call left to the model.
- **Sensitivity exclusion (see 3.3):** enforced the same way, a code-level filter, not a prompt
  instruction asking the model to "please not mention high-sensitivity visits."

The principle throughout: anything where a wrong answer is dangerous is enforced in code the model
cannot reason its way around, not requested of the model as an instruction.

This is a direct application of a well-established pattern in agent evaluation: fixes that live
in the environment or tool layer hold; fixes that only live in prompt wording don't, because the
model can still *want* to do the wrong thing even when a guardrail stops it from succeeding.
`check_allergy_conflict` is a wall, not a request — it doesn't ask the model to be careful about
allergies, it makes an allergy-conflicting response structurally impossible to complete. The
distinction matters: a prompt instruction can be forgotten or reasoned around; a code-level check
cannot.

**Phase 5 addition (cross-reactivity), 2026-09-17:** `check_allergy_conflict`'s allergy match was,
until now, a direct/substring comparison only -- it could not catch a proposed medication that is a
*different* drug in the same class as a recorded allergy (e.g. amoxicillin against a documented
penicillin allergy), because that relationship is general pharmacology, not something any FHIR
resource for the patient states. `clinical-copilot/app/clinical_reference.py` adds a small, curated
drug-class table (`DRUG_CLASSES`) that `check_allergy_conflict` also checks, scoped to just the
classes the demo patients need, starting with penicillins. **This table is explicitly a stand-in
for a production drug-interaction database (First Databank/Medi-Span/Multum-style), not a claim of
full clinical decision-support integration** -- it is sized to this project's fixture data, not
general pharmacology, and a real deployment must replace it with a licensed, maintained reference
before this touches real patients. The tool's output now reports `cross_reactive_class` explicitly
(non-`None` only when the match came from this table rather than a direct name match), so a
resident-facing response and this system's own logs can distinguish the two rather than presenting
an inferred class relationship as if it were an exact match on the chart.

## 3.3 Sensitivity compensating control

Confirmed necessary, not just deliberately redundant (Section 1.3): a resident's own `user/`-scoped
token does not enforce sensitivity filtering either, so `get_recent_encounters` and any tool
touching encounter data must apply its own filter -- any encounter this resident's role would not
be permitted to view under OpenEMR's own ACL (the audit's role matrix shows the `clin`/
physician-covering roles this persona maps to do not hold a High sensitivity grant) is excluded
from what reaches the model's context entirely, not just hidden from the final response.

**Implementation note:** the FHIR `Encounter` resource carries no sensitivity marker at all --
`src/Services/FHIR/FhirEncounterService.php` never maps `form_encounter.sensitivity` into any FHIR
field or extension, so this isn't just unfiltered, the FHIR representation doesn't expose the value
to filter on. The compensating control (`clinical-copilot/app/sensitivity.py`) therefore sources
`sensitivity` from OpenEMR's own standard REST API (`GET /apis/default/api/patient/{puuid}/
encounter`, `api:oemr` scope) instead of FHIR -- still "OpenEMR's REST/FHIR API" per Section 1.3,
not a direct-database read.

# 4. Failure modes

| Failure | Behavior |
|---|---|
| Tool call fails/times out | State the gap directly to the resident; never answer as if data was retrieved when it wasn't |
| Patient record is empty or clearly incomplete | Say so explicitly ("this chart has no recorded problems/meds/allergies") rather than presenting silence as "nothing to report", directly informed by the audit's data-quality finding that an empty chart currently renders with no warning at all |
| Duplicate patient records exist | Surface the ambiguity rather than silently picking one, the audit found OpenEMR does not flag this to a physician today, and picking the wrong one of two duplicate records with different medication doses is a real, demonstrated risk in this exact dataset |
| A claim fails source-attribution verification (3.1) | Strip or flag as unverified; never shown as fact |
| A domain constraint check (3.2) fails | Hard stop on that specific piece of the response; the agent explains what it can't confirm rather than proceeding around it |

# 5. Observability

Every invocation is assigned a correlation ID at the moment the resident sends a message, carried
through every tool call, every LLM call, and every verification check tied to that request, so a
full trace is reconstructable from logs alone (per the engineering requirements). Minimum tracked
per invocation: total latency, per-tool-call latency, tool failure/retry counts, token usage and
cost, and verification pass/fail outcome for both layers in Section 3. This is the same discipline
already applied throughout the audit itself, every finding there states which method produced it;
the agent's own logs should make the same kind of statement possible about every response it gives.

# 6. Known tradeoffs

- **Latency vs. direct DB access.** The FHIR API is slower than direct SQL, and assembling a full
  patient picture (Section 2's tools) means multiple sequential API calls. The audit already
  identified OpenEMR's `$export` bulk-data operation as an available option if this becomes a
  binding constraint; the fix for latency is caching or bulk export, not dropping to direct SQL,
  since that would reintroduce exactly the authorization and audit-trail gaps documented in
  `AUDIT.md`.
- **OAuth2 setup is real friction** compared to a database connection string, but this is accepted
  as the cost of the only integration path that is both centrally authorized and fully audited.
- **The sensitivity compensating control (3.3) is a deliberate duplication of platform logic.**
  If OpenEMR's own filtering is later confirmed to work correctly for user-scoped tokens, this
  control becomes redundant rather than harmful, the safer failure direction.

# 7. Engineering requirements

Each item below is a concrete decision, not a placeholder, chosen to fit inside the program's
approved-vendor policy (Cursor, Anthropic, OpenAI, OpenRouter, Railway, Google One, DigitalOcean,
Vultr, ElevenLabs, Midjourney) without introducing a new vendor dependency where a free or
already-covered option does the job just as well.

## 7.1 Test design: boundaries, invariants, regression

Built directly on the audit's existing fabricated test patients (`audit-notes.md`, pid 1-6) rather
than new fixtures, since they already encode the exact edge cases this requirement asks for:

- **Boundary cases:** pid 3 (empty chart, no problems/meds/allergies at all), pid 6 (duplicate of
  pid 1 with a conflicting medication dose), a malformed/missing patient ID passed to a tool.
- **Invariant:** every response must carry only source-attributed claims (`ARCHITECTURE.md` §3.1)
  — a standing test asserts this holds across every eval case, not just the ones designed to test
  it directly.
- **Regression:** each finding in `AUDIT.md` that the agent's design specifically guards against
  (the sensitivity gap, Finding 12; the empty-chart-with-no-warning gap) becomes a permanent
  regression test, so a future change can't silently reintroduce a gap the audit already found.

Each test case documents which of the three categories it belongs to and which specific failure
mode it guards against, per the PRD's requirement that this be explicit, not implicit.

## 7.2 Correlation IDs

Already specified in Section 5: one correlation ID generated per resident message, propagated
through every tool call, LLM call, and verification check tied to that request. No additional
tooling required, this is implemented directly in the service's own logging.

## 7.3 Schema contracts

Every tool in Section 2's table gets a Pydantic model defining its exact input and output shape,
treated as the source of truth the implementation must match, not documentation written after the
fact. A tool that returns something outside its declared schema fails loudly (and is logged as a
tool failure per Section 4) rather than passing malformed data to the model silently.

## 7.4 Dashboards

**Self-hosted Langfuse, running as an additional container on the existing DigitalOcean droplet.**
Neither Langfuse nor Braintrust appear on the program's approved-vendor list, but Langfuse's
open-source (MIT) self-hosted deployment requires no purchase and no new vendor relationship at
all, it is software running on infrastructure already approved and already paying for. Braintrust
has no equivalent self-host path once usage exceeds its free tier, which would force a vendor
question this project doesn't need to raise. Tracks, at minimum: request count, error count,
p50/p95 latency, tool call counts, and verification pass/fail rate (the North Star metric from
`KEY_METRICS.md`), satisfying the dashboard requirement with a single tool. (Retry counts, listed
here in earlier drafts, is dropped below -- no retry logic exists anywhere in this system today, so
there is nothing for that metric to count; see the status note immediately below for why.)

### Dashboard requirement status (confirmed 2026-09-16, not assumed)

**Confirmed live and covered, on both instances.** Langfuse ships a built-in, non-custom
`Langfuse Agent Dashboard` (owner: `LANGFUSE`, a stock template, not something this project built)
-- confirmed present via its API on both the local and droplet instances. Its 8 widgets: Total Tool
Calls, Total Tool Calls (over time), Top 20 Called Tools, Observations by Type, P95 Tool Latency by
Tool, P95 Tool Latency by Tool (over time), P95 Latency by Observation Type, and Tool Errors by
Tool. Combined with the three verification scores (`verification_pass_rate`,
`source_attribution_pass`, `domain_constraint_pass`) confirmed actually logged on every real trace
-- 682/681/681 scored entries on local, 218/218/218 on the droplet, queried directly from each
instance's own ClickHouse rather than assumed -- this covers the PRD's required minimum floor:
request count, error count, p50/p95 latency, tool call counts, and verification pass/fail rate.

**Queue depth: not applicable.** The current architecture is a single FastAPI process handling each
`/chat` request synchronously and directly (§1.2's single-agent design) -- there is no request
queue anywhere in front of it, so there is no "depth" for any metric to measure. This would become a
real, meaningful metric if the system moves to a queued/async request-handling model at higher
scale -- `COST_ANALYSIS.md`'s 10,000-user tier already names horizontal scaling of the service
behind a load balancer as a required architecture change at that volume, and a queue is a natural
component of that kind of redesign, though the cost doc doesn't name "queueing" explicitly itself.
This is future-architecture territory, not a gap in the current single-process design.

**Event retries: not applicable.** Confirmed directly in the code (`grep` across every file in
`app/`, plus `requirements.txt`) -- no retry logic exists anywhere: not around tool calls, not
around FHIR requests, not around LLM calls. Failures are surfaced directly to the resident rather
than retried automatically, matching §4's failure-mode table exactly (`state the gap directly...
never answer as if data was retrieved when it wasn't`) -- a deliberate current design choice, not an
oversight. One honest caveat: the Anthropic Python SDK itself applies its own default
transport-level retry (a small number of automatic retries on transient network/5xx errors) to LLM
calls specifically, since `app/agent.py` never overrides `max_retries` on the client -- this is
vendor-SDK behavior we haven't disabled, not application-level retry logic we built, and it does not
apply to the FHIR client (`httpx`, no retry behavior by default). A retry-count metric would become
meaningful if application-level retry logic is added later.

**What actually matters most here.** The PRD's dashboard requirement is a stated minimum floor, not
a ceiling -- it explicitly expects a team to add whatever metrics matter for their specific agent
design. For this design, that metric is `verification_pass_rate`: the one named in `KEY_METRICS.md`
as the North Star specifically because it is the most direct measurement of this project's actual
premise (a confidently wrong clinical answer is the failure this whole system exists to prevent).
It is also, of everything in this section, the metric that is real, live, and proven working
end-to-end multiple times over today -- the Warfarin hallucination-bait case and the stale
sign-out/duplicate-record case among them -- not just present in a dashboard widget.

## 7.5 Runnable API collection

A Bruno collection (free, open-source, no vendor relationship required) covering the agent's core
endpoints, checked into the repo so a grader can exercise every workflow without reading source
code first.

## 7.6 /health and /ready endpoints

- `/health`: returns 200 if the process is running. Minimal, standard practice.
- `/ready`: actively checks that OpenEMR's FHIR API, the Anthropic API, and the self-hosted
  Langfuse instance are all reachable, returning a per-dependency breakdown rather than a single
  yes/no, so a failure is diagnosable from the response itself.

## 7.7 Alert definitions

**Status: live, not just designed.** Self-hosted Langfuse (v4.36.1) ships a real, DB-backed
**Monitor** feature -- not an Enterprise-only add-on -- that evaluates a metric query on a rolling
window every ~1 minute and transitions a severity state (`OK` / `WARNING` / `ALERT`) when a
threshold is crossed. Confirmed directly against both running instances (not assumed): three
Monitors exist in the `clinical-copilot-dev` project on **each** of the local and droplet Langfuse
deployments (each self-hosted instance has its own independent Postgres/ClickHouse, so this is two
separate configurations, not one shared across both), each wired to a real Automation/Action so
severity changes are a genuine event, not just a number sitting in a table.

| Alert | Real threshold | Window | Plain-language meaning | Solo on-call response |
|---|---|---|---|---|
| p95 latency | `p95(latency)` on observations where `type = AGENT` (the whole `chat_turn` span) **> 20,000 ms local / > 30,000 ms droplet** | rolling 15 min | The slowest 5% of resident-facing turns are taking longer than the instance's threshold, end to end | Check `/ready` first to isolate which dependency is slow (OpenEMR FHIR, Anthropic, Langfuse), then open the specific slow trace in Langfuse to see which step -- an LLM call round, a tool call, or verification -- is eating the time |
| Error rate | `count` of observations where `level = ERROR` **> 3** | rolling 15 min | More than 3 failed operations of any kind happened recently | Look at which observations are `ERROR` in Langfuse; today this is populated exclusively by tool-call failures (see gap below), so correlate against OpenEMR/Anthropic status before assuming a code bug |
| Tool failure rate | `count` of observations where `type = TOOL AND level = ERROR` **> 3** | rolling 30 min | More than 3 tool calls (the FHIR-backed `get_patient_snapshot` / `check_allergy_conflict`) failed recently | Check whether failures cluster on one tool/input shape (e.g. repeated malformed patient IDs -- expected, not urgent) or spread across many real requests (FHIR/network-level, escalate) |

**Where the p95 thresholds came from:** `USERS.md` use case 3 states the rapid-response scenario
needs an answer "in seconds, not minutes" but names no exact number, so both thresholds were chosen
from real measured data rather than guessed -- and the two instances needed *different* numbers,
which is itself a finding worth recording. Querying every real `AGENT`-type observation recorded
directly in each instance's own ClickHouse to date:

- **Local** (677 real traces): p50 ≈ 7.95 s, p95 ≈ 12.47 s, max ≈ 18.4 s → threshold set to 20 s.
- **Droplet** (218 real traces): p50 ≈ 12.82 s, p95 ≈ 19.72 s, max ≈ 25.82 s → threshold set to 30 s.

The droplet is measurably slower across the board (consistent with its more modest resources,
the same pattern already documented for the `/ready` OpenEMR check's own dedicated timeout above);
using the local threshold on the droplet would have put its real p95 within striking distance of
"alert," which is not a meaningful signal. Each threshold sits comfortably above that instance's own
observed max so it does not fire on ordinary variance, while still catching a genuine regression
(e.g. the droplet resource-contention incident already logged in `COVERAGE.md`). It is also an
honest admission that the *current* system does not yet hit an idealized "few seconds" bound for its
tightest use case -- the multi-round tool-calling + verification design (Section 3) has a real
latency floor around 8-13 s median today, worse on the droplet; closing that gap is future work, not
something to paper over here.

**Why error rate and tool failure rate will show identical numbers right now:** confirmed by reading
`app/observability.py` directly -- `finish_tool_call()` is the *only* place that sets
`level="ERROR"` on a Langfuse observation. LLM generation spans (`start_llm_call` /
`finish_llm_call`) never get marked `ERROR` even if the underlying Anthropic call fails, and there is
no span-level error marking for an unhandled exception inside `/chat` itself. So today, every
`ERROR`-level observation is a tool failure, and the two alerts are deliberately scoped differently
(all types vs. `type = TOOL` only, plus different windows) so they *will* diverge once other error
sources get instrumented -- not invented to look distinct before they actually are.

**Notification channel, and its one real limitation:** each Monitor is linked to a Langfuse
Automation whose Action is type `WEBHOOK` (self-hosted Langfuse's alerting supports Webhook, Slack,
or GitHub Dispatch -- no native "email me" option; Slack would need a connected workspace not set up
for this solo project). Self-hosted Langfuse hard-codes its webhook validator to only allow target
port 80 or 443 (SSRF hardening, not configurable via env), and this project's own `/chat` service
runs on 8420 with no port-80/443 listener in front of it, so the configured webhook target
(`https://example.com/clinical-copilot-alerts`) is a placeholder that satisfies Langfuse's
"a Monitor needs at least one Automation" requirement but does not actually deliver anywhere. Per
this task's own explicit allowance for a solo-operator deployment, the real, live, checkable
notification target is **the Monitor's own state** -- `severity` and `alertedAt`, visible in
Langfuse's Alerts UI (`/project/clinical-copilot-dev/alerts`) and queryable via its API -- not the
webhook payload. Wiring real webhook delivery (a small reverse proxy on 80/443, or rebinding the
service) is a near-term next step, not done here, noted honestly rather than silently claimed.

**Proof it actually fires (2026-09-16, local instance), not just configured:**
- **Tool failure rate** and **Error rate** fired for real, with no threshold trick: 5 real
  malformed-patient-ID requests were sent to the live local `/chat` endpoint back to back. The
  scheduler's next tick (≤1 minute later, per Langfuse's own cadence for sub-day windows) picked up
  the 5 real `ERROR`-level tool observations and flipped both Monitors from `OK` to `ALERT`
  (`tool failure rate` at `22:12:08Z`, `error rate` at `22:12:38Z`), with `alertedAt` populated.
- **p95 latency** was proven by the threshold-lowering method instead (real traffic wasn't slow
  enough to trip 20,000 ms honestly): temporarily set `alertThreshold` to 100 ms via the Monitor's
  own update API, confirmed `severity` flip to `ALERT` within one scheduler tick, then restored
  `alertThreshold` to 20,000 and confirmed `severity` returned to `OK` on the following tick --
  full fire-then-recover cycle observed, not asserted.
- The two failure-driven alerts (`tool failure rate`, `error rate`) are expected to self-clear back
  to `OK` on their own once the 5 manufactured failures age out of their rolling windows (30 min and
  15 min respectively from `22:12Z`) -- no manual reset needed, which is itself a confirmation the
  rolling-window logic works as designed.

The same three Monitors were separately created on the **droplet** instance (its own Langfuse, its
own trigger IDs, its own 30,000 ms latency threshold per its own measured baseline above) and
confirmed evaluating against real droplet ClickHouse data within one scheduler tick of creation
(`severity: OK`, real `lastCompletedAt` timestamps). The fire-then-recover mechanism itself was
proven once, thoroughly, on local; it is the same Langfuse code path on both instances, so it was
not separately re-triggered on the droplet to avoid manufacturing needless load there.

## 7.8 Baseline CPU/memory/latency/throughput

Captured once the load tests below are run, using the same self-hosted Langfuse dashboard plus
`docker stats` on the droplet (already used during the audit's performance pass), so this reuses
tooling already in place rather than adding another.

## 7.9 Load/stress tests

**Locust** (free, Python-based, no vendor relationship required) simulating 10 and 50 concurrent
residents each running a mix of the four `USERS.md` use cases, not a single repeated request, since
a realistic mix is what actually exercises the tool-calling and verification paths under load.
p50/p95/p99 latency and error rate are recorded at each level and become the baseline in 7.8.
