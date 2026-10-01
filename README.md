# minty-billing-api

Minty's subscription engine and payer-portal API — Django 5.2 + django-ninja on Python 3.13,
port **8004**. Part 2 of `Minty/docs/modernisation/modernisation_plan.md` moves the whole
subscription domain out of the Flask app into this service: the `/api/me/*` portal routes
(Flask's fifteen paths, plus `transfer/seen` and the four `billing/accounts` routes this service
added), the module settings page's model and its nineteen actions, the dashboard notice, the
wizard's card and billing-account routes, the daily pass, the notification emails and the
Stripe writer. Flask keeps identity and the company until Part 3 and reads five subscription
facts through a read-only module; onboarding-backend proxies its money routes here.

**Status: Part 2 step 3 done (2026-09-22) — the engine is ported and every router is live.**
Step 2 (2026-09-21) put the whole engine in `billing/services/`; step 3 filled the four routers
from Flask's views: `me` (the fifteen `/api/me/*` portal routes, Flask's shell and status codes
kept, `billing/api/_json.py` standing in for `jsonify`), `modules` (the page model minty-web
renders and the nineteen actions behind one gate), `notice` (with Flask's claimless-token
fallback) and `onboarding` (the nine wizard routes and the new `trials/start`, which fails
loudly). `docs/openapi.json` is the committed contract, held current by `test_contract.py`
(`manage.py export_openapi` regenerates it). Next: step 4 finishes minty-web's screens against
the live API, step 5 cuts Flask's copies. The mirrors of all 21 tables are declared; the auth
rules and the guard tests are in place. Subscriptions are always on: the dark switch
(`SUBSCRIPTION_ENABLED`) was removed 2026-10-01, once the service ran on a test site. Skeleton verified 2026-09-21 on this
workstation (Python 3.13.15
via `uv`, PostgreSQL 18): `pytest` 35 passed on SQLite (00:02) and on the Postgres built from
`01_schema_rebased.sql` (00:04); `ruff check .` clean; the `MINTY_DB_SCHEMA=pettycash_alt` guard
passes; Minty's `audit_models.py` reports 0 for this repo (a planted bogus column is found);
`runserver 8004` against the dev DB — `/healthz` 200, `/api/me/subscriptions` 401 without a
token, `plans list` reads the catalog; `pytest e2e` passed (the identity-dependent ones with a
real dev-DB user).

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

## Always on

Subscriptions are always on: every route answers, every `manage.py subscriptions` job runs.
The feature-wide dark switch (`SUBSCRIPTION_ENABLED` and its 404-everything middleware) was
removed on 2026-10-01 by the user's decision — the service runs on a test site, so there is
nothing left to keep dark. Removing it wrote nothing: no trial started, nothing granted or
revoked. The scheduler keeps its own switch, `SUBSCRIPTION_SCHEDULER_ENABLED`, which alone
decides whether the timer starts; `manage.py subscriptions revoke-ungranted` stays a
deliberate command, dry unless `--apply`.

## The API

| Router | Mounted at | Auth | What |
|---|---|---|---|
| `me` | `/api/me` | `SelfBearerAuth` (person; token may be unscoped) | the payer portal — subscriptions, subscriber options, invite-admin (forwarded to Flask), transfers, invoices, cards |
| `modules` | `/api/entities/{id}/modules[/{action}]` | `EntityBearerAuth` (a company is required) + `MODULE_VIEW` / `MODULE_MANAGE` + payer rule | the module settings page model and its 19 actions |
| `notice` | `/api/entities/{id}/subscription-notice` | `NoticeBearerAuth` (`EntityBearerAuth` plus Flask's fallback for a token that names no company) | the notice the payment module's landing page shows: `past_due` and a paid `pending_cancel` only (no trial notices since 2026-10-01); `settings_path` is Flask's `/handoff/minty-web` hand-over to the module page |
| `onboarding` | `/api/onboarding/*` | `BearerAuth` (the wizard names its company in the body) | the wizard's 9 card/billing routes + `POST /trials/start` (finalize; must not fail silently — a failure fails finalize, the wizard offers Try again, both halves idempotent) |

Paths and JSON are Flask's byte for byte (`billing/tests/test_contract.py` lists them);
bodies say `{"error": …}` with Flask's status codes (`core/exceptions.py`).

## The scheduler (in-process, by decision)

`billing/scheduler.py` is the port of Minty's `services/app_runtime/scheduler.py`: an
APScheduler thread started from `billing.apps.ready()` in the web process only, a FULL pass
at `SUBSCRIPTION_SCHEDULER_FULL_HOUR` (05:00 Hong Kong) and a LIGHT pass every other hour,
gated by `SUBSCRIPTION_SCHEDULER_ENABLED` alone. Two gunicorn
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
curl -i http://localhost:8004/api/me/subscriptions      # 401 without a token
python manage.py plans list                             # the catalog - proves DB + schema
python manage.py export_openapi                         # rewrite docs/openapi.json (--check in CI)
```

In the docker stack (`Minty/docker/stack`) it is the `billing-api` service on host port 8004.

## Test it

```bash
set MINTY_TEST_PG_URI=
pytest                                        # unit suite on SQLite (tables from the models); ~1120 tests, 00:10
set MINTY_TEST_PG_URI=postgresql://postgres:***@localhost:5432/postgres
set MINTY_REPO=C:\Github\Minty
pytest                                        # the same suite on a Postgres built from 01_schema_rebased.sql; 00:10
set MINTY_DB_SCHEMA=pettycash_alt && pytest billing/tests/test_schema_name.py   # the name is a setting
ruff check .
pytest e2e                                    # HTTP smoke against a RUNNING service - e2e/README.md
```

`settings.py` loads `.env`, so a `.env` that carries `MINTY_TEST_PG_URI` (the dev one does)
makes the harness the default for every `pytest`; the blank `set MINTY_TEST_PG_URI=` is what
gets the SQLite run back. Report the run time (mm:ss) of every suite with its result.
`billing/tests/conftest.py` blocks the network and `billing/tests/engine/conftest.py` makes an
unstubbed `stripe_client.get_stripe()` an assertion: a test that needs Flask or Stripe stubs
the transport. `billing/tests/engine/` is the ported `Minty/tests/test_subscription_*` family
(51 files, 762 tests, same names) and `billing/tests/api/` the HTTP halves of the same files
against the routers (step 3, importing the engine's stubs) - `docs/features/subscriptions-api.md`
§8 says how they differ and which halves are still to come.

## Layout

```
config/          settings (the scheduler switch, CORS, DB, Stripe keys, mail, logging) · settings_test · urls (the four routers, /healthz)
core/            auth (BearerAuth, SelfBearerAuth, EntityBearerAuth) · exceptions ({"error"} shape) · middleware (service scope, request log) · policy (roles/permissions) · flask_client (the ONLY caller of Flask) · log_formatters
shared_models/   the 21 mirrors, managed = False · enums (the Postgres enums) · fields (PgEnumField, CharNField)
billing/         api/ (me, modules, notice, onboarding - the four routers, live; _json.py = Flask's jsonify) · services/ (THE ENGINE: the 24 modules of Minty's blueprints/subscription/services ported 1:1, plus entity_modules.py, _context.py, _log.py, and this service's own invoice_document.py + invoice_pdf.py - the invoice PDF, Figma 09-A) · static/email/ (the 9 inline images) · static/invoice/ (the PDF's Inter and Noto Sans HK fonts with their OFL licences, and 09-A's logo vector) · scheduler.py · management/commands/{subscriptions,plans,replay_scenarios,export_openapi}.py · tests/ (+ tests/engine/, the ported suite; tests/api/, the route tests)
scripts/         replay_diff.py (Flask report vs Django report, normalised)
templates/email/ subscription_notice.html - Minty's, verbatim (Jinja2 backend; a render from either side is byte-identical)
e2e/             HTTP smoke tests against a live service
docker/          entrypoint (waits for DB + schema; no migrate)
docs/features/   README · authentication.md · subscriptions-api.md (the route-by-route map and what each step fills)
docs/openapi.json  the committed contract (= /api/openapi.json; manage.py export_openapi)
```

## Verifying it (skeleton 2026-09-21; the engine port the same day; rerun after any change)

`MINTY_TEST_PG_URI= pytest` green on SQLite (1122 passed, 2 Postgres-only lock tests skipped,
00:09); `MINTY_TEST_PG_URI=… MINTY_REPO=C:\Github\Minty pytest` green (1124 passed, 00:14; the
harness builds `01_schema_rebased.sql`; a mirror column the schema lacks fails on the SELECT);
`MINTY_DB_SCHEMA=pettycash_alt pytest billing/tests/test_schema_name.py` passes; `ruff check .`
clean; `manage.py runserver 8004` → `/healthz` 200 and `/api/me/subscriptions` 401 without a
token; `python Minty/docs/schema/generators/audit_models.py` reports 0
for all four repos. Against the dev database:
`manage.py subscriptions revoke-ungranted` (dry) and `run-renewals` (dry) answer the same as
`flask subscriptions revoke-ungranted` / `run-renewals` on the same database.

## The engine (Part 2 step 2)

`billing/services/` is `Minty/blueprints/subscription/services/` on the Django ORM, module for
module and function for function - same names, same signatures, same `(payload, status)`
return shapes - so step 3 fills each router by porting its Flask view one for one and step 5
deletes the Flask copies. The translation is mechanical and the exceptions are few:
`Model.query` → `Model.objects`, `db.session.get` → `store._by_pk`, per-helper commits →
autocommit with `transaction.atomic()` in exactly three kinds of place (`docs/features/
subscriptions-api.md` §6 - never around a Stripe call), `flask.g` → `billing.services._context`
(a ContextVar scope: opened by `core.middleware.ServiceScopeMiddleware` per request, by every
`manage.py subscriptions` job and by the scheduler's pass), `loguru` → `billing.services._log`
(the same `{}` messages), uuid columns hand back hyphenated **str** (`shared_models.fields.
MintyUUIDField`) because the services compare ids with `==`. `test_no_flask_imports.py` keeps
the Flask world out; the memory of what the port found is in `docs/features/subscriptions-api.md`.

**The second golden** is the replay harness: `manage.py replay_scenarios` is Minty's
`scripts/subscription/replay_scenarios.py` on this engine (same runs, same Stripe test clocks,
same report), and `scripts/replay_diff.py` compares its `--report` with the Flask script's after
normalising Stripe ids, the clone's entity tag, the anchor's timezone rendering and the order of
same-day invoices. On 2026-09-21 the eight Angelika runs were run on both sides against the dev
database (the Django runs cloned to fresh payers with `--as angelika.tardaguela+django-<key>@…
--tag <a tag of the same length>`, since Stripe remembers an idempotency key for 24 h) and every report diffed
IDENTICAL, and again after the `_needs_stripe_clock` fix in both scripts. The logs are not kept
(by decision) - `docs/features/subscriptions-api.md` §8 says how to regenerate a run. Mail from a
replay is skipped (console backend) unless `--notify-to` names a recipient.

## Cross-cutting rules (from the plan; every repo carries them)

1. `SECRET_KEY` identical across every Python service; Flask is the only minter — this service verifies only.
2. `Minty/migrations` is the only DDL owner until Part 3's `minty-db`; no service declares a managed model for a `pettycashv3` table.
3. Outbound calls to another service go through one client module per service (`core/flask_client.py`).
4. The schema name is a setting (`MINTY_DB_SCHEMA`), never a literal; `billing/tests/test_schema_name.py` fails on any other spelling.
6. Ports: `800d` for the `-api`, `300d` for the `-web` — billing is `d = 4`: this service 8004, its pages in `minty-web` 3002.
8. `MAINTENANCE_MODE` will be honoured from Part 3 step 2 (the shared packages).
9. Only this service holds the Stripe keys.
