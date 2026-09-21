"""Seed the billing scenarios by LIVING them, not by writing their end state.

Run this against a fresh database to get subscription data that actually happened:
invoices with real Stripe ids, payments on a real (test) card, and the emails the jobs
send along the way. The scenarios in the dev database were originally hand-seeded — rows
written directly into their final shape — which left them with no invoices, no receipts
and `first_billed_at` equal to the billing anchor. Everything downstream that reads
history (the payer portal's Billing and Invoices tabs, dunning, the audit trail) had
nothing to read.

    python manage.py replay_scenarios --list
    python manage.py replay_scenarios --run angelika --setup --replay --report
    python manage.py replay_scenarios --run angelika-lifecycle --setup --replay --report

THIS IS THE DJANGO PORT of Minty's ``scripts/subscription/replay_scenarios.py`` (Part 2 step 2,
slice E), the second golden of the port: the same runs, driven through ``billing.services``
against the same database and the same Stripe test account, must produce the same report as the
Flask script. Read the Flask original for the reasoning; the differences are mechanical:
``app.app_context()`` is ``billing.services._context.scope()``, the SQLAlchemy reads are the
ORM, and mail goes to the CONSOLE backend (skipped, not spent - ``notify.mail_configured``) unless
``--notify-to`` names a recipient, so a replay never mails the payer's real address by accident.
The payer it creates gets a werkzeug-compatible ``pbkdf2:sha256`` password hash, so the same
login works in Flask. Run it against the DEV database and clone to a fresh payer (``--as … --tag
…``) when the Flask script has run the same shape today: Stripe remembers an idempotency key for
24 hours and the keys are per payer.

SEVEN SHAPES, and a payer for each person who needs to see one. ``RUNS`` is that pairing
and nothing more — the shapes themselves are the lists below. Two of them carry the bulk
of the work:

  the catalogue   Scenarios 2-12 — one state each, the shapes the UI has to render. Ten
                  weeks: long enough for a module to be cancelled AND have run out the
                  days that bought, the longest thing any of these eleven states needs.
  the lifecycle   Scenario 1 — four companies living four months on one account: trials
                  that convert and trials that lapse, a module added mid-period,
                  cancellations, monthly renewals, and a card that starts declining and is
                  then fixed. This one exercises the machinery rather than the pixels.

The other five are the edge shapes further down, each driving one account-level outcome.

A SHAPE NEVER SHARES A PAYER, with another shape or with another person running the same
one, because almost everything in this domain is PAYER-scoped: one anchor, one
paid-through, one dunning clock, one invoice per period covering every entity. Putting
scenario 1's deliberate payment failure on a catalogue payer would drag all eleven
catalogue entities into dunning with it.

A NEW payer for an existing shape needs no entry here — see ``clone_run``:

    ... --run angelika --as someone@oliveandvinehk.com --tag Zed --setup --replay

The entries that remain are the ones whose payer id is already IN a database, or whose
reason for existing is not derivable from an address.

DATES ARE RELATIVE, always. A replay runs from its origin to NOW, so every scenario is
really a statement about how long ago something happened — and the catalogue scenarios are
end states, which only exist relative to today. Absolute dates were tried and they rot
silently: nothing fails, the scenario simply starts rendering a different shape. They had
drifted eleven days when they were replaced, and one scenario ("resume after a renewal")
had never once produced its own state, because its anchor put the first renewal three
weeks past the end of the run. Events are day counts back from the last day; the two
scenarios whose subject IS the calendar — a 31st that clamps, a trial ending exactly on
the anchor — use the small helpers under "dates" below. The origin is derived from the
earliest event and is no longer written down anywhere.

HOW TIME WORKS. Two clocks move together and both are needed:

  * `clock.now()` is monkeypatched, which is what Minty reads to decide which period to
    bill and whether a trial has ended;
  * a STRIPE TEST CLOCK is advanced to the same day, because Stripe stamps
    `status_transitions.finalized_at` / `paid_at` from its own clock and `_record_of`
    copies those into `issued_at` / `paid_at`. Without it every invoice in a four-month
    replay is dated the minute the script ran.

Two properties of a Stripe test clock shape the script:

  * it can only be attached when the CUSTOMER is created, never retrofitted;
  * it cannot be rewound. So a re-run needs a NEW clock and therefore a new customer —
    `--setup` detects a spent clock and mints both. The payer, its email and its login are
    unchanged.

RE-RUNNING. `--reset` clears what a replay wrote (module rows, consents, invoices, the
billing cycle) and `--teardown` removes the entities as well. A same-day re-run needs
BOTH, and the teardown is not optional: Stripe remembers an idempotency key for 24 hours
regardless of what is deleted locally, and the purchase key is
``change-{entity_id}-{simulated_timestamp}-{CODE}``. Keep the entity ids and every buy
re-sends last run's key against a different customer, which Stripe rejects outright —
twelve failures in a row, every one of them reading like a declined card. `--teardown`
rotates the ids, and with them the keys.

    python scripts/subscription/replay_scenarios.py --run angelika --reset --teardown --setup --replay

`--setup` then PRUNES the Stripe customer it has just orphaned — see ``prune_orphans``.
A spent test clock cannot be rewound, so each re-run mints a new clock and customer and
leaves the last one holding its invoices forever. `--no-prune` keeps it.

SAFETY. Refuses to run against live Stripe — every charge here is fictional and belongs in
test mode. Renewals are scoped to the run's payer BY ID and never `ALL_PAYERS`: that
sentinel exists because a global run driven by an injected clock once billed a real
customer for catch-up periods. Email is NOT suppressed — the notices are half the point —
so each payer uses a plus-addressed variant of a real inbox.
"""
from __future__ import annotations

import calendar
import hashlib
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from billing.services import _context

# Test tokens. `tok_chargeCustomerFail` attaches fine and then declines every charge —
# which is what a card that stops working looks like, as opposed to one that was never
# valid. It is how the lifecycle run enters dunning.
CARD_GOOD = "tok_visa"
CARD_FAIL = "tok_chargeCustomerFail"

# The second card in the split lifecycle. Tagged so both companies land on the SAME one
# (see ``_new_card``), and a DIFFERENT TOKEN so it is a different card rather than merely
# a different PaymentMethod.
#
# ``tok_visa`` always mints 4242. Two of them are two ``pm_...`` objects and two groups —
# the split is real, and the invoices genuinely go to different payment methods — but they
# share a number, an expiry and a Stripe FINGERPRINT, so nothing anyone looks at can tell
# them apart. On the invoices both read "Visa 4242"; in the "Change billing account"
# dialog both rows read "Visa •••• 4242" and the payer cannot pick between them.
#
# tok_mastercard mints 5555 5555 5555 4444, so card B reads "Mastercard 4444" everywhere.
CARD_B = "B:tok_mastercard"


# --- dates ------------------------------------------------------------------
#
# Every date here is RELATIVE to the run's last simulated day, which is today. It has to
# be. A replay always runs origin -> now, and most of these scenarios are as-of-today
# SHAPES: a trial still running, one that has already expired, a module still inside its
# cancellation window. An absolute date becomes the wrong shape the moment today moves
# past it — silently, because nothing fails, the scenario just quietly renders something
# else. These dates were absolute until they had drifted eleven days out: scenario 5's
# "free trial + active" was three days from having no trial in it, and scenario 10 had
# never once shown the renewal it is named after, because its anchor put the first
# renewal three weeks beyond the end of the run.
#
# Two forms:
#
#   int         days before the run's end. 0 is the last day, -14 a fortnight before it.
#               Positive is rejected: nothing can happen after today.
#   callable    ``end -> datetime``, for the scenarios whose subject IS the calendar.
#               A day count cannot say "the 31st", and it cannot say "exactly one month
#               later" — E1 and C1 exist to bill on precisely those days.
#
# Relevant lengths, from ``services.policy``: a trial is 30 days, a cancelled module keeps
# access for 30, the past-due window is 15 and dunning retries on days 1..13.

def _end_of_run() -> datetime:
    """The last simulated day: today, at noon UTC. Every date hangs off this."""
    return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


def _day(when, end: datetime) -> datetime:
    """Resolve one event's date. ``when`` takes the two forms described above."""
    if callable(when):
        return when(end)
    if when > 0:
        raise ValueError(f"offset {when} is in the future; offsets count back from 0")
    return end + timedelta(days=when)


def _shift_months(when: datetime, months: int) -> datetime:
    """The first of the month ``months`` before ``when``."""
    index = when.year * 12 + (when.month - 1) - months
    return when.replace(year=index // 12, month=index % 12 + 1, day=1)


def _month_of_length(end: datetime, months_back: int, length: int) -> datetime:
    """The first month of exactly ``length`` days at or before ``months_back`` months ago.

    Walking back is the whole point. "Six months ago" is whatever month the calendar
    happens to offer, and a month-end clamp test anchored on a 30-day month proves
    nothing — so the caller asks for a LENGTH and takes the nearest month that has one.
    Never more than two steps back: 31-day months are never three apart, nor are 30-day.
    """
    month = _shift_months(end, months_back)
    for _ in range(12):
        if calendar.monthrange(month.year, month.month)[1] == length:
            return month
        month = _shift_months(month, 1)
    raise ValueError(f"no {length}-day month within a year of {end:%b %Y}")


def anchor_31(months_back: int = 6):
    """The 31st of a 31-day month, about ``months_back`` months back.

    For E1, whose entire subject is an anchor on a day most months do not have. Reaching
    six months back rather than two usually puts a February among the periods that follow,
    which is the harshest clamp there is — but the guarantee is the 31st, not the
    February. Which months lie in between is the calendar's business, and every clamp
    proves the same property: that ``period_containing`` re-derives from the anchor
    instead of accumulating.
    """
    return lambda end: _month_of_length(end, months_back, 31).replace(day=31)


def anchor_30(months_back: int = 2, day: int = 6):
    """Day ``day`` of a 30-day month, about ``months_back`` months back.

    A 30-day month is what makes ``start + 30 days`` land on the SAME day of the following
    month — which is the anchor. That coincidence is not decoration: it is the shape that
    produced a silently skipped renewal (C1), and it is why scenario 1's converting trial
    is billed on the same date as a company that joined a month earlier. Anchor either
    scenario in a 31-day month and the conversion falls a day short of the anchor, and
    neither one is testing anything any more.
    """
    return lambda end: _month_of_length(end, months_back, 30).replace(day=day)


def after(base, days: int):
    """``days`` after whatever ``base`` resolves to.

    Lets a multi-month run hold ONE calendar-anchored date and space everything else off
    it in plain days, so the internal shape of the scenario survives the calendar moving
    underneath it.
    """
    return lambda end: _day(base, end) + timedelta(days=days)


# Event kinds a scenario script may use:
#   trial   code   start a card-free trial
#   buy     code   subscribe (paid) — bundles automatically if the entity holds the other
#   cancel  code   in-app cancellation; queues the prorated extension
#   uncancel code  undo a cancellation ("Renew"). Free while the extension is still
#                  PENDING — nobody was billed, so it deletes a number; once the
#                  extension has been INVOICED it charges the uncovered window
#                  (app_access_until -> period end) at the MARGINAL price.
#   consent -      authorise billing for THIS entity, without buying anything
#   card    good|fail   swap the PAYER's default card (account-level, so it is written
#                       against the run rather than an entity)
#
# `consent` is not decoration. A trial converts to paid only if the payer has a card AND
# has agreed to be billed for that specific entity — the card is shared across every
# company they own, so having one does not authorise any particular company
# (`checkout._convert_due_trials`). Without a consent event a trial can only ever EXPIRE,
# which is what the first run of this scenario did: Growth Co was supposed to convert and
# quietly lapsed instead. In the product this event is the "Confirm billing" button.

# The catalogue is eleven END STATES, so every offset here is chosen backwards from the
# last day. Three windows decide all of them: a trial meant to be RUNNING starts less than
# 30 days out and one meant to have EXPIRED more than 30; a CANCELLED module is one still
# inside the 30 days its cancellation bought, and a TERMINATED one is the same module
# after those days ran out and the access sweep closed it.
#
# Two offsets are load-bearing beyond their own scenario, and they pull against each
# other. The payer's anchor is set by the FIRST charge on the account, which is scenario
# 12's buy at -65 — it has to be the earliest, because terminating means being cancelled
# for a full 30 days before today. That anchor then gives the whole catalogue renewals at
# about -35 and -5. Scenario 10 needs one of those to fall after ITS cancellation while
# the module is still resumable, which is a nine-day window; buying at -50 and cancelling
# at -25 puts the second renewal squarely inside it, and leaves the module live across the
# first one so that renewal has something to bill besides scenario 12's extension.
CATALOGUE = [
    ("Scenario 2: 1 Trial + Not started", [
        ("trial", "PAYMENT_REQUEST", -14),
        # Authorised the day after starting, which is the ordinary flow: start the trial,
        # then confirm billing. Safe anywhere in this scenario because the trial does not
        # end until +16 — consent only changes what happens AT trial end, and this one has
        # not reached it.
        ("consent", "-", -13),
    ]),
    ("Scenario 3 - Free Trial Without Billing Consent", [
        ("trial", "PETTY_CASH", -12),
        ("trial", "PAYMENT_REQUEST", -12),
    ]),
    ("Scenario 4 - Free Trial Different Dates", [
        # Both still running, started nineteen days apart. The absolute version of this
        # could not be written: its intended BILL start was in the future, so it fell back
        # to starting on the final day and ending short. Counting backwards, there is
        # nothing to fudge.
        ("trial", "PETTY_CASH", -19),
        # ONE consent covers both modules — it is per ENTITY, not per module — so this
        # authorises the BILL trial started seventeen days later too. Both end in the
        # future (+11 and +28), so nothing converts inside the run.
        ("consent", "-", -18),
        ("trial", "PAYMENT_REQUEST", -2),
    ]),
    ("Scenario 5 - Free trial + Active", [
        ("trial", "PETTY_CASH", -16),
        ("buy", "PAYMENT_REQUEST", -9),
    ]),
    ("Scenario 6 - Expired + Free trial", [
        ("trial", "PAYMENT_REQUEST", -40),          # ended at -10
        ("trial", "PETTY_CASH", -12),
        # AFTER the BILL trial has already expired, and that is not a detail — it is the
        # only window this event has. Consent is per ENTITY, so a consent recorded before
        # -10 would have been in force when `close-trials` reached BILL, and BILL would
        # have CONVERTED TO PAID instead of expiring. The scenario is named for that
        # expiry; consenting a day earlier silently turns it into scenario 5.
        #
        # So it sits five days after, which also leaves margin if the trial length in
        # `billing_policy` is ever raised: the constraint is "after BILL's trial_end", not
        # "at -5". PETTY_CASH is untouched — its trial runs to +18, so this consent means
        # it will convert then, which is the contrast scenario 3 has no consent for.
        ("consent", "-", -5),
    ]),
    ("Scenario 7 - Both Trial Expired", [
        ("trial", "PETTY_CASH", -40),
        ("trial", "PAYMENT_REQUEST", -40),
    ]),
    ("Scenario 8 - Both Active", [
        ("buy", "PETTY_CASH", -20),
        ("buy", "PAYMENT_REQUEST", -20),
    ]),
    ("Scenario 9 - Modules Cancelled On Different Dates", [
        ("buy", "PETTY_CASH", -20),
        ("buy", "PAYMENT_REQUEST", -20),
        ("cancel", "PETTY_CASH", -12),
        ("cancel", "PAYMENT_REQUEST", -11),
    ]),
    ("Scenario 10 - Resume Module After A Renewal", [
        # Cancelled late enough that the 30 days it bought have not run out, and bought
        # early enough that a renewal falls between the two — which is the only window in
        # which "resume after a renewal" is a state the UI can be in. The absolute dates
        # satisfied neither half: the buy sat 31 days before a renewal the run always
        # ended before reaching, so this rendered as a plain cancelled module and never
        # once showed the thing it is named for. The renewal also sweeps up the pending
        # extension, so this is where extension_state moves pending -> invoiced.
        ("buy", "PETTY_CASH", -50),
        ("cancel", "PETTY_CASH", -25),   # access to +5, renewal lands about -5
    ]),
    ("Scenario 11 - Cancelled Module on Petty Cash", [
        ("buy", "PETTY_CASH", -20),
        ("buy", "PAYMENT_REQUEST", -20),
        ("cancel", "PETTY_CASH", -19),
    ]),
    ("Scenario 12 - Both Modules Terminated", [
        # TERMINATED, not merely cancelled — the distinction scenarios 9 and 11 do not
        # draw. Cancelling stops the renewal but leaves 30 days of paid access, so a
        # module cancelled last week is still live and still resumable; it becomes
        # terminated only when those days run out and `sweep_expired_module_access` closes
        # it, which is the one state here that nobody clicks. So the cancellation has to
        # sit a clear month back: at -63 the access ran out at -33 and the sweep has long
        # since done its work. The absolute dates cancelled a day after buying and still
        # had three weeks of access left at the end of the run, so this scenario rendered
        # as another cancelled-but-live module, indistinguishable from 9 and 11.
        ("buy", "PETTY_CASH", -65),
        ("buy", "PAYMENT_REQUEST", -65),
        ("cancel", "PETTY_CASH", -63),   # access ran out at -33
        ("cancel", "PAYMENT_REQUEST", -63),
    ]),
]

# Scenario 1 — four months, four companies, one account.
#
# The anchor is set by the FIRST charge (day 0, Steady Co), so every company on this
# account renews on that day of the month regardless of when it joined. That is the rule
# the whole design rests on and it is worth seeing in real data: Growth Co's trial
# converts on day 30 and is billed on the same date as a company that joined a month
# earlier. `anchor_30` is what makes that true — see its docstring.
#
# The card failure is account-level BECAUSE THIS ACCOUNT HAS ONE CARD, which is no longer
# the same statement as "dunning is account-level". Every company here is nominated onto
# the same `payer_billing_group`, so the renewal is one invoice covering all four and a
# decline takes all four down — the shape this scenario has always had, and still the
# common one. Nominate one company onto a second card and the same failure stops at that
# card: see `tests/test_lifecycle_invoices.py`, which bills this exact timeline both ways
# and compares the invoices.
#
# The day-92 renewal fails, retries on the dunning offsets, and recovers once the card is
# replaced five days later, well inside the 13 retries. The day-123 renewal then succeeds,
# which is what proves the account actually healed rather than merely stopped being
# charged.
S1_ANCHOR = anchor_30(4, 6)

LIFECYCLE = [
    ("S1 Steady Co", [
        # Joins first, so it sets the anchor. Runs untouched for four months — the
        # control against which the others' churn is legible.
        ("buy", "PETTY_CASH", S1_ANCHOR),
    ]),
    ("S1 Growth Co", [
        # Trial -> authorises billing part-way through -> converts on its own at term end
        # -> adds a second module mid-period, which is a SWAP to the bundle rather than a
        # second charge (credit the single, charge the bundle).
        ("trial", "PETTY_CASH", S1_ANCHOR),
        ("consent", "-", after(S1_ANCHOR, 14)),
        ("buy", "PAYMENT_REQUEST", after(S1_ANCHOR, 75)),
    ]),
    ("S1 Churn Co", [
        # Buys both, then leaves in stages: one module in the third month, the other in
        # the fourth. Each cancellation queues a prorated extension that rides the NEXT
        # invoice, so this is where extension_state moves pending -> invoiced.
        ("buy", "PETTY_CASH", S1_ANCHOR),
        ("buy", "PAYMENT_REQUEST", after(S1_ANCHOR, 34)),
        ("cancel", "PAYMENT_REQUEST", after(S1_ANCHOR, 70)),
        ("cancel", "PETTY_CASH", after(S1_ANCHOR, 105)),
    ]),
    ("S1 Comeback Co", [
        # A trial that lapses with no card consent, then a paid return six weeks later.
        # Proves a module can be re-bought after expiry, and that the second purchase is
        # prorated to the shared anchor rather than starting its own cycle.
        ("trial", "PAYMENT_REQUEST", S1_ANCHOR),
        ("buy", "PAYMENT_REQUEST", after(S1_ANCHOR, 80)),
    ]),
    ("S1 Steady Co", [
        # Account-level card events, hung off an entity that already exists so the
        # timeline reads in one place. `code` is the card, not a module.
        ("card", CARD_FAIL, after(S1_ANCHOR, 87)),
        ("card", CARD_GOOD, after(S1_ANCHOR, 97)),
    ]),
]

# Scenario 1B — the SAME four months, with the account split across two cards.
#
# The point of comparison for `LIFECYCLE`, and the only shape here that exercises a payer
# with more than one card. Everything up to day 40 is identical, so the two runs can be
# read side by side; from there the account is deliberately split down the middle:
#
#     card A   Steady Co, Growth Co
#     card B   Churn Co, Comeback Co
#
# From that day on every renewal raises TWO invoices — one per card, each carrying only
# its own companies, each charged to its own card. `tests/test_lifecycle_invoices.py`
# asserts the arithmetic of that in memory; this is where it happens against real Stripe.
#
# THE FAILURE IS THE WHOLE POINT. Card B starts declining on day 87 and is replaced on
# day 97, straddling the day-92 renewal exactly as scenario 1 does. In scenario 1 that
# takes all four companies past due, because there is one invoice and one dunning clock.
# Here it takes TWO — Steady and Growth are billed, paid and untouched throughout — and
# `recard` on day 97 is what dunning then collects on, because it retries the group's
# CURRENT card rather than the one the invoice was raised against.
#
# The split lands on day 40 rather than at the start so the first renewal is still a
# single invoice: the run then contains the before and the after of the change itself,
# which is what somebody looking at the Invoices tab needs to see.
S1B_ANCHOR = anchor_30(4, 6)
S1B_SPLIT = after(S1B_ANCHOR, 40)

LIFECYCLE_SPLIT = [
    ("S1B Steady Co", [
        ("buy", "PETTY_CASH", S1B_ANCHOR),
    ]),
    ("S1B Growth Co", [
        ("trial", "PETTY_CASH", S1B_ANCHOR),
        ("consent", "-", after(S1B_ANCHOR, 14)),
        ("buy", "PAYMENT_REQUEST", after(S1B_ANCHOR, 75)),
    ]),
    ("S1B Churn Co", [
        ("buy", "PETTY_CASH", S1B_ANCHOR),
        ("buy", "PAYMENT_REQUEST", after(S1B_ANCHOR, 34)),
        ("cancel", "PAYMENT_REQUEST", after(S1B_ANCHOR, 70)),
        ("cancel", "PETTY_CASH", after(S1B_ANCHOR, 105)),
    ]),
    ("S1B Comeback Co", [
        ("trial", "PAYMENT_REQUEST", S1B_ANCHOR),
        ("buy", "PAYMENT_REQUEST", after(S1B_ANCHOR, 80)),
    ]),
    # THE SPLIT. Churn and Comeback move onto a second card; Steady and Growth stay on
    # the one the account has been billing to all along.
    #
    # Both name the SAME tagged card, which is what puts them in one group — a group is a
    # card, so a second company on card B is the same group and therefore the same
    # invoice. Two untagged nominations would mint two cards and quietly make this a
    # three-invoice account.
    #
    # Comeback Co is on a lapsed trial here and bills nothing until day 80. Deliberate: a
    # company with no charge yet can still be put on a card, and its first invoice has to
    # land on THAT card rather than on the account default.
    ("S1B Churn Co", [
        ("nominate", CARD_B, S1B_SPLIT),
    ]),
    ("S1B Comeback Co", [
        ("nominate", CARD_B, after(S1B_SPLIT, 1)),
    ]),
    ("S1B Churn Co", [
        # Card B dies four days before the renewal it has to break, and is replaced ten
        # days later — the same offsets scenario 1 uses, so the two runs fail and recover
        # on the same days of their timelines. Hung off Churn Co because `recard` acts on
        # the GROUP: Comeback Co is on the same card and moves with it, which is the
        # difference between replacing a card and nominating one.
        #
        # The replacement is a MASTERCARD, untagged so it mints a fresh ``pm_...``: a
        # re-issued card is a new payment method of the same brand, and using CARD_GOOD
        # here turned card B into a Visa 4242 from July onward — identical to card A on
        # every invoice and in every picker, which is the confusion this scenario exists
        # to avoid.
        ("recard", CARD_FAIL, after(S1B_ANCHOR, 87)),
        ("recard", "tok_mastercard", after(S1B_ANCHOR, 97)),
    ]),
]

# --- the edge scenarios -------------------------------------------------------
#
# Each of these is its own PAYER, because each drives a distinct ACCOUNT-level outcome
# and almost everything here is account-scoped: one anchor, one paid_through, one dunning
# clock, one card, one invoice per period covering every entity. Two of them on the same
# payer would not be two tests, it would be one confused one.
#
# Four of the five share ONE anchor helper. They do not share a payer — they share a
# SHAPE: buy on the anchor day, let exactly one renewal fall, and watch what that renewal
# does. Anchoring them all on the same day of the same 30-day month keeps their timelines
# directly comparable when two runs are read side by side, and keeps each span down to
# about ten weeks rather than the four months the absolute dates had drifted into.
EDGE_ANCHOR = anchor_30(2, 6)

# The half of dunning scenario 1 deliberately avoids: a card that is never repaired.
# Hopeful Co rides along because its trial ends INSIDE the past-due window — day 34, with
# the window running day 30 to day 45 — which asks a question nobody has answered:
# convert against a card known to be failing, or expire a trial the customer consented to
# while the account could still recover?
L1_GIVES_UP = [
    ("L1 Doomed Co", [
        ("buy", "PETTY_CASH", EDGE_ANCHOR),
        ("buy", "PAYMENT_REQUEST", EDGE_ANCHOR),
    ]),
    # Pays nothing, does nothing wrong, and loses access anyway — dunning is
    # account-level, so one bad card revokes every entity on the account. Worth seeing.
    ("L1 Bystander Co", [
        ("buy", "PETTY_CASH", EDGE_ANCHOR),
    ]),
    ("L1 Hopeful Co", [
        ("trial", "PAYMENT_REQUEST", after(EDGE_ANCHOR, 4)),
        ("consent", "-", after(EDGE_ANCHOR, 9)),
    ]),
    ("L1 Doomed Co", [
        # Four days before the renewal it has to break, so the margin survives the anchor
        # month changing length underneath it.
        ("card", CARD_FAIL, after(EDGE_ANCHOR, 26)),
    ]),
]

# Two conversions landing ON the anchor, which is the shape that produced the silently
# skipped renewal. Delta is the control: if `entities_billed_in` over-matches, Delta's
# 280 disappears and the account is quietly under-billed for the month. The whole test
# rests on trial-end and renewal being the SAME day, which is why the anchor is pinned to
# a 30-day month rather than a day count.
C1_CONVERSIONS = [
    ("C1 Alpha Co", [("trial", "PETTY_CASH", EDGE_ANCHOR),
                     ("consent", "-", after(EDGE_ANCHOR, 14))]),
    ("C1 Beta Co", [("trial", "PETTY_CASH", EDGE_ANCHOR),
                    ("consent", "-", after(EDGE_ANCHOR, 14))]),
    ("C1 Gamma Co", [("trial", "PETTY_CASH", EDGE_ANCHOR)]),  # no consent -> expires
    ("C1 Delta Co", [("buy", "PETTY_CASH", EDGE_ANCHOR)]),
]

# Both sides of un-cancelling, which take different branches on one condition: whether
# the extension has been INVOICED yet. Early Co gets there first and should pay nothing;
# Returner Co arrives after the day-30 renewal collected it and owes the uncovered window.
# The two offsets straddle that renewal with days to spare on each side, so neither lands
# on the wrong branch if the anchor month changes length.
R1_UNCANCEL = [
    ("R1 Early Co", [
        ("buy", "PETTY_CASH", EDGE_ANCHOR),
        ("cancel", "PETTY_CASH", after(EDGE_ANCHOR, 14)),
        ("uncancel", "PETTY_CASH", after(EDGE_ANCHOR, 26)),  # extension still pending
    ]),
    ("R1 Returner Co", [
        ("buy", "PETTY_CASH", EDGE_ANCHOR),
        ("cancel", "PETTY_CASH", after(EDGE_ANCHOR, 14)),
        # Past the renewal, and still inside the 30 days the cancellation bought (which
        # run to day 44) — outside those, Renew is not offered at all.
        ("uncancel", "PETTY_CASH", after(EDGE_ANCHOR, 34)),  # extension already invoiced
    ]),
    # The PAIR case, and the only shape that reaches either of the 2026-08-19 fixes.
    # Both modules go on the SAME day, so the extension is priced as the bundle they still
    # are — 280 to Payment Request, the 120 step to Petty Cash — rather than 120 each,
    # which totalled less than either module has ever cost.
    #
    # Then the day-30 renewal invoices both extensions and bills NEITHER module for the
    # period it opens. So the resume on day 34 has to price Petty Cash as a fresh JOIN at
    # its own 280: nothing is on the line to upgrade from. Reading the account's
    # paid_through alone said "covered" here — it moves for the whole payer while the
    # cancelling modules are left off the invoice — and charged the 120 bundle step
    # instead. R1 Returner Co cannot catch that: with one module there is no sibling to
    # count wrongly.
    ("R1 Pair Co", [
        ("buy", "PETTY_CASH", EDGE_ANCHOR),
        ("buy", "PAYMENT_REQUEST", EDGE_ANCHOR),
        ("cancel", "PAYMENT_REQUEST", after(EDGE_ANCHOR, 14)),
        ("cancel", "PETTY_CASH", after(EDGE_ANCHOR, 14)),
        # Past the renewal, and still inside the 30 days the cancellation bought.
        ("uncancel", "PETTY_CASH", after(EDGE_ANCHOR, 34)),
    ]),
    ("R1 Steady Co", [("buy", "PETTY_CASH", EDGE_ANCHOR)]),
]

# The payer whose LAST entity leaves. `build_renewal` has a branch for exactly this —
# nothing billable, but an extension still owed, so the price catalog is never consulted
# and the currency has to come from the billing cycle instead. It has never executed.
X1_LAST_ONE_OUT = [
    ("X1 Only Co", [
        ("buy", "PETTY_CASH", EDGE_ANCHOR),
        ("cancel", "PETTY_CASH", after(EDGE_ANCHOR, 14)),
    ]),
]

# Anchored on a day most months do not have, so the periods clamp: a 31st anchor turns
# over on the 30th of a 30-day month and on the 28th of a February, and then springs back.
# `period_containing` re-derives every period from the anchor precisely so those clamps
# cannot accumulate; Joiner Co is here so that a PRORATION is also computed against a
# clamped month, which is the arithmetic most likely to be wrong. `anchor_31` reaches six
# months back, far enough that the run usually crosses a February — the worst clamp — but
# any short month proves the property.
E1_ANCHOR = anchor_31(6)

E1_MONTH_END = [
    ("E1 Edge Co", [("buy", "PETTY_CASH", E1_ANCHOR)]),
    ("E1 Joiner Co", [("buy", "PETTY_CASH", after(E1_ANCHOR, 14))]),
]


def retag(scenarios: list, old: str, new: str) -> list:
    """The same SHAPE under a tag of its own — the whole of what separates two payers.

    `_entities` matches on entity NAME and is not payer-scoped, so a second payer running
    a shape under the same tag would adopt the first payer's entities instead of seeding
    its own. The rename is what keeps two runs apart; the payer id does not.

    Two naming forms, and this handles both without being told which. The lifecycle and
    edge scenarios carry their tag in the name already ("S1 Steady Co", "L1 Doomed Co"),
    so the first token is swapped. The catalogue's names do not ("Scenario 2: ...") —
    nothing matches, they pass through, and `_clone_name` prefixes the run's tag at seed
    time instead. Which is why the tag alone re-points a catalogue run and the two
    hand-written helpers this replaced were never actually doing different things.
    """
    return [
        ((f"{new} {name.split(' ', 1)[1]}" if name.startswith(f"{old} ") else name), script)
        for name, script in scenarios
    ]


def lifecycle_edge(tag: str, scenarios: list) -> list:
    """An edge scenario list re-tagged for a different payer. Every edge shape is
    anchored on its own tag, which is the first token of every name in it."""
    return retag(scenarios, scenarios[0][0].split(" ", 1)[0], tag)


def lifecycle_as(tag: str) -> list:
    """The lifecycle four under a tag of their own."""
    return retag(LIFECYCLE, "S1", tag)


def clone_run(base: dict, tag: str, email: str, *, user_id: str | None = None,
              name: tuple | None = None, notify_to: str | None = None) -> dict:
    """One shape, a different payer — the whole of what the entries in ``RUNS`` vary.

    Every run below is one of SEVEN shapes (the catalogue, the lifecycle, and the five
    edges) pointed at a payer. Nothing else differs, so a new payer for an existing shape
    does not need a new entry: ``--as`` and ``--tag`` build it at the command line.

    The payer id is DERIVED from the email by default — `uuid5`, so the same address
    always resolves to the same payer and a second `--setup` finds the run it seeded last
    time rather than minting a stranger. The entries in ``RUNS`` keep their hand-written
    ids instead: those are already in the database, on rows that would be orphaned by a
    derivation that disagreed with them. Pass ``--payer-id`` to adopt one of those.
    """
    inherited = {k: v for k, v in base.items() if k != "notify_to"}
    return {
        **inherited,
        "label": f"{base['label']} — as {tag}",
        "user_id": user_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"minty-replay:{email}")),
        "email": email,
        "name": name or (tag, "Replay"),
        "tag": tag,
        # Not inherited: a redirect belongs to the payer it was written for, and carrying
        # the base's silently would send a new payer's mail to someone else's inbox.
        **({"notify_to": notify_to} if notify_to else {}),
        "scenarios": retag(base["scenarios"], base["tag"], tag),
    }

RUNS = {
    "michael": {
        # The catalogue again, on a payer whose mail lands in Michael's inbox. A SEPARATE
        # user row, not his own `bec4a3d6…` account: the payer's `email` column is where
        # every notice is sent, so pointing the run at his real account would mean either
        # rewriting that column — breaking the address he signs in with — or seeding a
        # month of fictional invoices onto the account he actually uses. He signs in as
        # this address instead, and the plus-alias keeps the mail filterable.
        #
        # A distinct tag, because `_entities` matches on NAME alone and is not
        # payer-scoped: sharing another catalogue run's tag would make this one adopt
        # that payer's eleven entities rather than seed its own.
        "label": "Scenarios 2-12 for Michael",
        "user_id": "22222222-3333-4444-5555-666666666666",
        "email": "michael.leguira+catalogue@oliveandvinehk.com",
        "name": ("Michael", "Scenarios"),
        "tag": "Mike",
        "scenarios": CATALOGUE,
    },
    "michael-lifecycle": {
        # Scenario 1 on a second Michael alias. Still a separate payer from `michael`,
        # for the reason in the module docstring: this run deliberately breaks a card,
        # and dunning is account-level — sharing a payer with the catalogue would drag
        # all eleven of its entities into arrears alongside these four.
        "label": "Scenario 1 for Michael (mail -> michael.leguira+lifecycle)",
        "user_id": "33333333-4444-5555-6666-777777777777",
        "email": "michael.leguira+lifecycle@oliveandvinehk.com",
        "name": ("Michael", "Lifecycle"),
        "tag": "M1",
        "scenarios": lifecycle_as("M1"),
    },
    # HANDED OVER. These two payer rows were renamed in the database to the
    # digitalisation addresses, so the same entities, invoices and history now sign in
    # under the new login. The `email` here is what `--setup` would recreate the row
    # with if it were ever deleted, so it has to match the database rather than the
    # address the run was originally seeded under.
    #
    # A TAG IS ONLY FREE TO CHANGE WHILE NOTHING IS SEEDED UNDER IT. It is baked into
    # every entity NAME that a run has already created ("Ang - Scenario 2 ...",
    # "A1 Steady Co") and `_entities` matches on name, so renaming one against live data
    # renames nothing — the run stops seeing its own entities and seeds a second set
    # beside them. This one went "Ang" -> "Digitalisation" on 2026-08-17, checked first
    # and safe only because neither database held a single entity or payer row for it.
    # Check the same way before touching "A1" below, or any other.
    "digitalisation": {
        "label": "Scenarios 2-12, handed to digitalisation (entities tagged Digitalisation)",
        "user_id": "44444444-5555-6666-7777-888888888888",
        "email": "digitalisation+catalogue@oliveandvinehk.com",
        # The data is digitalisation's; the NOTICES go to Angelika — the same split as
        # the lifecycle run below, and a distinct alias from it so the two runs' mail
        # stays tellable apart in one inbox.
        "notify_to": "angelika.tardaguela+digitalisation-catalogue@oliveandvinehk.com",
        "name": ("Digitalisation", "Scenarios"),
        "tag": "Digitalisation",
        "scenarios": CATALOGUE,
    },
    "digitalisation-lifecycle": {
        "label": "Scenario 1, handed to digitalisation (entities tagged A1)",
        "user_id": "55555555-6666-7777-8888-999999999999",
        "email": "digitalisation+lifecycle@oliveandvinehk.com",
        # The data is digitalisation's; the NOTICES go to Angelika. A distinguishable
        # alias rather than her plain `+lifecycle@`, which already receives the A3 run's
        # notices — same inbox either way, but these stay tellable apart from her own.
        "notify_to": "angelika.tardaguela+digi-lifecycle@oliveandvinehk.com",
        "name": ("Digitalisation", "Lifecycle"),
        "tag": "A1",
        "scenarios": lifecycle_as("A1"),
    },
    # FRESH pair on the angelika addresses, kept for validating fixes. Their own payer
    # ids, because reusing digitalisation's would overwrite the handed-over data rather
    # than sit beside it. This one took the tag "Ang" back on 2026-08-17, once
    # digitalisation's rename freed it; it was "Ang2" for as long as the two collided.
    "angelika": {
        "label": "Scenarios 2-12 for Angelika — fix validation",
        "user_id": "88888888-9999-0000-1111-222222222222",
        "email": "angelika.tardaguela+catalogue@oliveandvinehk.com",
        "name": ("Angelika", "Scenarios"),
        "tag": "Ang",
        "scenarios": CATALOGUE,
    },
    # A THIRD payer id on the same address, because a same-day re-run cannot reuse the
    # second one. `renewals.period_key` is `renewal-{user_id}-{period_start}`: it is keyed
    # on the PAYER and the period, not on the run, and Stripe remembers an idempotency key
    # for 24 hours. `--teardown` rotates entity ids and so rotates the checkout keys, but
    # the payer id is what setup preserves — so every renewal in a same-day replay of the
    # same payer collides with the previous attempt and comes back as a decline. The
    # A2 run died that way from its first renewal onward.
    #
    # The previous payer keeps the data under a parked "+lifecycle-spent" address; this
    # one takes the real address over. Rotating the payer is the only way to get clean
    # renewals today without waiting out Stripe's 24-hour window.
    "angelika-lifecycle": {
        "label": "Scenario 1 for Angelika — fix validation",
        "user_id": "a3a3a3a3-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+lifecycle@oliveandvinehk.com",
        "name": ("Angelika", "Lifecycle"),
        "tag": "A3",
        "scenarios": lifecycle_as("A3"),
    },
    "angelika-split": {
        # Scenario 1B — the same four months on TWO cards. Its own payer, and for a
        # sharper reason than the usual one: this run's whole subject is that a decline
        # stops at the card that declined, and sharing an account with scenario 1 would
        # put both stories on one dunning history where neither could be read.
        #
        # Run it alongside `angelika-lifecycle` and read the two Invoices tabs side by
        # side: same companies, same money, one invoice a month against two.
        "label": "Scenario 1B — the lifecycle split across two cards",
        "user_id": "a4a4a4a4-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+split@oliveandvinehk.com",
        "name": ("Angelika", "Split"),
        "tag": "S1B",
        "scenarios": LIFECYCLE_SPLIT,
    },
    # The edge set. Five payers on Angelika's address family — one per account-level
    # outcome, since a give-up, a clean renewal and a 31st anchor cannot coexist on one
    # account. Same password as the rest.
    "L1": {
        "label": "Dunning that gives up, + a trial ending mid-arrears",
        "user_id": "b1b1b1b1-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+l1@oliveandvinehk.com",
        "name": ("Angelika", "GiveUp"),
        "tag": "L1",
        "scenarios": L1_GIVES_UP,
    },
    "C1": {
        "label": "Two conversions on the anchor day beside a plain renewer",
        "user_id": "c1c1c1c1-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+c1@oliveandvinehk.com",
        "name": ("Angelika", "Conversions"),
        "tag": "C1",
        "scenarios": C1_CONVERSIONS,
    },
    "R1": {
        "label": "Un-cancel on both sides of the extension being invoiced",
        "user_id": "d1d1d1d1-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+r1@oliveandvinehk.com",
        "name": ("Angelika", "Uncancel"),
        "tag": "R1",
        "scenarios": R1_UNCANCEL,
    },
    "X1": {
        "label": "The last entity leaves, and still owes an extension",
        "user_id": "e1e1e1e1-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+x1@oliveandvinehk.com",
        "name": ("Angelika", "LastOut"),
        "tag": "X1",
        "scenarios": X1_LAST_ONE_OUT,
    },
    "E1": {
        "label": "Anchored on the 31st — month-length clamping",
        "user_id": "f1f1f1f1-0000-1111-2222-333333333333",
        "email": "angelika.tardaguela+e1@oliveandvinehk.com",
        "name": ("Angelika", "MonthEnd"),
        "tag": "E1",
        "scenarios": E1_MONTH_END,
    },
    # The same five edge scenarios on MICHAEL. Separate payers and separate TAGS from
    # angelika's set, because `_entities` matches on entity NAME and is not payer-scoped:
    # sharing "L1" would make this run adopt her entities instead of seeding its own.
    "michael-L1": {
        "label": "Dunning that gives up, + a trial ending mid-arrears (Michael)",
        "user_id": "b2b2b2b2-0000-1111-2222-333333333333",
        "email": "michael.leguira+ml1@oliveandvinehk.com",
        "name": ("Michael", "ML1"),
        "tag": "ML1",
        "scenarios": lifecycle_edge("ML1", L1_GIVES_UP),
    },
    "michael-C1": {
        "label": "Two conversions on the anchor day beside a plain renewer (Michael)",
        "user_id": "c2c2c2c2-0000-1111-2222-333333333333",
        "email": "michael.leguira+mc1@oliveandvinehk.com",
        "name": ("Michael", "MC1"),
        "tag": "MC1",
        "scenarios": lifecycle_edge("MC1", C1_CONVERSIONS),
    },
    "michael-R1": {
        "label": "Un-cancel on both sides of the extension being invoiced (Michael)",
        "user_id": "d2d2d2d2-0000-1111-2222-333333333333",
        "email": "michael.leguira+mr1@oliveandvinehk.com",
        "name": ("Michael", "MR1"),
        "tag": "MR1",
        "scenarios": lifecycle_edge("MR1", R1_UNCANCEL),
    },
    "michael-X1": {
        "label": "The last entity leaves, and still owes an extension (Michael)",
        "user_id": "e2e2e2e2-0000-1111-2222-333333333333",
        "email": "michael.leguira+mx1@oliveandvinehk.com",
        "name": ("Michael", "MX1"),
        "tag": "MX1",
        "scenarios": lifecycle_edge("MX1", X1_LAST_ONE_OUT),
    },
    "michael-E1": {
        "label": "Anchored on the 31st - month-length clamping (Michael)",
        "user_id": "f2f2f2f2-0000-1111-2222-333333333333",
        "email": "michael.leguira+me1@oliveandvinehk.com",
        "name": ("Michael", "ME1"),
        "tag": "ME1",
        "scenarios": lifecycle_edge("ME1", E1_MONTH_END),
    },
    # Jayden's two runs, read by Angelika. `notify_to` is the ONLY thing that separates
    # the payer from the reader — see `_patch_notify`. It deliberately points at
    # addresses that already belong to the `angelika` runs' payers: the mail is meant to
    # land in that inbox, and nothing about a redirect requires the destination to be
    # free.
    "jayden": {
        "label": "Scenarios 2-12 for Jayden (mail -> angelika.tardaguela+catalogue)",
        "user_id": "66666666-7777-8888-9999-000000000000",
        "email": "jayden.kim+catalogue@oliveandvinehk.com",
        # Follows the `angelika` payer's address, which moved +scenarios -> +catalogue on
        # 2026-08-17. The point of this line is "the box Angelika reads her own catalogue
        # run in", so it tracks that address rather than the literal string it once held.
        "notify_to": "angelika.tardaguela+catalogue@oliveandvinehk.com",
        "name": ("Jayden", "Scenarios"),
        "tag": "Jay",
        "scenarios": CATALOGUE,
    },
    "jayden-lifecycle": {
        "label": "Scenario 1 for Jayden (mail -> angelika.tardaguela+jay-lifecycle)",
        "user_id": "77777777-8888-9999-0000-111111111111",
        "email": "jayden.kim+lifecycle@oliveandvinehk.com",
        # Her inbox, but NOT her plain `+lifecycle@`: that address is the A3 run's own
        # payer, so its notices would be indistinguishable from these — same subjects,
        # same amounts, same dates, different account.
        "notify_to": "angelika.tardaguela+jay-lifecycle@oliveandvinehk.com",
        "name": ("Jayden", "Lifecycle"),
        "tag": "J1",
        "scenarios": lifecycle_as("J1"),
    },
}

def _check_runs_are_distinct() -> None:
    """Three fields no two runs may share. Checked at import, because each collision is
    silent at the point it is made and expensive at the point it is noticed.

    A shared TAG is the dangerous one: `_entities` matches on name and is not
    payer-scoped, so the second run adopts the first's entities and seeds nothing —
    which looks exactly like a run that worked. It is also the field most likely to
    collide, because tags get renamed and freed (see "Ang" above), and a rename that
    lands on a tag still in use is one keystroke away from a rename that frees it.

    EMAIL and USER_ID collide harder but louder: `user.email` and `user.username` are
    unique in the schema, so the second `--setup` raises on the insert rather than
    corrupting anything.
    """
    for field in ("tag", "email", "user_id"):
        seen: dict = {}
        for key, run in RUNS.items():
            if run[field] in seen:
                raise SystemExit(
                    f"RUNS is broken: {key!r} and {seen[run[field]]!r} share "
                    f"{field}={run[field]!r}"
                )
            seen[run[field]] = key


_check_runs_are_distinct()

PASSWORD = "ReplayScenarios!2026"  # dev-only; these payers exist to be signed in as


# --- helpers ----------------------------------------------------------------

def _origin(run: dict, end: datetime) -> datetime:
    """The run's first simulated day: the day before its earliest event.

    DERIVED, not declared. It used to be a hand-written date sitting beside a hand-written
    schedule, and the two could disagree without anything complaining — an origin later
    than the first event just skipped it, seeding the scenario with a piece missing. There
    is no longer a second place for a date to be wrong.
    """
    earliest = min(
        _day(when, end)
        for _name, script in run["scenarios"]
        for _kind, _code, when in script
    )
    return earliest - timedelta(days=1)


def _clone_name(run: dict, original: str) -> str:
    tag = run["tag"]
    return (original if original.startswith(tag) else f"{tag} - {original}")[:100]


def _require_test_mode() -> None:
    """Refuse to touch a live account. Every charge here is fictional."""
    from billing.services.stripe_client import get_stripe

    account = get_stripe().Account.retrieve()
    key = settings.STRIPE_SECRET_KEY or ""
    if not key.startswith("sk_test_"):
        raise SystemExit(
            f"refusing to run: STRIPE_SECRET_KEY is not a test key (account {account.get('id')})"
        )


_SIMULATED: dict[str, datetime] = {}


def _patch_clock() -> None:
    from billing.services import clock

    clock.now = lambda: _SIMULATED.get("t") or datetime.now(UTC)


def _patch_renewal_keys(run: dict) -> None:
    """Scope this run's renewal idempotency keys to its Stripe CUSTOMER.

    `renewals.period_key` is `renewal-{payer}-{period_start}`, and that stability is the
    point of it in production: it is claimed under a unique index before the charge, so a
    runner that dies mid-charge cannot bill the same period twice. It must NOT be made to
    rotate there.

    A replay breaks the assumption behind it. Stripe honours an idempotency key for 24
    hours, and re-running the same payer the same day re-sends those keys with different
    amounts against a different customer — which Stripe rejects outright. The renewal then
    fails, dunning starts, and the whole run past the first renewal is fiction. It reads as
    a declined card, which is the most misleading possible symptom in a billing replay.

    `--teardown` does not help: it rotates ENTITY ids, so the checkout keys change, but the
    payer is exactly what setup preserves. The customer, though, is minted fresh whenever
    the test clock is spent — so it identifies the run attempt, and scoping to it gives
    each replay its own key space while keeping keys stable WITHIN a run, which is what
    makes the crash-recovery path still testable.
    """
    from billing.services import renewals, store

    customer_id = store.customer_id_for_user(run["user_id"]) or ""
    if not customer_id:
        return
    original = renewals.period_key
    suffix = customer_id[-12:]

    # ``group_id`` is part of the real key — a payer with two cards raises two invoices
    # for one period — so the wrapper has to carry it through. Taking only (user, period)
    # made this a TypeError on the first renewal of any run, which reads as a failed
    # charge and turns everything after it into fiction.
    def _scoped(user_id, period, group_id=None) -> str:
        return f"{original(user_id, period, group_id)}-{suffix}"

    renewals.period_key = _scoped
    print(f"renewal keys scoped to {suffix}")


def _patch_change_keys(run: dict) -> None:
    """The same scoping as ``_patch_renewal_keys``, for the OTHER idempotency key.

    ``changes.change_key`` is ``change-{entity}-{when}-{codes}``, and every part of it
    survives a replay: ``--reset`` keeps the entities, and the scenario dates are derived
    from today, so a second run on the same day reproduces the key exactly. The customer
    does not survive — a spent test clock mints a new one — so Stripe sees a key it has
    seen in the last 24 hours, with different parameters, and refuses the request
    outright.

    It surfaces as "We couldn't set up the subscription. Please check your payment method
    and try again.", which is the most misleading message it could possibly be: nothing is
    wrong with the card, and the purchase it refused is one the run needs. It cost a
    Comeback Co purchase and a reserved-but-never-sent invoice row before it was traced.

    Scoped to the CUSTOMER, exactly as the renewal keys are, so keys stay stable WITHIN a
    run — the crash-recovery path is still exercised — and cannot collide ACROSS one.
    """
    from billing.services import changes, store

    customer_id = store.customer_id_for_user(run["user_id"]) or ""
    if not customer_id:
        return
    original = changes.change_key
    suffix = customer_id[-12:]

    def _scoped(entity_id, at, after_codes) -> str:
        return f"{original(entity_id, at, after_codes)}-{suffix}"

    changes.change_key = _scoped


def _patch_notify(run: dict) -> None:
    """Send THIS run's mail to somewhere other than the payer's own address.

    A payer's notices go wherever ``notify.recipient_for`` says, which is the ``email``
    column on their user row — the same field they sign in with. So a run whose payer is
    one person and whose mail should reach another cannot be expressed by the data alone;
    rewriting the column to the reader's address would take the login with it, and the
    address may already belong to a different payer.

    Redirecting the lookup instead keeps the payer intact and touches no product code.
    Scoped to this run's payer BY ID: the daily jobs are global, and a blanket redirect
    would divert a real payer's mail if one ever came due mid-replay.
    """
    redirect = run.get("notify_to")
    if not redirect:
        return
    from billing.services import notify

    original = notify.recipient_for

    def _redirected(user_id):
        address, first_name = original(user_id)
        if str(user_id) == str(run["user_id"]):
            return redirect, first_name
        return address, first_name

    notify.recipient_for = _redirected
    print(f"mail for {run['email']} -> {redirect}")


def _advance_stripe_clock(run: dict, to: datetime) -> str | None:
    """Move the payer's Stripe test clock to ``to`` and wait for it to settle.

    Asynchronous — the clock reads `advancing` then `ready` — so this polls. Forward
    only; Stripe rejects a backwards advance, which is why the replay loop is ascending.
    """
    from billing.services import store
    from billing.services.stripe_client import get_stripe

    stripe = get_stripe()
    customer_id = store.customer_id_for_user(run["user_id"])
    clock_id = stripe.Customer.retrieve(customer_id).get("test_clock")
    if isinstance(clock_id, dict):
        clock_id = clock_id.get("id")
    if not clock_id:
        return None

    if int(stripe.test_helpers.TestClock.retrieve(clock_id)["frozen_time"]) >= int(to.timestamp()):
        return clock_id

    stripe.test_helpers.TestClock.advance(clock_id, frozen_time=int(to.timestamp()))
    for _ in range(120):
        state = stripe.test_helpers.TestClock.retrieve(clock_id)
        if state["status"] == "ready":
            return clock_id
        if state["status"] != "advancing":
            raise RuntimeError(f"test clock {clock_id} is {state['status']}")
        time.sleep(1)
    raise RuntimeError(f"test clock {clock_id} did not settle")


def _needs_stripe_clock(run: dict, day: datetime, event_days: set) -> bool:
    """Whether anything today could create a Stripe object, and so needs the clock moved.

    Advancing is what makes a replay slow: it is asynchronous, so the script polls once a
    second until Stripe reports `ready`, and at ~15s each that is most of the wall clock
    of a 124-day run. But the great majority of days in a lifecycle scenario are empty —
    no event, no renewal — and on those the clock does not matter, because nothing asks
    Stripe to stamp anything. `clock.now()` is a patched lambda and is always correct.

    So the clock is moved only on days that can reach Stripe, and jumps straight to the
    next such day when it does. Stripe allows an arbitrary forward jump; it only refuses
    to go backwards.

    Deliberately CONSERVATIVE — it errs towards advancing. A day wrongly skipped would
    stamp a real invoice with a stale date, which is the exact defect the test clock
    exists to prevent, while a day wrongly advanced costs only time.
    """
    if day.date() in event_days:
        return True

    from billing.services import store

    mapping = store.customer_mapping_for_user(run["user_id"])
    if mapping is None:
        return False
    # THE CARDS, not the account. ``paid_through`` and the dunning clock moved from
    # ``user_stripe_customer`` to ``payer_billing_group`` with per-entity cards (2026-08-25):
    # a payer with two cards has two cycles, and either one coming due is a Stripe write.
    # This read them off the mapping until 2026-09-21, raising AttributeError on every
    # non-event day - which the caller logged as ``!! test clock: ...`` and treated as "do
    # not advance", so for a month every renewal after the last scripted event was stamped
    # by Stripe with that event's date.
    groups = store.billing_groups_for_payer(run["user_id"])
    # Mid-dunning on any card: a retry can charge on any of the offset days, and working
    # out which from here would duplicate the schedule. Cheaper to advance for the whole
    # episode.
    if any(group.dunning_started_at is not None for group in groups):
        return True
    # A card's renewal is due AND has something to raise. Both halves matter:
    # `paid_through` only moves when a renewal SUCCEEDS, so on a card that has stopped
    # paying it freezes and "due" stays true every day for the rest of the replay — which
    # is how the first version of this skipped almost nothing on exactly the runs it was
    # meant to speed up. `due_renewals` asks the same second question, and when the
    # answer is no it returns without creating an invoice, so there is nothing to stamp.
    for group in groups:
        if group.paid_through is not None and group.paid_through <= day:
            from billing.services import renewals

            if renewals.billable_codes_by_entity(run["user_id"], group_id=group.id) or \
                    store.pending_extensions_for_payer(run["user_id"]):
                return True
    # A trial ending today CONVERTS, and a conversion bills.
    for row in store.module_rows_for_payer(run["user_id"]):
        if row.phase == "trial" and row.trial_end is not None and row.trial_end <= day:
            return True
    return False


def _new_card(run: dict, spec: str) -> str:
    """Mint a card from a test token and attach it to the payer's customer.

    The three card events below all start here and differ only in what they then point at
    it: the account default, one company, or a whole group.

    A TAGGED spec — ``"B:tok_visa"`` — is minted once and REUSED for the rest of the run.
    Without that, "put this company on the same card as that one" is inexpressible: every
    event minting its own ``pm_...`` from the same token means every company lands on a
    card of its own, and a scenario meant to show two invoices quietly shows four. The
    tag is the card's identity for the run; the token is only how Stripe is asked to make
    it (and therefore whether it will decline).
    """
    from billing.services import store
    from billing.services.stripe_client import get_stripe

    tag, _, token = spec.rpartition(":")
    minted = run.setdefault("_cards", {})
    if tag and tag in minted:
        return minted[tag]

    stripe = get_stripe()
    customer_id = store.customer_id_for_user(run["user_id"])
    pm = stripe.PaymentMethod.create(type="card", card={"token": token})
    stripe.PaymentMethod.attach(pm["id"], customer=customer_id)
    if tag:
        minted[tag] = pm["id"]
    return pm["id"]


def tagged(tag: str, token: str) -> str:
    """``"B:tok_visa"`` — one card, referred to by name for the rest of the run."""
    return f"{tag}:{token}"


def _set_card(run: dict, token: str) -> str:
    """Attach a card and make it the ACCOUNT DEFAULT.

    What every ``("card", ...)`` event in the scenarios below does, and it is no longer
    the same thing as "change what gets charged": each company is billed on the card it
    was nominated onto, so this only decides what the pickers offer first.

    It still matters to a replay for exactly that reason — the consent paths with no
    picker (``checkout._ensure_nominated``, and a transfer accept) nominate the default at
    the moment they need a card, so this is what a company seeded by one of those ends up
    on.
    """
    from billing.services import store
    from billing.services.stripe_client import get_stripe

    stripe = get_stripe()
    customer_id = store.customer_id_for_user(run["user_id"])
    pm = _new_card(run, token)
    stripe.Customer.modify(
        customer_id, invoice_settings={"default_payment_method": pm}
    )
    return pm


def _nominate_card(run: dict, entity, token: str) -> str:
    """Put ONE company on a new card — the "Change billing account" write.

    Mints a card and nominates this company onto it, which is what a payer choosing a
    card in the picker does. The company leaves whatever group it was in and joins (or
    opens) the one for this card, carrying its paid days with it — see
    ``store._carry_paid_days``, which is why the new card renews on the cycle the old one
    left off at rather than never renewing.
    """
    from billing.services import store

    pm = _new_card(run, token)
    store.nominate_card_for_entity(entity.id, run["user_id"], pm, "chosen")
    return pm


def _replace_group_card(run: dict, entity, token: str) -> str:
    """Swap the card on the GROUP this company is in — a card replacement.

    Distinct from ``_nominate_card`` and the distinction is the point: nominating moves
    ONE company to another card, replacing changes the card under every company already on
    it. An expiry, a lost card and a re-issued number are all this one.

    The group keeps its id, its cycle and its dunning clock, so a replacement mid-episode
    is what recovery looks like: the next retry charges the new card (dunning passes the
    group's CURRENT card, not the one the invoice was raised against).
    """
    from billing.services import store

    group = store.billing_group_for_entity(entity.id, run["user_id"])
    if group is None:
        raise RuntimeError(
            f"{entity.name} is on no card yet — nominate one before replacing it"
        )
    pm = _new_card(run, token)
    group.stripe_payment_method_id = pm
    group.save(update_fields=["stripe_payment_method_id"])
    return pm


PURPOSE = "scenario-replay"
CLOCK_PREFIX = "replay-"


def _clock_id(customer: dict) -> str | None:
    ref = customer.get("test_clock")
    return ref.get("id") if isinstance(ref, dict) else ref


def _survey(stripe, cutoff: datetime) -> tuple[list, list]:
    """(deletable clocks, orphan customers) - Minty's ``prune_replay_stripe.survey`` over
    THIS database only: a replay-purpose customer (or one on a ``replay-*`` clock) that no
    ``user_stripe_customer`` row names and that is older than ``cutoff``."""
    from shared_models.models import UserStripeCustomer

    clocks = {}
    for clock in stripe.test_helpers.TestClock.list(limit=100).auto_paging_iter():
        if (clock.get("name") or "").startswith(CLOCK_PREFIX):
            clocks[clock["id"]] = clock
    used = set(UserStripeCustomer.objects.exclude(stripe_customer_id=None).values_list("stripe_customer_id", flat=True))

    customers, seen = [], set()
    for clock_id in clocks:
        for customer in stripe.Customer.list(test_clock=clock_id, limit=100).auto_paging_iter():
            if customer["id"] not in seen:
                seen.add(customer["id"])
                customers.append(customer)
    try:
        for customer in stripe.Customer.search(query=f"metadata['purpose']:'{PURPOSE}'", limit=100).auto_paging_iter():
            if customer["id"] not in seen:
                seen.add(customer["id"])
                customers.append(customer)
    except Exception as exc:  # search is a separate, index-backed API
        print(f"  (metadata search unavailable: {exc}; clock-attached objects only)")

    orphans = []
    for customer in customers:
        ours = (customer.get("metadata") or {}).get("purpose") == PURPOSE or _clock_id(customer) in clocks
        if not ours or customer["id"] in used:
            continue
        if datetime.fromtimestamp(customer["created"], UTC) > cutoff:
            continue
        orphans.append(customer)
    orphan_ids = {c["id"] for c in orphans}
    deletable_clocks = []
    for clock_id, clock in clocks.items():
        members = {c["id"] for c in customers if _clock_id(c) == clock_id}
        if not members or members <= orphan_ids:
            deletable_clocks.append(clock)
    orphans = [c for c in orphans if _clock_id(c) not in {c["id"] for c in deletable_clocks}]
    return deletable_clocks, orphans


def prune_orphans() -> None:
    """Delete the Stripe objects `--setup` just orphaned. Runs straight after it.

    AFTER setup, not before, and the ordering is the whole trick. A spent clock cannot be
    rewound, so setup mints a new clock and customer and re-points the payer's mapping at
    them — which is the instant the PREVIOUS customer becomes unreferenced. Pruning
    beforehand would find it still named by the mapping, judge it in use, and leave it.

    NEVER FATAL. This is housekeeping and the replay is the job.
    """
    from billing.services.stripe_client import get_stripe
    from shared_models.models import Entity, SubscriptionAuditLog

    # The database half first: `--teardown` deletes this run's entities moments before
    # `--setup` recreates them, and `subscription_audit_log` is the one table with no
    # foreign key to follow them down. Replay payers only.
    try:
        payers = [run["user_id"] for run in RUNS.values()]
        alive = set(Entity.objects.values_list("id", flat=True))
        dangling = [
            row.id for row in SubscriptionAuditLog.objects.filter(payer_user_id__in=payers)
            if str(row.entity_id) not in alive
        ]
        if dangling:
            SubscriptionAuditLog.objects.filter(id__in=dangling).delete()
        print(f"  {len(dangling)} dangling audit row(s) deleted")
    except Exception as exc:
        print(f"prune: dangling rows skipped ({type(exc).__name__}: {exc})")

    try:
        stripe = get_stripe()
        clocks, customers = _survey(stripe, datetime.now(UTC) - timedelta(minutes=5))
    except Exception as exc:
        print(f"prune: skipped ({type(exc).__name__}: {exc})")
        return

    if not clocks and not customers:
        print("prune: nothing orphaned")
        return
    for clock in clocks:
        try:
            stripe.test_helpers.TestClock.delete(clock["id"])
            print(f"prune: deleted clock {clock['id']} and everything on it")
        except Exception as exc:
            print(f"prune: could not delete clock {clock['id']} ({exc})")
    for customer in customers:
        try:
            stripe.Customer.delete(customer["id"])
            print(f"prune: deleted customer {customer['id']}")
        except Exception as exc:
            print(f"prune: could not delete customer {customer['id']} ({exc})")


def _entities(run: dict) -> dict:
    from shared_models.models import Entity

    wanted = {_clone_name(run, name) for name, _ in run["scenarios"]}
    return {e.name: e for e in Entity.objects.filter(name__in=list(wanted))}


# --- setup ------------------------------------------------------------------

def setup(run: dict) -> None:
    from billing.services import store
    from billing.services.stripe_client import get_stripe
    from shared_models.models import Entity, User, UserEntity

    origin = _origin(run, _end_of_run())

    with _context.scope():
        _require_test_mode()
        stripe = get_stripe()

        user = User.objects.filter(pk=run["user_id"]).first()
        if user is None:
            first, last = run["name"]
            User.objects.create(
                id=run["user_id"], email=run["email"], username=run["email"],
                first_name=first, last_name=last,
                # The hash Flask's werkzeug verifies (pbkdf2, not its scrypt default: that
                # one is ~162 chars and the column is varchar(150)), so the payer can sign
                # in to Flask exactly as the Flask-seeded ones do.
                password=_werkzeug_pbkdf2(PASSWORD),
                system_role="normal", approved=True,
            )
            print(f"created payer {run['email']}")
        else:
            print(f"payer exists {run['email']}")

        mapping = store.customer_mapping_for_user(run["user_id"])
        existing_id = mapping.stripe_customer_id if mapping else None
        # Reusable ONLY while its clock is still at the origin. A spent clock cannot be
        # rewound, so every advance in the next replay would be a no-op and every invoice
        # would be stamped wherever the clock stopped.
        #
        # The origin now MOVES: it is derived from today, so a setup run yesterday and a
        # replay run today do not agree on it and the clock reads as spent. That is not a
        # bug to work around — yesterday's clock genuinely is at the wrong day for today's
        # schedule. `--setup --replay` in one command, which is the documented usage,
        # computes it once either side of the same midnight.
        at_origin = False
        if existing_id:
            try:
                ref = stripe.Customer.retrieve(existing_id).get("test_clock")
                if isinstance(ref, dict):
                    ref = ref.get("id")
                if ref:
                    frozen = int(stripe.test_helpers.TestClock.retrieve(ref)["frozen_time"])
                    at_origin = frozen == int(origin.timestamp())
                    if not at_origin:
                        print(f"clock {ref} is spent (at "
                              f"{datetime.fromtimestamp(frozen, UTC):%d %b %Y}) — minting a new one")
            except Exception:
                existing_id = None

        if existing_id and at_origin:
            customer_id = existing_id
            print(f"reusing customer {customer_id} (clock at origin)")
        else:
            clock = stripe.test_helpers.TestClock.create(
                frozen_time=int(origin.timestamp()), name=f"replay-{run['tag']}",
            )
            customer = stripe.Customer.create(
                email=run["email"], name=" ".join(run["name"]),
                test_clock=clock["id"], metadata={"purpose": "scenario-replay"},
            )
            customer_id = customer["id"]
            store.upsert_customer_mapping(run["user_id"], customer_id)
            print(f"created clock {clock['id']} at {origin:%d %b %Y} "
                  f"and customer {customer_id}")

        if not (stripe.Customer.retrieve(customer_id).get("invoice_settings") or {}).get(
            "default_payment_method"
        ):
            print(f"attached card {_set_card(run, CARD_GOOD)}")

        made = 0
        for original, _script in run["scenarios"]:
            name = _clone_name(run, original)
            if Entity.objects.filter(name=name).exists():
                continue
            Entity.objects.create(
                id=str(uuid.uuid4()), name=name, country_code="HK", status="disconnected",
            )
            made += 1

        joined = 0
        now = datetime.now(UTC)
        for entity in _entities(run).values():
            if not UserEntity.objects.filter(user_id=run["user_id"], entity_id=entity.id).exists():
                UserEntity.objects.create(
                    user_id=run["user_id"], entity_id=entity.id, role="admin",
                    approved=True, joined_at=now, created_at=now,
                )
                joined += 1
        print(f"created {made} entities, +{joined} membership(s); "
              f"sign in as {run['email']} / {PASSWORD}")


def _werkzeug_pbkdf2(password: str, iterations: int = 600000) -> str:
    """``werkzeug.security.generate_password_hash(password, method="pbkdf2:sha256")`` without
    werkzeug: ``pbkdf2:sha256:<iterations>$<salt>$<hex>``, which Flask's ``check_password_hash``
    verifies. 16-char alphanumeric salt as werkzeug's ``gen_salt`` makes it."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    salt = "".join(secrets.choice(alphabet) for _ in range(16))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"pbkdf2:sha256:{iterations}${salt}${digest}"


# --- the replay -------------------------------------------------------------

def _buy(run: dict, entity, user, code: str) -> list[str]:
    """Subscribe a module the way the browser would, minus the browser.

    `start_modules_checkout` resolves the card through `_customer_id_for_entity`, which is
    entity -> PAYER -> customer. A brand-new entity has no module row, so no payer, so no
    card — and it correctly returns a hosted setup-Checkout URL, because the real first
    purchase on a new company goes through Stripe's page and that page is what links the
    entity to the payer.

    A script cannot click that page, so it does what `complete_setup_checkout` does on the
    way back: hand the known customer and card to `_create_paid_subscriptions`. Nothing
    guarding money is skipped — consent is recorded by the caller, the codes still go
    through `_resolve_checkout_plans`, and the charge runs the identical path. What is
    skipped is the card CAPTURE, and the card is already on file.

    THE NOMINATION IS NOT SKIPPED EITHER, and it cannot be. Nothing charges a company
    with no card nominated for it, so a replay that records consent by hand — as this one
    does, to avoid the browser — must also do what the consent paths do and put the
    company on a card. ``_ensure_nominated`` is that step in the app; calling it here is
    the difference between a replay that bills and one where every purchase answers
    "check your payment method".
    """
    from billing.services import checkout as ck
    from billing.services import store
    from billing.services.stripe_client import customer_default_payment_method

    customer_id = store.customer_id_for_user(run["user_id"])
    payment_method = customer_default_payment_method(customer_id)
    if not payment_method:
        raise RuntimeError("payer has no default card — run --setup")
    ck._ensure_nominated(entity.id, run["user_id"])
    plans = ck._resolve_checkout_plans(entity.id, [code])
    return ck._create_paid_subscriptions(
        entity, user, customer_id, plans, payment_method, uuid.uuid4().hex
    )


@contextmanager
def _scoped_to_payer(user_id: str):
    """Narrow the global daily jobs to ONE payer, for the duration of one call.

    `run_renewals` takes a scope and this script has always passed it. The other three
    jobs do not, and ran across EVERY payer in the database on this run's injected clock.
    An R1 replay converted a trial belonging to an unrelated account, charged it and
    emailed it — the blast radius the ALL_PAYERS sentinel exists to prevent, reached
    through a different door. Nothing about it was visible in the run's own report.

    Patched at the STORE seam rather than by adding a scope parameter to three production
    functions. The harness already replaces `clock.now` exactly this way, and a dev script
    must not reshape the API of the jobs it exists to observe.
    """
    from billing.services import store

    names = {
        "due_trials": "payer_user_id",             # close-trials
        "trials_ending_between": "payer_user_id",  # notify-trial-ending
        "groups_in_dunning": "payer_user_id",      # retry-dunning
    }
    original = {name: getattr(store, name) for name in names}

    def _only_ours(fn, attr):
        def wrapped(*args, **kwargs):
            return [
                row for row in fn(*args, **kwargs)
                if str(getattr(row, attr, None)) == str(user_id)
            ]
        return wrapped

    for name, attr in names.items():
        setattr(store, name, _only_ours(original[name], attr))
    try:
        yield
    finally:
        for name, fn in original.items():
            setattr(store, name, fn)


def _daily_jobs(run: dict, today: datetime, log: list[str]) -> None:
    """The five scheduled jobs, in the order `daily.JOB_ORDER` pins them to.

    Kept as its own sequence rather than calling `daily.run_daily`, because renewals here
    must be SCOPED to this run's payer and that pass is global. The order, though, has to
    match it exactly — see the note on sweep-access below for what happens when it does
    not.
    """
    from billing.services import dunning, renewals
    from billing.services.access_sweep import sweep_expired_module_access
    from billing.services.checkout import convert_or_expire_due_trials, notify_trials_ending

    def _step(label: str, fn) -> dict | None:
        """Run one job, log what it did, and never let it poison the rest of the run.

        A failed job leaves the session in a failed transaction, and every statement
        after it — including the other jobs and the next 100 days — dies with
        PendingRollbackError. One dropped connection took out 84 days of a replay this
        way, and a missing column took out all 67 of another. The run is a simulation; a
        poisoned session is worth far more than the day that caused it.
        """
        try:
            with _scoped_to_payer(run["user_id"]):
                return fn()
        except Exception as exc:
            log.append(f"    !! {label}: {exc}")
            return None

    result = _step("notify-trial-ending", lambda: notify_trials_ending(days_before=3))
    for item in (result or {}).get("warned", []):
        log.append(f"    email  trial-ending  {item['codes']}")

    result = _step("close-trials", convert_or_expire_due_trials)
    for key in ("converted", "expired"):
        for item in (result or {}).get(key, []):
            log.append(f"    trial  {key:<9} {item.get('code')}")

    # SCOPED BY ID, never ALL_PAYERS — see the module docstring.
    result = _step(
        "run-renewals",
        lambda: renewals.run_renewals(today, scope=[run["user_id"]], issue=True),
    )
    for item in (result or {}).get("issued", []):
        log.append(f"    INVOICE renewal  {item['total']/100:>9,.2f}  {item['invoice']}")
    for item in (result or {}).get("failed", []):
        log.append(f"    DECLINED renewal {item['total']/100:>9,.2f}  {item.get('status')}")

    result = _step("retry-dunning", lambda: dunning.collect_due(today))
    for item in (result or {}).get("retried", []):
        log.append(f"    dunning retry -> {item.get('status')}")
    for item in (result or {}).get("recovered", []):
        log.append(f"    dunning RECOVERED {item.get('user_id', '')[:8]}")
    for item in (result or {}).get("gave_up", []):
        log.append(f"    dunning gave up {item.get('user_id', '')[:8]}")

    # LAST, and that is the whole point — see `cli/subscription_access.py`, which spells
    # out why: the sweep judges a paid module by the payer's `paid_through`, and
    # `run-renewals` is the only thing that advances it. Run third (where this used to run
    # it, and where the CLI's own list used to have it) the sweep revokes every payer
    # whose period elapsed this morning, seconds before the renewal step bills them for
    # the new period — and nothing re-syncs the map after a successful renewal, so access
    # comes back only on the NEXT day's pass. Every payer, every renewal, charged and
    # locked out for a day.
    #
    # The replay reproduced that faithfully, because it was running the old order: each
    # renewal day in a seeded run showed ACCESS revoked, then ACCESS restored the morning
    # after. Which made the harness disagree with production about the one ordering rule
    # production documents as load-bearing.
    # Scoped like the rest: the sweep accepts a payer already, and an
    # unscoped pass revokes access across accounts this run never touched.
    result = _step("sweep-access",
                   lambda: sweep_expired_module_access(run["user_id"]))
    # Both directions. The revocations were always the visible half; the restorations are
    # what a run has to show to prove an account that recovered actually got its access
    # back.
    for item in (result or {}).get("disabled", []):
        log.append(f"    ACCESS revoked  {item.get('code')}")
    for item in (result or {}).get("restored", []):
        log.append(f"    ACCESS restored {item.get('code')}")


def replay(run: dict, dry: bool = False) -> None:
    from billing.services import checkout as _checkout
    from billing.services import store
    from billing.services.checkout import cancel_module, reactivate_module, start_module_trials
    from billing.services.entity_modules import set_entity_module
    from shared_models.models import User

    _patch_clock()
    _patch_notify(run)
    last_day = _end_of_run()
    origin = _origin(run, last_day)

    if dry:
        # The CLONE name, not the script's — that is what `--setup` will create and what
        # `_entities` will look for, so it is the half worth checking before a run.
        rows = sorted(
            (_day(when, last_day), kind, code, _clone_name(run, name))
            for name, script in run["scenarios"]
            for kind, code, when in script
        )
        print(f"{origin:%d %b %Y} -> {last_day:%d %b %Y} "
              f"({(last_day - origin).days + 1} days, {len(rows)} events)\n")
        for when, kind, code, name in rows:
            print(f"  {when:%d %b %Y}  {kind:<7} {code:<22} {name}")
        return

    with _context.scope():
        _require_test_mode()
        # Inside the scope: they read the payer's customer id from the database. BOTH
        # idempotency keys have to be scoped, not just the renewal one — a purchase
        # colliding is harder to spot than a renewal colliding, because it is reported as
        # a card problem.
        _patch_renewal_keys(run)
        _patch_change_keys(run)
        user = User.objects.get(pk=run["user_id"])
        entities = _entities(run)
        expected = {_clone_name(run, n) for n, _ in run["scenarios"]}
        if len(entities) != len(expected):
            print(f"!! expected {len(expected)} entities, found {len(entities)}. Run --setup.")
            return

        # Keyed on the first three fields only. Two entities can have an identical
        # (day, kind, code) — the lifecycle run has two companies buying Petty Cash on
        # 06 Apr — and a bare sort then falls through to comparing Entity objects, which
        # are not orderable.
        events = sorted(
            (
                (_day(when, last_day), kind, code, entities[_clone_name(run, name)])
                for name, script in run["scenarios"]
                for kind, code, when in script
            ),
            key=lambda event: (event[0], event[1], event[2]),
        )
        print(f"replaying {run['tag']}: {origin:%d %b %Y} -> {last_day:%d %b %Y} "
              f"({(last_day - origin).days + 1} days, {len(events)} events)")

        event_days = {when.date() for when, _kind, _code, _entity in events}
        advanced = skipped_days = 0

        day = origin
        while day <= last_day:
            _SIMULATED["t"] = day
            log: list[str] = []
            try:
                if _needs_stripe_clock(run, day, event_days):
                    advanced += 1
                    if _advance_stripe_clock(run, day) is None:
                        log.append("    !! not on a test clock — dates will be today")
                else:
                    skipped_days += 1
            except Exception as exc:
                log.append(f"    !! test clock: {exc}")

            for when, kind, code, entity in events:
                if when.date() != day.date():
                    continue
                # Read BEFORE the try. `entity.name` is a lazy-loaded column, so on a
                # session already poisoned by a failed statement, reading it inside the
                # `except` raises a SECOND exception — from the handler, where nothing
                # catches it — and the whole replay dies on the first bad day instead of
                # logging it. That is how a missing column took out an entire run:
                # every daily job logged its failure as designed, then the first event's
                # error message could not be built. The name is fixed for the run, so
                # there is no reason to fetch it at the moment things are worst.
                name = entity.name
                try:
                    if kind == "trial":
                        start_module_trials(entity, user, [code])
                        set_entity_module(entity.id, code, True, actor="replay")
                        log.append(f"    trial  start   {code:<11} {name[:34]}")
                    elif kind == "buy":
                        store.record_billing_consent(entity.id, run["user_id"], "confirmed")
                        got = _buy(run, entity, user, code)
                        log.append(f"    BUY    {code:<11} {name[:30]} -> {got}")
                    elif kind == "cancel":
                        cancel_module(entity, user, code, reason="replay")
                        log.append(f"    cancel {code:<11} {name[:34]}")
                    elif kind == "uncancel":
                        # NOT a re-buy. `_resolve_checkout_plans` refuses one while the
                        # module is still inside its paid cancellation window — "use Renew
                        # instead" — because buying would charge again on top of an
                        # extension the customer already holds. This is that Renew.
                        reactivate_module(entity, user, code)
                        log.append(f"    UNCANCEL {code:<10} {name[:33]}")
                    elif kind == "consent":
                        # Both halves, as the app does them: the agreement, and the card
                        # it will be billed to. A trial that converts with consent but no
                        # nomination expires instead — there is no account default to
                        # fall back on at the charge.
                        store.record_billing_consent(entity.id, run["user_id"], "confirmed")
                        _checkout._ensure_nominated(entity.id, run["user_id"])
                        log.append(f"    consent billing authorised for {name[:32]}")
                    elif kind == "card":
                        pm = _set_card(run, code)
                        which = "DECLINING" if code == CARD_FAIL else "working"
                        log.append(f"    card   account default is a {which} card "
                                   f"({pm[:18]})")
                    elif kind == "nominate":
                        # ONE company onto a card of its own. This is what splits an
                        # account's invoice in two.
                        pm = _nominate_card(run, entity, code)
                        which = "DECLINING" if code == CARD_FAIL else "working"
                        log.append(f"    CARD   {name[:30]} -> {which} card "
                                   f"({pm[:18]})")
                    elif kind == "recard":
                        # The card UNDER a company changes — an expiry, a re-issue. Every
                        # company on that card moves with it; the group keeps its cycle.
                        pm = _replace_group_card(run, entity, code)
                        which = "DECLINING" if code == CARD_FAIL else "working"
                        log.append(f"    RECARD {name[:30]}'s card replaced with a "
                                   f"{which} one ({pm[:18]})")
                except Exception as exc:
                    log.append(f"    !! {kind} {code} {name[:26]}: {exc}")

            _daily_jobs(run, day, log)

            if log:
                print(f"\n{day:%d %b %Y}")
                for line in log:
                    print(line)
            day += timedelta(days=1)

        # Reported because the saving is the whole point of skipping, and because a run
        # that advanced on EVERY day means the predicate stopped discriminating — worth
        # noticing before it silently costs half an hour again.
        print(f"\nreplay complete — Stripe clock advanced on {advanced} day(s), "
              f"skipped {skipped_days}")


# --- report / cleanup -------------------------------------------------------

def report(run: dict) -> None:
    from django.db.models import F

    from billing.services import store
    from shared_models.models import SubscriptionInvoice

    with _context.scope():
        # NULLS LAST: Postgres sorts NULL first by default on ASC, so a reserved row
        # would head the list as if it were the earliest invoice of the run.
        invoices = list(
            SubscriptionInvoice.objects.filter(payer_user_id=run["user_id"])
            .order_by(F("issued_at").asc(nulls_last=True))
            .prefetch_related("lines")
        )
        # WHICH CARD each invoice was raised against, so a split account can be read at
        # all: on one card this column is the same value on every row and says nothing,
        # and on two it is the only thing that explains why a month has two documents.
        cards = {
            str(g.id): g.stripe_payment_method_id[:18]
            for g in store.billing_groups_for_payer(run["user_id"])
        }
        print(f"INVOICES: {len(invoices)} on {len(cards) or 1} card(s)")
        for inv in invoices:
            # `issued_at` is NULL on a RESERVED row — the key was claimed and we never
            # heard back that anything was sent (see the model docstring, and
            # `find_invoice_by_metadata` for how such a row is resolved). Rare in
            # production and the whole point of the reservation, but a replay produces
            # one whenever a charge fails, and formatting None as a date crashed the
            # report AFTER a successful 67-day run — losing the summary of work that had
            # actually completed.
            issued = f"{inv.issued_at:%d %b %Y}" if inv.issued_at else "not issued "
            card = cards.get(str(inv.billing_group_id), "-")
            print(f"  {issued}  {inv.status:<13} "
                  f"{inv.currency} {inv.total/100:>9,.2f}  "
                  f"{inv.period_start:%d %b}-{inv.period_end:%d %b}  "
                  f"card={card:<18} {inv.payment_method or '-'}")
            for line in inv.lines.all():
                print(f"       {line.entity_name[:40]:<40} {line.product_name:<14} "
                      f"{line.amount/100:>9,.2f}")

        mapping = store.customer_mapping_for_user(run["user_id"])
        print(f"\nACCOUNT anchor={getattr(mapping, 'anchor_at', None)}")

        # THE CARDS, and the cycle each one owns. ``paid_through`` and the dunning clock
        # moved here from the account: a payer with two cards has two of each, and one can
        # be past due while the other is paid up. The account line above keeps only what
        # is still per payer — the anchor every card renews on.
        names = {str(e.id): n for n, e in _entities(run).items()}
        for group in store.billing_groups_for_payer(run["user_id"]):
            on_it = sorted(
                names.get(e, e)[:28]
                for e in store.entity_ids_in_group(group.id)
            )
            print(f"  card {group.stripe_payment_method_id[:18]}  "
                  f"paid_through={str(group.paid_through)[:10]}  "
                  f"dunning={str(group.dunning_started_at)[:10]}  "
                  f"{', '.join(on_it) or '(no companies)'}")

        rows = store.module_rows_for_payer(run["user_id"])
        print(f"\nSTATE ({len(rows)} module rows):")
        for row in sorted(rows, key=lambda r: names.get(str(r.entity_id), "")):
            print(f"  {names.get(str(row.entity_id), '?')[:44]:<44} {row.function_code:<11} "
                  f"{row.phase:<17} trial_end={str(row.trial_end)[:10]:<10} "
                  f"ext={row.extension_state}")


def reset(run: dict) -> None:
    """Clear what a replay wrote. Keeps the payer, the customer and the entities.

    That includes the two LOGS, which it did not until 2026-08-17, and the omission was
    not harmless:

    * `subscription_audit_log` survived `--teardown` as well, so its rows outlived the
      entities they name. One payer replayed three times ended up with sixteen audit rows,
      EIGHT of them pointing at entity ids that no longer existed — history the portal
      would render for companies that are not there. Nothing stopped it, because the
      foreign key the model declares on `entity_id` does not exist in the database.
    * `subscription_email_log` is worse than noise. Its whole job is to make a send happen
      once: the row is claimed BEFORE the mail goes out, and a later pass finding it skips
      the send. Left behind across a `--reset` — which keeps entity ids, and so keeps the
      dedupe keys derived from them — the next replay's notices are silently suppressed by
      the last one's. The run looks identical and the mail never arrives.

    Both are append-only in production, deliberately, and nothing here changes that. This
    is the replay's own payer being rewound.
    """
    from billing.services import store
    from shared_models.models import (
        EntityBillingConsent,
        EntityBillingGroup,
        EntityModuleSubscription,
        PayerBillingGroup,
        SubscriptionAuditLog,
        SubscriptionEmailLog,
        SubscriptionInvoice,
        SubscriptionInvoiceLine,
    )

    with _context.scope():
        ids = [str(e.id) for e in _entities(run).values()]
        rows, _ = EntityModuleSubscription.objects.filter(payer_user_id=run["user_id"]).delete()
        consents = 0
        if ids:
            consents, _ = EntityBillingConsent.objects.filter(entity_id__in=ids).delete()
        invoice_ids = list(
            SubscriptionInvoice.objects.filter(payer_user_id=run["user_id"]).values_list("id", flat=True)
        )
        SubscriptionInvoiceLine.objects.filter(invoice_id__in=invoice_ids).delete()
        invoices, _ = SubscriptionInvoice.objects.filter(payer_user_id=run["user_id"]).delete()

        audits, _ = SubscriptionAuditLog.objects.filter(payer_user_id=run["user_id"]).delete()
        emails, _ = SubscriptionEmailLog.objects.filter(user_id=run["user_id"]).delete()

        # THE CARDS AND THEIR CYCLES. Nominations first, then the groups they point at —
        # the foreign key refuses it the other way round. Not optional: a group carries
        # ``paid_through`` and a dunning clock, so one left behind means the next replay
        # opens with a card already paid months ahead and ``due_renewals`` skips it.
        nominations = 0
        if ids:
            nominations, _ = EntityBillingGroup.objects.filter(entity_id__in=ids).delete()
        groups, _ = PayerBillingGroup.objects.filter(payer_user_id=run["user_id"]).delete()

        mapping = store.customer_mapping_for_user(run["user_id"])
        if mapping:
            # Only the anchor lives on the account now; the cycle and the dunning clock
            # went with the groups just deleted (Flask's copy still assigns the old
            # attributes, which its model no longer maps - a silent no-op there).
            mapping.anchor_at = None
            mapping.save(update_fields=["anchor_at"])
        print(f"reset {run['tag']}: {rows} module row(s), {consents} consent(s), "
              f"{invoices} invoice(s), {audits} audit row(s), {emails} email log row(s), "
              f"{nominations} nomination(s) on {groups} card group(s); "
              f"billing cycle cleared")


def teardown(run: dict) -> None:
    """Remove the entities too. Matched on this run's names, so it cannot reach a real one."""
    from shared_models.models import Entity, EntityModuleSubscription, UserEntity

    with _context.scope():
        ids = [str(e.id) for e in _entities(run).values()]
        if not ids:
            print("nothing to remove")
            return
        EntityModuleSubscription.objects.filter(entity_id__in=ids).delete()
        UserEntity.objects.filter(entity_id__in=ids).delete()
        Entity.objects.filter(id__in=ids).delete()
        print(f"removed {len(ids)} entities for {run['tag']}")


class Command(BaseCommand):
    help = __doc__.split("\n")[0]

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="show the runs")
        parser.add_argument("--run", choices=sorted(RUNS), help="which run to act on")
        parser.add_argument("--plan", action="store_true", help="print the timeline only")
        parser.add_argument("--setup", action="store_true")
        parser.add_argument("--replay", action="store_true")
        parser.add_argument("--report", action="store_true")
        parser.add_argument("--reset", action="store_true")
        parser.add_argument("--teardown", action="store_true")
        # Point an existing SHAPE at a new payer, instead of adding an entry to RUNS for it.
        parser.add_argument("--as", dest="payer_email", metavar="EMAIL",
                            help="run the chosen shape as this payer (requires --tag)")
        parser.add_argument("--tag", help="entity name prefix for the new payer's own copies")
        parser.add_argument("--payer-id", dest="payer_id",
                            help="adopt an existing payer id instead of deriving one from the email")
        parser.add_argument("--notify-to", dest="notify_to", metavar="EMAIL",
                            help="send this run's mail somewhere other than the payer (mail is "
                                 "otherwise skipped: console backend)")
        parser.add_argument("--no-prune", dest="no_prune", action="store_true",
                            help="keep the Stripe customer --setup just orphaned")

    def handle(self, *args, **options):
        class _Args:
            pass

        a = _Args()
        for key in ("list", "run", "plan", "setup", "replay", "report", "reset", "teardown",
                    "payer_email", "tag", "payer_id", "notify_to", "no_prune"):
            setattr(a, key, options.get(key))
        if not a.notify_to:
            # Skipped, not spent: ``notify.mail_configured`` is false for the console backend,
            # so no dedupe row is written and nobody's real inbox is reached by a replay.
            settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        _main(a)


def _main(args) -> None:
        if args.list or not args.run:
            end = _end_of_run()
            for key, run in sorted(RUNS.items()):
                origin = _origin(run, end)
                print(f"  {key:<10} {run['label']}")
                print(f"             payer {run['email']}, {origin:%d %b %Y} -> "
                      f"{end:%d %b %Y} ({(end - origin).days + 1} days), "
                      f"{len(run['scenarios'])} entries")
            if not args.run:
                return

        selected = RUNS[args.run]
        if args.payer_email or args.tag:
            # Both, always. A new payer on an existing tag would seed nothing and quietly
            # adopt the other payer's entities; a new tag on an existing payer would seed a
            # second set of companies onto an account that already has one.
            if not (args.payer_email and args.tag):
                raise SystemExit("--as and --tag go together")
            if args.tag in {run["tag"] for run in RUNS.values()}:
                raise SystemExit(f"tag {args.tag!r} already belongs to a run in RUNS — pick "
                                 f"another, or use --run for that one")
            selected = clone_run(selected, args.tag, args.payer_email,
                                 user_id=args.payer_id, notify_to=args.notify_to)
            print(f"{selected['label']}\n  payer {selected['email']} "
                  f"({selected['user_id']}), entities tagged {selected['tag']}\n")

        # Order matters: teardown before setup so a re-seed gets fresh entity ids (and so
        # fresh Stripe idempotency keys), and reset before replay.
        if args.reset:
            reset(selected)
        if args.teardown:
            teardown(selected)
        if args.setup:
            setup(selected)
            # Immediately after, while the customer it just replaced is unreferenced and
            # before the replay spends any time. See `prune_orphans`.
            if not args.no_prune:
                prune_orphans()
        if args.plan:
            replay(selected, dry=True)
        if args.replay:
            replay(selected)
        if args.report:
            report(selected)
