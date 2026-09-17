# AI Cost Analysis

## Actual development spend to date

| Item | Cost | Notes |
|---|---|---|
| DigitalOcean droplet (4 vCPU / 8GB, "Basic") | $48/month (confirmed on DigitalOcean's current pricing page) | Resized up from 2 vCPU/4GB ($24/month) specifically to resolve the CPU-contention issue that was making Langfuse silently lose visibility into real request latency under load — see `evals/ERROR_ANALYSIS.md` Entry 6. Prorated actual spend so far is a fraction of this given the project's short runtime |
| Claude Code (development assistant) | Flat subscription (Claude Pro/Max), not metered per-token | This is the cost of *building* the agent, separate from what the deployed agent itself will cost to *run* — see below |
| GitHub + GitLab (labs.gauntletai.com) | $0 | Both used at their free tier |
| Self-hosted Langfuse | $0 direct cost | Runs as a container on the already-paid-for droplet; no separate vendor spend |
| ElevenLabs, Midjourney, OpenRouter | $0 | Not used in this project |

**Actual measured spend, pulled directly from the Anthropic Console (Spend this month) on the day
of Early Submission: $0.32.** This covers all development testing, manual interactions, and the
full eval suite run twice (once locally, once against the deployed droplet). Notably, this closely
matches the per-interaction estimate below ($0.03/interaction) — roughly 10 interactions worth of
usage, consistent with the number of eval cases and manual tests actually run.

**Total known cost so far: ~$48/month (the droplet, after the resize) + $0.32 (measured API
usage) — genuinely minimal for a working, verified, deployed agent.**

---

## Cost model methodology

This is not a simple cost-per-token × user-count calculation. The PRD explicitly asks for
projected cost **and** the architectural changes needed at each scale — because those two things
are linked: the naive calculation undercounts real cost at scale (it ignores infrastructure that
must change) and overstates it at small scale (it assumes per-user costs that don't actually apply
until real concurrency exists).

**Per-interaction token estimate**, based on the two tools built for Early Submission
(`get_patient_snapshot`, `check_allergy_conflict`) and Anthropic's current published pricing
(Claude Sonnet 5: $2/MTok input, $10/MTok output, confirmed current as of this writing, not
introductory/expiring pricing):

| Component | Estimated tokens |
|---|---|
| System prompt + tool definitions (tool-use overhead) | ~500 |
| Retrieved FHIR data injected into context (Patient + Condition + AllergyIntolerance + MedicationRequest bundles for one patient) | ~1,500-2,500 |
| Conversation history (grows per turn) | ~300-800, turn-dependent |
| Model's response (output tokens) | ~300-600 |

**Estimated cost per conversational turn:** roughly 2,500 input tokens × $2/M + 450 output tokens ×
$10/M ≈ **$0.0095, call it $0.01 per turn**.

**Estimated cost per full resident interaction** (one page → resolution, averaging ~3 turns
including follow-up questions, per `USERS.md` use case 1): **roughly $0.03**.

This is a planning estimate, not a measured figure — it should be replaced with real numbers
pulled from Langfuse's token-usage logging once enough real usage exists to average over.

**Usage assumption, stated explicitly:** because this agent serves overnight cross-covering
residents (not all patients or all staff, see `USERS.md`), "users" in the tiers below means
registered resident accounts, and a blended estimate of **5 interactions per user per day** is
used, accounting for the fact that any individual resident is only on overnight cross-cover
rotation some nights, not every night.

---

## Projected cost by scale, with required architectural changes

### 100 users
**LLM cost:** 100 users × 5 interactions/day × $0.03 × 30 days ≈ **$450/month**

**Architecture:** no changes needed *for this specific usage assumption* — but that claim needs to
be scoped precisely, not left as a blanket "comfortably handles it." "100 users" here means 100
*registered* users at 5 interactions/day each, not 100 users hitting the service at once, and that
distinction matters: real load testing (`clinical-copilot/PERFORMANCE_BASELINE.md`) found p95
latency around 37 seconds at just **10 concurrent** users, even on the droplet after its resize to
4 vCPU/8GB. At registered-user scale (5 interactions/day, spread across the day), the odds of 10+
truly simultaneous requests are low, so the "no changes needed" conclusion for *this* tier's stated
usage pattern still holds. But the FHIR/OpenEMR bottleneck this document already predicts for later
tiers (see the 10,000-user tier below) is not only a future concern — it is already measurably
present at small scale, confirmed directly by that load test, not projected. See
`PERFORMANCE_BASELINE.md` for the actual numbers rather than restating them here.

### 1,000 users
**LLM cost:** 1,000 users × 5 × $0.03 × 30 ≈ **$4,500/month**

**Architecture changes required:**
- Separate the Co-Pilot service onto its own droplet, away from OpenEMR. The audit already found
  the current droplet running at ~92% memory with just the base OpenEMR stack; adding real
  concurrent agent traffic on the same box risks resource contention affecting OpenEMR itself.
- **Enable prompt caching** on the system prompt and tool definitions (these repeat on every call
  and don't change per-request) — at this volume, caching starts meaningfully reducing input token
  cost, since a cache read is priced at 10% of the base input rate.

### 10,000 users
**LLM cost, naive:** 10,000 × 5 × $0.03 × 30 ≈ **$45,000/month** — but this figure is misleading
without the architecture changes below, which directly reduce it.

**Architecture changes required:**
- **Horizontal scaling of the Co-Pilot service** — multiple instances behind a load balancer, not
  a single process on a single droplet.
- **A dedicated, managed database for Langfuse**, not a single self-hosted container — observation
  volume at this scale needs real database performance, not a Docker volume on a shared droplet.
- **Prompt caching becomes a real cost lever, not just an optimization** — with caching properly
  applied to the repeated system/tool-definition tokens, realistic effective cost is meaningfully
  below the naive figure above; the exact savings depend on cache hit rate, which should be
  measured, not assumed.
- **OpenEMR's own database becomes a real bottleneck**, independent of the LLM cost. The audit
  found zero foreign-key constraints and 1,938 direct SQL calls scattered through the legacy UI —
  at this concurrency, a caching layer or a FHIR bulk-export pattern (already identified in
  `ARCHITECTURE.md` §6 as an available OpenEMR operation) becomes necessary to avoid the FHIR API
  itself becoming the bottleneck, not just the LLM calls.
- **Real alerting and on-call rotation**, not the single-person "check the dashboard yourself"
  posture that's adequate at 100-1,000 users.

### 100,000 users
**LLM cost, naive:** 10x the above, order of **$450,000/month** before any mitigation — this is the
tier where the naive calculation is most misleading, and where architecture, not just token
pricing, determines whether this is viable at all.

**Architecture changes required:**
- This tier is no longer "one project's infrastructure" — it's the infrastructure of a real
  multi-hospital-system healthcare product. A single droplet-based deployment model doesn't apply
  at all; this requires a proper cloud architecture (managed Kubernetes or equivalent), likely
  across multiple regions if serving hospital systems in different geographies (relevant given
  HIPAA's data-residency-adjacent concerns already discussed in `AUDIT.md`).
- **Model tiering becomes necessary, not optional.** Not every interaction needs Sonnet-level
  reasoning — Anthropic's own pricing guidance recommends Haiku for simpler tasks. A lookup-only
  interaction (e.g., "what's this patient's code status") could route to a cheaper model tier;
  reserving Sonnet for interactions that genuinely require the verification/reasoning layer in
  `ARCHITECTURE.md` §3 would meaningfully reduce blended cost at this volume.
- **Volume-based pricing negotiation with Anthropic directly** — Anthropic's published pricing page
  explicitly notes that volume discounts are available and negotiated case-by-case at enterprise
  scale; a project at this size would not be paying list-rate per-token pricing.
- **A dedicated compliance function**, not a project-level BAA checklist. At 100,000 users across
  presumably multiple hospital systems, this needs a formal, ongoing compliance program — SOC 2,
  regular audits, dedicated security staff — not the one-time audit this project performed.

---

## Summary

| Tier | Naive LLM cost | Realistic cost after architecture changes | Key structural change |
|---|---|---|---|
| 100 users | ~$450/mo | ~$450/mo (no change needed) | None — current single-droplet design holds |
| 1,000 users | ~$4,500/mo | Lower, with caching | Separate Co-Pilot from OpenEMR; enable prompt caching |
| 10,000 users | ~$45,000/mo | Meaningfully lower with caching + tiering | Horizontal scaling, managed Langfuse DB, FHIR bulk-export/caching layer |
| 100,000 users | ~$450,000/mo | Substantially lower with model tiering + negotiated pricing | Full cloud architecture, model tiering, enterprise Anthropic pricing, formal compliance program |

The point of this table is the pattern, not the exact dollar figures: **cost does not scale
linearly with users, because the architecture isn't allowed to stay fixed while users grow.** Each
tier above represents a point where the current design would break or become uneconomical without
a specific, named change — which is the actual question a hospital CTO evaluating this product
would ask, not "what does it cost today."
