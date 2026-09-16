# Clinical Co-Pilot -- Early Submission build

Minimal, working slice of the Clinical Co-Pilot described in `../ARCHITECTURE.md`:
an agentic chatbot (Python, direct Anthropic tool-calling, no framework), a
simplified two-layer verification pass, Langfuse observability, and a 7-case
eval suite. Built and tested end-to-end against the local
`docker/development-easy` stack -- see "Known gaps" below for what's
simplified for this pass, and the deployment section for what changes to run
this against the droplet.

## Local setup

Prerequisites: the `docker/development-easy` stack running (`docker compose
up --detach --wait` from that directory), Python 3.11+.

```bash
cd clinical-copilot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Register + enable an OAuth2 client against the local instance

```bash
curl -sk -X POST -H 'Content-Type: application/json' \
  https://localhost:9300/oauth2/default/registration \
  --data '{
    "application_type": "private",
    "redirect_uris": ["https://localhost:9300/clinical-copilot/callback"],
    "client_name": "Clinical Co-Pilot (dev)",
    "token_endpoint_auth_method": "client_secret_post",
    "scope": "openid offline_access api:oemr api:fhir user/Patient.read user/Condition.read user/AllergyIntolerance.read user/MedicationRequest.read"
  }'
```

Save the returned `client_id` / `client_secret` into `.env`. Then, from
`docker/development-easy`, enable the client and grant it the password
grant type (both disabled by default -- see `architecture-audit.md` 6.1 and
"Known gaps" below):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET is_enabled=1, grant_types='authorization_code|password|refresh_token' WHERE client_id='<your client_id>';"
```

### 2. Set the remaining `.env` values

- `OPENEMR_OAUTH_USERNAME` / `OPENEMR_OAUTH_PASSWORD`: `admin` / `pass` (the
  documented local dev default). If password-grant login fails with
  `invalid_grant`, the `users_secure` hash may have drifted from that default
  on your instance -- reset it directly for local dev only:
  `UPDATE users_secure SET password='$(php -r "echo password_hash('pass', PASSWORD_BCRYPT, ['cost'=>12]);")' WHERE username='admin';`
- `ANTHROPIC_API_KEY`: required, no default.
- `OPENEMR_VERIFY_TLS=false` (self-signed local dev cert only).

### 3. (Optional but recommended) bring up Langfuse

```bash
cd docker
openssl rand -hex 32  # run 3x for ENCRYPTION_KEY, NEXTAUTH_SECRET, and a public/secret key pair
```

Fill in `docker/.env.langfuse` (git-ignored, never commit it) with those
values plus `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` (any
`pk-lf-...` / `sk-lf-...`-shaped strings) and `LANGFUSE_INIT_USER_EMAIL` /
`_PASSWORD`, then:

```bash
docker compose -f docker-compose.langfuse.yml --env-file .env.langfuse up -d
```

Web UI: http://localhost:3300 (login with the `LANGFUSE_INIT_USER_*` values).
Put the generated public/secret key pair into `clinical-copilot/.env`'s
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`. If left blank, the app still
runs and still logs everything locally (structured JSON to stdout) -- it just
skips sending to Langfuse.

## Running it

```bash
uvicorn app.main:app --reload --port 8420
```

```bash
curl -X POST http://localhost:8420/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "Give me a quick orientation on this patient.", "patient_id": "<a FHIR Patient id>"}'
```

Send a second request with the same `conversation_id` from the first
response to continue the conversation.

## Running the eval suite

```bash
python3 -m evals.run_evals
```

Runs all 7 cases (`evals/cases.py`) against the live agent -- real Anthropic
calls, real FHIR calls. Prints a pass/fail table and writes full detail to
`evals/last_run_results.json` (git-ignored: it's a run artifact, not the
deliverable -- the suite code is the deliverable). Each case documents its
category (boundary / invariant / regression / adversarial, per
`ARCHITECTURE.md` 7.1) and the specific failure mode it guards against.

## Known gaps (stated honestly, not fixed under time pressure)

- **OAuth grant type.** `ARCHITECTURE.md` 1.3 calls for a `user/`-scoped
  token bound to the resident's own `authorization_code` session. This build
  uses the `password` grant with a single configured credential instead --
  still `user/`-scoped and still centrally audited (`architecture-audit.md`
  6.1-6.4), but NOT bound to an individual resident's session, so the "access
  ceiling no higher than the resident's own" property doesn't hold yet. See
  `app/auth.py`'s docstring. Migrating to `authorization_code` is the first
  thing to do before this touches real patients.
- **Source-attribution verification is a curated-vocabulary substring match**
  (`app/verification.py`), not full per-claim tagging. It will miss a
  clinical claim phrased outside `_MEDICATION_TERMS` / `_ALLERGY_TERMS` /
  `_CONDITION_TERMS`, and can't distinguish a positive assertion ("on
  Warfarin") from a negation ("not on Warfarin") -- both are exactly what the
  build prompt asked to defer past this pass.
- **Domain constraint / duplicate / empty-chart enforcement all assume a
  single active patient per turn** (the `patient_id` passed to `/chat`).
  Multi-patient conversations in one turn aren't handled.
- **`check_allergy_conflict` is a substring match**, not a drug-class
  knowledge base -- it will not catch e.g. amoxicillin against a documented
  penicillin allergy. Documented in `app/tools.py`.
- **Conversation state is in-memory**, single-process. Fine for this demo,
  not for more than one server instance.
- Only the two tools in scope for Early Submission are implemented
  (`get_patient_snapshot`, `check_allergy_conflict`) -- `get_recent_encounters`,
  `get_recent_observations`, `compare_signout_to_chart`, `summarize_shift_events`
  are not built yet, per the build prompt's explicit scope cut.
- No `/health` / `/ready`, no Bruno collection, no load tests, no alerts --
  all explicitly deferred to Final Submission per the build prompt.
- No OpenEMR module / chart UI entry point (`ARCHITECTURE.md` 1.1) -- `/chat`
  is the only interface, also explicitly deferred.
