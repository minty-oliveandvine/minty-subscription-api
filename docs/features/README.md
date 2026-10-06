# Features — the subscription engine's API

`minty-subscription-api` is the Django (Ninja) service that owns Minty's subscriptions from Part 2
of the modernisation plan on: the payer portal (`/api/me/*`), the module settings page
(`/api/entities/{id}/modules`), the dashboard notice, the wizard's card routes, the daily
pass and the notification emails. It verifies the token Minty minted, reads and writes the
thirteen subscription tables in the shared schema (`?schema=` on `DATABASE_URL`, default `pettycashv3`,
every model `managed = False`), writes the `entity_function_map.is_enabled` projection Flask's
module gate reads, and is the only holder of the Stripe keys. Written for someone new to the
codebase; the plan and Minty's own `docs/features/` are linked, not repeated.

| Feature | Document |
|---|---|
| Verifying the module token, person- vs company-scoped routes (`SelfBearerAuth` / `EntityBearerAuth` / `BearerAuth`), the permission port, what this service never does | [authentication.md](authentication.md) — Minty's `docs/features/authentication.md` has the system-wide picture |
| The API route by route (the portal's twenty paths — Flask's fifteen plus `transfer/seen` and the billing accounts, the page model + ten actions, the notice, seven wizard routes + `trials/start`; no route opens a Stripe-hosted page since 2026-10-01), always on (the dark switch removed 2026-10-01), the thirteen tables and the one write outside them, the daily pass and its scheduler, Stripe and mail, configuration, tests, and which step fills what | [subscriptions-api.md](subscriptions-api.md) — Minty's `docs/features/modules-and-subscriptions.md` describes the Flask original this is a 1:1 port of |
| Manual QA checklist for this service, alongside the automated suites | [qa-checklist.md](qa-checklist.md) |

The three rules the service is built on (verifies never mints · no migrations, `managed =
False` · single Stripe writer) and the in-process scheduler's known costs
are in the repo `README.md`; read it first. Running it: `manage.py runserver 8000` with `.env`
(`APP_ENV`, `SECRET_KEY` shared with Minty, `DATABASE_URL` with its `?schema=`, the scheduler's
`SUBSCRIPTION_SCHEDULER_*` settings, the Stripe keys); tests `pytest` (SQLite, 35 on 2026-09-21) and `MINTY_TEST_PG_URI=…
MINTY_REPO=… pytest` (Postgres from the schema file); `pytest e2e` against a running service
(`e2e/README.md`).

Keep these current: when a route, a rule or a test named here changes, change the line that
names it in the same commit. `subscriptions-api.md` §9 says which step fills which part; move a
row out of it when the step lands.
