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
`docker/development-easy`, enable the client, grant it the password grant
type (both disabled by default -- see `architecture-audit.md` 6.1 and
"Known gaps" below), point its `redirect_uri` at this app's real `/callback`
(the registration above used a placeholder), and add `user/Encounter.read`
and `user/Observation.read` to its allowed `scope` (needed for the Phase 1
sensitivity-filter work and UC2's get_recent_observations respectively --
see ARCHITECTURE.md 3.3, the FHIR API omits Encounter's sensitivity field
entirely; and OpenEMR won't grant a scope a client isn't registered for
regardless of what the consent screen shows):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET is_enabled=1, grant_types='authorization_code|password|refresh_token', \
   redirect_uri=CONCAT(redirect_uri, '|http://localhost:8420/callback'), \
   scope=CONCAT(scope, ' user/Encounter.read user/Observation.read') \
   WHERE client_id='<your client_id>';"
```

Retrofitting an existing client that's missing just `user/Observation.read`
(e.g. it already has `Encounter.read` from an earlier setup):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET scope=CONCAT(scope, ' user/Observation.read') \
   WHERE client_id='<your client_id>' AND scope NOT LIKE '%user/Observation.read%';"
```

Password grant stays enabled for one reason: the automated eval suite
(`evals/run_evals.py`) has no browser to complete an interactive login, so it
authenticates with a fixed service credential -- that's an intentional,
scoped decision for offline/CI-style regression runs only. **The live
`/chat`/`/ui` path never uses it** -- every resident-facing request is
scoped to whichever real login completed `authorization_code` (Phase 1,
`CLAUDE_CODE_BUILD_INSTRUCTIONS.md`; see `app/oauth_session.py`).

### 2. Set the remaining `.env` values

- `OPENEMR_OAUTH_USERNAME` / `OPENEMR_OAUTH_PASSWORD`: `admin` / `pass` (the
  documented local dev default). If password-grant login fails with
  `invalid_grant`, the `users_secure` hash may have drifted from that default
  on your instance -- reset it directly for local dev only:
  `UPDATE users_secure SET password='$(php -r "echo password_hash('pass', PASSWORD_BCRYPT, ['cost'=>12]);")' WHERE username='admin';`
- `ANTHROPIC_API_KEY`: required, no default.
- `OPENEMR_VERIFY_TLS=false` (self-signed local dev cert only).
- `COPILOT_BASE_URL`: where this app itself is reachable -- defaults to
  `http://localhost:8420`, matching the `redirect_uri` registered above.
  Must be updated (and the client's registered `redirect_uri` updated to
  match) if you run this somewhere else, e.g. the droplet.
- `OPENEMR_SCOPE_TEST_USERNAME` / `OPENEMR_SCOPE_TEST_PASSWORD`: optional.
  A dedicated low-privilege demo account (`copilot_resident_1`, ACL group
  `clin`, Provider off, no High-sensitivity grant -- mirrors `clin_1` per
  `audit-notes.md`, created the same way via the real Add User admin form)
  used only by `evals/cases.py`'s `oauth_scope_enforcement_denied` case and
  for manually verifying scope restriction against a real
  `authorization_code` token. Never a real resident's credential; the eval
  case skips (not fails) if unset.

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

`/chat` requires a real OpenEMR login (Phase 1, `CLAUDE_CODE_BUILD_INSTRUCTIONS.md`) -- there is no
more anonymous/shared-credential access. Open http://localhost:8420/ui in a browser, click **Log in
with OpenEMR** (`admin`/`pass`, or any real account), and use the chat form; the page handles the
session cookie for you and keeps `conversation_id` across turns automatically.

A bare `curl -X POST /chat` with no session cookie now gets a clean 401 by design:

```bash
curl -i -X POST http://localhost:8420/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "Give me a quick orientation on this patient.", "patient_id": "<a FHIR Patient id>"}'
# HTTP/1.1 401 Unauthorized
```

To drive `/chat` from curl/scripts instead of `/ui`, complete the browser login once, capture the
`copilot_session` cookie your browser's dev tools shows after `/callback` redirects, and pass it as
`-H "Cookie: copilot_session=<value>"` on subsequent requests -- the session lives in-memory on the
server for as long as the process runs (see `app/oauth_session.py`).

## Running the eval suite

```bash
python3 -m evals.run_evals
```

Runs all 9 cases (`evals/cases.py`) against the live agent -- real Anthropic
calls, real FHIR calls. Prints a pass/fail table and writes full detail to
`evals/last_run_results.json` (git-ignored: it's a run artifact, not the
deliverable -- the suite code is the deliverable). Each case documents its
category (boundary / invariant / regression / adversarial, per
`ARCHITECTURE.md` 7.1) and the specific failure mode it guards against.
`oauth_scope_enforcement_denied` (Phase 1) records a labeled `SKIP`, not a
pass or failure, if `OPENEMR_SCOPE_TEST_USERNAME`/`_PASSWORD` aren't set.

## Bruno API collection

`bruno/` is a [Bruno](https://www.usebruno.com/) collection covering
`/health`, `/ready`, and 7 proven `/chat` scenarios pulled directly from
the Golden Set / behavioral coverage work, for manual poking at either
instance without writing curl by hand.

Open the `bruno/` folder in the Bruno app, pick an environment (`local` =
`http://localhost:8420`, `droplet` = `http://157.230.11.142:8420`), and
run any request. Each request's `docs` tab explains what the scenario is
proving and what a correct response looks like.

**Environments:**
- `local` — talks straight to a `uvicorn` instance on the host.
- `droplet` — points at the droplet's public IP. As of 2026-09-17, this is
  genuinely externally reachable: `uvicorn` is bound to `0.0.0.0:8420` and
  `ufw allow 8420/tcp` is in place, confirmed with a real curl from
  outside the droplet (not just SSH-local), so Bruno's `droplet`
  environment can be pointed at it directly, no SSH tunnel needed.
  **Security note, worth knowing before using this:** `/chat` on that port
  has no authentication at all -- anyone who finds the port can trigger
  real Anthropic API calls (real cost) against it. Fine for a graded
  submission's reachability requirement, but not something to leave open
  indefinitely without at least considering an allowlist or rate limit.

**Requests:**

| # | Request | Scenario |
|---|---------|----------|
| health/01 | Health Check | Liveness -- always 200 if the process is up |
| health/02 | Readiness Check | Per-dependency check (OpenEMR, Anthropic, Langfuse); 503 if any are down |
| chat/01 | Normal Orientation (pid1) | Grounded, multi-fact summary + duplicate-record warning |
| chat/02 | Empty Chart (pid3) | True negative -- chart genuinely has nothing, must be reported honestly |
| chat/03 | Malformed Patient ID | Graceful failure, no crash, still HTTP 200 |
| chat/04 | Allergy Hard-Block (pid1) | Domain-constraint hard block on an uncoded/free-text allergy |
| chat/05 | Adversarial Hallucination Bait (pid1) | Asks about a med the patient isn't on -- no fabricated dose/date reaches the user, whether by refusal or by the verification layer stripping it |
| chat/06 | Duplicate Record Stale Signout (pid6) | Cross-checks a stale verbal sign-out against the live chart across a known duplicate record pair |
| chat/07 | Ambiguous Query (pid1) | Underspecified question with no prior turn context -- known to have some run-to-run variance |

`bru` (the Bruno CLI) was not installed in this environment, so these were
verified by hand: every request's JSON body was run as an equivalent curl
call against both the local instance and the droplet (via SSH), and each
response was confirmed to match its documented expectation before this
collection was committed.

## Known gaps (stated honestly, not fixed under time pressure)

- **OAuth grant type -- RESOLVED for live traffic (Phase 1,
  `CLAUDE_CODE_BUILD_INSTRUCTIONS.md`).** `/chat`/`/ui` now use a real
  `authorization_code` login bound to whichever resident actually
  authenticates (`app/oauth_session.py`), matching `ARCHITECTURE.md` 1.3.
  Password grant still exists in `app/auth.py` but is used only by the
  automated eval suite (`evals/run_evals.py`), which has no browser to
  complete an interactive login -- that's a deliberate, scoped exception
  for offline regression runs, not a live-traffic path.
- **Cross-provider patient access is a separate, still-open OpenEMR ACL
  gap** that the `authorization_code` migration does not fix, and can't --
  `audit-notes.md` confirmed live in the UI that a physician (`dr_1`) could
  fully open, edit, and create encounters on another provider's patient
  (pid 4, admin's). Because that's a platform-level, UI-visible gap (not
  something the FHIR API adds on top of), a resident-scoped token's access
  ceiling is only as good as what OpenEMR's own ACL already grants that
  resident directly -- which today includes other providers' patients.
  Named honestly rather than silently tested around; a genuine future
  threat-model item.
- **The FHIR Encounter resource carries no sensitivity field at all** --
  confirmed by both code read and a live test (`architecture-audit.md`
  6.9): a real `authorization_code`-obtained `user/`-scoped token's
  `Encounter` search returns a known `sensitivity='high'` test encounter in
  full, same as a `system/`-scoped one. The compensating filter
  (`app/sensitivity.py`) is real, load-bearing work, not a defensive
  no-op -- see `ARCHITECTURE.md` 3.3. It's built and unit-tested but not
  yet wired into a live tool, since `get_recent_encounters` (the tool that
  would call it) isn't built until a later phase.
- **Source-attribution verification is a curated-vocabulary substring match**
  (`app/verification.py`), not full per-claim tagging. It will miss a
  clinical claim phrased outside `_MEDICATION_TERMS` / `_ALLERGY_TERMS` /
  `_CONDITION_TERMS`, and can't distinguish a positive assertion ("on
  Warfarin") from a negation ("not on Warfarin") -- both are exactly what the
  build prompt asked to defer past this pass.
- **Domain constraint / duplicate / empty-chart enforcement all assume a
  single active patient per turn** (the `patient_id` passed to `/chat`).
  Multi-patient conversations in one turn aren't handled.
- **`check_allergy_conflict` catches direct name matches plus a small,
  curated cross-reactivity table** (`app/clinical_reference.py`, Phase 5) --
  it now catches amoxicillin against a documented penicillin allergy, but
  only for the classes actually curated there (penicillins today). It is
  explicitly not a drug-class knowledge base or a substitute for a
  production drug-interaction database (First Databank/Medi-Span/
  Multum-style) -- a proposed medication in an uncurated class still isn't
  caught. Documented in `app/tools.py` and `ARCHITECTURE.md` 3.2.
- **Conversation state is in-memory**, single-process. Fine for this demo,
  not for more than one server instance.
- All six planned tools are implemented: the two in scope for Early Submission
  (`get_patient_snapshot`, `check_allergy_conflict`), plus `get_recent_encounters`
  (Phase 6, UC2), `get_recent_observations` (UC2), `summarize_shift_events`
  (UC4), and `compare_signout_to_chart` (UC2), added afterward.
- No `/health` / `/ready`, no Bruno collection, no load tests, no alerts --
  all explicitly deferred to Final Submission per the build prompt.
- **`/chat` on the droplet (port 8420) no longer has the open, no-auth
  reachability previously documented here** -- superseded by Phase 1's
  `authorization_code` login requirement above. A bare `curl -X POST
  /chat` now gets a clean 401; a real OpenEMR login is required first.
  This removes the earlier accepted risk (anyone triggering real,
  cost-incurring Anthropic calls with zero auth) as a side effect of doing
  the auth migration properly, not as a separate fix.
- **The Bruno collection (`bruno/`) predates this change** and its saved
  `/chat` requests will now get 401s as-is -- they were built against the
  anonymous-access version of `/chat`. Each request needs a `copilot_session`
  cookie (captured from a browser after completing `/login`) added manually
  until the collection itself is updated to document/automate this;
  `/health` and `/ready` are unaffected, they never required auth.
- No OpenEMR module / chart UI entry point (`ARCHITECTURE.md` 1.1) yet. A
  minimal `GET /ui` chat page (plain HTML/CSS/JS, no build step, same
  FastAPI app/port) exists as a standalone grader convenience, now gated
  behind the same real OpenEMR login as `/chat` (see the top-level README)
  rather than being anonymously reachable. It does not replace the real
  chart-embedded module, which remains the actual planned next step per
  `ARCHITECTURE.md` 1.1.
