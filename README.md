# minty-billing-api

Minty's subscription engine and payer-portal API — Django 5.2 + django-ninja on Python 3.13,
port **8004**. Part 2 of `Minty/docs/modernisation/modernisation_plan.md` moves the whole
subscription domain out of the Flask app into this service: the fifteen `/api/me/*` portal
routes, the module settings page's model and its nineteen actions, the dashboard notice, the
wizard's card and billing-account routes, the daily pass, the notification emails and the
Stripe writer. Flask keeps identity and the company until Part 3 and reads five subscription
facts through a read-only module; onboarding-backend proxies its money routes here.

**Status: step 1 of Part 2 — the skeleton.** Every route exists, is authenticated and answers
`501 {"error": "not_implemented"}`; the mirrors of all 21 tables are declared; the dark
contract, the auth rules and the guard tests are in place. Steps 2–3 port the engine
(`billing/services/`) and fill the routes. Verified 2026-09-21 on this workstation (Python 3.13.15
via `uv`, PostgreSQL 18): `pytest` 35 passed on SQLite (00:02) and on the Postgres built from
`01_schema_rebased.sql` (00:04); `ruff check .` clean; the `MINTY_DB_SCHEMA=pettycash_alt` guard
passes; Minty's `audit_models.py` reports 0 for this repo (a planted bogus column is found);
`runserver 8004` against the dev DB — `/healthz` 200, `/api/me/subscriptions` 404 with CORS dark and
401 live, `plans list` reads the catalog, `tick` no-ops; `pytest e2e` 6 passed dark and 7 live
(the identity-dependent ones with a real dev-DB user).

## The three rules

1. **Verifies, never mints.** Flask mints the module JWT (`_generate_module_token`); this
   service checks its signature with the shared `SECRET_KEY` and looks the user up. There is
   no refresh endpoint: a lapsed token goes back through Flask's login-gated
   `GET /handoff/minty-web?next=…`. `core/auth.py`.
2. **No migrations, `managed = False` everywhere.** Alembic in Minty owns the schema
   (`MINTY_DB_SCHEMA`, default `pettycashv3`); this repo has no `migrations/` directory and
   `docker/entrypoint.sh` runs no `migrate`. A needed column is a Minty revision *and* a plan
   amendment. `shared_models/models.py`, pinned by `billing/tests/test_models_guard.py` and
   Minty's `docs/schema/generators/audit_models.py`.
3. **The single Stripe writer.** Only this service holds `STRIPE_SECRET_KEY` /
   `STRIPE_PUBLISHABLE_KEY`; `billing/services/stripe_client.py` is the one module that
   imports `stripe`, `billing_gateway.py` the one that charges. Flask's copy goes in step 5.

## The dark contract

`SUBSCRIPTION_ENABLED` is **off unless set** — production cuts over with subscriptions dark.
Off, every path but `/healthz` and `/api/openapi.json` answers `404 {"error": "not_found"}`
**with CORS headers** (so the browser apps read "not there", not a CORS failure), the
scheduler does not start whatever its own switch says, `manage.py subscriptions tick` exits 0
having done nothing, and `revoke-ungranted` refuses. Switching it on writes nothing — no
grant, no trial, no revocation; `revoke-ungranted` is the separate launch-day command.
`core/middleware.py::SubscriptionsDarkMiddleware`, pinned by `billing/tests/test_dark.py`
and `e2e/`.

## The API

| Router | Mounted at | Auth | What |
|---|---|---|---|
| `me` | `/api/me` | `SelfBearerAuth` (person; token may be unscoped) | the payer portal — subscriptions, subscriber options, invite-admin (forwarded to Flask), transfers, invoices, cards |
| `modules` | `/api/entities/{id}/modules[/{action}]` | `EntityBearerAuth` (a company is required) + `MODULE_VIEW` / `MODULE_MANAGE` + payer rule | the module settings page model and its 19 actions |
| `notice` | `/api/entities/{id}/subscription-notice` | `EntityBearerAuth` | the notice the payment module's landing page shows |
| `onboarding` | `/api/onboarding/*` | `BearerAuth` (the wizard names its company in the body) | the wizard's 9 card/billing routes + `POST /trials/start` (finalize; must not fail silently — a failure fails finalize, the wizard offers Try again, both halves idempotent) |

Paths and JSON are Flask's byte for byte (`billing/tests/test_contract.py` lists them);
bodies say `{"error": …}` with Flask's status codes (`core/exceptions.py`).

## The scheduler (in-process, by decision)

`billing/scheduler.py` is the port of Minty's `services/app_runtime/scheduler.py`: an
APScheduler thread started from `billing.apps.ready()` in the web process only, a FULL pass
at `SUBSCRIPTION_SCHEDULER_FULL_HOUR` (05:00 Hong Kong) and a LIGHT pass every other hour,
gated by `SUBSCRIPTION_ENABLED` **and** `SUBSCRIPTION_SCHEDULER_ENABLED`. Two gunicorn
workers start two timers; the pass's Postgres advisory lock (`daily.daily_lock`) lets one run.
Known costs, kept on purpose until Part 3's Terraform: the job store is in memory, so a
restart slips the next light pass by up to an hour and **a deploy after 05:00 HKT loses that
day's full pass** (no unscoped sweep, no dunning retries until tomorrow); a paused instance
runs nothing. `manage.py subscriptions tick` is what a Render Cron Job will call instead —
the pass does not care who calls it.

## Run it

```bash
uv venv .venv --python 3.13                             # uv fetches CPython 3.13 if only 3.11 is installed
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\activate                                  # (or python -m venv with a 3.13 on PATH, then pip)
copy .env.example .env                                  # SECRET_KEY = Minty's; POSTGRES_* = the dev DB
python manage.py runserver 8004
curl http://localhost:8004/healthz                      # {"status":"ok","service":"minty-billing-api"}
curl -i http://localhost:8004/api/me/subscriptions      # 404 not_found while dark; 401 when live
python manage.py plans list                             # the catalog - proves DB + schema
python manage.py subscriptions tick                     # no-op while dark
```

In the docker stack (`Minty/docker/stack`) it is the `billing-api` service on host port 8004.

## Test it

```bash
pytest                                        # unit suite on SQLite (tables from the models)
set MINTY_TEST_PG_URI=postgresql://postgres:***@localhost:5432/postgres
set MINTY_REPO=C:\Github\Minty
pytest                                        # the same suite on a Postgres built from 01_schema_rebased.sql
set MINTY_DB_SCHEMA=pettycash_alt && pytest billing/tests/test_schema_name.py   # the name is a setting
ruff check .
pytest e2e                                    # HTTP smoke against a RUNNING service - e2e/README.md
```

Report the run time (mm:ss) of every suite with its result. `billing/tests/conftest.py`
blocks the network: a test that needs Flask or Stripe stubs the transport.

## Layout

```
config/          settings (the two switches, CORS, DB, Stripe keys, mail, logging) · settings_test · urls (the four routers, /healthz)
core/            auth (BearerAuth, SelfBearerAuth, EntityBearerAuth) · exceptions ({"error"} shape) · middleware (dark gate, request log) · policy (roles/permissions) · flask_client (the ONLY caller of Flask) · log_formatters
shared_models/   the 21 mirrors, managed = False · enums (the Postgres enums) · fields (PgEnumField, CharNField)
billing/         api/ (me, modules, notice, onboarding - stubs) · services/ (the engine, step 2) · scheduler.py · management/commands/{subscriptions,plans}.py · tests/
templates/email/ subscription_{notice,receipt}.html arrive in step 2 (Jinja2, verbatim from Flask)
e2e/             HTTP smoke tests against a live service
docker/          entrypoint (waits for DB + schema; no migrate)
docs/features/   README · authentication.md · subscriptions-api.md (the route-by-route map and what each step fills)
```

## Verifying the skeleton (done 2026-09-21; rerun after any change)

`pytest` green on SQLite; `MINTY_TEST_PG_URI=… MINTY_REPO=C:\Github\Minty pytest` green (the
harness builds `01_schema_rebased.sql`; a mirror column the schema lacks fails on the
SELECT); `MINTY_DB_SCHEMA=pettycash_alt pytest billing/tests/test_schema_name.py` passes;
`ruff check .` clean; `manage.py runserver 8004` → `/healthz` 200 and `/api/me/subscriptions`
404 with `Access-Control-Allow-Origin`; `AUDIT_REPOS=minty-billing-api python
Minty/docs/schema/generators/audit_models.py` reports 0.

## Cross-cutting rules (from the plan; every repo carries them)

1. `SECRET_KEY` identical across every Python service; Flask is the only minter — this service verifies only.
2. `Minty/migrations` is the only DDL owner until Part 3's `minty-db`; no service declares a managed model for a `pettycashv3` table.
3. Outbound calls to another service go through one client module per service (`core/flask_client.py`).
4. The schema name is a setting (`MINTY_DB_SCHEMA`), never a literal; `billing/tests/test_schema_name.py` fails on any other spelling.
6. Ports: `800d` for the `-api`, `300d` for the `-web` — billing is `d = 4`: this service 8004, its pages in `minty-web` 3002.
8. `MAINTENANCE_MODE` will be honoured from Part 3 step 2 (the shared packages).
9. Only this service holds the Stripe keys.
