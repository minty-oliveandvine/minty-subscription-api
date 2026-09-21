# Subscriptions API — what the service owns, route by route

The subscription engine and its API, moved out of the Flask app in Part 2 of
`Minty/docs/modernisation/modernisation_plan.md`. This page is the map of the service as it
stands (**step 1: every route exists and answers `501 not_implemented`**) and of what each later
step fills in. Minty's `docs/features/modules-and-subscriptions.md` describes the Flask original,
which this is a 1:1 port of; when a rule there and here disagree, the Flask page is the spec
until step 2 lands and this page becomes it.

## 1. What a person gets

- **The payer portal** (`minty-web`, `/subscription/*`): the companies I pay for and who pays
  for each, a change of subscriber (offer, accept, decline, withdraw), my billing accounts and
  saved cards, my invoices. Fifteen `/api/me/*` routes.
- **A company's module settings page** (`minty-web`, `/subscription/entities/{id}/modules`):
  the two module cards (Petty Cash, Payment Request) with their state, and the nineteen actions —
  start a trial, buy, restart, cancel, retry a payment, change the card, manage billing. One page
  model plus one action endpoint.
- **The notice** a dashboard shows for a company (trial ending, past due, cancelled): one route,
  read by billing-frontend's landing page and by Flask's dashboard server-side.
- **The wizard's money steps** (onboarding step 8/9): capturing a card, choosing a billing
  account, authorising billing, and — on All Set — starting the trial. Nine routes proxied here
  by onboarding-backend plus `POST /api/onboarding/trials/start`.
- **The daily pass**: trials close, renewals are raised, failed payments retried, access swept,
  trial-ending notices sent. Nobody calls it; the in-process scheduler does.

## 2. The API

Mounted by `config/urls.py`; auth per router in `docs/features/authentication.md`. Bodies are
`{"error": "<sentence>"}` with Flask's status codes (`core/exceptions.py`): 400 bad input, 401
no/invalid token, 402 card declined, 403 not allowed / no consent, 404 not yours or dark, 409
already done, 5xx upstream. `billing/tests/test_contract.py` pins every table below.

### `me` — the payer portal (`/api/me`, `SelfBearerAuth`)

| Method | Path | Flask origin (`routes/portal.py`) | Answers |
|---|---|---|---|
| GET | `/subscriptions` | `my_subscriptions_api` | the payer, their billing anchor/paid-through, a page of companies with module status per company; `q`, `sort`, `direction`, `page`, `per_page` |
| GET | `/subscriptions/subscriber-options` | `my_subscriber_options_api` | who a company's bill could move to (admins), each with a quote and inherited trials; blockers; the pending transfer |
| POST | `/subscriptions/invite-admin` | `my_invite_admin_api` | **forwarded to Flask** `POST /api/onboarding/invite` (the invitation is Flask's) |
| POST | `/subscriptions/transfer` | `my_transfer_initiate_api` | offer a company's billing to another admin |
| POST | `/subscriptions/transfer/respond` | `my_transfer_respond_api` | accept (charges the quoted amount) or decline |
| POST | `/subscriptions/transfer/cancel` | `my_transfer_cancel_api` | withdraw an offer |
| GET | `/subscriptions/transfers` | `my_transfers_api` | offers made TO me |
| GET | `/invoices` | `my_invoices_api` | my invoices, newest first; `entity`, `page`, `per_page` |
| GET | `/billing/payment-methods` | `my_payment_methods_api` | my saved cards and the default |
| POST | `/billing/payment-methods/setup-intent` | `my_payment_method_setup_intent_api` | a Stripe SetupIntent + the publishable key |
| POST | `/billing/payment-methods/confirm` | `my_payment_method_confirm_api` | save the confirmed card, optionally as default |
| POST | `/billing/payment-methods/default` | `my_payment_method_default_api` | change the default |
| GET / POST | `/billing/entity-payment-method` | `my_entity_payment_method_api` | which card a company is billed to; nominate one |
| POST | `/billing/payment-methods/update` | `my_payment_method_update_api` | expiry, name, address |
| POST | `/billing/payment-methods/remove` | `my_payment_method_remove_api` | detach a card |

### `modules` — the module settings page (`/api/entities`, `EntityBearerAuth`)

| Method | Path | Flask origin | Answers |
|---|---|---|---|
| GET | `/{entity_id}/modules` | the Jinja page (`templates/entity/partials/module_*.html`) | the page model: cards with state, summary, panel, next payment, `can_manage_modules`, payer, consent-takeover prompt |
| POST | `/{entity_id}/modules/{action}` | `POST /entity/settings/module/<org_id>/<action>` (`entity/routes/settings.py` 1419–2431) | one of the nineteen actions below, JSON body per action |

Actions (`billing/api/modules.py::ACTIONS`): `checkout`, `authorize-billing`, `payment-methods`,
`payment-methods/setup-intent`, `payment-methods/confirm`, `payment-methods/default`,
`restart-quote`, `restart-billing`, `confirm-billing`, `checkout-complete`, `start-trial`,
`resume-preview`, `subscribe-preview`, `cancel-preview`, `retry-payment`, `cancel`,
`payment-method`, `renew`, `manage-billing`. Reading needs `MODULE_VIEW`, acting needs
`MODULE_MANAGE` **and** the payer rule (`store.may_manage_subscription`).

### `notice` (`/api/entities`, `EntityBearerAuth`)

`GET /{entity_id}/subscription-notice` — Flask's `/api/entity/<id>/subscription-notice`
(`entity/routes/modules.py`), the plural being the one path change; keeps `settings_path`.

### `onboarding` (`/api/onboarding`, `BearerAuth`; the company is in the body)

| Method | Path | Flask origin (`entity/routes/create.py`) |
|---|---|---|
| GET | `/payment-method` | `onboarding_payment_method_status` |
| POST | `/payment-method/setup` | `onboarding_payment_method_setup` |
| POST | `/payment-method/complete` | `onboarding_payment_method_complete` |
| GET | `/billing/payment-methods` | `onboarding_billing_payment_methods` |
| POST | `/billing/payment-methods/setup-intent` | `onboarding_billing_setup_intent` |
| POST | `/billing/payment-methods/confirm` | `onboarding_billing_confirm` |
| POST | `/billing/payment-methods/default` | `onboarding_billing_set_default` |
| GET / POST | `/billing/accounts` | `onboarding_billing_accounts` |
| POST | `/billing/authorize` | `onboarding_billing_authorize` |
| POST | `/trials/start` | **new** — `{entity_id} → {trial_end}`; what finalize calls |

**`trials/start` must not fail silently** (decision 2026-09-21): onboarding-backend's native
`finalize` flips the company live and then calls this; a failure here fails finalize, the All
Set screen offers *Try again*, and both halves are idempotent — a company already live stays
live, a trial already started is returned, never duplicated.

### Everything else

`GET /healthz` (liveness, no database; answers while dark) and `GET /api/openapi.json` (the
contract; answers while dark). Nothing else is public.

## 3. The dark contract

`SUBSCRIPTION_ENABLED` off (the default, and production's state from the cutover to launch):
every path above but the two open ones answers `404 {"error": "not_found"}` **with CORS headers**,
before authentication, with or without a token; the scheduler does not start whatever its own
switch says; `manage.py subscriptions tick` and every other job exit 0 having done nothing;
`revoke-ungranted` refuses with a `CommandError` so nobody believes access was revoked. Switching
it on writes nothing. `core/middleware.py::SubscriptionsDarkMiddleware`; `billing/tests/test_dark.py`;
`e2e/test_smoke.py::TestDark`. Launch day (plan step 8b) switches the API on **first**, then the
web apps, then Minty and onboarding-backend.

## 4. The data — what this service writes

Thirteen tables, this service the only writer once live (`shared_models/models.py`, section K
of `Minty/docs/schema/01_schema_rebased.sql`):

| Table | Holds |
|---|---|
| `billing_plan` | the price catalog (`code` = module or the bundle `BILL+PETTY_CASH`; minor units) |
| `billing_policy` | the singleton of windows: trial days, post-cancel access days, past-due window, retry offsets `1..13` |
| `payer_billing_group` | a billing ACCOUNT: payer, the card it charges, `paid_through`, dunning state |
| `billing_account_payment_method` | the account's cards, one default |
| `entity_billing_group` | which account pays for a company (one payer per company) |
| `entity_billing_consent` | a member's consent to be billed for a company |
| `entity_module_subscription` | THE subscription: company × module, phase, payer, `app_access_until`, trial/billing dates |
| `user_stripe_customer` | a person's Stripe customer, billing anchor, currency |
| `subscription_invoice` / `_line` | invoices raised on an account and each company's share; `idempotency_key` is the double-charge guard |
| `subscription_transfer` | change-of-subscriber offers and their outcome |
| `subscription_audit_log` | every state change, before/after, actor |
| `subscription_email_log` | the dedup ledger of the notification emails |

**The one write outside the domain:** `entity_function_map.is_enabled` / `enabled_at` /
`disabled_at` — the projection Flask's per-request module gate reads
(`blueprints/entity/routes/modules.py::_is_module_enabled`). Written only through
`billing/services/entity_modules.py` (step 2), never for a company still `onboarding`, never
`created_by`, never while dark. Rows must be byte-identical to Flask's
`entity/services/modules._write_pairs`.

Read-only mirrors: `user`, `user_entity`, `entities`, `entity_function`, `country_info`,
`currency_info`, `invitation`. No `migrations/`, `managed = False` everywhere;
`billing/tests/test_models_guard.py` and Minty's `audit_models.py` (four repos) keep it so.

## 5. The daily pass and the scheduler

`billing/scheduler.py` — the port of Minty's `services/app_runtime/scheduler.py`, in-process by
decision until Part 3's Terraform. FULL pass at `SUBSCRIPTION_SCHEDULER_FULL_HOUR` (05:00
`Asia/Hong_Kong`): close trials, raise renewals, retry dunning, notify trial-ending, sweep access
for everyone. LIGHT pass every other hour: close trials and raise renewals, sweep the payers
touched. Gated by `SUBSCRIPTION_ENABLED` **and** `SUBSCRIPTION_SCHEDULER_ENABLED`; started from
`billing.apps.ready()` in the web process only (never from a management command, the test
runner or the autoreloader parent). Two gunicorn workers → two timers → the pass's advisory lock
(`daily.daily_lock`, step 2) lets one run. Known costs, kept on purpose: an in-memory job store,
so a restart slips the next light pass by up to an hour and **a deploy after 05:00 HKT loses that
day's full pass**; a paused instance runs nothing.

`manage.py subscriptions <job>`: `tick` (what a cron will call — full at the full hour, light
otherwise), `run-daily --mode full|light [--issue]`, `close-trials`, `run-renewals`,
`retry-dunning`, `notify-trial-ending`, `sweep-access`, `reconcile-customers`,
`revoke-ungranted [--apply]` (launch day; refuses while dark). `manage.py plans list` reads the
catalog and works dark — the quickest proof the service reaches the database.

## 6. Money and mail

Stripe: only this service holds `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` (cross-cutting
rule 9); `billing/services/stripe_client.py` will be the one `import stripe`,
`billing_gateway.py` the one charger (Invoices, idempotency keys claimed before the charge).
`test_models_guard.py` allows the import in that one module only. The setup-Checkout
`success_url` and the billing-portal `return_url` become minty-web pages.

Mail: Django `EMAIL_*` on the same Brevo SMTP Minty uses, sender `SUBSCRIPTION_EMAIL`; without
`EMAIL_HOST` every send is written to the console and skipped, never failing the pass. The two
templates (`templates/email/subscription_{notice,receipt}.html`) come over verbatim on the
Jinja2 backend in step 2; dedup stays in `subscription_email_log`. Links in emails point at
minty-web through Flask's login-gated re-handoff.

## 7. Configuration

`.env.example` is the list. The two switches; `SECRET_KEY` (shared, verify only); `POSTGRES_*` /
`DB_*` + `MINTY_DB_SCHEMA` (default `pettycashv3`, a setting never a literal —
`billing/tests/test_schema_name.py`); `STRIPE_*`; `EMAIL_*` / `SUBSCRIPTION_EMAIL`;
`FLASK_APP_URL` (the forwarded call and the re-handoff links); `MINTY_WEB_URL` /
`PAYMENTS_WEB_URL` / `CORS_ALLOWED_ORIGINS` (the two browser origins; `x-entity-id` is allowed).
In the docker stack it is the `billing-api` service on 8004.

## 8. Where it is tested

`billing/tests/`: `test_contract.py` (the tables above and the OpenAPI document), `test_dark.py`,
`test_auth.py`, `test_models_guard.py`, `test_schema_name.py`, `test_settings_guard.py` — on
SQLite (`pytest`) and on a Postgres built from `01` (`MINTY_TEST_PG_URI` + `MINTY_REPO`).
`e2e/test_smoke.py` against a running service, dark and live (`e2e/README.md`). The engine's
own tests arrive with step 2: the ported `Minty/tests/test_subscription_*` family and
`scripts/subscription/replay_scenarios.py`, whose output must match the Flask run on the same
fixture database — the golden test of the port.

## 9. What arrives when

| Step | Lands here |
|---|---|
| 2 | `billing/services/` — the 1:1 port of `blueprints/subscription/services/` in dependency order, `entity_modules.py`, the templates, the ported tests, the replay golden test; the job bodies behind `manage.py subscriptions` |
| 3 | the routers filled (each stub replaced by the port of its Flask view), `docs/openapi.json` committed, the invite-admin forward, Flask's server-side notice fetch |
| 5 | Flask's copies deleted; onboarding-backend proxies here; the Stripe keys leave Flask |
| 7 | deployed dark beside the phase-C builds at the cutover; 8b switches it on |
