# AgentForge — Clinical Co-Pilot

An AI agent embedded in OpenEMR that gives an overnight cross-covering resident the patient
context they need, the moment they need it — grounded, verified, and sourced from the actual
chart, not inferred or assumed.

**Live deployment:** http://157.230.11.142:8300

**Try the Clinical Co-Pilot directly:** there is no chat UI yet inside OpenEMR (see Known Gaps
below) — the agent is reached via its own `/chat` endpoint:

```bash
curl -s -X POST http://157.230.11.142:8420/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "The day team sign-out says this patient is on Metformin 500mg twice daily and has no allergies. Can you confirm that is still accurate so I can give the next dose?",
    "patient_id": "98c4c4c8-b07e-11f1-8334-022958ad0af8"
  }' | python3 -m json.tool
```

This is a real fixture patient with a deliberately incorrect sign-out claim baked into the
question — the response should catch the dose discrepancy and flag a duplicate patient record.
See the Bruno collection (`clinical-copilot/bruno/`) for more example requests, including
`/health` and `/ready`.

---

## What this is

A fork of [OpenEMR](https://github.com/openemr/openemr) with a Clinical Co-Pilot built on top,
targeting a specific, narrow user: the overnight cross-covering resident, paged about patients
they've never met, who needs rapid, trustworthy orientation before acting. See `USERS.md` for the
full persona and use cases this project is built around, and why that specific user was chosen.

Every design decision in this repo traces back to one of two things: a specific finding in
`AUDIT.md`, or a specific use case in `USERS.md`. Nothing here is built because it was technically
interesting — see `ARCHITECTURE.md` for the reasoning behind each choice.

## Repository guide

| Document | What it covers |
|---|---|
| `AUDIT.md` | Full security, architecture, and compliance audit of the base OpenEMR system, performed before any agent code was written |
| `USERS.md` | The target user (overnight cross-cover resident), their workflow, and the four use cases the agent addresses |
| `ARCHITECTURE.md` | The integration plan: where the agent lives, how it accesses data, the verification system design, and known tradeoffs |
| `KEY_METRICS.md` | The North Star metric (verification pass rate) and supporting metrics, with rationale |
| `COST_ANALYSIS.md` | Actual dev spend and projected production costs at scale, with the architectural changes each tier requires |

## Setup — running this locally

**Prerequisites:**
- Docker Desktop
- Node.js (v24.x)
- Composer

**Steps:**
```bash
git clone https://github.com/drtclem/openemr-base-clean_clem.git
cd openemr-base-clean_clem/docker/development-easy
docker compose up -d
```

First run takes several minutes (pulling 7 container images, npm install, composer install,
database initialization). Once complete:

- OpenEMR: `http://localhost:8300`
- Default login: `admin` / `pass` (change this immediately — see `AUDIT.md`'s security findings
  on why shipping with unchanged default credentials is itself a documented risk)

See `DOCKER_README.md` for the full OpenEMR-specific Docker setup reference.

## Deployment

Deployed to a DigitalOcean droplet (2 vCPU / 4GB RAM, Docker-on-Ubuntu marketplace image):

```bash
ssh root@157.230.11.142
cd openemr-base-clean_clem/docker/development-easy
docker compose up -d
```

Firewall is configured to expose only the OpenEMR web ports (8300, 9300) — every other service
(MySQL, phpMyAdmin, CouchDB, Selenium) remains blocked from public access, directly addressing the
network-exposure gap documented in `AUDIT.md` (Finding 4).

## Architecture overview

The Clinical Co-Pilot is built as an external service, not code inside OpenEMR, registered via
OAuth2/SMART on FHIR — the pattern OpenEMR's own platform already anticipates for AI decision
-support tools. It accesses patient data exclusively through the REST/FHIR API, never the
database directly, because the audit found the FHIR API is the only part of this codebase with
centralized, consistently-enforced authorization.

Two layers of verification run on every response before it reaches the resident: source
attribution (every clinical claim must trace back to a specific retrieved record) and domain
constraint enforcement (hard, code-level checks — e.g., allergy conflicts — that don't depend on
the model's own judgment). Full reasoning and tradeoffs are in `ARCHITECTURE.md`.

## Observability

Self-hosted [Langfuse](https://langfuse.com), running as an additional container on the same
droplet — chosen specifically because it requires no new vendor relationship or purchase, only
infrastructure already in place.

## Status

This repo is under active development as part of a one-week sprint. Current state:
- [x] OpenEMR running locally and deployed publicly
- [x] Full audit (security, architecture, compliance) complete
- [x] Target user and use cases defined
- [x] Integration architecture and key metrics defined
- [x] Clinical Co-Pilot agent built and deployed — agentic chatbot, two tools
      (`get_patient_snapshot`, `check_allergy_conflict`), source-attribution and domain-constraint
      verification, self-hosted Langfuse observability, 7/7 eval cases passing against the live
      droplet
- [ ] Full engineering requirements (load testing, alerting, health/ready endpoints — planned for
      Final Submission)

## Known gaps (documented deliberately, not hidden)

- **Authentication uses OAuth2 password-grant, not the `user/`-scoped authorization_code flow
  `ARCHITECTURE.md` specifies.** This was a scoped, explicit tradeoff for Early Submission timing.
  Migrating to authorization_code — tokens genuinely bound to a resident's own session, rather than
  an admin credential — is the top priority before Final Submission, since it's the actual fix for
  the sensitivity-filtering gap `AUDIT.md` Finding 12 identified.
- **Only two of `ARCHITECTURE.md`'s six planned tools are built** (`get_patient_snapshot`,
  `check_allergy_conflict`). The remaining four (`get_recent_encounters`, `get_recent_observations`,
  `compare_signout_to_chart`, `summarize_shift_events`) cover use cases 2 and 4 more fully and are
  planned next.
- **No UI module inside OpenEMR's chart yet** — the agent is reachable via a `/chat` endpoint,
  not yet surfaced as a card in the patient dashboard per `ARCHITECTURE.md` §1.1.
- **Fixture patient data was manually copied from the local dev stack to the droplet via SQL
  import** (same UUIDs preserved) rather than created through a repeatable seed script — worth
  turning into an actual script before relying on it again.
- **The eval suite does not yet test unauthorized-access attempts** ("inputs that attempt to
  extract information the requester is not authorized to see," per the PRD's Evaluation
  requirement). This is a direct consequence of the password-grant auth gap above: the current
  build doesn't yet distinguish requester permission levels, so there's no authorization boundary
  to test against yet. This becomes testable once the authorization_code migration is complete.
