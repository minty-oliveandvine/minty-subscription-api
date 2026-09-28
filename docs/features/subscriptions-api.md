# Subscriptions API — what the service owns, route by route

The subscription engine and its API, moved out of the Flask app in Part 2 of
`Minty/docs/modernisation/modernisation_plan.md`. This page is the map of the service as it
stands (**step 1: every route exists and answers `501 not_implemented`**) and of what each later
step fills in. Minty's `docs/features/modules-and-subscriptions.md` describes the Flask original,
which this is a 1:1 port of; when a rule there and here disagree, the Flask page is the spec
until step 2 lands and this page becomes it.

## 1. What a person gets

- **The payer portal** (`minty-web`, `/subscription/*`): the companies I pay for and who pays
  for each, a change of subscriber (offer, accept, decline, withdraw), my billing accounts —
  each a name, an email, its cards and the companies it pays for — and my invoices, per
  account, and any invoice's breakdown company by company. Twenty-one `/api/me/*` paths
  (twenty-two operations): Flask's fifteen, plus `transfer/seen`, the four
  `billing/accounts` routes and `invoices/{invoice_id}/breakdown`.
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
| GET | `/subscriptions` | `my_subscriptions_api` | the payer, their billing anchor/paid-through and **`next_billing` / `next_billing_iso`** (2026-09-25: the end of the anchor period now is in — the anchor is the FIRST charge and never moves, and the landing printed it as the next billing date), a page of companies with module status per company; `q`, `sort`, `direction`, `page`, `per_page`. **Plus `transfer_outcomes`** — how the caller's OWN offers ended (declined / expired / accepted) where they have not been shown yet, the only read of a finished transfer in the engine; `cancelled` is excluded (their own withdrawal, already answered by 07-K). Read advisorily, so a failure leaves it empty rather than taking the page down |
| GET | `/subscriptions/subscriber-options` | `my_subscriber_options_api` | who a company's bill could move to (admins), each with a quote and inherited trials; blockers; the pending transfer; and `paid_through`, what the COMPANY is paid up until (the screen's footer needs it whether or not there is a candidate to quote) |
| POST | `/subscriptions/invite-admin` | `my_invite_admin_api` | **forwarded to Flask** `POST /api/onboarding/invite` (the invitation is Flask's) |
| POST | `/subscriptions/transfer` | `my_transfer_initiate_api` | offer a company's billing to another admin — **any** admin, with a saved card or without: being asked is not being charged, so the card is required at the accept, not here (the offer-time refusal was dropped 2026-09-24) |
| POST | `/subscriptions/transfer/respond` | `my_transfer_respond_api` | accept or decline. Body `{transfer, accept, codes?}` — `codes` is the modules being taken on (07-D "Choose Modules"); anything the company holds that it does not name is **cancelled** as part of accepting, ending at the outgoing payer's `paid_through` so no extension is owed by anybody (the ordinary `cancel_module` cannot be used: its access end is `max(paid_through, now + paid_cancel_access_days)`, which books a real extension and trips the handover's own blocker). Omitted means the whole company; naming none is refused. **Accepting takes no money** when the window being bought has not started yet (the usual case — the outgoing payer has bought days nobody has used): the charge is parked on `subscription_transfer.collect_at` and taken by `collect-transfers` on that day, and **no `billed_through` claim is written** until it is collected. A window that has already lapsed is charged inline as before. Either way a card is required — refuses with "Add a payment method before taking over the billing." when there is none to nominate, leaving the offer pending |
| POST | `/subscriptions/transfer/seen` | — (**new; Flask had none**) | mark how one of MY offers ended as seen, `{transfer}`. The payer pressing Done on 07-I / A-07 / A-08. Stamps `subscription_transfer.outcome_seen_at` — from the click, not from the read that drew the modal and not from the email, which records that a message was SENT. Idempotent; the same answer for a transfer that does not exist and one that is not mine |
| POST | `/subscriptions/transfer/cancel` | `my_transfer_cancel_api` | withdraw an offer |
| GET | `/subscriptions/transfers` | `my_transfers_api` | offers made TO me |
| GET | `/invoices/{invoice_id}/breakdown` | — (**new**, 2026-09-25) | 08-B's "Billing Breakdown · Download csv": ONE invoice, company by company - a row per line it charged (the subscription, the monthly rate that line was priced at, the days it paid for, what was charged; a credit negative, a zero line left out). The days and rate are the ones the line RECORDED when it was issued (`subscription_invoice_line.period_start` / `period_end` / `unit_amount`, schema item 23, same day - written by whatever priced it: a renewal is the whole period at its plan's price; a mid-period start or upgrade, and the credit for the plan it replaced, run from the change to the period's end; a cancellation extension runs from the paid-through date to its access end at the rate the cancellation priced it at - the marginal step for a module leaving a bundle). An extension priced at two rates recorded none: its row shows the rate its days add up to. A line issued BEFORE then is read back from how its kind is priced (`portal.build_invoice_breakdown`) - an extension's end from the module's `app_access_until` while the row still holds it, left out (null) once a resume has cleared it, never guessed. Someone else's invoice, or a malformed id, is 404; the web writes the CSV |
| POST | `/invoices/{invoice_id}/retry` | — (**new**, 2026-09-28) | 08-B's *Retry payment* on a declined invoice's row (08-K): collect THIS invoice now, on its account's card (`billing_accounts.retry_invoice` → `dunning.retry_now(group_id=…, expect_invoice=…)` - the engine's own manual collection: the same attempt budget and give-up deadline as the scheduled retries, the period settled and access switched back on when paid). Pinned from the row: the CARD (the invoice's account; the payer's oldest for one from before accounts) and the INVOICE (charged only if it is the one the engine's rules pick - else `not_this_invoice`, nothing charged, no attempt spent). Refused before the engine unless the list marks it `retryable`. Answers `{ok, status, message}` in the module page's own words (`api/_retry.py`, shared with `retry-payment`): `paid`, `failed` (the processor's reason), `no_card`, `gave_up`, `nothing_owed`, `older_debt_only`, `not_this_invoice`, `not_collectable` (the processor will no longer collect it and it could not be re-issued automatically - nothing charged; §6). An invoice Stripe will no longer collect is RE-ISSUED and the replacement charged in the same press (§6). Someone else's invoice or a malformed id is 404; one not waiting for a payment is 409 - or `not_this_invoice` when it was re-issued since the page was drawn; a processor failure is 502 |
| GET | `/invoices` | `my_invoices_api` | my invoices, newest first; `entity`, `page`, `per_page`, and **`account`** (added 2026-09-25) — ONE billing account's invoices for 08-B; a pre-accounts invoice (no `billing_group_id`) belongs to the payer's OLDEST account, the attribution dunning already collects by; someone else's account matches nothing. Echoes `account_id`. Each row carries **`retryable`** (2026-09-28): whether *Retry payment* would charge it now - per card, the ONE open invoice `dunning.retry_now` picks (`portal.retryable_invoice_ids`, calling `dunning._manual_target` over our own rows: the current period's renewal, else an open mid-period charge, never an abandoned give-up bill), and nothing on a card past its give-up deadline or whose access has run out - with or without a dunning stamp, since giving up leaves `paid_through` where it stopped. Judged over ALL the payer's failed invoices, before the `account` / `entity` narrowing (narrowed first, a company's own charge could be offered while the engine would collect the renewal), from the rows the list already read; a list with nothing failed reads nothing more. No Stripe call, no writes |
| GET | `/billing/payment-methods` | `my_payment_methods_api` | my saved cards and the default |
| POST | `/billing/payment-methods/setup-intent` | `my_payment_method_setup_intent_api` | a Stripe SetupIntent + the publishable key |
| POST | `/billing/payment-methods/confirm` | `my_payment_method_confirm_api` | save the confirmed card, optionally as default. **Plus the onboarding twin's account fields** (2026-09-25): `billing_group_id` puts the card on one of my accounts (08-B "Add payment method"); `billing_company` + `billing_email` OPEN one ("New billing account") and both are then required. Both checked in the ROUTE, before the card is attached — the service only reaches them after Stripe holds it. A retry after a lost answer re-answers the account it opened rather than opening a second on the same card |
| POST | `/billing/payment-methods/default` | `my_payment_method_default_api` | change the default |
| GET / POST | `/billing/entity-payment-method` | `my_entity_payment_method_api` | which card a company is billed to; nominate one |
| POST | `/billing/payment-methods/update` | `my_payment_method_update_api` | expiry, name, address |
| POST | `/billing/payment-methods/remove` | `my_payment_method_remove_api` | detach a card. **`account?`** (2026-09-25) — the billing account whose page asked: its own charged card is refused in its words, and when the card is the Stripe customer's default it is handed to that account's card instead of refused (08-B has no button for the customer default) |
| GET | `/billing/accounts` | — (**new**) | my billing accounts, oldest first (the first is the one 08-A shows by default): each with `name` (the company it bills under, else me), `billing_company` / `billing_email` raw, `card` (the one it CHARGES, null when Stripe no longer holds it), `cards` (its shelf, `is_default` = THIS account's card), `address` (the charged card's Stripe billing address — accounts hold none), `companies` (`entity_id`, `entity_name`, `past_due`), `in_dunning`, `past_due`, and **`next_bill`** (2026-09-25, 08-B's "Amount (estimated)": `{amount, amount_minor, currency}` or null - what its next renewal will charge, priced by the renewal runner's own `build_renewal` for the period starting on the next billing date, with the trials that will have converted by then; `portal.next_bill_for_account`); plus the payer, ONE `next_billing` / `next_billing_iso` (every account renews on the payer's anchor), the flat wallet, and `countries` and **`publishable_key`** only with `?countries=1` (08-C, whose address form is Stripe's own `AddressElement`: the registry limits its countries, the key mounts it - null where this environment has no Stripe) |
| POST | `/billing/accounts/update` | — (**new**) | `{account, billing_company?, billing_email?, address?, cardholder?}` — 08-C. Validated first (company not blank, email shaped, each at most 255 characters; address needs line 1 and a registered country; `cardholder` - the name Stripe's address form asks for with it - at most 255), then the address and cardholder to the charged card at Stripe in ONE `billing_details` write, then the name — Stripe first because it is the write that fails |
| POST | `/billing/accounts/default-card` | — (**new**) | `{account, payment_method}` — the card the account CHARGES (08-B "Set as default", 08-N): both halves of the pair, and the Stripe customer default untouched |
| POST | `/billing/accounts/move` | — (**new**) | `{entity, account}` — "Change billing account": the company moves to another of my accounts; nothing is charged and its paid days travel (`store.nominate_group_for_entity`, source `moved`). Refused (409): a company on no account, a PAST-DUE company (its debt, its retries and "Pay now" follow the account it is on), a target in dunning, a target whose card is gone. Answers the accounts plus `moved` (null when it was already there) |

**Live since step 3 slice A (2026-09-21)** — `billing/api/me.py`, each view the port of its Flask
twin. What the views keep is Flask's shell: `400 {"error": "<field> is required"}` for a missing
routing id, `404 "That company isn't on your billing account."` when the read model answers
None (not-the-payer and no-such-company are the same answer), `422 {"error": <the service's
sentence>}` for a stated refusal (handovers, the invitation - 422 not 403 because the client
shows the server's words only when they read as prose), each route's own 500 copy for a
surprise, and `payment_methods.run`'s 200/409/422/500 for the wallet. What the framework does
instead: CORS on every answer including errors (`corsheaders`; a preflight is 200 where
Flask-CORS said 204, and `Vary: origin`), the OPTIONS answer, the dark 404, the bearer check.
`billing/api/_json.py` is `jsonify`: a `datetime`/`date` in a payload renders RFC 822 (`Thu, 06
Aug 2026 12:00:00 GMT` - `transfers._as_dict`'s `expires_at`, the subscriber screen's `since`;
the clients parse that form), `Decimal` as a string, and a body that is missing, not JSON or
not an object is `{}` (Flask's `get_json(silent=True) or {}`), so the answer is the
missing-field 400 and never a parse error. The invitation is the one forward: `send=lambda
invite: flask_client.forward(request, "/api/onboarding/invite", json=invite)` - the caller's own
bearer, Flask's non-2xx sentence back as the 422, Flask unreachable as the route's 500.

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

**Live since step 3 slice B (2026-09-21)** — `billing/api/modules.py`. The gate is one function
every request runs through (`_gate`): the path's company must be the one the token was checked
against (`X-Entity-Id`, else the claim; a mismatch is 403 "That token is for a different
company."), then the permission (403 with Flask's sentence), then for every action but
`checkout-complete` the payer rule (403 "Only the person who pays for this company can change
its subscription."). `checkout-complete` is Stripe's return leg and carries the permission
only — refusing it would strand a payment that has already happened. Flask pinned these rules
by reading the decorator stack with regexes (`test_subscription_payer_permission`,
`test_restart_billing_guard`); here every action is called as the wrong person
(`billing/tests/api/test_module_settings_api.py`). `payment-methods` and `restart-quote` also
answer GET, as Flask served them.

The page model (`GET /{entity_id}/modules`): `entity_id`, `entity_name`, `cards` (Flask's card
dicts key for key — minty-web's `ModuleCard` type — with `period_end` ISO and `access_end_date`
re-shaped to `YYYY-MM-DD` because the client counts days from them; `IsoJSONEncoder` for this
router, since Flask never served this page as JSON), `summary` and `panel` (opaque to the client
until its screens read them), `next_payment_date` (read off the panel), `can_manage_modules`
(admin AND payer, or no payer yet), `payer` (`{user_id, name, email}` when it is somebody
else, else null), `viewer` (`{name, initials}`), `consent_takeover`. Three deliberate
differences from Flask, each written where it happens: the Stripe return URLs point at
minty-web's page (`{MINTY_WEB_URL}/subscription/entities/{id}/modules`, `?session_id=
{CHECKOUT_SESSION_ID}` on the return, `&purpose=payment_method` for a card-only session);
`checkout-complete` takes `{session_id, purpose?}` and answers JSON (`{"ok": true, "created"}`;
400 no session, 409 nothing created, the service's own status, 500) where Flask redirected with
`?checkout_error=`; dates render ISO.

### `notice` (`/api/entities`, `NoticeBearerAuth`)

`GET /{entity_id}/subscription-notice` — Flask's `/api/entity/<id>/subscription-notice`
(`entity/routes/modules.py`), the plural being the one path change; keeps `settings_path`.

**Live since step 3 slice C (2026-09-22)** — `billing/api/notice.py`. Its auth class is
`EntityBearerAuth` plus Flask's one fallback: a token that names NO company (billing-frontend's
refresh path mints through billing-backend and sends only the bearer, never `X-Entity-Id`) is
held to the caller's membership of the company in the PATH, which is what authorises the read
in any case. A token that names another company is 403 `entity_mismatch`; a stranger is refused
at the door (401 where Flask said 403 `not_a_member` — billing-frontend treats every non-200 as
"no notice"). A builder failure is `{"items": []}` with 200: a notice never takes the landing
page down. Stateless: the "show once per session" claim stays Flask's.

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

**Live since step 3 slice C (2026-09-22)** — `billing/api/onboarding.py`, plain `BearerAuth`.
The company routes read the company from the query or body and check the caller's MEMBERSHIP
(`_entity_for_member`: 400 without an id, 403 for a stranger, 404 for no such company — Flask's
rule and sentences); the four billing-sheet card routes act on the payer and check no company.
`/billing/authorize` nominates the card it was given BEFORE recording consent, with
`establish_payer=True` (during the wizard no company has a payer yet). `trials/start` runs
`checkout.start_trials_for_enabled_modules` (idempotent: modules already holding a trial or a
subscription are skipped) and reads `trial_end` BACK from the rows (the earliest); a
`CheckoutError` answers with its status, anything else 502 "The trial could not be started.
Please try again." — never swallowed, as Flask's finalize did. The setup Checkout returns the
browser to `ONBOARDING_WEB_URL` (`/?pm_session_id=…`, `/?pm_cancelled=1`). Flask's
`/api/onboarding/plans` is NOT here: onboarding-backend serves the catalogue natively
(`onboarding/api_reference.py`).

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
| `payer_billing_group` | a billing ACCOUNT: payer, its name (`billing_company`) and `billing_email`, the card it charges, `paid_through`, dunning state. Several per payer, all renewing on the payer's one anchor; opened, renamed, re-carded and given companies from the portal (§2) |
| `billing_account_payment_method` | the account's cards, one default |
| `entity_billing_group` | which account pays for a company (one payer per company); `source` is how it got there — `capture`, `chosen`, `backfill`, `confirmed`, `transfer`, and `moved` for the portal's "Change billing account" |
| `entity_billing_consent` | a member's consent to be billed for a company |
| `entity_module_subscription` | THE subscription: company × module, phase, payer, `app_access_until`, trial/billing dates |
| `user_stripe_customer` | a person's Stripe customer, billing anchor, currency |
| `subscription_invoice` / `_line` | invoices raised on an account and each company's share; `idempotency_key` is the double-charge guard. A RE-ISSUED invoice (§6) adds two key forms: the replacement claims `refresh-<dead id>` until it takes the period's key, and the dead row keeps `<key>~<dead id>` (`~`, not `-`: `dunning._names_period` reads `<key>-…` as the same period) |
| `subscription_transfer` | change-of-subscriber offers and their outcome |
| `subscription_audit_log` | every state change, before/after, actor |
| `subscription_email_log` | the dedup ledger of the notification emails |

**The one write outside the domain:** `entity_function_map.is_enabled` / `enabled_at` /
`disabled_at` — the projection Flask's per-request module gate reads
(`blueprints/entity/routes/modules.py::_is_module_enabled`). Written only through
`billing/services/entity_modules.py` (step 2) - the Django copy of Flask's
`entity/services/modules._write_pairs`, whose rows it matches column for column (explicit UTC
stamps, `created_by` only on insert) - and never while dark. The access SWEEP exempts companies
still `onboarding` (the wizard writes the map at step 2 but trials start only at finalize);
the writer itself has no such check, exactly like Flask's.

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
(`daily.daily_lock`) lets one run: `pg_try_advisory_lock` on a **dedicated raw connection**
(`connection.get_new_connection`), never the ORM's — the pass autocommits on that one and a
session lock would follow it back to the pool. The key is the same sha256 of
`minty:subscriptions:run-daily` Flask hashes, so a Flask pass and a Django pass on one database
exclude each other during the cutover (`billing/tests/engine/test_daily_lock.py`, contention
proven on Postgres). Known costs, kept on purpose: an in-memory job store, so a restart slips the
next light pass by up to an hour and **a deploy after 05:00 HKT loses that day's full pass**; a
paused instance runs nothing.

The pass itself is `billing/services/daily.py::run_daily(now, issue, mode)` — the job order
`notify-trial-ending → close-trials → repair-transfers → collect-transfers → run-renewals →
retry-dunning → sweep-access` (light: `close-trials → repair-transfers → collect-transfers →
run-renewals → sweep-touched`), each job caught on its own, a sweep skipped when a job that
grants entitlement failed before it. Both transfer jobs run **before** the renewal and for
mirrored reasons: `repair-transfers` writes a claim for money already collected, and
`collect-transfers` writes one by collecting it — run afterwards, the renewal would either
re-bill days already settled or skip a card that has never collected.
`scheduler.run_pass_now` runs on APScheduler's worker thread, so it opens the engine's
request scope itself (`billing.services._context.scope()` — what Flask's `app.app_context()`
gave the timer: the clock, policy, catalog and default-card memos) and calls
`close_old_connections()` either side of the pass.

`manage.py subscriptions <job>`, the port of `flask subscriptions` (ASCII output, every job
inside a scope): `tick` (what a cron will call — full at the full hour, light otherwise),
`run-daily --mode full|light [--issue] [--days-before N]`, `close-trials [--limit N]`,
`run-renewals [--issue] [--user ID]… [--limit N]` (DRY by default — the only job whose flag moves
money; `run-daily` without `--issue` still converts due trials, retries dunning and sweeps),
`retry-dunning [--limit N]`, `notify-trial-ending [--days-before 3] [--limit N]`, `sweep-access`,
`reconcile-customers [--repair]`, `revoke-ungranted [--apply]` (launch day; refuses while dark;
the ORM rewrite of Flask's two raw statements — `billing/tests/engine/test_revoke_ungranted.py`).
`manage.py plans list` reads the catalog and works dark — the quickest proof the service reaches
the database. On 2026-09-21 Django's and Flask's `revoke-ungranted` (dry) and `run-renewals`
(dry) were run against the same dev database (`postgres`) and answered identically (0 rows, 0
payers due).

## 6. Money and mail

Stripe: only this service holds `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` (cross-cutting
rule 9); `billing/services/stripe_client.py` is the one `import stripe` (settings only, never
the process environment — `settings_test` blanks the key so an unstubbed test call raises),
`billing_gateway.py` the one charger (Invoices, idempotency keys claimed before the charge).
`test_models_guard.py` allows the import in that one module only. The setup-Checkout
`success_url` and the billing-portal `return_url` become minty-web pages.

**Transactions — the rule step 3 inherits.** The engine runs on Django's autocommit, which is
what Flask's per-helper commits were. `transaction.atomic()` appears in exactly four kinds of
place: the store groups Flask staged with flush-only helpers (`create_billing_account`,
`nominate_card_for_entity`, `set_group_default_card`) and the account-keyed
`nominate_group_for_entity`, plus `transfers._complete` (payer flip,
consent, nomination clear and the offer's `accepted` land together — Flask could only ORDER
them); the insert-then-catch-`IntegrityError` guards as savepoints (`store.reserve_invoice`,
`transfers.offer_transfer`, `notify._claim`, and `store._carry_paid_days`, whose writes are
swallowed on failure and so must not poison the move around them); `store.supersede_invoice`,
the two-row hand-over of a re-issued invoice's key (released, then re-claimed, both rows
locked - below); and nowhere else. **Never a Stripe call inside
`atomic()`, never `ATOMIC_REQUESTS`**: the double-charge guard depends on the invoice
reservation being COMMITTED before the processor is called, and a request-wide transaction
would hold it back until after the charge.

**An invoice Stripe will no longer collect is re-issued (2026-09-28, both engines).** Stripe
cancels an invoice's PaymentIntent once it has been confirmed too many times — its docs: "a
variable upper limit on how many times a PaymentIntent can be confirmed"; our test account: TEN
declines, the eleventh call cancels — and a cancelled one "can't be undone": every later attempt
fails with "This invoice can no longer be paid". With daily retries 1..13 that left retries
10–13 unable to collect, and a customer who fixed their card late unable to pay at all (by
retry or by either Retry-payment button).
- **Detection** — `billing_gateway.retry_invoice` reads the invoice with its `payment_intent`
  expanded, before paying and again after a failed pay (the call that crosses the limit is the
  one that cancels), and answers `(False, DEAD_PAYMENT)` — a marker, never a message.
- **The re-issue** — `billing_gateway.refresh_invoice`: a replacement with the dead invoice's
  recorded lines and Stripe items copied (never re-priced — an already-invoiced cancellation
  extension would be lost), its metadata plus `replaces=<dead id>`, on the group's current card;
  finalized (which does not charge), THEN `store.supersede_invoice` hands it the period key and
  marks the dead row void, THEN the dead invoice is voided at Stripe. Voided only after the
  replacement is open, so dunning's "nothing open — settled elsewhere" can never see a half-done
  refresh; every interrupted state still has the dead invoice open with its payment cancelled,
  so the next attempt resumes through the same door (idempotency keys `refresh-<id>`,
  `…-item-<n>`, `…-finalize`; `replaces` found by `find_invoice_by_metadata(live_only=True)`).
  An invoice with no local record, or whose record disagrees with what Stripe holds, is not
  re-issued: ERROR logged ("void and re-issue it by hand").
- **Collection** — `dunning._charge`, shared by `collect_due` and `retry_now`: retry; if dead,
  refresh and charge the replacement IN THE SAME ATTEMPT (one slot, the card hit once), with
  `target`/`current` re-pointed so a paid replacement RECOVERS the period. Entries and results
  carry `refreshed` (the invoice replaced); `retry_now` answers `not_collectable` when it could
  not be re-issued. `_replaced_by` lets a row showing the replacement collect while the original
  is still open (mid-refresh); `billing_accounts.retry_invoice` answers `not_this_invoice` for a
  page drawn before the re-issue.

Mail: Django `EMAIL_*` on the same Brevo SMTP Minty uses, sender `SUBSCRIPTION_EMAIL` (fallback
`DEFAULT_FROM_EMAIL`). `notify.mail_configured()` is false for the console and dummy backends
and for SMTP with no `EMAIL_HOST`, and it is checked BEFORE the dedupe claim — an unconfigured
host skips the notice without spending it, so the first configured run still sends it (Flask's
extension always existed, so its unconfigured case claimed and then failed to connect; same net
effect). The two templates (`templates/email/subscription_{notice,receipt}.html`) are Minty's
verbatim on the Jinja2 backend — a render from each backend with the same context is
byte-identical (checked 2026-09-21); the nine inline images live in `billing/static/email/`;
`InlineImageMessage` keeps the `multipart/related; type="multipart/alternative"` wire shape
with `Content-ID` parts; dedup stays in `subscription_email_log`. Links in emails point at
minty-web through Flask's login-gated re-handoff (`notify.settings_url` →
`{MINTY_PUBLIC_URL}/handoff/minty-web?next=/subscription/entities/{id}/modules&entity_id={id}`,
`notify.portal_url` → `…?next=/subscription/subscriptions[/incoming]`), so no token ever
travels in a link from here — Flask stays the only minter. **Until step 5 lands
`/handoff/minty-web` in Flask those links are dead by design.**

## 7. Configuration

`.env.example` is the list. The two switches; `SECRET_KEY` (shared, verify only); `POSTGRES_*` /
`DB_*` + `MINTY_DB_SCHEMA` (default `pettycashv3`, a setting never a literal —
`billing/tests/test_schema_name.py`); `STRIPE_*`; `EMAIL_*` / `SUBSCRIPTION_EMAIL`;
`FLASK_APP_URL` (the forwarded call); `MINTY_PUBLIC_URL` (the address a PERSON reaches Minty
at — every link in an email; defaults to `FLASK_APP_URL`, which in the docker stack is the
internal service name, so set it there); `MINTY_WEB_URL` / `PAYMENTS_WEB_URL` /
`CORS_ALLOWED_ORIGINS` (the two browser origins; `x-entity-id` is allowed); `ONBOARDING_WEB_URL`
(the wizard — where a setup Checkout opened from it returns the browser; not a CORS origin,
onboarding-backend proxies server-side; Flask's `ONBOARDING_APP_URL` under Part 3's name). In
the docker stack it is the `billing-api` service on 8004. `MINTY_WEB_URL` also builds the
Stripe return URLs of the module page's actions.

## 8. Where it is tested

`billing/tests/`: `test_contract.py` (the tables above and the OpenAPI document), `test_dark.py`,
`test_auth.py`, `test_models_guard.py`, `test_schema_name.py`, `test_settings_guard.py`,
`test_no_flask_imports.py` (no `flask` / `sqlalchemy` / `loguru` / `blueprints` / `models.db`
import anywhere under `billing`, `core`, `shared_models`, `config`) — and
`billing/tests/engine/`, the ported `Minty/tests/test_subscription_*` family: 51 files, 762
test functions, named as in Minty so a failure can be read against the original — and
`billing/tests/api/`, the HTTP halves of those files driven through Django's test client against
the routers (step 3; same names again, the service fixtures imported from their engine twins so
both halves stub the same seams; `conftest.py` there lists the three ways Django's client differs
from Flask's). Run on SQLite (`MINTY_TEST_PG_URI= pytest` — the blank prefix matters once a
`.env` names the harness; 983 passed + 2 Postgres-only lock tests skipped, 00:07 on 2026-09-21
after slice A) and on the Postgres built from `01` (`MINTY_TEST_PG_URI=… MINTY_REPO=C:\Github\Minty
pytest`; 985 passed, 00:10). `e2e/test_smoke.py` against a running service, dark and live
(`e2e/README.md`). After step 3 (2026-09-22): 1122 + 2 skipped on SQLite (00:09), 1124 on
Postgres (00:14); `pytest e2e` live against `runserver` on the dev database with a replay
payer: 8 passed, 3 dark-only skipped (00:05). After the billing accounts (2026-09-25): 1219 + 2
skipped on SQLite (00:09), 1221 on Postgres (00:15), `pytest e2e` 9 passed + 3 dark-only skipped
— `engine/test_portal_billing_accounts.py` (37: the read model, the three writes, and one pin per
silent failure the work found, each proven to fail against the old code) and
`api/test_billing_accounts_api.py` (19). After `next_bill`, the Ended-date fix and the invoice
breakdown (same day): 1231 + 2 skipped on SQLite (00:11), 1233 on Postgres (00:23) —
`engine/test_portal_billing_accounts.py` now 40, `engine/test_invoice_breakdown.py` (3) and
`api/test_invoice_breakdown_api.py` (3). The harness enforces the `currency_info` foreign keys
(`fk_si_currency`, `fk_usc_currency`) that SQLite never checks, so a test helper that writes a
currency seeds it (`seed_currency`) — two breakdown tests passed on SQLite and failed only there.
After schema item 23 and 08-C's Stripe address (same day): 1255 + 2 skipped on SQLite (00:26),
1257 on Postgres (00:34) — `engine/test_invoice_line_terms.py` (15: every constructor's span
and rate, the extension's one-rate rule, the row written; its Flask twin
`tests/test_invoice_line_terms.py` the same 15, Flask then 1960 + 2 skipped), two recorded-line
breakdown tests, and the cardholder / publishable key / 255-limit cases in
`engine/test_portal_billing_accounts.py`.

How the ported tests differ from Minty's, by rule: DB tests use pytest-django's `db` (a
transaction per test) where Flask's `db_session` DELETEd tables afterwards — which is why the
three insert-then-catch guards are savepoints; `SimpleNamespace(query=…)` fakes became
`fakes.fake_model(rows)` (a Django-shaped in-memory manager); a test that held a row object
across a service call re-reads it (`offer.refresh_from_db()` — Flask's identity map made the
test's object and the service's the same one, the ORM does not); `datetime.now()` is
`datetime.now(UTC)` (a naive datetime is an error here); the Stripe stubs the tests already had
are kept, and the SDK-boundary fake (`test_char_subscription.FakeStripe`) stands in where
Minty's test made a real test-mode call. **Goldens:** `test_lifecycle_invoices.py` (the
processor-free four-month lifecycle, in CI; since 2026-09-28 its `_World` also models Stripe's
confirmation limit — the call after an invoice's tenth decline kills it — and proves a card fixed
on day 10–13 is still collected through the re-issue, §6; `test_invoice_refresh.py` pins the
re-issue itself on the real store) and the replay
comparison: `manage.py replay_scenarios` (the port of `scripts/subscription/replay_scenarios.py`
- same `RUNS`, same Stripe test clocks, `app.app_context()` → `_context.scope()`, the payer's
password a werkzeug-compatible `pbkdf2:sha256` so Flask's login still works, mail skipped
unless `--notify-to`) run for the eight Angelika keys on the same day as the Flask script,
each cloned to a fresh payer (`--as angelika.tardaguela+django-<key>@… --tag <same-length tag>` -
the report truncates names to fixed widths, so a longer tag loses trailing characters), and
`scripts/replay_diff.py` over the two `--report` outputs. Normalised: Stripe ids and uuids, the
clone's tag, the anchor's timezone rendering, console encoding, the order of invoices sharing an
`issued_at` and of set-ordered job lines. 2026-09-21: all eight IDENTICAL twice - first with
the `_needs_stripe_clock` bug in both scripts, then with it fixed on both sides. The logs are not
kept anywhere (user's decision) - the repo records the outcome, and a run is regenerated when it
is wanted.

Regenerating a golden run (nothing is kept on disk): Flask from `C:\Github\Minty` with both
URIs pointed at the LOCAL database and `MAIL_SERVER=` blanked unless the notices are wanted -
`python scripts/subscription/replay_scenarios.py --run <key> --reset --teardown --setup --replay
--report`; Django from this repo - `python manage.py replay_scenarios --run <key> --as
angelika.tardaguela+django-<key>@… --tag <tag of the SAME length as the run's> --reset --teardown
--setup --replay --report` (a fresh payer, because Stripe keeps an idempotency key for 24 h and the
keys are per payer; the same length because the report truncates names to fixed widths); then
`python scripts/replay_diff.py <flask log> <django log> --tags <run tag>=<clone tag>` — IDENTICAL is
the only acceptable answer. Keys: `X1 C1 L1 L2 R1 E1 angelika angelika-lifecycle angelika-split`.
**L2** (2026-09-28) is the re-issue's live proof: the card dies before the day-30 renewal and is
fixed on day 42, after Stripe gave up on the invoice (day 40: `dunning REFRESHED in_… -> in_…`,
then the replacement declines; day 41 declines; day 42 `dunning retry -> paid`, `RECOVERED`). The
day-30 invoice ends `void`, a day-40 one for the same period and lines `paid`. L1 (never fixed)
shows the same REFRESHED on day 40 and then declines on the replacement until the give-up.

### The replay catalogue — Figma 05·A

The `angelika` and `digitalisation` runs share one shape, `CATALOGUE`. It is Figma
section **05·A** "Subscription Summary — all 36 module-status combinations" (file
`43YI3MYtTfX5Xzz6dRoRuT`, node `1521:1292`). Down is Petty Cash and across is Payment Request, over
1 NOT_STARTED, 2 TRIAL, 3 TRIAL_EXPIRED, 4 ACTIVE, 5 CANCELLATION_PENDING and 6 SUSPENDED:

- **M11..M66** are the 36 cells, with every trial unconfirmed.
- **The N-frames** show a trial *confirmed*: a = Petty Cash's trial, b = Payment Request's.
- **Section 05·B** (every tick and untick) needs no data of its own. Each of its frames is one of
  these states after a click.

It replaced Scenario 2-12 on 2026-09-28, and with them the hand-seeded Scenario 13 (past due, now
M66) and Scenario 14 (nothing started, now M11); `seed_past_due.py` and `seed_no_trial.py` are
gone. Each company is named `"<frame> <the design's company>"`, and the run's tag is prefixed as
usual ("Ang - M44 Nexora Health Limited"). `_check_scripts` refuses at import:
- an unknown event kind, a code that doesn't fit its kind, or a future offset;
- a name without its frame code, or a frame seeded twice;
- any 05·A frame that is neither lived nor listed in `UNREACHABLE_05A`;
- a broken relation between the offsets below.

`billing/tests/test_replay_catalogue.py` pins all of these.

**Offsets.** They are in days before the run's last day, which is noon UTC:

| Name | Offset | Meaning |
|---|---|---|
| `ANCHOR_BUY` | -35 | M44's buys: the payer's first charge, so its anchor. Renewal R falls a calendar month later, between -7 and -4 depending on month length. |
| `BOUGHT` | -20 | An ordinary purchase. |
| `RUNNING` | -16 | A trial start. It ends at +14 and the card reads "Trial Active". |
| `LAPSED` | -50 | A trial start. It expires at -20 with no consent. |
| `AFTER_LAPSE` | -18 | A buy or consent beside a lapsed sibling. It must come after -20, because consent is per company and would otherwise convert the sibling. |
| `CONSENTED` | -15 | Consent on a running trial. |
| `CANCELLED` | -15 | Access runs to max(R, +15) = +15, which is the design's "Ends in 15 days". |
| `FAILING` | -8 | A `nominate` onto one shared declining card (`FAILING_CARD`, tagged `F:`). R declines; that card's ACTIVE rows go past due and stay SUSPENDED until R+15. Trials and cancellations keep their phase. |

The run covers 52 days.

**The two billing accounts are named for what happens to them**: **Success** (the working
card, which 20 companies renew on) and **Failed** (the declining card the 9 suspended
companies are on). Unnamed, an account reads as its payer (`portal.account_name`), so the two
read the same in the 08-A picker. The event is `rename` (the store write 08-C's "Save billing
account" makes, `store.set_account_identity`). It sorts after `buy`, `consent` and `nominate`
on the same day, so a rename on the day of a move names the account moved to. `_check_catalogue`
refuses a rename that comes before its company's last move.

| Frame | Company | Script |
|---|---|---|
| M11 | Harbour & Vine | none (`--setup` creates the company only; it is never in the payer's *list*, which is built from module rows, so open its module settings page) |
| M12 / M21 | Kestrel Foods / Ashcroft | trial RUNNING |
| M13 / M31 | Mino Market / Beacon Hill | trial LAPSED |
| M14 / M41 | Lantern Bay / Driftwood | buy BOUGHT |
| M15 / M51 | Orchid Lane / Glasswater | buy BOUGHT; cancel CANCELLED |
| M16 / M61 | Willow Court / Kingsmead | buy BOUGHT; nominate FAILING |
| M22 | Pier 9 Trading | trial both RUNNING |
| M23 / M32 | Quarry Hill / Cobblestone | trial the other LAPSED; trial RUNNING |
| M33 | Ember & Co | trial both LAPSED |
| M34 / M43 | Thread & Craft / Fernbank | trial the other LAPSED; buy AFTER_LAPSE |
| M35 / M53 | Saltwater Studio / Ironvale | as M34/M43, then cancel CANCELLED |
| M36 / M63 | Copperline / Meadowfield | as M34/M43, then nominate FAILING |
| M44 | Nexora Health | buy both ANCHOR_BUY (the anchor); rename its account **Success** |
| M45 / M54 | Solera Group / Juniper Row | buy both BOUGHT; cancel one CANCELLED |
| M55 | Tidal Works | buy both BOUGHT; cancel both CANCELLED |
| M56 / M65 | Northgate / Oakhaven | buy both BOUGHT; cancel one CANCELLED; nominate FAILING (the extension rides the declined invoice) |
| M66 | Halcyon Labs | buy both BOUGHT; nominate FAILING; rename that account **Failed** |
| N12b / N21a | Rosewood / Silverbrook | trial RUNNING; consent CONSENTED |
| N23a / N32b | Vantage Point / Amberton | trial the other LAPSED; trial RUNNING; consent CONSENTED |
| N24a / N42b | Westbay / Birchwood | buy the other BOUGHT; trial RUNNING (the buy's consent confirms it) |
| N25a / N52b | Yardley / Cedarcroft | as N24a/N42b, then cancel the bought one CANCELLED |
| N26a / N62b | Zephyr Lane / Dunmore | as N24a/N42b, then nominate FAILING |

**The ten frames not lived.** Each is in `UNREACHABLE_05A` with its reason:

- **M24, M42, M25, M52, M26, M62.** A trial beside a paid module is always confirmed.
  - `will_convert` is card AND consent (`cards.py:74-80`). Consent is per company, it has no
    revoke, and every purchase records it.
  - The card belongs to the whole account. Removing it is only possible once nothing bills, and it
    would unconfirm every trial the payer has.
  - minty-web's `?summary=M24` fixture draws a state the engine never produces.
- **M46, M64.** One module past due beside one paid up needs two paid-through dates on one company,
  and one card group cannot hold them.
  - The only route is a probable engine bug: `payment_methods.set_for_entity` moves a past-due
    company onto a working card without `billing_accounts.move_company`'s past-due refusal. The new
    card renews it, the row stays `past_due` for good, and the old card's open invoice is still
    chased.
  - The recipe, if it is ever wanted: buy; nominate FAILING at -8; nominate onto a working card at
    -2; buy the other module at -1.
- **N22a, N22b.** `will_convert` is computed once per company, so two trials are confirmed together
  or not at all. A cancelled trial renders CANCELLATION_PENDING, so it doesn't help.

**Where it differs from the drawings, knowingly:**
- Cards read Visa 4242 (`tok_visa`) instead of 4121.
- Trials read "Trial Active" rather than "3 days remaining". Longer trials keep the catalogue alive.
- The footer dates are the design's fixed text.
- minty-web lists newest-created first, so Figma's neighbouring rows are not reproduced.
- The suspended companies make `payer_is_dunning` true, which blocks handovers for this payer, and
  08-A shows the payment-failed state.

**Shelf life.** About eight days. SUSPENDED lapses at R+15 (+8 to +11); trials end at +14 and
turn red from +7; cancellations end at +15. Re-run to refresh:
`--run angelika --reset --teardown --setup --replay --report`.

`replay()` refuses a payer that still holds an anchor (run `--reset`). `reset()` deletes the payer's
nominations *by payer*: a company nominated onto one of its cards by hand once blocked the group
delete, rolled the reset back and left the old anchor in place.

**Rename a scenario only after `--teardown`.** `_entities` matches on name, so a renamed company is
orphaned beside a freshly seeded one.

**Not ported, and where the behaviour lives on.** Tests that render Flask's Jinja templates,
walk Flask route source or use Flask-Login's session stay in Minty until step 5 deletes the
partials (`test_consent_takeover`, `test_subscription_templates`, `test_past_due_card_action`,
`test_settings_users_subscriber_tag`, `test_csrf_exemptions`, `test_restart_billing_guard`,
the SRC/TPL halves of `test_purchase_card_choice` / `test_module_card_lapsed` /
`test_entity_list_trial_badge`, the `_is_module_enabled` half of `test_module_access_gate`,
the session half of `test_subscription_notice`). **The HTTP halves were ported in step 3**
against the ninja routers, into `billing/tests/api/`:

- `tests/test_payer_portal_api.py` (22) — **PORTED, slice A**: `billing/tests/api/test_payer_portal_api.py`, all 22 plus the empty-table 200, the not-JSON body, the read model's 500, the RFC 822 rendering and five invitation-forward tests (no Flask twin: Flask's view sent the invite in-process)
- `tests/test_billing_payment_methods.py` (6) — **PORTED, slice A**: `billing/tests/api/test_billing_payment_methods.py`, all 6 plus the shell's 500-with-CORS
- `tests/test_onboarding_payment_method.py` (14) — **PORTED, slice C**: `billing/tests/api/test_onboarding_payment_method.py`, 13 of the 14 (not `test_buy_now_card_routes_answer_the_onboarding_origin`: the wizard's browser never calls this API, onboarding-backend proxies server-side) plus the account routes, the wallet-described card, the setup return URLs, and six `trials/start` tests (the real trial over the seeded catalogue, idempotent, no-module null, the loud failure) — 38 tests
- `tests/test_onboarding_plans.py` (4) — **not applicable**: `/api/onboarding/plans` is onboarding-backend's own (`onboarding/api_reference.py`, read from `billing_plan`); this API has no plans route
- `tests/test_purchase_card_choice.py` (2 route tests) — **PORTED, slice B** into `billing/tests/api/test_module_settings_api.py`, together with the behaviour versions of `test_subscription_payer_permission`'s and `test_restart_billing_guard`'s route-source checks (every action as a co-admin, as a cashier who holds the card, the return leg as a co-admin; the restart route's four refusals in order) and the page model's wire shape — 84 tests
- `tests/test_subscription_notice.py` (10) — **PORTED, slice C**: `billing/tests/api/test_subscription_notice.py`, all 10 (the stranger cases answer 401 at the door, see §2) plus the superadmin read and the real builder over an empty company — 12 tests
- `tests/test_char_subscription.py`: the report-page gate check (Flask's gate); the rest is `billing/tests/engine/test_char_subscription.py` through the services
- `tests/test_char_subscription_dark.py`: the HTTP and CLI-runner halves (`test_dark.py` and `test_subscriptions_command.py` pin the same contracts here)

Step 3 also renders the raw datetimes the services return in dicts (`transfers._as_dict`,
the portal's `since`) as RFC 822, which Flask's `jsonify` did for free (`billing/api/_json.py`).
`billing/tests/test_contract.py` also holds `docs/openapi.json` to what the API serves;
`manage.py export_openapi` regenerates it (`--check` for CI).

Two things the port found in Minty. Left for step 5 (Flask's tests are frozen during step 2):
`test_a_trial_that_ends_without_a_card_expires_and_lapses` makes a REAL Stripe `Customer.search`
in test mode (the app loads `.env`, nothing stubs `get_stripe`), and
`scripts/subscription/prune_replay_stripe.prune_dangling_rows` fails on `uuid = text`. FIXED on
both sides the same day (user's decision; scripts are not in the frozen set):
`replay_scenarios.py::_needs_stripe_clock` read `dunning_started_at` / `paid_through` off
`UserStripeCustomer`, columns that moved to `payer_billing_group` in the per-entity-cards
cutover, so since 2026-08-25 the Stripe test clock advanced only on scripted-event days (every
non-event day logged `!! test clock: …`) and every renewal after the last event carried that
event's Stripe stamp in `issued_at`. It reads the payer's billing groups now; the runs take
about twice as long, which is the clock doing its job.

**Also fixed on both sides, 2026-09-28: the lifecycle's and L1's card failures had stopped
happening.**
- **The cause.** Both shapes failed their card with a `("card", CARD_FAIL, …)` event, which
  changes only the payer's *default* card. Since the per-entity-cards cutover a renewal charges
  each company's own group, so the event declined nothing. Every run in between renewed on a
  working card: Scenario 1's day-92 failure and L1's give-up never happened.
- **Why the golden missed it.** Flask and Django agreed, so the golden could not see it.
- **The fix.** Both now use `recard`, which replaces the card under the group, as Scenario 1B
  always did.
- **The guard.** `_check_script` refuses a declining `card` event at import.
  `billing/tests/test_replay_catalogue.py` pins that every shape meant to fail puts a declining
  card under a group.

## 9. What arrives when

| Step | Lands here |
|---|---|
| 2 | DONE 2026-09-21: `billing/services/` — the 1:1 port of all 24 modules of `blueprints/subscription/services/` plus `entity_modules.py` (from `entity/services/modules.py`), `_context.py` (the request scope that replaced `flask.g`) and `_log.py`; the templates and images; 51 ported test files; the job bodies behind `manage.py subscriptions`; the scoped scheduler pass; `manage.py replay_scenarios` + `scripts/replay_diff.py`, the eight Angelika runs identical on both sides |
| 3 | DONE 2026-09-22: all four routers filled from Flask's views — `me` (`billing/api/me.py`, `_json.py` = `jsonify`; the invitation forwarded with the caller's bearer), `modules` (the page model minty-web renders + the 19 actions behind one gate), `notice` (`NoticeBearerAuth`, Flask's claimless fallback), `onboarding` (the nine + `trials/start`, which fails loudly); `docs/openapi.json` committed and held current by `test_contract.py` / `manage.py export_openapi`; 173 route tests in `billing/tests/api/` (32 portal + 7 wallet, 84 module page, 12 notice, 38 onboarding); e2e smoke asserts the live shapes |
| 4c+ | DONE 2026-09-25: **billing accounts in the portal** — `GET /billing/accounts` (`portal.build_billing_accounts`), `update` / `default-card` / `move` (`billing/services/billing_accounts.py`), the account fields on `confirm`, `account` on `invoices` and `remove`, `next_billing` on `subscriptions`. Five silent failures fixed on the way: the landing's "Next Billing Date" was the anchor (the FIRST charge); the removal guard stopped at the first account on a shared card; card-keyed nomination raised on a shared card (`_group_for_card`, oldest wins); a company moved onto an emptied account lost access (`_carry_paid_days`' idle branch); a blanked address field was dropped by the SDK instead of cleared. Flask not mirrored (dark there; Django replaces it). **Same day, later: `next_bill` per account** (08-B's "Amount (estimated)") - `portal.next_bill_for_account` prices the account's next renewal with `renewals.build_renewal(..., converting_by=period.start)`: the runner's own invoice for the period starting on the next billing date, plus the trials that will have converted by then (`renewals._trial_converts` - the trial-end job's conjunction of customer, card for the company and consent, read from the database alone, never Stripe). `converting_by` is the forecast's only; the runner never passes it, so what it bills is unchanged. A figure that cannot be priced is null and logged, never a failed page. **Later still: schema item 23** - `subscription_invoice_line` records what each line PAID FOR (`period_start` / `period_end` / `unit_amount`), written by BOTH engines at issue: `billing.Line` carries them from whatever priced the line (`paid_from` holds a mid-period start to the period as `prorate` does), and an extension's come from `checkout.pending_extension_terms` - the paid-through date to the access end, and the rate only when re-deriving it piece by piece (`_extension_pieces`, which `_segmented_extension` now sums) reproduces the billed amount at ONE rate; never a blend, never a failed renewal. The breakdown reads them first. Minty migration `x1a01_invoice_line_span` ALTERs a database already up; both local ones have it, Supabase does not yet. **And 08-C's address became Stripe's own form** (the user's call): `?countries=1` also answers `publishable_key`, `update` takes `cardholder` (written with the address in one `billing_details` update, the account's card required as for the address), and every field is held to 255 characters. **2026-09-28: 08-K** - `POST /invoices/{invoice_id}/retry` and `retryable` on the invoice rows (above); `retry_now` gained `group_id` / `expect_invoice` in BOTH engines. And the engine now recognises a REPLAY-SCOPED renewal key (`dunning._names_period`: `<key>` or `<key>-<suffix>` - `replay_scenarios` scopes every key it issues so same-day runs do not collide at Stripe) where it picks the current period's invoice and where it settles one, so the dev database's lived past-due accounts can be retried and settle; production keys are never scoped. NOT covered: the renewal runner's duplicate guard (`_already_invoiced`) still matches keys exactly, so the live scheduler re-bills a replay-lived period (seen 2026-09-28 07:00 UTC on the catalogue's two 'Failed' accounts) |
| 5 | Flask's copies deleted; onboarding-backend proxies here; the Stripe keys leave Flask |
| 7 | deployed dark beside the phase-C builds at the cutover; 8b switches it on |
