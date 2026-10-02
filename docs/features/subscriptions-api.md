# Subscriptions API — what the service owns, route by route

The subscription engine and its API, moved out of the Flask app in Part 2 of
`Minty/docs/modernisation/modernisation_plan.md`. This page is the map of the service as it
stands, route by route, and - since step 2 landed (2026-09-21) - the spec of its rules. It began
as a 1:1 port of the Flask original (Minty's `docs/features/modules-and-subscriptions.md`).
Since 2026-09-30 the two differ: that day's billing fixes (§6) are in this service only,
because Flask's subscription engine is to be deleted (the user's decision), so where the Flask
page and this one disagree, this one is right.

## 1. What a person gets

- **The payer portal** (`minty-web`, `/subscription/*`): the companies I pay for and who pays
  for each, a change of subscriber (offer, accept, decline, withdraw), my billing accounts —
  each a name, an email, its cards and the companies it pays for — and my invoices, per
  account: any invoice's breakdown company by company, a retry of a failed one, and the
  invoice itself as a PDF drawn to Figma 09-A. Twenty-three `/api/me/*` paths (twenty-four
  operations): Flask's fifteen, plus `transfer/seen`, the four `billing/accounts` routes and an
  invoice's `breakdown`, `retry` and `pdf`.
- **A company's module settings page** (`minty-web`, `/subscription/entities/{id}/modules`):
  the two module cards (Petty Cash, Payment Request) with their state, and the ten actions —
  start a trial, authorise billing, restart, cancel, renew, retry a payment, and the previews. One
  page model plus one action endpoint. No action hands the browser to a Stripe-hosted page
  (2026-10-01): cards are added only through a billing account, on `/api/me/billing/*`.
- **The notice** a dashboard shows for a company (past due, or a paid module winding down; no
  trial notices since 2026-10-01): one route, read by minty-payment-request-web's landing page and by
  Flask's dashboard server-side.
- **The wizard's money steps** (onboarding step 8/9): adding a card INTO a billing account,
  authorising billing, and — on All Set — starting the trial. Seven of Flask's nine routes proxied
  here by minty-onboarding-api (its two setup-mode Checkout routes were deleted 2026-10-01) plus
  `POST /api/onboarding/trials/start`.

**THE CARD RULE (the user's, 2026-10-01).** A payment method is only ever added through a
BILLING ACCOUNT (`payer_billing_group`, cards on `billing_account_payment_method`), via the
in-app Stripe Elements SetupIntent flow (`billing/payment-methods/setup-intent` + `confirm`).
No route hands the browser to a Stripe-hosted page (setup-mode Checkout or the Billing Portal -
both deleted, with `stripe_client`'s session/portal helpers), no confirm saves a card unattached
to an account (422 "Choose a billing account for this card."), a purchase decides on the card
NOMINATED for the company (never the customer default), and a handover's accept nominates the
account the recipient names. Authorising billing (`authorize-billing`, `billing/authorize`) for
a company on NO billing account, with no `payment_method`, is refused 402 "Choose a billing
account for this company." and records no consent - the screen opens the Billing Accounts
picker on it. The old backstop `checkout._ensure_nominated`, which put such a company on the
Stripe customer's DEFAULT card, is deleted (2026-10-01).
- **The daily pass**: trials close, renewals are raised, failed payments retried, access swept,
  trial-ending notices sent. Nobody calls it; the in-process scheduler does.

## 2. The API

Mounted by `config/urls.py`; auth per router in `docs/features/authentication.md`. Bodies are
`{"error": "<sentence>"}` with Flask's status codes (`core/exceptions.py`): 400 bad input, 401
no/invalid token, 402 card declined, 403 not allowed / no consent, 404 not yours, 409
already done, 5xx upstream. `billing/tests/test_contract.py` pins every table below.

**Every email input is English only** (2026-10-01, the frontends' rule): `billing_accounts.EMAIL_RE`
= printable ASCII minus "@", one "@", a dot in the domain — still deliberately shallow
(`a+b@sub.domain.museum` passes). A non-ASCII address (Korean, accents) is refused **422**
`"Email can only contain English letters, numbers and symbols."` (`EMAIL_NOT_ENGLISH`, checked
first, via `billing_accounts.email_refusal`); any other miss keeps its form's own sentence. It
holds on `me` `billing/payment-methods/confirm` (with or without `billing_group_id`),
`billing/accounts/update` and `subscriptions/invite-admin` (refused before Flask is asked), and
on `onboarding` `billing/payment-methods/confirm` and `POST billing/accounts` (both before
anything is written or attached). Stored rows are not rewritten.

### `me` — the payer portal (`/api/me`, `SelfBearerAuth`)

| Method | Path | Flask origin (`routes/portal.py`) | Answers |
|---|---|---|---|
| GET | `/subscriptions` | `my_subscriptions_api` | the payer, their billing anchor/paid-through and **`next_billing` / `next_billing_iso`** (2026-09-25: the end of the anchor period now is in — the anchor is the FIRST charge and never moves, and the landing printed it as the next billing date), a page of companies with module status per company; `q`, `sort`, `direction`, `page`, `per_page`. **Plus `transfer_outcomes`** — how the caller's OWN offers ended (declined / expired / accepted) where they have not been shown yet, the only read of a finished transfer in the engine; `cancelled` is excluded (their own withdrawal, already answered by 07-K). Read advisorily, so a failure leaves it empty rather than taking the page down |
| GET | `/subscriptions/subscriber-options` | `my_subscriber_options_api` | who a company's bill could move to (admins), each with a quote and inherited trials; blockers; the pending transfer; and `paid_through`, what the COMPANY is paid up until (the screen's footer needs it whether or not there is a candidate to quote) |
| POST | `/subscriptions/invite-admin` | `my_invite_admin_api` | **forwarded to Flask** `POST /api/onboarding/invite` (the invitation is Flask's), once the address passes the email rule above (422 otherwise) |
| POST | `/subscriptions/transfer` | `my_transfer_initiate_api` | offer a company's billing to another admin — **any** admin, with a saved card or without: being asked is not being charged, so the card is required at the accept, not here (the offer-time refusal was dropped 2026-09-24) |
| POST | `/subscriptions/transfer/respond` | `my_transfer_respond_api` | accept or decline. Body `{transfer, accept, codes?, billing_group_id?}` — **`billing_group_id`** (2026-10-01) is the caller's OWN billing account the company will be billed by: checked first (someone else's or an unknown id is 422 "That billing account couldn't be found.", nothing moves) and, on accept, the company is nominated onto THAT account (`store.nominate_group_for_entity`, source `transfer` — not "the oldest account holding its card"), a trial-only handover included. Omitted with no nomination already in place, an accept with anything to charge is refused 422 "Choose a billing account before taking over the billing." — the Stripe customer's default card is no longer a fallback. `codes` is the modules being taken on (07-D "Choose Modules"); anything the company holds that it does not name is **cancelled** as part of accepting, ending at the outgoing payer's `paid_through` so no extension is owed by anybody (the ordinary `cancel_module` cannot be used: its access end is `max(paid_through, now + paid_cancel_access_days)`, which books a real extension and trips the handover's own blocker). Omitted means the whole company; naming none is refused. **Accepting takes no money** when the window being bought has not started yet (the usual case — the outgoing payer has bought days nobody has used): the charge is parked on `subscription_transfer.collect_at` and taken by `collect-transfers` on that day, and **no `billed_through` claim is written** until it is collected. A window that has already lapsed is charged inline as before. Either way an account is required (see `billing_group_id` above), and a refusal leaves the offer pending |
| POST | `/subscriptions/transfer/seen` | — (**new; Flask had none**) | mark how one of MY offers ended as seen, `{transfer}`. The payer pressing Done on 07-I / A-07 / A-08. Stamps `subscription_transfer.outcome_seen_at` — from the click, not from the read that drew the modal and not from the email, which records that a message was SENT. Idempotent; the same answer for a transfer that does not exist and one that is not mine |
| POST | `/subscriptions/transfer/cancel` | `my_transfer_cancel_api` | withdraw an offer |
| GET | `/subscriptions/transfers` | `my_transfers_api` | offers made TO me |
| GET | `/invoices/{invoice_id}/breakdown` | — (**new**, 2026-09-25) | 08-B's "Billing Breakdown · Download csv": ONE invoice, company by company - a row per line it charged (the subscription, the monthly rate that line was priced at, the days it paid for, what was charged; a credit negative, a zero line left out). The days and rate are the ones the line RECORDED when it was issued (`subscription_invoice_line.period_start` / `period_end` / `unit_amount`, schema item 23, same day - written by whatever priced it: a renewal is the whole period at its plan's price; a mid-period start or upgrade, and the credit for the plan it replaced, run from the change to the period's end; a cancellation extension runs from the paid-through date to its access end at the rate the cancellation priced it at - the marginal step for a module leaving a bundle). An extension priced at two rates recorded none: its row shows the rate its days add up to. A line issued BEFORE then is read back from how its kind is priced (`portal.build_invoice_breakdown`) - an extension's end from the module's `app_access_until` while the row still holds it, left out (null) once a resume has cleared it, never guessed. Someone else's invoice, or a malformed id, is 404; the web writes the CSV |
| POST | `/invoices/{invoice_id}/retry` | — (**new**, 2026-09-28) | 08-B's *Retry payment* on a declined invoice's row (08-K): collect THIS invoice now, on its account's card (`billing_accounts.retry_invoice` → `dunning.retry_now(group_id=…, expect_invoice=…)` - the engine's own manual collection: the same attempt budget and give-up deadline as the scheduled retries, the period settled and access switched back on when paid). Pinned from the row: the CARD (the invoice's account; the payer's oldest for one from before accounts) and the INVOICE (charged only if it is the one the engine's rules pick - else `not_this_invoice`, nothing charged, no attempt spent). Refused before the engine unless the list marks it `retryable`. Answers `{ok, status, message}` in the module page's own words (`api/_retry.py`, shared with `retry-payment`): `paid`, `failed` (the processor's reason), `no_card`, `gave_up`, `nothing_owed`, `older_debt_only`, `not_this_invoice`, `not_collectable` (the processor will no longer collect it and it could not be re-issued automatically - nothing charged; §6), `unavailable` (2026-09-30: the payment processor itself failed - nothing charged, no attempt spent; §6). An invoice Stripe will no longer collect is RE-ISSUED and the replacement charged in the same press (§6). Someone else's invoice or a malformed id is 404; one not waiting for a payment is 409 - or `not_this_invoice` when it was re-issued since the page was drawn; a processor failure is 502 |
| GET | `/invoices/{invoice_id}/pdf` | — (**new**, 2026-09-29) | 08-B's "Invoice PDF" download, and since 2026-09-30 its view-only Inv# preview (minty-web fetches the bytes and draws them with pdf.js, so the `attachment` header below does not apply to it): the invoice drawn as Figma 09-A (`invoice_document.build_invoice_document` → `invoice_pdf.render_invoice_pdf`; fpdf2, Inter embedded, Noto Sans HK loaded only for a document with Chinese in it), NOT Stripe's hosted page - Stripe's PDF cannot be restyled, and its Bill to is the Stripe customer (one per payer). **Bill to = the invoice's billing account** as 08-B prints it (`account_name`, `store.account_email` - the billing email, else the business email every company on the account shares - else the payer's, the charged card's address laid out as the web's `addressLines`), read LIVE - a rename reaches old invoices; an invoice from before accounts is the payer's oldest account's (`portal.invoice_account_id`, the list's and dunning's rule). **Lines**: 09-A's plan lines ("Petty cash module only", "Payment request module only", "SuperMinty"), each with its companies listed under it, one row per Stripe item in Stripe's own words (`billing.line_description`) minus the plan the heading names. The lines must add up to `total` or nothing is served (500, ERROR logged); a character no font can draw is logged at ERROR with the invoice. `application/pdf`, `Content-Disposition: attachment; filename="Inv-<ref>.pdf"`, `Cache-Control: private, no-store`. 404 not the caller's (or a malformed id); 409 no document - never sent, a draft, void (`has_pdf` false); 502 the address could not be read from the processor |
| GET | `/invoices` | `my_invoices_api` | my invoices, newest first; `entity`, `page`, `per_page`, and **`account`** (added 2026-09-25) — ONE billing account's invoices for 08-B; a pre-accounts invoice (no `billing_group_id`) belongs to the payer's OLDEST account, the attribution dunning already collects by; someone else's account matches nothing. Echoes `account_id`. Each row carries **`retryable`** (2026-09-28): whether *Retry payment* would charge it now - per card, the ONE open invoice `dunning.retry_now` picks (`portal.retryable_invoice_ids`, calling `dunning._manual_target` over our own rows: the current period's renewal, else an open mid-period charge, never an abandoned give-up bill), and nothing on a card past its give-up deadline or whose access has run out - with or without a dunning stamp, since giving up leaves `paid_through` where it stopped. Judged over ALL the payer's failed invoices, before the `account` / `entity` narrowing (narrowed first, a company's own charge could be offered while the engine would collect the renewal), from the rows the list already read; a list with nothing failed reads nothing more. No Stripe call, no writes. And **`has_pdf`** (2026-09-29): whether `/invoices/{id}/pdf` has a document to serve - sent to the processor and paid, open or uncollectible (`portal.has_document`, the same rule the route's 409 asks, so the button never refuses). `hosted_invoice_url` is still answered, but minty-web no longer links it |
| GET | `/billing/payment-methods` | `my_payment_methods_api` | my saved cards and the default |
| POST | `/billing/payment-methods/setup-intent` | `my_payment_method_setup_intent_api` | a Stripe SetupIntent + the publishable key |
| POST | `/billing/payment-methods/confirm` | `my_payment_method_confirm_api` | save the confirmed card INTO A BILLING ACCOUNT, optionally as default. Body `{setup_intent, make_default?, billing_group_id?, billing_email?, billing_company?}`: `billing_group_id` puts the card on one of my accounts (08-B "Add payment method"; someone else's is 404 "That billing account couldn't be found."); `billing_company` + `billing_email` OPEN one ("New billing account") and both are then required (one alone is 422 in the form's own words). **Naming neither is 422 "Choose a billing account for this card."** (2026-10-01; the old "save the card and nothing else" path is gone). Every check runs before anything is attached or created at Stripe (`payment_methods.confirm_into_account`, shared with the onboarding twin; `confirm_setup` itself refuses an account-less confirm first thing too). Answers the wallet plus `account`. A retry after a lost answer re-answers the account it opened rather than opening a second on the same card |
| POST | `/billing/payment-methods/default` | `my_payment_method_default_api` | change the default |
| GET / POST | `/billing/entity-payment-method` | `my_entity_payment_method_api` | which card a company is billed to; nominate one |
| POST | `/billing/payment-methods/update` | `my_payment_method_update_api` | expiry, name, address |
| POST | `/billing/payment-methods/remove` | `my_payment_method_remove_api` | detach a card. **`account?`** (2026-09-25) — the billing account whose page asked: its own charged card is refused in its words, and when the card is the Stripe customer's default it is handed to that account's card instead of refused (08-B has no button for the customer default) |
| GET | `/billing/accounts` | — (**new**) | my billing accounts, oldest first (the first is the one 08-A shows by default): each with `name` (the company it bills under, else me), `billing_company` / `billing_email` raw, **`bill_to_email`** (2026-09-30, what 08-B's "Bill to" prints: `store.account_email` — the billing email, else the business email every company on it shares — else my email; the money emails and the invoice PDF use the same rule), `card` (the one it CHARGES, null when Stripe no longer holds it), `cards` (its shelf, `is_default` = THIS account's card), `address` (the charged card's Stripe billing address — accounts hold none), `companies` (`entity_id`, `entity_name`, `past_due`), `in_dunning`, `past_due`, and **`next_bill`** (2026-09-25, 08-B's "Amount (estimated)": `{amount, amount_minor, currency}` or null - what its next renewal will charge, priced by the renewal runner's own `build_renewal` for the period starting on the next billing date, with the trials that will have converted by then; `portal.next_bill_for_account`); plus the payer, ONE `next_billing` / `next_billing_iso` (every account renews on the payer's anchor), the flat wallet, and `countries` and **`publishable_key`** only with `?countries=1` (08-C, whose address form is Stripe's own `AddressElement`: the registry limits its countries, the key mounts it - null where this environment has no Stripe) |
| POST | `/billing/accounts/update` | — (**new**) | `{account, billing_company?, billing_email?, address?, cardholder?}` — 08-C. Validated first (company not blank, email shaped and English only, each at most 255 characters; address needs line 1 and a registered country; `cardholder` - the name Stripe's address form asks for with it - at most 255), then the address and cardholder to the charged card at Stripe in ONE `billing_details` write, then the name — Stripe first because it is the write that fails |
| POST | `/billing/accounts/default-card` | — (**new**) | `{account, payment_method}` — the card the account CHARGES (08-B "Set as default", 08-N): both halves of the pair, and the Stripe customer default untouched |
| POST | `/billing/accounts/move` | — (**new**) | `{entity, account}` — "Change billing account": the company moves to another of my accounts; nothing is charged and its paid days travel (`store.nominate_group_for_entity`, source `moved`). **Since 2026-09-29 also the FIRST placement** of a company on no account yet (a card-free trial; source `chosen`, `moved.from_account` null): Manage Subscriptions' confirm asks which account bills a change before applying it; no consent is written (the confirm's seam does that), nothing is charged, no days to carry. Refused (409): a PAST-DUE company (its debt, its retries and "Pay now" follow the account it is on), a target in dunning, a target whose card is gone. Answers the accounts plus `moved` (null when it was already there) |

**Live since step 3 slice A (2026-09-21)** — `billing/api/me.py`, each view the port of its Flask
twin. What the views keep is Flask's shell: `400 {"error": "<field> is required"}` for a missing
routing id, `404 "That company isn't on your billing account."` when the read model answers
None (not-the-payer and no-such-company are the same answer), `422 {"error": <the service's
sentence>}` for a stated refusal (handovers, the invitation - 422 not 403 because the client
shows the server's words only when they read as prose), each route's own 500 copy for a
surprise, and `payment_methods.run`'s 200/409/422/500 for the wallet. What the framework does
instead: CORS on every answer including errors (`corsheaders`; a preflight is 200 where
Flask-CORS said 204, and `Vary: origin`), the OPTIONS answer, the bearer check.
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
| POST | `/{entity_id}/modules/{action}` | `POST /entity/settings/module/<org_id>/<action>` (`entity/routes/settings.py` 1419–2431) | one of the ten actions below, JSON body per action |

Actions (`billing/api/modules.py::ACTIONS`): `authorize-billing`, `restart-quote`,
`restart-billing`, `start-trial`, `resume-preview`, `subscribe-preview`, `cancel-preview`,
`retry-payment`, `cancel`, `renew`. Any other word is 404 "Unknown action." — including Flask's
other nine, **deleted 2026-10-01** under the card rule (§1): `checkout`, `confirm-billing`,
`checkout-complete`, `payment-method`, `manage-billing` (setup-mode Checkout and the Billing
Portal) and `payment-methods`, `payment-methods/setup-intent`, `payment-methods/confirm`,
`payment-methods/default` (the module page's copies of the card routes; `/api/me/billing/*` are
the card routes). `restart-billing` (`{codes, payment_method?}`) nominates the card it was given,
answers **402 "Choose a card before restarting billing."** when the company has no card
nominated, and otherwise charges through `checkout.confirm_modules_checkout(entity, user,
requested_codes=…)` — it never answers `{url}`; success is `{ok: true, restarted: [...]}`. The
engine decides on the card nominated for the company (`store.card_for_entity`), never the
customer default, and refuses with 402 "Choose a card before subscribing." if none is (the card
can vanish between the route's check and the charge). Reading needs `MODULE_VIEW`, acting needs
`MODULE_MANAGE` **and** the payer rule (`store.may_manage_subscription`). `retry-payment`
answers in `api/_retry.py`'s words, `unavailable` included. `renew` - undoing a cancellation
whose extension was already invoiced charges the rest of the period - is 402 only for a real
decline; 409 "This module is already being restored. Refresh the page in a moment." while
another press's charge is in flight, and 503 "We couldn't reach the payment provider. Nothing
was charged - please try again shortly." when the processor failed (both 2026-09-30, §6).

**Live since step 3 slice B (2026-09-21)** — `billing/api/modules.py`. The gate is one function
every request runs through (`_gate`): the path's company must be the one the token was checked
against (`X-Entity-Id`, else the claim; a mismatch is 403 "That token is for a different
company."), then the permission (403 with Flask's sentence), then for every action the payer
rule (403 "Only the person who pays for this company can change its subscription."; the one
exception, Stripe's return leg `checkout-complete`, went with it on 2026-10-01). Flask pinned
these rules by reading the decorator stack with regexes (`test_subscription_payer_permission`,
`test_restart_billing_guard`); here every action is called as the wrong person
(`billing/tests/api/test_module_settings_api.py`). `restart-quote` also answers GET, as Flask
served it.

The page model (`GET /{entity_id}/modules`): `entity_id`, `entity_name`, `cards` (Flask's card
dicts key for key — minty-web's `ModuleCard` type — with `period_end` ISO and `access_end_date`
re-shaped to `YYYY-MM-DD` because the client counts days from them; `IsoJSONEncoder` for this
router, since Flask never served this page as JSON), `summary` and `panel` (opaque to the client
until its screens read them), `next_payment_date` (read off the panel), `can_manage_modules`
(admin AND payer, or no payer yet), `payer` (`{user_id, name, email}` when it is somebody
else, else null), `viewer` (`{name, initials}`), `consent_takeover`. Dates render ISO, the one
deliberate difference from Flask left (the Stripe return URLs and `checkout-complete`'s JSON
answer went with the hosted routes on 2026-10-01).

### `notice` (`/api/entities`, `NoticeBearerAuth`)

`GET /{entity_id}/subscription-notice` — Flask's `/api/entity/<id>/subscription-notice`
(`entity/routes/modules.py`), the plural being the one path change; keeps `settings_path`, which
is now Flask's hand-over to minty-web's module page, a relative path:
`/handoff/minty-web?next=/subscription/entities/{id}/modules&entity_id={id}` (url-encoded,
`notify.handoff_path`; Flask's own notice points at the same destination). It was
`/entity/settings/module/{id}`, Flask's retired settings page.

**Two kinds, no trial notices (the user's decision, 2026-10-01).** `billing/services/notices.py`
emits `past_due` (critical; only once the customer has been told, `dunning.told_of_failure`) and
`pending_cancel` (warning; a PAID module cancelled and still inside its paid period - a cancelled
free trial also reads `pending_cancel` and is skipped by `trial_cancelled`). The trial kinds -
`trial_ending`, `needs_card` / `needs_consent` (a trial that will not convert) and the lapsed
trial's `needs_card` - were removed, with `TRIAL_ENDING_SOON_DAYS`. `entity_modules._NOTICE_ORDER`
is `("past_due", "pending_cancel")` and doubles as the list of kinds: an emitted kind missing
there makes the sort raise, which the route turns into an empty notice. The trial-ending EMAIL
(`notify.py`, event `trial_ending`) is unchanged and is what tells a payer about their trial.

**Live since step 3 slice C (2026-09-22)** — `billing/api/notice.py`. Its auth class is
`EntityBearerAuth` plus Flask's one fallback: a token that names NO company (minty-payment-request-web's
refresh path mints through minty-payment-request-api and sends only the bearer, never `X-Entity-Id`) is
held to the caller's membership of the company in the PATH, which is what authorises the read
in any case. A token that names another company is 403 `entity_mismatch`; a stranger is refused
at the door (401 where Flask said 403 `not_a_member` — minty-payment-request-web treats every non-200 as
"no notice"). A builder failure is `{"items": []}` with 200: a notice never takes the landing
page down. Stateless: the "show once per session" claim stays Flask's.

### `onboarding` (`/api/onboarding`, `BearerAuth`; the company is in the body)

| Method | Path | Flask origin (`entity/routes/create.py`) |
|---|---|---|
| GET | `/payment-method` | `onboarding_payment_method_status` |
| GET | `/billing/payment-methods` | `onboarding_billing_payment_methods` |
| POST | `/billing/payment-methods/setup-intent` | `onboarding_billing_setup_intent` |
| POST | `/billing/payment-methods/confirm` | `onboarding_billing_confirm` — the `me` twin's body and rules exactly (a billing account is required: 422 "Choose a billing account for this card." when none is named; all checks before Stripe, `payment_methods.confirm_into_account`) |
| POST | `/billing/payment-methods/default` | `onboarding_billing_set_default` |
| GET / POST | `/billing/accounts` | `onboarding_billing_accounts` |
| POST | `/billing/authorize` | `onboarding_billing_authorize` |
| POST | `/trials/start` | **new** — `{entity_id} → {trial_end}`; what finalize calls |

**`trials/start` must not fail silently** (decision 2026-09-21): minty-onboarding-api's native
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
Please try again." — never swallowed, as Flask's finalize did. Flask's
`/api/onboarding/plans` is NOT here: minty-onboarding-api serves the catalogue natively
(`onboarding/api_reference.py`). Neither are Flask's `POST /payment-method/setup` and
`/payment-method/complete` (a Stripe-hosted setup-mode Checkout returning to the wizard with
`?pm_session_id=`): deleted 2026-10-01 under the card rule (§1), with the `ONBOARDING_WEB_URL`
setting that only they read.

### Everything else

`GET /healthz` (liveness, no database) and `GET /api/openapi.json` (the contract). Nothing else
is public (`billing/tests/test_contract.py`).

## 3. Always on (the dark switch is gone)

Subscriptions are always on: every route answers. The feature-wide switch `SUBSCRIPTION_ENABLED`
and its `SubscriptionsDarkMiddleware` (every path 404 with CORS, the scheduler held, every job a
no-op, `revoke-ungranted` refusing) were removed on 2026-10-01, by the user's decision, once the
service ran on a test site. Removing it wrote nothing - no trial started, nothing granted or
revoked. What remains: the scheduler has its own switch, `SUBSCRIPTION_SCHEDULER_ENABLED`, which
alone decides whether the timer starts (§5); `manage.py subscriptions revoke-ungranted` stays a
deliberate command, dry unless `--apply`.

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
stamps, `created_by` only on insert). The access SWEEP exempts companies
still `onboarding` (the wizard writes the map at step 2 but trials start only at finalize);
the writer itself has no such check, exactly like Flask's.

Read-only mirrors: `user`, `user_entity`, `entities`, `entity_function`, `country_info`,
`currency_info`, `invitation`. No `migrations/`, `managed = False` everywhere;
`billing/tests/test_models_guard.py` and Minty's `audit_models.py` (four repos) keep it so.

## 5. The daily pass and the scheduler

`billing/scheduler.py` — the port of Minty's `services/app_runtime/scheduler.py`, in-process by
decision until Part 3's Terraform. FULL pass at `SUBSCRIPTION_SCHEDULER_FULL_HOUR` (05:00
`Asia/Hong_Kong`): close trials, raise renewals, retry dunning, notify trial-ending, sweep access
for everyone. LIGHT pass every hour except the full one: close trials and raise renewals, sweep
the payers touched. Gated by `SUBSCRIPTION_SCHEDULER_ENABLED` alone; started from
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
`notify-onboarding → notify-trial-ending → close-trials → repair-transfers → collect-transfers → run-renewals →
retry-dunning → sweep-access` (light: `close-trials → repair-transfers → collect-transfers →
run-renewals → sweep-touched`), each job caught on its own, a sweep skipped when a job that
grants entitlement failed before it. Both transfer jobs run **before** the renewal and for
mirrored reasons: `repair-transfers` writes a claim for money already collected, and
`collect-transfers` writes one by collecting it — run afterwards, the renewal would either
re-bill days already settled or skip a card that has never collected.
After run-renewals (with `issue`) the pass reports the cards billed THIS pass and still due —
more than a period behind, billed again next pass (`_log_renewal_backlog`, WARNING). **Fixed
2026-09-30, both engines:** it unpacked `due_renewals` as `(account, paid_through)` pairs after
the list became one `(account, group, paid_through)` per card, so it raised on the very case it
reports, OUTSIDE the step's try: `run_daily` raised and every pass skipped retry-dunning and the
sweep, for every payer, as long as one card stayed due (a declined renewal was enough). It now
reads the triple, counts only cards in the run's `issued` (a declined card is dunning's, not a
backlog), and sits in its own try (ERROR, the pass carries on) —
`engine/test_daily_backlog.py`, 4 in each engine (Flask's old test stubbed the two-value shape).
`scheduler.run_pass_now` runs on APScheduler's worker thread, so it opens the engine's
request scope itself (`billing.services._context.scope()` — what Flask's `app.app_context()`
gave the timer: the clock, policy, catalog and default-card memos) and calls
`close_old_connections()` either side of the pass.

**What the jobs answer since 2026-09-30 (this service only; §6).** `close-trials` has a fourth
bucket, `deferred`: a conversion the payment processor failed to make, kept in its trial and
tried again next pass. `collect-transfers` no longer re-charges a declined handover every hour -
it is chased like a renewal decline, by dunning. `run-renewals` and `retry-dunning` mail each
card the moment its outcome is recorded, and a card that raises is logged, held in its grace and
skipped ("could not be billed; retrying next pass") instead of ending the pass; a processor
failure skips the card as "processor unavailable; retrying next pass", with no email.

`manage.py subscriptions <job>`, the port of `flask subscriptions` (ASCII output, every job
inside a scope): `tick` (what a cron will call — full at the full hour, light otherwise),
`run-daily --mode full|light [--issue] [--days-before N]`, `close-trials [--limit N]`,
`run-renewals [--issue] [--user ID]… [--limit N]` (DRY by default — the only job whose flag moves
money; `run-daily` without `--issue` still converts due trials, retries dunning and sweeps),
`retry-dunning [--limit N]`, `notify-trial-ending [--days-before 3] [--limit N]`,
`notify-onboarding [--limit N]` (the setup reminder, below), `sweep-access`,
`reconcile-customers [--repair]`, `revoke-ungranted [--apply]` (deliberate only; dry unless `--apply`;
the ORM rewrite of Flask's two raw statements — `billing/tests/engine/test_revoke_ungranted.py`).
`manage.py plans list` reads the catalog and writes nothing — the quickest proof the service reaches
the database. On 2026-09-21 Django's and Flask's `revoke-ungranted` (dry) and `run-renewals`
(dry) were run against the same dev database (`postgres`) and answered identically (0 rows, 0
payers due).

## 6. Money and mail

Stripe: only this service holds `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` (cross-cutting
rule 9); `billing/services/stripe_client.py` is the one `import stripe` (settings only, never
the process environment — `settings_test` blanks the key so an unstubbed test call raises),
`billing_gateway.py` the one charger (Invoices, idempotency keys claimed before the charge).
`test_models_guard.py` allows the import in that one module only. Stripe is used for Customer,
PaymentMethod, SetupIntent and Invoice / InvoiceItem only: no Stripe-hosted page is ever opened
(setup-mode Checkout and the Billing Portal, with their return URLs, were deleted 2026-10-01 —
the card rule, §1).

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

**Every charge names the billing account's card, and there is no fallback** (the
per-entity-cards decision of 2026-08-25). A renewal (`renewals.run_renewals`), a mid-period
change and a trial conversion (`checkout._bill_module_change_in_house`, `changes.issue_change`),
a transfer's first charge (`transfers`), a dunning retry and a re-issued invoice
(`dunning._charge`) all hand the company's `payer_billing_group` to `billing_gateway`, which
pins its `stripe_payment_method_id` on the document and records `billing_group_id`. An invoice
raised with no card named is charged by Stripe to the customer's account default, which is a
bug, not a fallback: a company with no nomination is refused or skipped, never billed
elsewhere. Fixed 2026-09-29 in both engines: undoing a cancellation after its extension was
invoiced (`checkout._bill_reinstatement_in_house`) called `issue_change` without `group`, so
the reinstatement invoice carried no `billing_group_id` and went to the account default (the
dev DB's R1/DR1 Returner and Pair Co rows of 09-21 and the +catalogue N52b/M45/M15 rows of
09-29 are that bug's output); it now resolves the company's group and refuses (409, "Choose a
payment method for this company before restoring this module.") when there is none, proven by
`test_reinstating_a_company_with_no_card_is_refused_not_billed_elsewhere` in both engines.

**A refused purchase leaves no open invoice (2026-09-29, both engines).** A decline raises out of
`Invoice.pay` with the document finalized and OPEN; left open, `dunning.collect_due` chases it
(the payer's OLDEST open invoice) and 08-B offers it as *Retry payment* — a customer paying for
something never given. Trial conversions and handovers already voided theirs; a declined
reinstatement (`_bill_reinstatement_in_house`) did not: the module stayed cancelled and its bill
stayed owed. All three now go through `checkout._void_unpaid_invoice(invoice_id, entity_id,
what)` on BOTH failure paths (raised, and a non-paid status returned); it never raises. Pinned by
`test_a_card_declined_while_reinstating_voids_the_invoice_it_left_open` and its two twins (a
non-paid status; a void that itself fails) in both engines, each proven to fail with the void
taken out.

**Each Stripe item carries its own days (2026-09-29, both engines).** `issue_invoice` stamped
every `InvoiceItem.period` with the invoice's whole period, so Stripe's PDF, hosted page and
revenue recognition dated a prorated start, an upgrade's credit and an access extension as the
full month. `billing_gateway._item_period` now sends the line's recorded span
(`billing.Line.period_start` / `period_end`, schema item 23) and the invoice's period only for a
line that recorded none; an empty span is logged and sent as the invoice's — never a refused
charge over a display date. Future invoices only: a finalized invoice cannot be edited at Stripe.
Pinned by `test_each_item_carries_the_days_its_own_line_paid_for` in both engines'
`test_invoice_record.py`, proven to fail with the whole period sent.

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

**A draft nobody finalized is loud, a renewal's is FINISHED, and dunning does not call it
settled (2026-09-30, both engines).** `issue_invoice` creates the invoice (`auto_advance=False`),
records its id as a draft, adds the items and finalizes — and only then asks for payment. An
error or a crash in between left a draft that nothing ever touched again: Stripe does not
finalize it, dunning and Pay now chase only OPEN invoices, and every later renewal pass skipped
the period as "already invoiced; unpaid" — never billed, its extensions closed uncollected. On
an error the customer was told "payment failed" and, a day or so later, "Thank you for your
payment": dunning found nothing open, called it settled elsewhere and closed the episode. The
only automatic retry anywhere was the SDK's own (stripe 11.4.1, `max_network_retries` 2, within
the one HTTP call). Now:
- **Loud.** `billing_gateway.stranded_draft(invoice_id, key, where, next_step)` logs ONE line to
  grep for — `billing: STRANDED DRAFT <in_…> (<key>, <where>): created at Stripe but never
  finalized, so it is not billed. <next step>` — where the next step is `RESUMED` ("The next
  renewal pass finishes it."), `NOT_RETRIED` (a transfer: "finalize or delete it in Stripe"),
  `MISMATCHED` / `GONE` (a resume that refused) or `WITHDRAWN` (an earlier attempt's draft found
  by `issue_change`, which every caller refuses and withdraws). It fires from the renewal
  runner's except (which also puts the draft on the failed entry — `BillingError.invoice_id`
  was dropped), `repair_stranded` and `_bill_transfer_in_house`, `issue_change`, a refused
  resume, and dunning; `issue_invoice`'s own ERROR now names the draft and its key. "Loud"
  means ERROR on stderr and `logs/core.log`: neither repo has an alerting sink.
- **Finished** (`renewals._renew_one_group` → `billing_gateway.resume_invoice`). A renewal row
  that still reads "draft" was never charged — the finalize is recorded BEFORE `pay` is called,
  and Stripe cannot charge an `auto_advance=False` draft — so whatever Stripe now holds is
  finished on the account's CURRENT card: a DRAFT gets the items it is missing and only those
  (rebuilt from `store.invoice_lines`, matched by amount, company and days — never the words,
  which a resume rebuilds from names cut to 255 characters), is finalized and charged; OPEN is
  charged; PAID is adopted; VOID / UNCOLLECTIBLE are somebody's act and are not billed. An item
  nobody reserved, a count or total that disagrees with the reservation, or a draft deleted at
  Stripe (404) is refused: STRANDED DRAFT, nothing finalized, nothing charged, no email, and
  the pass skips the card ("left a draft; not finished"). A draft found by metadata (the crash
  came before its id was recorded) is finished in the same pass. The receipt states the
  RESERVED lines and total (`renewals._as_reserved`). No app idempotency keys on the resume's
  items or finalize, deliberately: Stripe replays a keyed ERROR for 24 hours, and the pass's
  advisory lock already serialises resumes. `issue_invoice`'s requests are unchanged — its tail
  became `_finalize` / `_collect` and its items `_item_kwargs`, shared with the resume.
- **Not settled.** `dunning._nothing_open_but_owed`: when nothing is open for the card, the row
  of the period it is behind on (`_current_period_key`) is re-read, and a draft there is logged
  and keeps the episode open — no recovery, no email, no attempt counted; the deadline still
  closes it. Only the CURRENT period's row counts, so a draft some abandoned purchase left can
  never hold a card in dunning. Pay now (`_retry_context`) answers `nothing_owed` as before but
  no longer closes the episode. Widened the same day to any missing or unconfirmed invoice
  ("Settled needs evidence", below).
Pinned by `engine/test_invoice_resume.py` (14, on the real store with the refresh tests' fake
processor, which now keeps a draft's total live and answers 404 for a deleted invoice), the
resume and stranded-draft cases in `engine/test_renewal_runner.py`, three in
`engine/test_dunning_policy.py`, one each in `test_dunning_retry_now.py`,
`test_subscription_transfer.py` and `test_change_billing.py` — the same in Minty's twins.

**A row says what Stripe says, however it was learned (2026-09-30, both engines).** There is no
webhook, so the paths that find an invoice after the fact are the only writers of
`subscription_invoice` — and they left it behind: a reservation recovered by metadata was
settled with its id and status only (the list showed "—" for its paid date and link for good),
a renewal row left "open" by a crash after payment was answered from the store for ever (never
adopted, shown as failed with Retry payment), `changes.issue_change` returned an earlier
attempt's invoice without touching the row that attempt reserved, and dunning's "settled
elsewhere" left the card's rows "open". Now `billing_gateway.record_found_invoice(record,
found)` is the one way an invoice found at Stripe becomes the row — status, total, times, link,
and the card when paid, exactly what the normal path writes — and `refresh_record(record)`
re-reads a row that can still move (never raises; the stored status is the answer when Stripe
cannot be reached). `renewals._already_invoiced` refreshes a stored draft/open row before
answering (one read per pass for an invoiced, unsettled period — in practice a card in dunning;
the light pass is hourly) and records a found reservation whole; `issue_change` records what it
found; `dunning._nothing_open_but_owed` re-reads the card's rows still reading "open"
(`store.open_invoices_for_group`). This also makes `_settle_period`'s "the next renewal adopts
it" true for a row that has an `external_id`. Existing rows are not backfilled (the user's
call: "Only prevent new ones").

**The Stripe API version is pinned (2026-09-30, both engines).** `stripe_client.get_stripe()`
set only the key, so the API version was whatever the installed SDK defaulted to. stripe-python
12 defaults to `2025-03-31.basil`, which REMOVED Invoice `charge` and `payment_intent`: the
card-capture reader (`_capture_payment_method`) would have recorded no card, and the dead-payment
check (`_payment_is_dead`) would have answered "not dead" for ever — both silently.
`STRIPE_API_VERSION = "2024-12-18.acacia"` (11.4.1's own default, so nothing changes today) is
now set on every call, and Minty's `prune_replay_stripe.py` uses the same pin.
`billing/tests/test_stripe_api_version.py` fails the build when the installed SDK's default or
its Invoice fields stop matching, naming the migration (`charge` → `payments`, `payment_intent`
→ `payments.data.payment.payment_intent`); at run time a REAL Stripe invoice with either field
absent is logged at ERROR (`_api_changed`, judged by `stripe_client.lacks_field`, which ignores
the hand-made dict fakes the tests use).

**An extension-only invoice says so (2026-09-30, both engines' portal).** `portal._event`
returned "renewal" for any invoice with a whole-period line, but the renewal runner writes a
cancelled company's extension as kind `full` too, so a card renewing only to collect its last
company's extension read "Renewal · Petty Cash". A renewal now needs a whole-period line that is
not an extension; one carrying an extension is still a renewal. Headline only — the emails, the
PDF and the CSV never read it.

**The billing fixes of 2026-09-30 — this service only.** Six failures found while fixing the
stranded drafts (above), fixed with the user's decisions of that day; a review of the fixes found
five defects in them, fixed the same day and folded in below. Flask's engine is to be deleted
(the user's decision), so none of this was carried back: from here the two engines differ on
these paths.

**The processor failing is not the card declining (the user's rule: "retry next hour").** Every
failure used to read as a decline: a Stripe outage, a timeout, a rate limit or our own key
expired a trial at its end, and told a renewing customer "We couldn't process your payment".
`stripe_client.is_transient(exc)` names the processor's failures (`APIConnectionError`,
`APIError`, `RateLimitError`, `AuthenticationError`, `PermissionError`, `IdempotencyError`, and
`StripeNotConfigured`, which `get_stripe` now raises — inside `issue_invoice`'s try, so it is a
`BillingError` like any other); `billing_gateway.retryable(exc)` answers for a raw error or a
`BillingError` (`.retryable`, `.claimed`). A card error, an invalid request and anything
unrecognised — our own bugs included — stay declines: the conservative reading, and what every
path was written for.
- **Renewal** — a retryable failure holds a SILENT grace (`store.hold_group_grace`: the card's
  rows go past due with NO dunning stamp), sends nothing, starts no dunning, and is logged at
  ERROR every pass — CRITICAL with three days of grace left (`renewals._hold_grace`, which a
  card whose renewal RAISED gets too: the processor, or our own error). The next paid, adopted
  or covered outcome ends it (`store.release_group_grace`). Down for the whole grace, access
  lapses as after any grace.
- **A dunning stamp now means exactly "the customer was told".** `dunning.customer_told` (a
  stamp, or collection over) and its per-company form `dunning.told_of_failure(entity_id, now)`
  (unknown is told) are the one rule the notice (`notices`), the module cards'
  `subscription_status`, the subscriptions list (`portal._module_state(told=)`) and the
  accounts' `past_due` ask: a silent grace shows as active, never as a failed payment.
- **Dunning and Pay now** — `retry_invoice` answers `(False, UNAVAILABLE)`, a marker like
  `DEAD_PAYMENT`: the attempt is given back (`store.refund_group_dunning_attempt`), nothing is
  mailed, and Pay now answers its own status, `unavailable` ("We couldn't reach the payment
  provider. Nothing was charged — please try again shortly." — `api/_retry.py`), never "that
  card was declined". A `refresh_invoice` that raises on the processor counts the same.
- **Trial end** — a retryable failure raises `checkout.ChargeDeferred`: nothing is withdrawn,
  the trial stays in `trial` and keeps its access (`access_sweep._conversion_pending` leaves a
  trial past its end on for up to the grace), and the next pass tries again; past the grace
  window it expires as a decline would (ERROR). `convert_or_expire_due_trials` reports these
  under `deferred`. Each attempt takes a fresh `convert-<entity>-<when>-<codes>` key
  (`changes.change_key(kind="convert")`), and before a new one
  `checkout._resolve_prior_conversions` settles the earlier attempts of THIS close-out — those
  whose WHEN (read off the key: the app's clock, the one `trial_end` is on, in whole seconds, so
  an attempt in the second the trial ended counts) is not before the trial's end. PAID is
  adopted, never charged again — unless its period has ENDED by now: it paid for that period,
  and adopted, the company entered the new one marked as billed and its renewal skipped it (a
  free month). OPEN and never tried is charged on the current card; OPEN and refused is
  withdrawn and the trial expires; a DRAFT is withdrawn; one paid for a different module set
  waits for a person (ERROR). The processor unreadable defers again.

**A charge whose answer was lost is a charge.** When Stripe took the money but the reply never
arrived, the attempt read as a decline: a buy or a handover was refused and charged again on the
retry (a fresh key), a trial conversion expired paid. Now `billing_gateway._collect` re-reads the
invoice after a failed `pay` and records it PAID when it is; `retry_invoice` does the same (and
still spots a cancelled payment, `DEAD_PAYMENT`); and `void_invoice` answers
`"paid" | "voided" | "deleted"` — a paid invoice cannot be withdrawn and is recorded paid.
`checkout._void_unpaid_invoice` passes the answer on, and conversion, buy, handover and
reinstatement GRANT what was paid for (WARNING: "lost reply"). Wherever a reservation is
recognised by its metadata, the row is read again after `record_found_invoice`: the store
settles a FRESH copy, so the old object still had no id, and the re-check after it asked Stripe
for invoice None and read the refusal as a decline (`changes._next_attempt`,
`checkout._resolve_prior_conversions`, `transfers._settle_last_attempt`).

**A paid charge is never reported as a decline.** The renewal's post-charge writes (extensions,
the card's date) sat in the charge's own try: one failing put a PAID card into dunning, sent "We
couldn't process your payment", then "Thank you for your payment". `renewals._renew_one_group` is
two steps now — `_charge_period` (resolve, issue, resume or charge; its failure is a decline or a
processor failure, `_charge_failed`) and `_record_charge` (its own try; a failure is an ERROR and
the next pass adopts the paid invoice by its key). Dunning marks a paid retry `_paid` the moment
the money moves — never mailed as "failed again" — and records the recovery in its own try,
counting it `recovered` (the "Thank you") only once the episode has actually ended. Before GIVING
UP it re-reads the current period's invoice (`_paid_at_the_deadline`) and asks whether the card
is paid up (`_paid_up`: `paid_through` past now and no handover's first charge owed — the renewal
pass adopted a payment and moved it on, so a re-read by the period it WAS behind on finds
nothing): either is recovered, not closed. Pay now at the deadline makes the same check
(`_retry_context`: `nothing_owed`, the episode ended active), and answers `paid` whatever becomes
of recording it (it used to be a 500).

**Settled needs evidence.** "Nothing open for the card" ended dunning as recovered — "Thank you
for your payment", companies back on — for any failure that left no open invoice.
`dunning._nothing_open_but_owed(account, group, now)` now calls a card still BEHIND
(`paid_through` not past `now`) owed unless its current period's invoice exists and was paid,
voided or written off: missing, never confirmed at Stripe, a draft (STRANDED DRAFT) or still open
after a failed re-read is owed — no recovery, no email, no attempt. A handover's first charge
still due (`store.handover_owed`) is owed too.

**An open renewal nobody charged is charged (the user's call).** A crash between recording the
invoice open and calling `pay` left it open, never tried, with dunning never started — so dunning
never looked, every pass skipped it "already invoiced; unpaid", and the companies went dark at
the next sweep (an active row gets no grace). `renewals._open_invoice` asks Stripe's own record
(`billing_gateway.recheck` + `declined`: `attempted` and the payment's `last_payment_error` —
checked in Stripe test mode: false and none before `pay`, true and `card_declined` after a
decline): NEVER TRIED, no dunning running and collection not over → charged now on the account's
current card (`resume_invoice`, which also takes an open row now); TRIED AND REFUSED → dunning's,
but a decline whose dunning never started (a crash just after it) is started, and a decline
notice that never went out (mail down, a restart) is sent — once, by its period key
(`notify.already_sent`); collection over → left, with an ERROR if it was never charged; unknown
→ left, ERROR.

**Mail per card.** Renewals and dunning mailed after the whole batch, so an exception or a
restart lost every notice before it — for good. Both now mail each card as soon as its outcome
is recorded (`notify`'s rule 3, rewritten). `run_renewals` also catches a card that raises:
logged, held in its grace (`_hold_grace`), "could not be billed; retrying next pass", and the
pass goes on.

**A declined reinstatement can be retried.** Its key (`change-<entity>-<covered_to>-<codes>`)
never changed between presses, and the declined attempt's voided invoice kept it claimed: every
retry was refused — 402 "check your payment method", the card never tried — until the
cancellation window ran out. `changes.issue_change(attempts_of=payer)` now raises each press
under the next attempt's key (`<key>~2`, `~3`: `~`, because `dunning._names_period` reads `-` as
the same period, and never a refresh's retired `~in_…`), settling the latest attempt first
(`changes._next_attempt`): paid → adopted, open → withdrawn, draft → deleted. A reservation
Stripe cannot show yet is another press's charge IN FLIGHT for its first ten minutes
(`changes.IN_FLIGHT`) and is refused as claimed — discarded, the next attempt charged the
customer twice the moment the first landed; after that it is looked up, and discarded only if it
never reached Stripe. A new attempt never reuses a key (Stripe replays a keyed error for 24
hours). A press racing another answers 409, the processor failing 503, and only a real decline
402 (§2). Jammed rows heal themselves: their key is void, so the next press is `~2`.

**A handover onto a card that has never collected.** `_bill_transfer_in_house` set the card's
`paid_through` only on the PAYER's first charge — so a retried first charge (the anchor is
written before the charge), an already-paying payer's new card, a zero-total or an adopted charge
left the card with no date: never renewed, and read as no access at all.
`checkout._establish_card_cycle(group, first_charge, period)` — the trial rule, judged on the
CARD — now runs on every paid return. A PARKED handover's company is paid through its
`collect_at` (`store.paid_through_for_entity` answers the later of the card's date and that), so
a brand-new payer's company is no longer switched off at accept; and the renewal leaves a
company whose parked charge falls in or after its period off the invoice
(`store.entities_awaiting_handover`), which billed the old payer's days and then the window
again.

**A deferred handover charge is chased like a renewal decline (the user's call).**
`transfers.collect_due` voided a failed charge's invoice and retried hourly under a fresh key
with no end, told the new payer nothing, and dunning — finding nothing open — thanked them for a
payment nobody made, every day or two. Now (`_collect_one`): the LAST attempt is settled first
(`_settle_last_attempt`: paid → settled; open and refused → dunning's, made sure to be running
with its notice sent; open and never tried → charged; a draft → finished). A fresh attempt
(`_bill_transfer_in_house(keep_open=True)`) leaves a declined invoice OPEN: dunning starts, the
`renewal_failed` notice goes out once (deduped per attempt key), and dunning retries it daily;
dunning collecting it runs `transfers.settle_paid_handover` (the claim, the offer, the audit — as
the collection would have). A processor failure holds the silent grace; no card is logged at
ERROR and chased as a decline (the notice, and dunning) rather than silently; a card whose
collection is already over is not put back into dunning. `collect_at` stays set until the window
is paid. Past the grace the last attempt is read once more — paid at the last moment is settled,
not abandoned — and otherwise the offer is abandoned ("its grace ran out unpaid") and the invoice
it left open is withdrawn with it: left open, dunning or *Retry payment* could still collect it
for a handover that is over.

**Still open (found 2026-09-30, not fixed):**
- two double-charge windows: a buy or a handover accept whose reply is lost AND whose re-read
  after the void fails too; a crash between a buy's charge and `_grant_purchased_modules`;
- an operator-voided renewal still gets "Thank you for your payment", and its card's date never
  moves;
- a module combination the catalogue cannot price is handed over free (`_bill_transfer_in_house`);
- cards the handover bug already left without a date are not repaired ("only prevent new ones");
- a non-Stripe exception (our own bug) still reads as a decline;
- during a silent grace the invoice list still shows the period's open, never-tried invoice as
  failed;
- a deferred trial's module card says `trial_closing` for its first six hours
  (`TRIAL_CLOSING_WINDOW`) and trial expired after that, while its access stays on for the grace.

Mail: `SMTP_URL` (parsed into Django's `EMAIL_*` settings by `config/smtpurl.py`) on the same
Brevo SMTP Minty uses, sender `SUBSCRIPTION_EMAIL` (fallback `MAIL_FROM`, the setting
`DEFAULT_FROM_EMAIL`; the setup reminder's is `ONBOARDING_EMAIL`, production
`onboarding@dailyminty.com`, same fallback - `notify.sender_for`); each SMTP step times out after `?timeout=` seconds (`settings.EMAIL_TIMEOUT`, default 10; Django's
own default blocks forever, inside the pass that holds the scheduler lock). `notify.mail_configured()` is false for the console and dummy backends
and for SMTP with no host (`SMTP_URL` unset), and it is checked BEFORE the dedupe claim — an unconfigured
host skips the notice without spending it, so the first configured run still sends it (Flask's
extension always existed, so its unconfigured case claimed and then failed to connect; same net
effect). The template (`templates/email/subscription_notice.html`; the receipt's went with the
receipt, 2026-09-30) is Minty's verbatim on the Jinja2 backend — a render from each backend with
the same context is byte-identical (checked 2026-09-21); the ten inline images (the logo and nine illustrations) live in `billing/static/email/`;
`InlineImageMessage` keeps the `multipart/related; type="multipart/alternative"` wire shape
with `Content-ID` parts; dedup stays in `subscription_email_log`. Links in emails point at
minty-web through Flask's login-gated re-handoff (`notify.settings_url` →
`{PETTY_CASH_PUBLIC_URL}/handoff/minty-web?next=/subscription/entities/{id}/modules&entity_id={id}`,
`notify.portal_url` → `…?next=/subscription/subscriptions[/incoming]`), so no token ever
travels in a link from here — Flask stays the only minter. Flask's `/handoff/minty-web` exists
and minty-web is live (2026-09-30), so those links work.

**Which emails exist (2026-09-30, both engines): exactly the eight approved Figma designs** —
trial ending, renewal failed, dunning retry failed (the same design), payment recovered, and
the four transfer notices. `renewal_paid` (the receipt) and `trial_expired` are retired: a
successful charge and a lapsed trial are silent (Stripe's own receipt, if switched on in its
Dashboard, is the only receipt). Dates are written in the company's time zone
(`notify.entity_zone`: `entities.timezone`, Asia/Hong_Kong when NULL or unknown); an email about
a whole billing account uses the zone all its companies share, else Asia/Hong_Kong
(`notify.account_zone`). The two payment-failed emails name the LAST FULL DAY to pay
(`notify.pay_by`): the day before the account's past-due access runs out
(`notify.payment_deadline` → `dunning.suspension_at`, `paid_through` + the past-due window),
because from that instant "Pay now" is refused. Every read behind a date is its own savepoint,
taken before the dedupe claim, so a failed one sends the email without that detail and never
leaves the claim's transaction broken.

**The setup reminder (2026-10-01, this service only - Flask never had it).** One email that is not
about money: `onboarding_reminder`, "Finish setting up {Company} on Minty" (Figma frame 2969:1368
on page 573:990). `billing/services/onboarding_reminders.py::notify_unfinished_onboarding`, run
first in the full daily pass (`notify-onboarding`), picks every company still `onboarding` that
has been quiet - `entities.updated_at`, which a trigger moves on every write including the
wizard's saved step - for **1, 3 or 7 days**; each reminder has a window (1-2, 3-6, 7-8 days) so
a missed pass catches up, and nothing goes out after day 9, so the companies abandoned before this
shipped are never mailed. It goes to the person who started the company - the earliest approved
admin on `user_entity`, skipped (not replaced) when that user is inactive - at their own address,
never the company's business email. Dedupe key `{entity}:{user}:{updated_at}:{day}`: each
reminder once per quiet spell, and a fresh series after they come back and stop again. It stops
once `finalize` moves the company off `onboarding`. The body lists the steps still to do from
`onboarding_saved_step` (`notify.ONBOARDING_STEPS`/`remaining_steps`: 2 modules, 3 invite, 4 Xero,
5-8 accounts; the saved step itself counts as not done, NULL shows all four) under the template's
optional `steps` block, and both "Continue setup" buttons open Flask's login-gated `/entity/{id}`
(`notify.setup_url`), which sends a company in setup into the wizard at its saved step - the
wizard's own one-hour token never goes in an email. No unsubscribe link: three emails at most,
stopping by themselves. Review with `manage.py preview_emails [--only EVENTS] [--send ADDRESS]`
(all nine events from fixtures, no log rows - the port of Minty's `preview_billing_emails.py`).

**Who a money email goes to (2026-09-30, both engines).** Every notice went to the payer's login
address (`notify.recipient_for`), so a billing account's "Billing Email" — where the company
wants its invoices, and what its invoice prints as Bill to — never received one. Now
`notify.address_for(user_id, event, context)` is the one place a recipient is decided: the three
MONEY emails (`MONEY_EVENTS`: `renewal_failed`, `dunning_retry_failed`, `payment_recovered`,
each already carrying `billing_group_id`) go, when the account is THIS payer's, to
`store.account_email(group)` — the user's order: its `billing_email`, else the business email
EVERY company on the account shares (`store.shared_business_email`: blanks skipped, case
ignored, two different addresses mean none - one company's inbox never gets another's
charges; "the companies on it" are the ones 08-B lists, so a company that has left is never
mailed), else the payer. Trial ending goes to its company's business email (onboarding step 1),
else the payer; the handover notices always go to the person. Anything that fails to read -
logged - goes to the payer. The same `account_email` is the invoice PDF's Bill to email and
the accounts read's `bill_to_email`, so the inbox an email reaches is the one its invoice
names. (The notice template draws no greeting, so the address is all that changes.) Dedupe
keys never held the address, so nothing is re-sent. Two silent failures fixed with it:
`subscription_email_log.recipient` holds 200 characters and a billing email 255, so a long one
failed the save AFTER a successful send and the same email went out on every run (the address
is now cut to `RECIPIENT_MAX`); and `dunning._notify_dunning` suppressed a retry notice for any
PAYER that recovered in the pass, swallowing a second card's "your payment failed again" — it is
keyed by payer AND account now. `replay_scenarios --notify-to` redirects `address_for` as well
as `recipient_for`, so a replay never mails a real billing address. Pinned in
`engine/test_subscription_notifications.py` (13 new) and Minty's twin.

## 7. Configuration

`.env.example` is the list. `APP_ENV` (`development` | `production`, the default; `DEBUG`
follows it); the scheduler switches; `SECRET_KEY` (shared, verify only); `DATABASE_URL` with
`?schema=` (default `pettycashv3`, a setting never a literal — `config/dburl.py`,
`billing/tests/test_schema_name.py`); `STRIPE_*`; `SMTP_URL` / `MAIL_FROM` /
`SUBSCRIPTION_EMAIL` / `ONBOARDING_EMAIL`; `PETTY_CASH_URL` (the forwarded call);
`PETTY_CASH_PUBLIC_URL` (the address a PERSON reaches Minty at — every link in an email;
defaults to `PETTY_CASH_URL`, which in the docker stack is the internal service name, so set it
there); `MINTY_WEB_URL` / `PAYMENT_REQUEST_WEB_URL` / `CORS_ALLOWED_ORIGINS` (the browser
origins, default those two plus `PETTY_CASH_PUBLIC_URL`; `x-entity-id` is allowed). The
onboarding web URL is not read (2026-10-01): its only reader was the deleted onboarding setup
Checkout. In the docker stack it is the `subscription-api` service on 8000.

## 8. Where it is tested

`billing/tests/`: `test_contract.py` (the tables above, the OpenAPI document, `/healthz` and the
CORS preflight), `test_auth.py`, `test_models_guard.py`, `test_schema_name.py`, `test_settings_guard.py`,
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
pytest`; 985 passed, 00:10). `e2e/test_smoke.py` against a running service (`e2e/README.md`; its dark cases were
removed 2026-10-01). After step 3 (2026-09-22): 1122 + 2 skipped on SQLite (00:09), 1124 on
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
`engine/test_portal_billing_accounts.py`. After the invoice PDF and the two engine fixes of
§6 (2026-09-29): 1415 + 2 skipped on SQLite (00:26), 1417 on Postgres (00:38) —
`engine/test_invoice_document.py` (16: 09-A's plan lines and their company rows, the credit's
sign, Bill to from the account and every fallback, the rows EQUAL to what a gateway run sent
the fake Stripe, and the three refusals), `engine/test_invoice_pdf.py` (10: the frame's
coordinates read back from the canvas's own record, the shrink-to-fit reference, flow, paging,
the CJK fallback and the loud missing glyph), `api/test_invoice_pdf_api.py` (11, incl. `has_pdf`
against the route for six statuses), and the reinstatement-void and item-period tests (3 + 3,
the same in Minty's twins). After the follow-ups of §5/§6 (2026-09-30: stranded drafts and their
resume, the row reconcile, the pinned API version, the extension label, the money emails, the
backlog crash): 1468 + 2 skipped on SQLite (00:17), 1470 on Postgres (00:21) —
`engine/test_invoice_resume.py` (14), `engine/test_daily_backlog.py` (4),
`test_stripe_api_version.py` (6) and new cases in the renewal, dunning, change, transfer,
portal and notification files; Minty 2130 + 2 skipped. Every fix was proven by a mutation that
puts its old behaviour back (20 mutants, each failing at least one of the new tests, in both
engines). After the billing fixes of 2026-09-30 (§6, this service only): 1597 + 2 skipped on
SQLite (00:20), 1599 on Postgres (00:27), no call reaching Stripe — `engine/test_processor_failures.py`
(25), `engine/test_change_attempts.py` (7), `engine/test_conversion_attempts.py` (10) and new
cases in the renewal, dunning, retry-now, transfer, transfer-charge, trial, manage, change,
portal, notice, module-card and lifecycle files. The fake processor in
`engine/test_invoice_refresh.py` raises the SDK's own error classes for an outage, can lose a
`pay` reply, and answers `attempted` / `last_payment_error` as Stripe does. Every fix was proven
by a mutation that puts its old behaviour back — 42 mutants (31 for the six failures, 11 for the
review's findings), each failing at least one test. Both conftests (`engine/`, `api/`) now import
`billing_gateway` before `_no_stripe` stubs the client: first imported under the stub, the module
kept the stub's `get_stripe` after the test that installed it. After the dark switch and the
trial notices were removed (2026-10-01): 1571 + 2 skipped on SQLite (00:30), 1573 on Postgres
(00:39) — `test_dark.py` deleted (its scheduler-switch case moved to
`engine/test_scheduler_pass.py`, `/healthz` and the preflight to `test_contract.py`), the dark
cases of `test_subscriptions_command.py` and `test_scheduler_pass.py` and the eight trial cases
of `engine/test_subscription_notice.py` deleted, one case added there (`test_no_trial_produces_a_notice`:
running, no card, consent only, lapsed and cancelled trials each say nothing).

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

Regenerating a golden run (nothing is kept on disk): Flask from `C:\Github\Minty` with
`DATABASE_URL` pointed at the LOCAL database and `SMTP_URL=` blanked unless the notices are wanted -
`python scripts/subscription/replay_scenarios.py --run <key> --reset --teardown --setup --replay
--report`; Django from this repo - `python manage.py replay_scenarios --run <key> --as
angelika.tardaguela+django-<key>@… --tag <tag of the SAME length as the run's> --reset --teardown
--setup --replay --report` (a fresh payer, because Stripe keeps an idempotency key for 24 h and the
keys are per payer; the same length because the report truncates names to fixed widths); then
`python scripts/replay_diff.py <flask log> <django log> --tags <run tag>=<clone tag>` — IDENTICAL is
the only acceptable answer where a run stays off the paths §6's fixes of 2026-09-30 changed; the
Flask engine (to be deleted) does not have them. Keys: `X1 C1 L1 L2 R1 E1 angelika angelika-lifecycle angelika-split`.
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
- `tests/test_onboarding_payment_method.py` (14) — **PORTED, slice C**: `billing/tests/api/test_onboarding_payment_method.py`, 13 of the 14 (not `test_buy_now_card_routes_answer_the_onboarding_origin`: the wizard's browser never calls this API, minty-onboarding-api proxies server-side) plus the account routes, the wallet-described card, the setup return URLs, and six `trials/start` tests (the real trial over the seeded catalogue, idempotent, no-module null, the loud failure) — 38 tests
- `tests/test_onboarding_plans.py` (4) — **not applicable**: `/api/onboarding/plans` is minty-onboarding-api's own (`onboarding/api_reference.py`, read from `billing_plan`); this API has no plans route
- `tests/test_purchase_card_choice.py` (2 route tests) — **PORTED, slice B** into `billing/tests/api/test_module_settings_api.py`, together with the behaviour versions of `test_subscription_payer_permission`'s and `test_restart_billing_guard`'s route-source checks (every action as a co-admin, as a cashier who holds the card, the return leg as a co-admin; the restart route's four refusals in order) and the page model's wire shape — 84 tests
- `tests/test_subscription_notice.py` (10) — **PORTED, slice C**: `billing/tests/api/test_subscription_notice.py`, all 10 (the stranger cases answer 401 at the door, see §2) plus the superadmin read and the real builder over an empty company — 12 tests
- `tests/test_char_subscription.py`: the report-page gate check (Flask's gate); the rest is `billing/tests/engine/test_char_subscription.py` through the services
- `tests/test_char_subscription_dark.py`: not applicable since 2026-10-01 - this service has no dark switch any more (`test_dark.py` and the dark cases of `test_subscriptions_command.py` / `test_scheduler_pass.py` were deleted with it)

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
| 4c+ | DONE 2026-09-25: **billing accounts in the portal** — `GET /billing/accounts` (`portal.build_billing_accounts`), `update` / `default-card` / `move` (`billing/services/billing_accounts.py`), the account fields on `confirm`, `account` on `invoices` and `remove`, `next_billing` on `subscriptions`. Five silent failures fixed on the way: the landing's "Next Billing Date" was the anchor (the FIRST charge); the removal guard stopped at the first account on a shared card; card-keyed nomination raised on a shared card (`_group_for_card`, oldest wins); a company moved onto an emptied account lost access (`_carry_paid_days`' idle branch); a blanked address field was dropped by the SDK instead of cleared. Flask not mirrored (dark there; Django replaces it). **Same day, later: `next_bill` per account** (08-B's "Amount (estimated)") - `portal.next_bill_for_account` prices the account's next renewal with `renewals.build_renewal(..., converting_by=period.start)`: the runner's own invoice for the period starting on the next billing date, plus the trials that will have converted by then (`renewals._trial_converts` - the trial-end job's conjunction of customer, card for the company and consent, read from the database alone, never Stripe). `converting_by` is the forecast's only; the runner never passes it, so what it bills is unchanged. A figure that cannot be priced is null and logged, never a failed page. **Later still: schema item 23** - `subscription_invoice_line` records what each line PAID FOR (`period_start` / `period_end` / `unit_amount`), written by BOTH engines at issue: `billing.Line` carries them from whatever priced the line (`paid_from` holds a mid-period start to the period as `prorate` does), and an extension's come from `checkout.pending_extension_terms` - the paid-through date to the access end, and the rate only when re-deriving it piece by piece (`_extension_pieces`, which `_segmented_extension` now sums) reproduces the billed amount at ONE rate; never a blend, never a failed renewal. The breakdown reads them first. Minty migration `x1a01_invoice_line_span` ALTERs a database already up; both local ones have it, Supabase does not yet. **And 08-C's address became Stripe's own form** (the user's call): `?countries=1` also answers `publishable_key`, `update` takes `cardholder` (written with the address in one `billing_details` update, the account's card required as for the address), and every field is held to 255 characters. **2026-09-28: 08-K** - `POST /invoices/{invoice_id}/retry` and `retryable` on the invoice rows (above); `retry_now` gained `group_id` / `expect_invoice` in BOTH engines. And the engine now recognises a REPLAY-SCOPED renewal key (`dunning._names_period`: `<key>` or `<key>-<suffix>` - `replay_scenarios` scopes every key it issues so same-day runs do not collide at Stripe) where it picks the current period's invoice and where it settles one, so the dev database's lived past-due accounts can be retried and settle; production keys are never scoped. NOT covered: the renewal runner's duplicate guard (`_already_invoiced`) still matches keys exactly, so the live scheduler re-bills a replay-lived period (seen 2026-09-28 07:00 UTC on the catalogue's two 'Failed' accounts). **2026-09-29: the invoice PDF (Figma 09-A)** - `GET /invoices/{invoice_id}/pdf` and `has_pdf` on the invoice rows (above; Django only, Flask never had the portal's invoice actions); and in BOTH engines, a declined reinstatement voids the invoice it left open, and every Stripe item carries its own line's days (§6) |
| 5 | Flask's copies deleted; minty-onboarding-api proxies here; the Stripe keys leave Flask |
| 7 | deployed beside the phase-C builds (on a test site since before 2026-10-01; the dark switch the plan's step 7/8b relied on was removed that day) |
