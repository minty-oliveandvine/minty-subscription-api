"""Unit tests for the dunning schedule.

Dunning decides when a customer's card is charged again and when their subscription is
given up on. Both directions cost something: retrying too long charges someone whose
access has already ended, and giving up too early loses a recoverable customer.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from billing.services.access import PAST_DUE_GRACE_DAYS
from billing.services.dunning import (
    GIVE_UP_AFTER_DAYS,
    MAX_ATTEMPTS,
    RETRY_OFFSETS_DAYS,
    attempts_remaining,
    give_up_at,
    is_exhausted,
    next_attempt_at,
    should_attempt_now,
    should_give_up,
)

FAILED_AT = datetime(2027, 3, 8, 13, tzinfo=UTC)


def day(n: float) -> datetime:
    return FAILED_AT + timedelta(days=n)


# --- the invariant -------------------------------------------------------------


def test_dunning_finishes_inside_the_access_grace_window():
    """THE coherence rule between the two modules.

    Retries running past the grace end would charge a customer whose access was already
    revoked. Giving up well before it ends would leave them with free access and no way
    to recover even after fixing their card. Changing either constant without the other
    must fail here rather than drift.
    """
    assert GIVE_UP_AFTER_DAYS <= PAST_DUE_GRACE_DAYS
    assert max(RETRY_OFFSETS_DAYS) < GIVE_UP_AFTER_DAYS


def test_the_last_retry_leaves_time_to_settle():
    """A payment attempted at the very end of the window could not clear before access
    was cut, making the attempt pointless."""
    assert GIVE_UP_AFTER_DAYS - max(RETRY_OFFSETS_DAYS) >= 2


# --- the schedule --------------------------------------------------------------


def test_retries_are_timed_from_the_first_failure_not_the_last_attempt():
    """A delayed attempt — worker outage, queue backlog — must not push everything after
    it back and quietly extend dunning past the grace window."""
    assert next_attempt_at(FAILED_AT, 0) == day(RETRY_OFFSETS_DAYS[0])
    assert next_attempt_at(FAILED_AT, 1) == day(RETRY_OFFSETS_DAYS[1])
    assert next_attempt_at(FAILED_AT, MAX_ATTEMPTS - 1) == day(RETRY_OFFSETS_DAYS[-1])


def test_the_schedule_runs_out():
    assert next_attempt_at(FAILED_AT, MAX_ATTEMPTS) is None
    assert is_exhausted(MAX_ATTEMPTS)
    assert not is_exhausted(MAX_ATTEMPTS - 1)


def test_retries_are_front_loaded():
    """Most failures are transient, so the early attempts recover most of what is
    recoverable; the tail is spaced to give a customer time to act."""
    gaps = [b - a for a, b in zip(RETRY_OFFSETS_DAYS, RETRY_OFFSETS_DAYS[1:])]
    assert RETRY_OFFSETS_DAYS[0] <= 1
    assert all(gap >= 1 for gap in gaps)


def test_a_negative_attempt_count_is_rejected():
    """It would index backwards into the schedule and silently retry at the wrong time."""
    with pytest.raises(ValueError):
        next_attempt_at(FAILED_AT, -1)


# --- when to act ---------------------------------------------------------------


def test_no_retry_before_it_is_due():
    assert not should_attempt_now(day(0.5), FAILED_AT, attempts=0)
    assert should_attempt_now(day(1), FAILED_AT, attempts=0)


def test_a_late_worker_still_runs_the_due_attempt():
    """Catching up matters: the attempt is due, not expired."""
    assert should_attempt_now(day(2.9), FAILED_AT, attempts=0)


def test_no_retry_once_the_schedule_is_exhausted():
    assert not should_attempt_now(day(9), FAILED_AT, attempts=MAX_ATTEMPTS)


def test_no_retry_after_the_deadline_even_with_attempts_left():
    """A poller must not keep charging a card past the point of giving up — the deadline
    wins over the attempt count."""
    assert not should_attempt_now(day(GIVE_UP_AFTER_DAYS), FAILED_AT, attempts=1)
    assert not should_attempt_now(day(GIVE_UP_AFTER_DAYS + 5), FAILED_AT, attempts=0)


# --- when to stop --------------------------------------------------------------


def test_running_out_of_retries_does_NOT_cancel():
    """Exhausting retries means stop charging the card, not cancel the subscription.

    With retries at 1/4/7/10/13 and a 15-day window, cancelling on exhaustion would end
    access on day 13 while the past-due grace still promised 15 — and would remove the
    customer's remaining chance to pay in the portal and recover."""
    assert not should_give_up(day(7.1), FAILED_AT)
    assert not should_attempt_now(day(7.1), FAILED_AT, attempts=MAX_ATTEMPTS)


def test_give_up_at_the_deadline_however_many_attempts_were_made():
    """The deadline is the only thing that cancels — whether every retry ran or a worker
    outage meant none did."""
    assert should_give_up(day(GIVE_UP_AFTER_DAYS), FAILED_AT)
    assert should_give_up(day(GIVE_UP_AFTER_DAYS + 5), FAILED_AT)


def test_do_not_give_up_while_the_schedule_is_still_running():
    assert not should_give_up(day(4), FAILED_AT)


def test_access_and_collection_end_on_the_same_day():
    """The whole point of the deadline being the sole cancel trigger: there is no window
    where one has stopped and the other has not."""
    assert give_up_at(FAILED_AT) == FAILED_AT + timedelta(days=PAST_DUE_GRACE_DAYS)


def test_give_up_lands_on_the_grace_boundary():
    """Collection and access end together — no window where one has stopped and the
    other has not."""
    assert give_up_at(FAILED_AT) == FAILED_AT + timedelta(days=GIVE_UP_AFTER_DAYS)
    assert give_up_at(FAILED_AT) <= FAILED_AT + timedelta(days=PAST_DUE_GRACE_DAYS)


# --- the clamp: collection can never outlive access ------------------------------
#
# The window is one number, but the two halves of going past due count it from different
# instants: access from ``paid_through``, collection from ``dunning_started_at``. Those
# coincide only if the renewal ran the moment the period ended, and it does not have to —
# ``due_renewals`` picks up anyone whose paid_through has passed, so an outage, a paused
# cron or a batch limit pushes the first failure days later. Passing ``access_ends_at``
# caps the deadline so the drift cannot turn into charging a locked-out customer.


def test_the_deadline_is_unchanged_when_the_renewal_ran_on_time():
    """The overwhelmingly common case: the clamp must be a no-op, not a shortening."""
    on_time = FAILED_AT + timedelta(days=GIVE_UP_AFTER_DAYS)
    assert give_up_at(FAILED_AT, GIVE_UP_AFTER_DAYS, on_time) == on_time


def test_a_late_renewal_cannot_push_collection_past_access():
    """Period ended 1 Mar, worker down a week, so dunning starts on the 8th. Unclamped it
    would retry to the 23rd while access ended on the 16th — a week of charging a card
    for a customer who is locked out."""
    period_end = datetime(2027, 3, 1, 13, tzinfo=UTC)
    started = datetime(2027, 3, 8, 13, tzinfo=UTC)          # a week late
    access_ends = period_end + timedelta(days=GIVE_UP_AFTER_DAYS)   # 16 Mar

    assert give_up_at(started, GIVE_UP_AFTER_DAYS) == datetime(2027, 3, 23, 13, tzinfo=UTC)
    assert give_up_at(started, GIVE_UP_AFTER_DAYS, access_ends) == access_ends


def test_retries_stop_when_access_does():
    """``should_attempt_now`` gates on ``give_up_at``, so clamping the deadline stops the
    schedule too — no separate check, and no way for the two to disagree."""
    access_ends = day(5)

    # Offset 4 is due and access is still live, so it fires.
    assert should_attempt_now(day(4), FAILED_AT, attempts=1, access_ends_at=access_ends)
    # Offset 7 is still "due", but access has gone, so it must not fire.
    assert not should_attempt_now(
        day(7), FAILED_AT, attempts=2, access_ends_at=access_ends
    )


def test_a_payer_whose_access_already_lapsed_is_given_up_on_immediately():
    """The worst version of the drift: the worker was down longer than the whole window,
    so dunning starts after access has already ended. There is nothing left to protect
    and no reason to touch the card."""
    access_ends = FAILED_AT - timedelta(days=3)     # lapsed before dunning even began

    assert should_give_up(FAILED_AT, FAILED_AT, GIVE_UP_AFTER_DAYS, access_ends)
    assert not should_attempt_now(
        FAILED_AT, FAILED_AT, attempts=0, access_ends_at=access_ends
    )


def test_the_clamp_never_extends_a_deadline():
    """One-directional by construction. Access outlasting the window — a module riding a
    paid cancellation extension — must not buy extra days of card retries."""
    generous = FAILED_AT + timedelta(days=90)
    assert give_up_at(FAILED_AT, GIVE_UP_AFTER_DAYS, generous) == day(GIVE_UP_AFTER_DAYS)


def test_the_runner_gives_up_on_a_payer_whose_access_has_lapsed(monkeypatch):
    """End to end through ``collect_due``: paid_through is a month back, so whatever the
    dunning clock says, this payer lost access weeks ago and is closed out rather than
    retried."""
    lapsed = _Account(
        started=day(0), attempts=0,
        paid_through=FAILED_AT - timedelta(days=30), anchor=ANCHOR,
    )
    dunning, calls = _wire_runner(monkeypatch, account=lapsed, invoices=[RENEWAL_INV])

    dunning.collect_due(day(1))

    assert calls["ended"] == [("u1", "closed")]
    assert calls["retried"] == []        # the card is never touched
    assert calls["paid_through"] == []


def test_attempts_remaining_never_goes_negative():
    """It is shown to customers ("2 more attempts"), so an over-count must not produce
    a nonsense message."""
    assert attempts_remaining(0) == MAX_ATTEMPTS
    assert attempts_remaining(MAX_ATTEMPTS) == 0
    assert attempts_remaining(MAX_ATTEMPTS + 3) == 0


# --- the runner ------------------------------------------------------------------
#
# Everything above is pure policy. collect_due is the part that ACTS on it, and it had
# no tests: both bugs below were found by running it against Stripe on a test clock.


class _Account:
    """The payer's account: the anchor and the Stripe customer.

    The retry clock and the paid-through belong to the CARD now — ``_wire_runner`` builds
    a ``_Group`` from these same values, so an account with one card (which is every
    account the backfill produced) reads exactly as it did.
    """

    def __init__(self, *, started, attempts=0, paid_through=None, anchor=None):
        self.user_id = "u1"
        self.stripe_customer_id = "cus_1"
        self.dunning_started_at = started
        self.dunning_attempts = attempts
        self.paid_through = paid_through
        self.anchor_at = anchor


class _Group:
    """One card of a payer, carrying that card's cycle and its collection state."""

    def __init__(self, account, id="g1", card="pm_1"):
        self.id = id
        self.payer_user_id = account.user_id
        self.stripe_payment_method_id = card
        self.paid_through = account.paid_through
        self.dunning_started_at = account.dunning_started_at
        self.dunning_attempts = account.dunning_attempts


ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
# Deliberately the SAME instant as FAILED_AT, and it has to stay that way.
#
# ``renewals.due_renewals`` picks a payer up as soon as ``paid_through`` has passed, so
# in production the renewal fails at (or within a run of) the period end — the two dates
# are together. They used to be a month apart here, which quietly described a worker that
# had been down for a month, and these tests are about what a RECOVERY does to the cycle,
# not about timing.
#
# It matters now because ``collect_due`` clamps the give-up deadline to
# ``paid_through + window``: a payer whose access ended weeks ago is given up on
# immediately rather than retried, which is the whole point of the clamp. Pull these
# apart again and every test below stops exercising the path it names.
PAID_TO = FAILED_AT
# The GROUP is in the key: one card, one invoice per period, and a payer with two
# cards has two of them for the same month.
RENEWAL_INV = {"id": "in_r", "metadata": {"renewal_key": "renewal-u1-20270308-g1"}}
# next_period(ANCHOR, PAID_TO) — what a recovered payment buys.
NEXT_PERIOD_END = datetime(2027, 4, 8, 13, tzinfo=UTC)


def _wire_runner(monkeypatch, *, account, invoices=None, paid=True, group=None):
    billing_gateway = pytest.importorskip("billing.services.billing_gateway")  # slice B
    from billing.services import dunning, policy, store

    calls = {"paid_through": [], "ended": [], "attempts": 0, "retried": []}
    group = group if group is not None else _Group(account)
    # The runner reads the live policy; these tests are about the runner, not the table.
    # Pinning it to the shipped defaults also keeps them out of an app context.
    monkeypatch.setattr(policy, "current", lambda: policy.DEFAULTS)
    monkeypatch.setattr(store, "groups_in_dunning", lambda: [group])
    monkeypatch.setattr(store, "billing_groups_for_payer", lambda uid: [group])
    monkeypatch.setattr(store, "billing_group", lambda gid: group)
    monkeypatch.setattr(store, "customer_mapping_for_user", lambda uid: account)
    # No local invoice rows behind these fakes: nothing to re-read when nothing is open,
    # and no stranded draft for the period (``dunning._nothing_open_but_owed``).
    monkeypatch.setattr(store, "open_invoices_for_group", lambda gid: [])
    monkeypatch.setattr(store, "invoice_for_key", lambda key: None)
    # Recorded under the PAYER, so the assertions below read the same whether the cycle
    # lives on the account or on its one card.
    monkeypatch.setattr(
        store, "set_group_paid_through",
        lambda gid, until: calls["paid_through"].append((account.user_id, until)),
    )
    monkeypatch.setattr(
        store, "end_group_dunning",
        lambda gid, *, status="active": calls["ended"].append(
            (account.user_id, status)
        ),
    )
    monkeypatch.setattr(
        store, "record_group_dunning_attempt",
        lambda gid: calls.__setitem__("attempts", calls["attempts"] + 1),
    )
    monkeypatch.setattr(
        billing_gateway, "open_invoices",
        lambda cid: [] if invoices is None else invoices,
    )
    monkeypatch.setattr(
        billing_gateway, "retry_invoice",
        lambda iid, pm=None: calls["retried"].append(iid)
        or (paid, None if paid else "declined"),
    )
    return dunning, calls


def test_a_recovered_payment_advances_what_the_payer_is_PAID_THROUGH(monkeypatch):
    """Otherwise the card is charged and the customer stays unentitled until the next
    monthly run adopts the invoice — they have paid for a month of nothing."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])

    dunning.collect_due(day(1))

    assert calls["paid_through"] == [("u1", NEXT_PERIOD_END)]
    assert calls["ended"] == [("u1", "active")]


def test_the_cycle_is_advanced_BEFORE_dunning_is_cleared(monkeypatch):
    """If the process dies between the two, the safe half-state is 'entitled but still
    in dunning' — a spare retry — not 'paid but locked out'."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])
    order: list = []
    from billing.services import store

    monkeypatch.setattr(
        store, "set_group_paid_through", lambda g, t: order.append("paid")
    )
    monkeypatch.setattr(
        store, "end_group_dunning",
        lambda g, *, status="active": order.append("ended"),
    )

    dunning.collect_due(day(1))

    assert order == ["paid", "ended"]


def test_settling_a_CHANGE_invoice_does_not_advance_the_cycle(monkeypatch):
    """Dunning chases the oldest open invoice, which may be a mid-period upgrade. Those
    cover no period, so advancing off one hands the customer a free month."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    change = {"id": "in_c", "metadata": {"change_key": "change-e1-x"}}
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[change])

    dunning.collect_due(day(1))

    assert calls["paid_through"] == []
    assert calls["ended"] == [("u1", "active")]     # still recovered


def test_a_debt_settled_OUTSIDE_the_app_is_left_for_the_renewal_run_to_adopt(monkeypatch):
    """Nothing is left to read, so guessing risks the free month. Doing nothing
    self-heals: the next renewal run finds the paid invoice by its key."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=None)

    dunning.collect_due(day(1))

    assert calls["paid_through"] == []
    assert calls["ended"] == [("u1", "active")]


class _InvoiceRow:
    """A local ``subscription_invoice`` row, for ``dunning._nothing_open_but_owed``."""

    def __init__(self, external_id, status, key="renewal-u1-20270308-g1"):
        self.id = f"row_{external_id}"
        self.external_id = external_id
        self.status = status
        self.idempotency_key = key


def _stranded_period(monkeypatch, row, now_says):
    """The card's current-period invoice is ``row``, and re-reading it answers ``now_says``."""
    from billing.services import billing_gateway, store

    asked = {"keys": [], "reread": []}
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda key: asked["keys"].append(key) or row)
    monkeypatch.setattr(billing_gateway, "refresh_record",
                        lambda record: asked["reread"].append(record) or now_says)
    return asked


def test_nothing_open_while_the_periods_invoice_is_a_stranded_draft_is_not_recovery(
    monkeypatch, caplog
):
    """A draft nobody finalized is not in the open list, so it looked exactly like a debt
    settled elsewhere: dunning ended as recovered, thanked the customer for a payment
    nobody made, and put the companies back on a period nobody paid for. It stays in
    dunning, says so at ERROR, and the renewal pass finishes the draft."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=None)
    asked = _stranded_period(monkeypatch, _InvoiceRow("in_draft", "draft"), "draft")

    result = dunning.collect_due(day(1))

    assert asked["keys"] == ["renewal-u1-20270308-g1"]     # the period it is behind on
    assert calls["ended"] == []
    assert result["recovered"] == []
    assert calls["attempts"] == 0
    assert any(r.levelname == "ERROR" and "STRANDED DRAFT in_draft" in r.getMessage()
               for r in caplog.records)


def test_a_draft_row_the_processor_says_was_paid_is_recovery_as_before(monkeypatch):
    """"Draft" is only what the row said when its process stopped. Paid is paid."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=None)
    _stranded_period(monkeypatch, _InvoiceRow("in_left", "draft"), "paid")

    result = dunning.collect_due(day(1))

    assert calls["ended"] == [("u1", "active")]
    assert len(result["recovered"]) == 1


def test_nothing_open_re_reads_the_cards_rows_still_saying_open(monkeypatch):
    """Settled somewhere this code cannot see, each such row went on showing as failed with
    Retry payment on it - there is no webhook to tell it otherwise."""
    from billing.services import billing_gateway, store

    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=None)
    stale = _InvoiceRow("in_stale", "open")
    monkeypatch.setattr(store, "open_invoices_for_group", lambda gid: [stale])
    reread = []
    monkeypatch.setattr(billing_gateway, "refresh_record",
                        lambda record: reread.append(record) or "paid")

    dunning.collect_due(day(1))

    assert reread == [stale]
    assert calls["ended"] == [("u1", "active")]


def test_a_failed_retry_advances_nothing(monkeypatch):
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(
        monkeypatch, account=account, invoices=[RENEWAL_INV], paid=False
    )

    dunning.collect_due(day(1))

    assert calls["paid_through"] == []
    assert calls["ended"] == []          # stays in dunning


# --- Collecting is not recovering ----------------------------------------------------
#
# Every give-up leaves its unpaid renewal open on purpose, so a payer who lapses and later
# comes back carries a landmine: an old invoice that is not the period they are behind on.
# Dunning charges the OLDEST, which is right for collections and wrong for entitlement —
# the account is only recovered when the CURRENT period is settled.

# A renewal for the period BEFORE the one the cycle is on — an abandoned give-up bill.
STALE_INV = {"id": "in_stale", "metadata": {"renewal_key": "renewal-u1-20270208-g1"}}


def test_paying_a_STALE_renewal_collects_without_recovering(monkeypatch):
    """The bug this guards: some invoice was paid, so the episode ended and access came
    back — while the period the customer is actually behind on stayed unpaid. They were
    told they were settled and were locked out again by the next access sweep.

    Both are open, which is the real shape of it: the give-up left ``in_stale`` behind,
    a reinstatement moved the cycle on, and the renewal that failed raised ``in_r``.
    """
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(
        monkeypatch, account=account, invoices=[STALE_INV, RENEWAL_INV]
    )

    result = dunning.collect_due(day(1))

    assert calls["retried"] == ["in_stale"]      # policy still chases the oldest debt
    assert calls["paid_through"] == []           # it buys no period
    assert calls["ended"] == []                  # and it does not end the episode
    assert [e["user_id"] for e in result["collected"]] == ["u1"]
    assert result["recovered"] == []


def test_an_UNCOLLECTABLE_stale_debt_cannot_lock_a_settled_payer_out(monkeypatch):
    """The rule is "the current period is settled", NOT "nothing is open" — and the
    difference is the whole design. Here the only thing outstanding is an abandoned bill
    from an earlier give-up: the period being dunned is not among the open invoices, so it
    was paid somewhere this code cannot see. Requiring an empty list would keep this payer
    past due forever over a debt nobody is collecting."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[STALE_INV])

    result = dunning.collect_due(day(1))

    assert calls["ended"] == [("u1", "active")]
    assert [e["user_id"] for e in result["recovered"]] == ["u1"]
    # Still buys no period: the invoice that paid belongs to one that ended long ago.
    assert calls["paid_through"] == []


def test_a_collected_payer_is_not_emailed_that_the_retry_FAILED(monkeypatch):
    """Their card was just debited. "Your payment failed" is false, and "you're all
    settled" is false too — so neither is sent, and the past-due state keeps saying what
    is still owed."""
    notify = pytest.importorskip("billing.services.notify")  # slice B

    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, _ = _wire_runner(
        monkeypatch, account=account, invoices=[STALE_INV, RENEWAL_INV]
    )
    sent: list = []
    monkeypatch.setattr(notify, "notify_many", lambda events: sent.extend(events))

    dunning.collect_due(day(1))

    assert [event for event in sent if event[1] == notify.DUNNING_RETRY_FAILED] == []
    assert [event for event in sent if event[1] == notify.PAYMENT_RECOVERED] == []


def test_a_stale_renewal_never_advances_the_CURRENT_period(monkeypatch):
    """``_settle_period`` gated on "is this a renewal", not on WHICH period's. So a May
    bill settling while the cycle sat in August advanced August — a free month bought
    with a payment for a period that had already ended."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[STALE_INV])

    dunning.collect_due(day(1))

    assert calls["paid_through"] == []


def test_giving_up_does_not_advance_the_cycle(monkeypatch):
    """The debt is real and unpaid. Advancing would record them as entitled to a period
    nobody paid for."""
    account = _Account(started=day(0), attempts=4, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])

    dunning.collect_due(day(GIVE_UP_AFTER_DAYS + 1))

    assert calls["paid_through"] == []
    assert calls["ended"] == [("u1", "closed")]


# --- a replay's scoped keys (the dev database's lived data) ----------------------------


def test_a_key_names_its_period_exactly_or_with_a_replays_scope_suffix():
    """A replay issues ``<key>-<suffix>`` so same-day runs do not collide at Stripe, and its
    data stays behind for the live engine, whose keys are plain. The same period - never
    another one."""
    from billing.services import dunning

    key = "renewal-u1-20270308-g1"
    assert dunning._names_period(key, key)
    assert dunning._names_period(f"{key}-ENwIm8kHRqpJ", key)
    # Another period, another card, the payer-wide key of old, nothing at all: never.
    assert not dunning._names_period("renewal-u1-20270208-g1-ENwIm8kHRqpJ", key)
    assert not dunning._names_period("renewal-u1-20270308-g2", key)
    assert not dunning._names_period("renewal-u1-20270308", key)
    assert not dunning._names_period(None, key)
    assert not dunning._names_period(key, None)


def test_a_replay_scoped_renewal_recovers_and_advances_the_cycle(monkeypatch):
    """The dev database's past-due payers were lived by the replay harness: their declined
    renewal is this period's, scoped. Collected, it settles the period like any other."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    scoped = {"id": "in_s", "metadata": {"renewal_key": "renewal-u1-20270308-g1-ENwIm8kHRqpJ"}}
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[scoped])

    dunning.collect_due(day(1))

    assert calls["paid_through"] == [("u1", NEXT_PERIOD_END)]
    assert calls["ended"] == [("u1", "active")]


def test_a_replay_scoped_renewal_for_another_period_is_still_stale(monkeypatch):
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    old = {"id": "in_o", "metadata": {"renewal_key": "renewal-u1-20261208-g1-ENwIm8kHRqpJ"}}
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[old])

    dunning.collect_due(day(1))

    assert calls["paid_through"] == []


# --- an invoice the processor will no longer collect (``dunning._charge``) --------------------
#
# Stripe cancels an invoice's payment once it has been confirmed too many times (ten declines
# in our account), and no retry can ever succeed after that. ``_charge`` re-issues the invoice
# and charges the replacement - inside the same attempt, so the slot is counted once.

REPLACEMENT = {"id": "in_r2", "metadata": {**RENEWAL_INV["metadata"], "replaces": "in_r"}}


def _dead_renewal(monkeypatch, *, pays=True, replacement=REPLACEMENT, refresh_raises=None):
    """``in_r`` can no longer be paid; re-issuing it yields ``replacement``, which ``pays``."""
    from billing.services import billing_gateway

    seen = {"retried": [], "refreshed": []}

    def _retry(invoice_id, payment_method=None):
        seen["retried"].append(invoice_id)
        if invoice_id == RENEWAL_INV["id"]:
            return False, billing_gateway.DEAD_PAYMENT
        return (True, None) if pays else (False, "Your card was declined.")

    def _refresh(invoice_id, payment_method=None):
        seen["refreshed"].append(invoice_id)
        if refresh_raises is not None:
            raise refresh_raises
        return replacement

    monkeypatch.setattr(billing_gateway, "retry_invoice", _retry)
    monkeypatch.setattr(billing_gateway, "refresh_invoice", _refresh)
    return seen


def _marker_free(result) -> bool:
    """The gateway's DEAD_PAYMENT marker is for ``_charge`` alone - never in what it reports."""
    from billing.services import billing_gateway

    return all(
        billing_gateway.DEAD_PAYMENT not in map(str, entry.values())
        for entries in result.values() for entry in entries
    )


def test_a_dead_invoice_is_refreshed_and_the_replacement_RECOVERS_the_period(monkeypatch):
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])
    seen = _dead_renewal(monkeypatch)

    result = dunning.collect_due(day(1))

    assert seen == {"retried": ["in_r", "in_r2"], "refreshed": ["in_r"]}
    # The replacement carries the renewal key, so paying it advances the cycle and ends it.
    assert calls["paid_through"] == [("u1", NEXT_PERIOD_END)]
    assert calls["ended"] == [("u1", "active")]
    assert result["recovered"][0]["invoice"] == "in_r2"
    assert result["retried"][0]["refreshed"] == "in_r"


def test_the_refresh_and_the_replacement_share_ONE_attempt(monkeypatch):
    """Counted once, before anything runs - the card is hit once."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])
    _dead_renewal(monkeypatch)

    dunning.collect_due(day(1))

    assert calls["attempts"] == 1


def test_a_declining_replacement_reports_the_processors_words_never_the_marker(monkeypatch):
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])
    _dead_renewal(monkeypatch, pays=False)

    result = dunning.collect_due(day(1))

    entry = result["retried"][0]
    assert (entry["invoice"], entry["reason"]) == ("in_r2", "Your card was declined.")
    assert (calls["ended"], calls["paid_through"]) == ([], [])
    assert _marker_free(result)


def test_an_invoice_that_cannot_be_refreshed_is_a_plain_failed_attempt(monkeypatch):
    """Nothing to charge and no replacement: a failed attempt with no reason to give (the
    gateway has logged which invoice needs a person), not a decline and not a crash."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])
    _dead_renewal(monkeypatch, replacement=None)

    result = dunning.collect_due(day(1))

    entry = result["retried"][0]
    assert (entry["invoice"], entry["reason"], "refreshed" in entry) == ("in_r", None, False)
    assert (calls["ended"], calls["attempts"]) == ([], 1)
    assert _marker_free(result)


def test_a_refresh_that_raises_is_contained_and_resumed_next_run(monkeypatch):
    """The processor unreachable half-way: this card's cycle fails quietly, the next run
    finishes the refresh and collects."""
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[RENEWAL_INV])
    _dead_renewal(monkeypatch, refresh_raises=RuntimeError("processor unreachable"))

    dunning.collect_due(day(1))
    assert (calls["ended"], calls["attempts"]) == ([], 1)

    _dead_renewal(monkeypatch)
    dunning.collect_due(day(2))
    assert calls["ended"] == [("u1", "active")]


def test_a_refreshed_STALE_invoice_is_collected_not_recovered(monkeypatch):
    """Dunning chases the oldest debt, and a dead one is re-issued like any other - but
    paying an abandoned period's bill still does not recover the current period."""
    from billing.services import billing_gateway

    stale = {"id": "in_s", "metadata": {"renewal_key": "renewal-u1-20270208-g1"}}
    account = _Account(started=day(0), attempts=0, paid_through=PAID_TO, anchor=ANCHOR)
    dunning, calls = _wire_runner(monkeypatch, account=account, invoices=[stale, RENEWAL_INV])
    monkeypatch.setattr(
        billing_gateway, "retry_invoice",
        lambda iid, pm=None: (False, billing_gateway.DEAD_PAYMENT) if iid == "in_s"
        else (True, None),
    )
    monkeypatch.setattr(
        billing_gateway, "refresh_invoice",
        lambda iid, pm=None: {"id": "in_s2",
                              "metadata": {**stale["metadata"], "replaces": "in_s"}},
    )

    result = dunning.collect_due(day(1))

    assert result["collected"][0]["invoice"] == "in_s2"
    assert (result["recovered"], calls["ended"], calls["paid_through"]) == ([], [], [])
