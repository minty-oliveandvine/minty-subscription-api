# Manual QA checklist — subscription API

A manual walkthrough checklist for `minty-subscription-api`'s HTTP surface, to run alongside the
automated suites (`## Tests` in [authentication.md](authentication.md) and `## 8. Where it is
tested` in [subscriptions-api.md](subscriptions-api.md)). This is not a replacement for those —
it exists for exercising the service by hand (Postman, curl, or through `minty-web` /
`minty-payment-request-web` / the onboarding wizard) before a release, and for checking the money
and idempotency traps the docs call out explicitly. Run it against a dev DB or a replay-seeded
entity (`manage.py replay_scenarios --run catalogue`) — never a real payer or a real
Stripe card.

## Auth (`BearerAuth` / `SelfBearerAuth` / `EntityBearerAuth` / `NoticeBearerAuth`, [authentication.md](authentication.md))

- [ ] No/malformed/wrong-signature/expired token → 401 `{"error": "Unauthorized"}` on any
      non-public route (only `/healthz` and `/api/openapi.json` are public).
- [ ] A token just inside the 60 s clock-skew leeway (`CLOCK_SKEW_LEEWAY_SECONDS`) is accepted.
- [ ] `X-Entity-Id` header wins over the token's `entity_id` claim when both are present (a
      mismatch is logged, not refused at that point).
- [ ] `modules` router (`EntityBearerAuth`): no company anywhere (no claim, no header) → 401.
      A valid token naming a company the caller has no `user_entity` row for → 401 at the door;
      inside, the path's company must match the resolved one or 403 "That token is for a
      different company."
- [ ] `notice` router (`NoticeBearerAuth`): a token naming NO company is judged on the caller's
      membership of the company in the PATH; a token naming another company → 403
      `entity_mismatch`; a stranger → 401 (not Flask's old 403 `not_a_member`).
- [ ] `onboarding` router (plain `BearerAuth`, company in the body): no id → 400; a stranger to
      the company → 403; no such company → 404.
- [ ] `me` router (`SelfBearerAuth`): identity only — a company the caller has no role on is
      irrelevant, the request still proceeds (every row found by `user_id`).
- [ ] A system `superadmin` (`system_role` trusted from the token first, then the row —
      `superuser` is the pre-rename spelling) reaches `modules` read-only
      (`core.policy.is_superuser_readonly`) without a `user_entity` row.
- [ ] There is no refresh route — a lapsed token round-trips through Flask's login-gated
      `GET /handoff/minty-web?next=…` rather than failing dead in the frontend.

## Billing accounts & cards — THE CARD RULE

- [ ] `billing/payment-methods/confirm` (both `me` and `onboarding`) with neither
      `billing_group_id` nor a new account's `billing_company` + `billing_email` → 422 "Choose a
      billing account for this card." Naming only one of `billing_company` / `billing_email` is
      also 422, in the form's own words.
- [ ] `authorize-billing` / `billing/authorize` for a company on NO billing account, with no
      `payment_method` given, → 402 "Choose a billing account for this company." and records no
      consent.
- [ ] The deleted hosted-Stripe actions (`checkout`, `confirm-billing`, `checkout-complete`,
      `payment-method`, `manage-billing`, and the module page's card-route copies
      `payment-methods*`) all 404 "Unknown action." — confirm none of them proxy anywhere or open
      a Stripe-hosted page.
- [ ] A purchase, renewal or trial conversion always charges the card NOMINATED for the
      company/account (`store.card_for_entity` / the account's charged card), never the Stripe
      customer default; an account-less company is refused or skipped, never billed elsewhere.
- [ ] `billing/accounts/update`: company not blank, email English-only and shaped, address needs
      line 1 + a registered country, `cardholder` ≤ 255 — every check runs BEFORE the Stripe
      write; the Stripe `billing_details` write (address + cardholder in one call) happens before
      the name is saved locally.
- [ ] `billing/accounts/move` (including first placement of a card-free company) is refused 409
      for: a past-due company, a target account in dunning, a target with no card.
- [ ] Non-ASCII email (Korean, accents) on `me`/`onboarding` `payment-methods/confirm`,
      `billing/accounts/update`, and `subscriptions/invite-admin` → 422 "Email can only contain
      English letters, numbers and symbols." — checked before anything else, before Flask is
      asked for the invite.
- [ ] `billing/payment-methods/remove` on an account's own charged card is refused in the
      account's words; removing the customer DEFAULT when it is also an account's card hands that
      account its own card refusal instead (08-B has no button for the customer default).

## Invoices & PDFs

- [ ] `/invoices` list: `retryable` is true only for the ONE open invoice `dunning.retry_now`
      would pick right now (current period's renewal, else an open mid-period charge) — never an
      abandoned give-up bill, never one past its access-run-out point.
- [ ] `/invoices/{id}/retry` is refused unless the row is `retryable`; it charges only if it's
      still the engine-picked invoice, else answers `not_this_invoice` with nothing charged and
      no attempt spent (including when it was re-issued since the page was drawn).
- [ ] `/invoices/{id}/pdf`: someone else's invoice or a malformed id → 404; a never-sent / draft
      / void invoice → 409; the address unreadable from the processor → 502; lines that don't
      foot to `total` → 500 (logged, never served).
- [ ] `has_pdf` is never true for an invoice `/pdf` would then 409 on.
- [ ] `/invoices/{id}/breakdown`: a credit line renders negative, a zero line is left out; the
      days and rate shown are what the LINE recorded at issue time (`period_start` /
      `period_end` / `unit_amount`), never re-derived from today's catalogue.
- [ ] `/billing/accounts` `next_bill` (the estimated next renewal) is null — not a failed page —
      for a figure that can't be priced.

## Dunning, retries and the stranded-draft machinery (the highest-risk area)

- [ ] **A dunning stamp means exactly "the customer was told."** Force a transient processor
      failure (or confirm against existing cases) and verify the card shows ACTIVE — not a failed
      payment — in the notice, the module card's `subscription_status`, the subscriptions list and
      the account's `past_due` flag, until `dunning.customer_told` actually fires.
- [ ] **The processor failing is not the card declining.** A transient failure
      (`stripe_client.is_transient`: outage, timeout, rate limit, our key) holds a SILENT grace —
      no dunning stamp, no email — logged ERROR (CRITICAL with 3 days of grace left); Pay now
      answers `unavailable` ("We couldn't reach the payment provider. Nothing was charged —
      please try again shortly."), never "that card was declined."
- [ ] **An invoice Stripe will no longer collect (10 confirmations) is re-issued, not
      abandoned.** Replay key `L2` is the documented live proof: a card dead before day 30,
      fixed day 42 — day-30 invoice ends `void`, a day-40 replacement for the same period and
      lines ends `paid`. Confirm a card fixed on retry 10–13 still collects through the
      replacement, not "This invoice can no longer be paid."
- [ ] **A draft nobody finalized is loud, a renewal's own draft is FINISHED, never silently
      adopted or dropped.** Look for the `STRANDED DRAFT <in_…> (<key>, <where>)` log line;
      verify a renewal-pass draft gets its missing items added, finalized and charged on the
      CURRENT card next pass rather than being read as "already invoiced; unpaid" forever.
- [ ] **Idempotency**: no route re-raises an invoice for a period already claimed
      (`claimed_period_key` / the reservation's unique key checked before any Stripe call); a
      REPLAY-scoped key (`<key>-<suffix>`) must not let the live scheduler re-bill a
      replay-lived period — this is a documented, **not yet fixed**, gap (seen 2026-09-28 on the
      catalogue's two "Failed" accounts) — confirm you can still reproduce it rather than assume
      it's fixed.
- [ ] **In-flight guard**: pressing a change/reinstatement action twice inside its first ten
      minutes (`changes.IN_FLIGHT`) answers 409 on the second press, never a double charge; a
      press racing another also answers 409, a processor failure 503.
- [ ] A charge whose Stripe reply was lost is read as paid on re-check, never charged again on
      retry and never reported as a decline.

## Cancellation & reinstatement

- [ ] **Undo-after-renewal charges at the click — this is the user's rule, not a bug to
      "fix."** Undoing a cancellation whose extension has already been invoiced charges the rest
      of the period immediately on `renew`; verify it is 402 ONLY for a genuine card decline, 409
      "This module is already being restored. Refresh the page in a moment." for a concurrent
      press, and 503 for a processor failure.
- [ ] **A declined reinstatement voids the invoice it left open.** Confirm no module stays
      `cancelled` with a live unpaid invoice still chased by dunning or offered as *Retry
      payment*; confirm a declined reinstatement CAN be retried (since 2026-09-30) under a fresh
      attempt key (`~2`, `~3` — never reusing the same key twice), settling the prior attempt
      first (paid → adopted, open → withdrawn, draft → deleted).
- [ ] `authorize-billing` / a company with no card nominated → 402 "Choose a card before
      restarting billing." / "Choose a card before subscribing." (distinct from the reinstatement
      decline case above).

## Change of subscriber (transfer)

- [ ] `transfer/respond` accept requires the caller's OWN billing account via `billing_group_id`
      — someone else's or an unknown id → 422 "That billing account couldn't be found.", nothing
      moves; omitted with no nomination already in place and something to charge → 422 "Choose a
      billing account before taking over the billing."
- [ ] Accepting a trial-only handover, or a window that hasn't started yet, takes NO money at
      accept — it's parked on `collect_at` and collected later; a window already lapsed is
      charged inline.
- [ ] Modules the recipient does not name are cancelled ending at the OUTGOING payer's
      `paid_through` — never a fresh extension booked on the recipient.
- [ ] `transfer/seen` is idempotent and keyed to the click (`outcome_seen_at`), not to the read
      that drew the modal and not to the notification email.
- [ ] A deferred handover charge is chased like a renewal decline (dunning + the
      `renewal_failed` notice, sent once per attempt key) — never silently retried hourly with no
      notice.

## Trial start / onboarding finalize handoff

- [ ] `POST /api/onboarding/trials/start` must not fail silently — a non-200 fails
      minty-onboarding-api's `finalize` with THIS route's status and error sentence; the All Set
      screen's *Try again* only retries the trial start.
- [ ] Idempotent both ways: finalize on an already-live company stays live; a module that
      already holds a trial or subscription is not duplicated on a repeat finalize.
- [ ] Success shape is exactly what finalize expects (`trial_end` read back from the rows, the
      earliest when more than one module started).

## The daily scheduler jobs

- [ ] Job order is fixed and transfer jobs run BEFORE renewals: full pass
      `notify-onboarding → notify-trial-ending → close-trials → repair-transfers →
      collect-transfers → run-renewals → retry-dunning → sweep-access`; light pass
      `close-trials → repair-transfers → collect-transfers → run-renewals → sweep-touched`.
- [ ] `run-renewals` without `--issue` is DRY by default — confirm nothing is charged and no
      card state changes on a dry run; `run-daily` without `--issue` still converts due trials,
      retries dunning and sweeps access.
- [ ] A card that raises during `run-renewals` is logged and held in its grace
      ("could not be billed; retrying next pass") — the pass continues for every other card, it
      does not abort.
- [ ] `revoke-ungranted` is dry unless `--apply` — confirm a dry run reports rows without
      writing.
- [ ] `SUBSCRIPTION_SCHEDULER_ENABLED` is the only switch; subscriptions themselves are always
      on (no feature-wide dark switch exists any more).

## Dashboard notices

- [ ] Only two kinds are ever emitted: `past_due` (critical, only once
      `dunning.told_of_failure`) and `pending_cancel` (warning — a PAID module cancelled and
      still inside its paid period). No trial-ending / needs-card / needs-consent notice is ever
      produced (removed 2026-10-01) — including for a cancelled free trial, which must read
      nothing, not `pending_cancel`.
- [ ] A notice-builder failure answers `{"items": []}` with 200 — the landing page never goes
      down over a bad notice.
- [ ] `GET /{entity_id}/subscription-notice` honors the claimless-token-vs-path-membership rule
      (see Auth section above) exactly the same whether called from Flask server-side or from
      minty-payment-request-web directly.

## Out of scope for this checklist

- **A real Stripe webhook.** There is no webhook anywhere in this engine (`subscriptions-api.md`
  §6) — every row that reflects Stripe's state is written by the path that happened to look, not
  by a push. Nothing here tests for one because there is nothing to receive.
- **A real SMTP send.** `notify.mail_configured()` and the template render are covered by the
  automated suite; an actual inbox is not reachable from this checklist. Use
  `manage.py preview_emails [--only EVENTS] [--send ADDRESS]` to actually see one rendered.
- **Xero's own OAuth / token state.** Owned by Minty; this service reads none of it
  (`authentication.md`, "What this service never does").

## See also

- [authentication.md](authentication.md) — the four auth classes this checklist's first section
  is built on.
- [subscriptions-api.md](subscriptions-api.md) — §2 (the routes and their status codes), §5 (the
  scheduler), §6 (money and mail — the source of every dunning/stranded-draft/idempotency item
  above), §8 (the automated suites this checklist runs alongside).
- Repo `README.md` — the three rules the service is built on, and how to run it locally.
- `Minty/docs/features/modules-and-subscriptions.md` — the Flask original this service was
  ported from (historical behaviour only; Flask's own engine was deleted 2026-10-06).
