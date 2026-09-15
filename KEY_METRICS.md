# KEY_METRICS.md

## Structure: one North Star, a few supporting metrics

Every metric below exists to do one of exactly two jobs: explain movement in the North Star, or
guard against the North Star being gamed. A metric that does neither is logged for context, not
tracked as a key metric — see the closing section for what got deliberately left out and why.

This matters more than it sounds for this specific project. The PRD's central concern is that a
confidently wrong clinical answer is a failure, not a demo quirk — so the metrics here are chosen
to make that concern measurable, not to produce a wall of numbers that looks thorough but doesn't
actually prove anything to a hospital CTO deciding whether to trust this in front of physicians.

---

## North Star: Verification pass rate

**Definition:** the percentage of agent responses in which every clinical claim survives the
source-attribution check (`ARCHITECTURE.md` §3.1) without being stripped or flagged as unverified.

**Formula:** `(responses with zero stripped/flagged claims) / (total responses) × 100`

**Why this is the North Star, not a generic AI-quality metric:** this project's stated premise
(see the PRD's "Why This Matters" section) is that the entire gap between a demo and something
deployable in a hospital is whether the system can be trusted not to hallucinate. Verification
pass rate is the most direct possible measurement of that specific claim — not "does the agent
sound confident," not "did the resident say it was helpful," but "did every factual statement it
made actually trace back to a real record." It is the one number that most directly reflects
whether the product is delivering on its core promise.

**Target:** 100% is the goal in principle (an unverifiable claim should never reach the resident),
but the metric is tracked as a rate specifically to surface *how often* the verification layer is
catching something, which is itself useful signal, not just a pass/fail gate.

---

## Supporting metric 1: Tool failure rate

**Job:** explains movement in the North Star.

**Definition:** the percentage of tool calls (per `ARCHITECTURE.md` §2's tool table) that fail,
time out, or return malformed data.

**Why it's needed:** if verification pass rate drops, the agent's own reasoning isn't necessarily
the cause. It could just as easily mean the FHIR API is degraded and the agent has less real data
to ground its claims in — a different problem requiring a different fix (infrastructure, not
prompt or model changes). Without this metric, a drop in the North Star is a mystery; with it,
it's diagnosable within minutes. This directly operationalizes `ARCHITECTURE.md` §4's failure-mode
table, which treats a failed tool call as a first-class event, not an edge case to shrug off.

---

## Supporting metric 2: p95 latency

**Job:** guards against a good North Star number hiding an unusable product.

**Definition:** the 95th-percentile end-to-end response time, from the resident's message to a
verified response being returned.

**Why it's needed:** `USERS.md` use case 3 (synthesis during a rapid response or acute event) is
explicitly time-critical — a resident asking about code status or allergies mid-emergency needs an
answer in seconds, not after a thorough thirty-second verification pass. A verification-obsessed
system that is airtight but slow would still fail this specific use case. Tracking p95 (not just
average) matters because the worst-case tail is exactly what a resident experiences in the highest
-stakes moment, which is precisely when the cost of slowness is highest.

---

## Supporting metric 3: Verified claims per response (anti-gaming guard)

**Job:** guards against the North Star being gamed.

**Definition:** the average number of distinct, source-attributed clinical claims contained in a
response that passes verification.

**Why this one is necessary, not optional:** verification pass rate alone has an obvious, boring
failure mode — an agent that hedges everything, gives vague non-answers, or refuses to commit to
specifics will show a *perfect* pass rate while being nearly useless to a resident at 2 a.m. deciding
whether to escalate to an attending. This metric exists specifically so that a high North Star
number can't be achieved by the agent simply saying less. If a hospital CTO asked "couldn't you
just get 100% verification by having it say nothing useful," this metric is the direct answer:
we track that it isn't.

---

## Summary table

| Metric | Job | What it catches |
|---|---|---|
| **Verification pass rate** (North Star) | Reflects the core promise | Whether claims are actually grounded in real data |
| Tool failure rate | Explains movement | Whether a North Star drop is a data problem, not a reasoning problem |
| p95 latency | Guards against a hidden tradeoff | Whether verification rigor is quietly making the agent too slow for its most time-critical use case |
| Verified claims per response | Guards against gaming | Whether a high pass rate is being achieved by saying less, not by being more correct |

---

## What we deliberately did not make a key metric

Tracked and logged for operational context, but not treated as key metrics, because none of them
explain North Star movement or guard against gaming it on their own:

- **Total request volume / token cost** — necessary for the cost-analysis deliverable and for
  capacity planning, but volume alone says nothing about whether the product is trustworthy or
  useful; a busy but unreliable agent is not a success.
- **Uptime / error rate at the infrastructure level** — a baseline operational requirement (and
  covered by the engineering requirements' dashboards and alerts), but it's a floor to maintain,
  not a signal that distinguishes a good version of this product from a mediocre one.
- **Raw user satisfaction / thumbs-up rate**, if collected — useful qualitative signal, but easy
  to game with a confident, likeable, wrong answer, which is exactly the failure mode this whole
  project exists to prevent. It's logged, not promoted to a key metric, for that reason.

## Traceability

Every metric above maps to a specific requirement already established in this project's other
documents, not invented independently:

- Verification pass rate operationalizes `ARCHITECTURE.md` §3.1 (source attribution).
- Tool failure rate operationalizes `ARCHITECTURE.md` §4 (failure modes) and §2 (per-tool error
  handling).
- p95 latency operationalizes the time-criticality explicit in `USERS.md` use case 3.
- Verified claims per response exists because of the specific gaming vector the North Star alone
  would otherwise permit.

**The bar, stated plainly:** not "we're measuring a lot," but "we know exactly what we're claiming,
and these four numbers, together, are how we'd prove it's true."
