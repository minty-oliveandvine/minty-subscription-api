# Authentication — minty-subscription-api's half

This service **verifies tokens; it never mints them** (`core/auth.py`). Minty (Flask) signs
the person in — email OTP or Xero — mints the module JWT (`_generate_module_token`, 30
minutes) and hands it to `minty-web` in the launch URL (`/landing?token=…`); `minty-web` then
sends it here as a bearer. The system-wide picture is `Minty/docs/features/authentication.md`.

## The bearer token

`Authorization: Bearer <jwt>` on every route; nothing is public but `/healthz` and
`/api/openapi.json`. `BearerAuth` (minty-payment-request-api's, verbatim — `SelfBearerAuth` is its
person-scoped variant):

- HS256 over the **shared `SECRET_KEY`**, with 60 s of leeway on `iat`/`exp` for clock skew
  between hosts (`CLOCK_SKEW_LEEWAY_SECONDS`, 2026-10-06 — minty-onboarding-api's allowance,
  so a token it accepts and forwards is not refused here); a malformed token, a bad signature, an expired
  one and an unknown `user_id` are all `401 {"error": "Unauthorized"}` — the body Flask's
  portal answered, so `payerPortal.ts` reads it (`core/exceptions.py` overrides ninja's
  `detail`).
- Claims read: `user_id` (looked up in `user`), `entity_id` (may be empty — the *unscoped*
  token the entity list hands the portal), `system_role` (trusted from the token first,
  then the row; `superuser` is the pre-rename spelling of `superadmin`). `role`, `module`,
  `sid`, `billing_enabled` and `petty_cash_enabled` are Flask's for the frontends and are
  not consulted here — the database, not the claim, decides what a company has.
- **The company comes from the token's `entity_id` or the `X-Entity-Id` header** (header
  wins when both are present; a mismatch is logged). The module page reached *from the
  portal* carries an unscoped token and names the company in the header, exactly as
  minty-payment-request-web does with minty-payment-request-api — so `x-entity-id` is in `CORS_ALLOW_HEADERS`.

## Person-scoped vs company-scoped

| Router | Auth | The door |
|---|---|---|
| `me` (`/api/me/*`) | `SelfBearerAuth` | identity only. Every row is found by the caller's `user_id` — their subscriptions, invoices, cards, transfers — so a company the caller has no role on is irrelevant and the request continues without one. Flask's `routes/portal.py` applied the same rule (`_user_id_from_bearer`). |
| `modules` | `EntityBearerAuth` | the caller must hold a `user_entity` row on the resolved company, or be a system `superadmin` (a virtual `super_admin` role, read-only through `core/policy.is_superuser_readonly`). No company anywhere (no claim, no header), or no role → 401 - minty-payment-request-api's `BearerAuth` would let a company-less token through as an unscoped person, because it has person-level routes on the same router; this service has `SelfBearerAuth` for those, so a company route with no company is refused at the door. Inside, the path's company must be the resolved one (403 otherwise). |
| `notice` | `NoticeBearerAuth` (`billing/api/notice.py`) | `EntityBearerAuth` plus Flask's one fallback: a token that names NO company - minty-payment-request-web sends only the bearer, and its refresh through minty-payment-request-api need not preserve the claim - is held to the caller's membership of the company in the PATH, which is what authorises the read in any case. A token naming another company than the path → 403 `entity_mismatch`; a stranger → 401. |
| `onboarding` | `BearerAuth` | the wizard's token is unscoped and names its company in the body, as with minty-onboarding-api; the handler checks membership itself. Since 2026-10-06 it arrives forwarded by minty-onboarding-api (Flask's onboarding token: `user_id`, `scope: "onboarding"`, `iat`, `exp`, no `entity_id`); `scope` is not checked here. |

Inside the door the module page applies Flask's two permissions from `core/policy.py` (a
verbatim copy of minty-onboarding-api's port of Minty's `services/permission_policy.py`):
`MODULE_VIEW` (cashier and up) to read the page model, `MODULE_MANAGE` (admin and up) for
every action — **and** the subscription's own rule, `store.may_manage_subscription` (the
`@require_subscription_payer` port, step 2): only the payer, or a member with billing consent
on a company that has no payer yet, may act. A role is not enough to touch somebody else's
card. Every action carries the payer rule; the one exception, Stripe's return leg
`checkout-complete`, was deleted with the other hosted-Stripe actions on 2026-10-01.

## Refresh

There is none. minty-payment-request-api re-mints on `/auth/token/refresh`; this service does not,
because Flask stays the single minter (rule 1) and its 24-hour session outlives the 30-minute
token. A lapsed token sends `minty-web` to Flask's login-gated
`GET /handoff/minty-web?next=<path>&entity_id=…` (Part 2 step 5), which mints the same token
and lands back on the page — silent while the Flask session is alive, a login when it is not.

## Server-to-server

- **Flask → this service** (the dashboard notice while Flask still renders the dashboard):
  a five-minute assertion Flask self-mints with the shared key, as it does for
  minty-payment-request-api's internal Xero token route. Step 3. Live since 2026-10-06:
  Petty Cash's dashboard reads `GET /api/entities/{id}/subscription-notice` server-side with a
  token naming the viewer and the company (Minty `services/subscription_api.py`).
  minty-payment-request-web's landing notice calls the same route directly with its own bearer.
- **minty-onboarding-api → this service** (the wizard's card routes and `trials/start`, which
  its `POST /finalize` calls — 2026-10-06): the caller's own bearer, forwarded verbatim
  (`core/subscription_client.py` there) — no service credential to leak or scope wrongly.
- **This service → Flask** (`invite-admin`): the same, through `core/flask_client.py`, the
  only module that calls Flask.

## What this service never does

- **Mint or refresh a token.** No `SECRET_KEY` signing anywhere; `jwt.encode` appears only in
  the tests, minting the token Flask would.
- **Read a Xero token.** No `user_token` mirror, no `XERO_*` setting.
- **Sign anyone in.** No session, no cookie; `django.contrib.auth` is not installed.

## Configuration

`SECRET_KEY` (shared), `APP_ENV` (`DEBUG` is `APP_ENV == "development"`), `?schema=` on
`DATABASE_URL` → `DB_SCHEMA` (the `search_path`), `MINTY_WEB_URL` / `PAYMENT_REQUEST_WEB_URL` /
`PETTY_CASH_PUBLIC_URL` / `CORS_ALLOWED_ORIGINS` (the browser origins), `PETTY_CASH_URL` (the one
forwarded call) and `PETTY_CASH_PUBLIC_URL` (the re-handoff links).

## Tests

`billing/tests/test_auth.py` (every acceptance and refusal, scoped and unscoped, the header,
the superadmin), `billing/tests/api/test_onboarding_payment_method.py` (the forwarded
onboarding token, and one minted 30 s ahead, are accepted by `trials/start`), `test_dark.py` (404 before auth, with CORS), `test_settings_guard.py` (the
placeholder key refuses to boot unless `APP_ENV=development`); `e2e/test_smoke.py` against a running
service; in the browser `minty-web/e2e/01_landing.spec.ts` (the handoff sets the cookie; a
missing token does not).
