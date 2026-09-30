"""Unit tests for module-based cancel / reactivate.

Cancellation is per MODULE. The payer has ONE billing cycle however many entities and
modules they own, so cancelling one of two modules re-prices what the entity bills
rather than cancelling anything.

Covers the rules that are easy to get wrong:
* the extension is priced against what is LEAVING, not against the line being kept: a
  module going on its own bills its standalone 280, and a pair going together bills the
  400 bundle between them (280 to the first code by sort order, the 120 step to the
  second) — never 120 each;
* cancelling RECORDS the extension amount on the row for the next renewal run to
  collect — it never charges up-front, so it never depends on a card and never aborts;
* undoing a cancellation before that run deletes a number (no money moved); undoing it
  AFTER charges the window the extension stopped covering, because the renewal already
  skipped the module for the rest of the period;
* a cancelled TRIAL keeps its free days — cancelling one only means "don't convert".

NOTE: imports are done INSIDE each test — the conftest ``app`` fixture clears and
re-imports project modules mid-session, so importing at call time keeps references
mutually consistent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# The price catalog is patched BY DOTTED PATH, not via an imported reference:
# conftest re-imports project modules mid-session, so a module object captured at
# import time is not the one the code under test ends up calling.
_CATALOG = "billing.services.catalog"


class _FakeEntity:
    id = "e1"
    name = "Acme"


class _FakeUser:
    id = "u1"
    email = "u1@example.com"


_UNSET = object()
_BILLED_AT = datetime(2026, 12, 8, 13, tzinfo=UTC)


class _Row:
    """Stand-in for an entity_module_subscription row.

    Carries a SUBSET of the real model's columns — but never anything the model lacks.
    A fake that defined a column the model didn't have is precisely why a 500 on every
    reactivate_module went unnoticed, so only add a field here once the model really
    has it.

    ``billed`` is what separates a cancelled PAID module from a cancelled TRIAL. It
    used to be read off the Stripe subscription item id, which only the Stripe biller
    ever set — so in-house every paid module read as a trial: cancelled ones were
    expired by the trial-end job, and un-cancelling one was refused with "your free
    trial has ended". It is ``first_billed_at`` now.
    """

    def __init__(self, code="PETTY_CASH", *, phase="active", ext_state=None,
                 app_access_until=None, trial_end=None, billed=True,
                 first_billed_at=_UNSET, ext_amount=None):
        self.entity_id = "e1"
        self.function_code = code
        self.payer_user_id = "u1"
        self.phase = phase
        self.extension_state = ext_state
        # What a cancellation recorded, and what re-pricing rewrites when the set of
        # modules leaving together changes.
        self.extension_amount = ext_amount
        self.app_access_until = app_access_until
        # An APP-LEVEL trial has a trial_end bounding its free days; together with
        # first_billed_at these are how the cancel/reactivate paths tell a trial from a
        # paid module.
        self.trial_end = trial_end
        self.first_billed_at = (
            (_BILLED_AT if billed else None)
            if first_billed_at is _UNSET
            else first_billed_at
        )


_FN = {"PETTY_CASH": "fn_pc", "PAYMENT_REQUEST": "fn_bill"}


def _plan(code, amount=28000):
    from billing.services import catalog

    return catalog.PlanView(_FN[code], code, code.title().replace("_", " "), amount,
                            "HKD", "month", 1, True)


def _bundle():
    from billing.services import catalog

    return catalog.BundlePlanView(
        function_codes=("PETTY_CASH", "PAYMENT_REQUEST"),
        display_name="Super Minty",
        amount=40000,
        currency_code="HKD",
        billing_interval="month",
        billing_interval_count=1,
    )


class _Plan:
    def __init__(self, amount):
        self.amount = amount
        self.display_name = "plan"
        self.currency = "HKD"


def _wire(monkeypatch, *, rows, paid_through=None, now=None):
    """Mock the store + billing seams; return (checkout, calls).

    ``rows`` is the entity's module rows; ``rows[0]`` is what ``store.module_row``
    returns (pass ``[]`` for "this module isn't billed"). ``now`` pins the trusted
    clock; leave it None for the trial cases, which build their dates off the real one.

    Nothing here mocks a Stripe SUBSCRIPTION: there is no longer one, and checkout no
    longer imports a single function that would create or edit one. ``charged`` is the
    only way money moves, so a test asserting it stayed empty is asserting that a path
    billed nothing.
    """
    from billing.services import changes, checkout, store
    from billing.services import clock as clock_mod

    if now is not None:
        monkeypatch.setattr(clock_mod, "now", lambda: now)

    calls = {"charged": [], "rows": [], "audit": [], "created": []}

    monkeypatch.setattr(store, "module_row",
                        lambda eid, code: rows[0] if rows else None)
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: rows)
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_1")
    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: "cus_1")
    # An entity being managed here is already billing, so it has consent. The gate
    # itself is covered in test_subscription_checkout / test_subscription_trials.
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: True)
    monkeypatch.setattr(store, "record_billing_consent", lambda eid, uid, source: None)
    monkeypatch.setattr(
        store, "billing_cycle_for_user",
        lambda uid: (datetime(2027, 1, 8, 13, tzinfo=UTC), "HKD"),
    )
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        lambda codes: _Plan(40000 if len(set(codes)) > 1 else 28000),
    )
    monkeypatch.setattr(
        store, "upsert_module_row",
        lambda e, code, payer, **f: calls["rows"].append((code, f)) or _Row(code),
    )
    monkeypatch.setattr(store, "record_action", lambda **kw: calls["audit"].append(kw))

    monkeypatch.setattr(f"{_CATALOG}.plan_for_module", lambda code: _plan(code.upper()))
    monkeypatch.setattr(f"{_CATALOG}.bundle_plan", lambda: _bundle())

    monkeypatch.setattr(checkout, "customer_default_payment_method", lambda cid: "pm_1")
    monkeypatch.setattr(
        changes, "issue_change",
        lambda cid, eid, nm, before, after, period, at: calls["charged"].append(
            {"before": set(before), "after": set(after), "at": at}
        ) or {"id": "in_1", "status": "paid"},
    )
    monkeypatch.setattr(
        checkout, "_create_paid_subscriptions",
        lambda entity, user, cid, plans, pm, key: calls["created"].append(
            [p.function_code for p in plans]
        ) or [],
    )
    return checkout, calls


# --- cancelling a paid module ------------------------------------------------


def test_in_house_cancel_records_the_extension_instead_of_charging_it(monkeypatch):
    """Cancelling must never depend on a card clearing — an expired card cannot be
    allowed to trap somebody in a subscription. The amount is written to the row and
    collected by the next renewal run.

    Petty Cash is the only module leaving, so it is priced on its own: 280 x 11 of 31
    days = 99.35."""
    rows = [_Row("PETTY_CASH"), _Row("PAYMENT_REQUEST")]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    result = checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert result["access_end"] == datetime(2027, 2, 19, 13, tzinfo=UTC)  # now + 30d
    assert result["extension_state"] == "pending"
    written = dict(calls["rows"])["PETTY_CASH"]
    assert written["phase"] == "scheduled_cancel"
    # 28000 x 11/31: it is the only module leaving, so it is priced on its own.
    assert written["extension_amount"] == 9935
    assert written["extension_state"] == "pending"
    # Recorded, not charged: nothing was billed on the way out.
    assert calls["charged"] == []


def test_a_cancellation_records_the_reason_in_the_audit_log(monkeypatch):
    """The dialog asks why, so the answer has to survive the click.

    It goes to the LOG, not the module row: why someone left is history, and nothing
    reads it to decide access or billing. A module can be cancelled, renewed and
    cancelled again, and a column on the row would keep only the second reason —
    silently erasing the first, which is the one that explains the pattern.

    It is also its own column rather than text appended to ``note``: ``note`` is what
    WE write about the action, and mixing the two makes "why do people leave?" a string
    search instead of a query.
    """
    rows = [_Row("PETTY_CASH", phase="active"), _Row("PAYMENT_REQUEST")]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(
        _FakeEntity(), _FakeUser(), "PETTY_CASH", reason="  too expensive  "
    )

    assert calls["audit"][0]["cancel_reason"] == "too expensive"
    # The row itself carries no reason — it is not state.
    assert "cancel_reason" not in dict(calls["rows"])["PETTY_CASH"]


def test_a_cancellation_with_no_reason_stores_NULL_not_an_empty_string(monkeypatch):
    """Nobody has to justify leaving, so "didn't say" is a normal outcome — and it must
    read as absent rather than as a blank answer. An empty string in the column is
    indistinguishable from someone who typed spaces and meant nothing by it."""
    rows = [_Row("PETTY_CASH", phase="active"), _Row("PAYMENT_REQUEST")]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH", reason="   ")

    assert calls["audit"][0]["cancel_reason"] is None
    # And the note keeps its own sentence, unpolluted by an empty reason.
    assert calls["audit"][0]["note"] == (
        "cancelled in-house; extension recorded for the next invoice"
    )


def test_a_reason_longer_than_the_column_is_trimmed_not_rejected(monkeypatch):
    """Refusing to cancel a subscription because the explanation ran long would be
    absurd — the reason is a courtesy on the way out, not a validated field."""
    rows = [_Row("PETTY_CASH", phase="active"), _Row("PAYMENT_REQUEST")]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH", reason="x" * 900)

    assert len(calls["audit"][0]["cancel_reason"]) == 500


def test_a_paid_cancellation_logs_the_phase_it_came_FROM(monkeypatch):
    """The audit log answers "why was I charged?", so the state money moved from is the
    one entry that cannot be missing.

    It was: the paid path passed ``phase_after`` and omitted ``phase_before``, logging
    "NULL -> scheduled_cancel" while the trial path logged "trial -> scheduled_cancel".
    The state was absent on exactly the cancellations that produced a charge.

    Reading it off the row at the audit call is not enough either — ``upsert_module_row``
    writes through the same object, so by then the row already says scheduled_cancel.
    """
    rows = [_Row("PETTY_CASH", phase="active"), _Row("PAYMENT_REQUEST")]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    entry = calls["audit"][0]
    assert entry["phase_before"] == "active"
    assert entry["phase_after"] == "scheduled_cancel"


def test_a_module_leaving_ALONE_is_priced_on_its_own(monkeypatch):
    """The extension is priced against what is LEAVING, not against the line being kept.

    Petty Cash is the only module going, so nothing is leaving with it to share the line:
    it is charged its own 280. The 120 margin is what it was worth to a subscription that
    KEPT Payment Request, and that subscription is not what these extra days are.
    """
    rows = [_Row("PETTY_CASH"), _Row("PAYMENT_REQUEST")]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    written = dict(calls["rows"])["PETTY_CASH"]
    # 28000 x 11/31, not the 12000 margin (which would be 4258).
    assert written["extension_amount"] == 9935


def test_a_pair_leaving_together_splits_the_BUNDLE_by_code_order(monkeypatch):
    """Two modules leaving are worth the bundle BETWEEN them, not a margin each.

    They held Super Minty for those extra days, so the days are worth Super Minty. The
    400 is allocated in sorted code order — Payment Request first at its own 280, Petty
    Cash the 120 step on top — and the shares telescope back to the bundle. Pricing each
    one against the OTHER LEAVER gave 120 + 120 = 240, less than either module has ever
    cost, for days on which the customer had both.

    The share depends on ``sorted()``, never on which was clicked first: see
    ``test_the_price_does_not_depend_on_the_order_of_the_two_clicks``.
    """
    rows = [_Row("PAYMENT_REQUEST"), _Row("PETTY_CASH", phase="scheduled_cancel",
                               app_access_until=datetime(2027, 2, 19, 13, tzinfo=UTC))]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    written = dict(calls["rows"])["PAYMENT_REQUEST"]
    assert written["extension_amount"] == 9935, "BILL sorts first: 28000 x 11/31"


def test_the_price_does_not_depend_on_the_order_of_the_two_clicks(monkeypatch):
    """Cancel BILL then PETTY_CASH, or PETTY_CASH then BILL: the same two figures.

    The allocation is by ``sorted()`` precisely so that it cannot depend on the order the
    customer happened to click in. This is the invariant the old
    "second cancellation is worth the same as the first" test was really protecting: the
    two shares are no longer equal, but which module gets which is still fixed, and so is
    the total.
    """
    access_end = datetime(2027, 2, 19, 13, tzinfo=UTC)

    def _run(first, second):
        rows = {"PAYMENT_REQUEST": _Row("PAYMENT_REQUEST"), "PETTY_CASH": _Row("PETTY_CASH")}
        written: dict[str, dict] = {}
        for code in (first, second):
            # store.module_row hands back rows[0], so the module being cancelled leads.
            ordered = [rows[code]] + [r for c, r in rows.items() if c != code]
            checkout, calls = _wire(
                monkeypatch,
                rows=ordered,
                paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
                now=datetime(2027, 1, 20, 13, tzinfo=UTC),
            )
            monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

            checkout.cancel_module(_FakeEntity(), _FakeUser(), code)

            # Replay the commit onto the fixture: the SECOND cancellation reads this
            # state back off the rows to work out what is leaving with it.
            rows[code].phase = "scheduled_cancel"
            rows[code].app_access_until = access_end
            for c, fields in calls["rows"]:
                written[c] = {**written.get(c, {}), **fields}
                for attr in ("extension_amount", "extension_state"):
                    if attr in fields:
                        setattr(rows[c], attr, fields[attr])
        return written

    for written in (_run("PAYMENT_REQUEST", "PETTY_CASH"), _run("PETTY_CASH", "PAYMENT_REQUEST")):
        assert written["PAYMENT_REQUEST"]["extension_amount"] == 9935
        assert written["PETTY_CASH"]["extension_amount"] == 4258


def test_a_leaver_whose_access_already_ran_out_does_not_dilute_the_new_one(monkeypatch):
    """A module that stopped before the window even opens shares nothing.

    ``_leaving_codes`` counts every winding-down row, so a module cancelled last cycle is
    in the leaving SET even though its days are spent. The per-slice ``live`` sets are
    what actually price the window, and they are built from the access ends — so a leaver
    whose end is behind ``paid_through`` is never live, and Payment Request is charged its
    own 280 rather than the 120 step it would owe beside a real companion.
    """
    rows = [
        _Row("PAYMENT_REQUEST"),
        # Cancelled last cycle: access ran out before this period's anchor.
        _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="pending",
             ext_amount=9935,
             app_access_until=datetime(2027, 1, 15, 13, tzinfo=UTC)),
    ]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    written = dict(calls["rows"])["PAYMENT_REQUEST"]
    assert written["extension_amount"] == 9935, "28000 x 11/31, not the 12000 step"


def test_cancelling_the_second_module_RE_PRICES_the_first(monkeypatch):
    """What a cancellation costs depends on what else is leaving with it, and that is not
    settled when the customer clicks.

    Petty Cash left alone and was charged its own 280 (9935). Payment Request following it
    out makes them a pair again for those days, worth the 400 bundle between them — and
    Payment Request sorts FIRST, so it takes the 280 and Petty Cash is re-priced DOWN to
    the 120 step. The row already written has to be corrected either way, or the renewal
    collects a figure that was only ever true while Petty Cash was leaving by itself.
    """
    rows = [
        _Row("PAYMENT_REQUEST", phase="active"),
        _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="pending",
             ext_amount=9935,
             app_access_until=datetime(2027, 2, 19, 13, tzinfo=UTC)),
    ]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    written = dict(calls["rows"])
    assert written["PAYMENT_REQUEST"]["extension_amount"] == 9935
    assert written["PETTY_CASH"]["extension_amount"] == 4258, (
        "re-priced DOWN from 9935 — Payment Request sorts first and takes the 280"
    )
    # The property the whole rule exists for: the two shares are the bundle rate prorated,
    # to within the one minor unit two separate roundings cost (40000 x 11/31 = 14194).
    assert (
        written["PAYMENT_REQUEST"]["extension_amount"] + written["PETTY_CASH"]["extension_amount"]
        == 14193
    )


def test_uncancelling_one_module_RE_PRICES_the_one_still_leaving(monkeypatch):
    """The other direction. Payment Request comes back, so Petty Cash is leaving alone
    again and goes back to its own 280 — otherwise it keeps a pair's price for days it
    now spends by itself."""
    rows = [
        # The state a pair leaving together actually reaches: BILL sorts first and holds
        # the standalone 280, PETTY_CASH the 120 step. Two equal 4258s is not producible.
        _Row("PAYMENT_REQUEST", phase="scheduled_cancel", ext_state="pending", ext_amount=9935,
             app_access_until=datetime(2027, 2, 19, 13, tzinfo=UTC)),
        _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="pending",
             ext_amount=4258,
             app_access_until=datetime(2027, 2, 19, 13, tzinfo=UTC)),
    ]
    checkout, calls = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    written = dict(calls["rows"])
    assert written["PAYMENT_REQUEST"]["extension_amount"] is None, "discarded, never billed"
    assert written["PETTY_CASH"]["extension_amount"] == 9935, "alone again"


def test_a_module_already_cancelled_cannot_be_cancelled_again(monkeypatch):
    """Pricing counts a module that is winding down; cancelling must not — a second
    cancellation would record a second extension on the same row."""
    rows = [_Row("PETTY_CASH", phase="scheduled_cancel",
                 app_access_until=datetime(2027, 2, 19, 13, tzinfo=UTC))]
    checkout, _ = _wire(
        monkeypatch,
        rows=rows,
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 20, 13, tzinfo=UTC),
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")
    assert exc.value.status == 409


def test_cancel_rejects_a_module_that_isnt_billed(monkeypatch):
    checkout, calls = _wire(monkeypatch, rows=[])

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")
    assert exc.value.status == 409


# --- cancelling a trial ------------------------------------------------------


def _trial_row(code="PAYMENT_REQUEST", *, phase="trial", days=10):
    """An app-level trial row: never billed, free days still running."""
    from billing.services import clock

    return _Row(code, phase=phase, billed=False,
                trial_end=clock.now() + timedelta(days=days),
                app_access_until=clock.now() + timedelta(days=days))


def test_cancelling_a_trial_keeps_access_to_the_trial_end(monkeypatch):
    """Cancelling a free trial must NOT end it. It only means "don't convert to paid" —
    the user keeps the free days they were given, exactly as a cancelled paid module
    keeps the days it was charged for."""
    row = _trial_row()
    checkout, calls = _wire(monkeypatch, rows=[row])
    access: list = []
    monkeypatch.setattr(
        checkout, "_set_module_access", lambda e, c, on: access.append((c, on))
    )

    result = checkout.cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    # Access runs to the trial end, not to now.
    assert result["access_end"] == row.trial_end
    assert result["extension_state"] is None
    # Nothing billed — a free trial has nothing to extend.
    assert calls["charged"] == []
    # Scheduled, not expired, and access was NOT revoked.
    written = calls["rows"][-1][1]
    assert written["phase"] == "scheduled_cancel"
    assert written["app_access_until"] == row.trial_end
    assert access == []  # the module stays switched ON
    assert calls["audit"][-1]["phase_after"] == "scheduled_cancel"


def test_cancelling_an_already_ended_trial_expires_it(monkeypatch):
    """No free days left to keep — an overdue trial the end-job hasn't swept yet. There's
    nothing to preserve, so it just expires."""
    row = _trial_row(days=-1)  # trial_end in the past
    checkout, calls = _wire(monkeypatch, rows=[row])
    access: list = []
    monkeypatch.setattr(
        checkout, "_set_module_access", lambda e, c, on: access.append((c, on))
    )

    result = checkout.cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    assert result == {"access_end": None, "extension_state": None}
    assert calls["rows"][-1][1]["phase"] == "expired"
    assert access == [("PAYMENT_REQUEST", False)]  # access revoked
    assert calls["audit"][-1]["phase_after"] == "expired"


def test_uncancelling_a_trial_puts_it_back_in_the_trial_phase(monkeypatch):
    """Within the access period the user can change their mind. Nothing moved money on
    the way out, so there's no extension to reverse."""
    row = _trial_row(phase="scheduled_cancel")
    checkout, calls = _wire(monkeypatch, rows=[row])
    access: list = []
    monkeypatch.setattr(
        checkout, "_set_module_access", lambda e, c, on: access.append((c, on))
    )

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    written = calls["rows"][-1][1]
    assert written["phase"] == "trial"          # converts again at trial end
    assert written["app_access_until"] == row.trial_end
    # The TERM is fixed. A trial runs its full TRIAL_PERIOD_DAYS and converts at the
    # end — it cannot be shortened or extended, so a round trip through cancel/uncancel
    # must not rewrite trial_end. It is written once, when the trial starts.
    assert "trial_end" not in written
    assert access == [("PAYMENT_REQUEST", True)]
    assert calls["audit"][-1]["phase_after"] == "trial"
    # No reversal: nothing was ever charged for a free trial.
    assert calls["charged"] == []


def test_uncancelling_a_trial_after_it_ended_is_refused(monkeypatch):
    """The free days are gone; resuming would be a purchase, and Renew must not be a
    way to buy something."""
    row = _trial_row(phase="scheduled_cancel", days=-1)
    checkout, calls = _wire(monkeypatch, rows=[row])
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    assert exc.value.status == 409
    assert calls["rows"] == []  # nothing written


# --- undoing a cancellation --------------------------------------------------


class _Group:
    """The billing account (card) a reinstated company is charged on."""

    id = "grp_1"
    stripe_payment_method_id = "pm_company"


_GROUP = _Group()


def _wire_reinstate(monkeypatch, checkout, *, billed_codes, paid=True, group=_GROUP,
                    raises=None):
    """Capture what reinstating bills. ``raises`` is what the charge throws — the way a
    declined card really arrives: out of ``Invoice.pay``, with the invoice left open."""
    from billing.services import changes, store

    charged: list = []
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: set(billed_codes))
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_1")
    # The card this company is nominated onto. None = no nomination, and there is no
    # fallback to the account default.
    monkeypatch.setattr(store, "billing_group_for_entity", lambda eid, uid=None: group)

    def _issue(cid, eid, nm, before, after, period, at, **kw):
        charged.append(
            {"before": set(before), "after": set(after), "at": at, "period": period,
             "group": kw.get("group"), "attempts_of": kw.get("attempts_of")}
        )
        if raises is not None:
            raise raises
        return {"id": "in_r", "status": "paid" if paid else "open"}

    monkeypatch.setattr(changes, "issue_change", _issue)
    return charged


def _capture_voids(monkeypatch, *, fails=False):
    """What the gateway is asked to void. ``fails``: the void itself errors."""
    from billing.services import billing_gateway

    voided: list = []

    def _void(invoice_id):
        voided.append(invoice_id)
        if fails:
            raise RuntimeError("processor unavailable")

    monkeypatch.setattr(billing_gateway, "void_invoice", _void)
    return voided


def _reinstatable(monkeypatch):
    """A module cancelled after the renewal collected its extension — restoring it is a
    purchase of the uncovered rest of the period."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="invoiced",
               billed=True,
               app_access_until=datetime(2027, 1, 20, 13, tzinfo=UTC))
    row.extension_amount = 4258
    checkout, calls = _wire(
        monkeypatch,
        rows=[row],
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 25, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    return checkout, calls


def test_in_house_uncancel_discards_the_extension_before_it_is_billed(monkeypatch):
    """Nothing was ever charged — the extension was recorded for the next run to
    collect — so undoing it is deleting a number, not reversing a payment. No invoice
    item to remove, no credit note, no money moved either way."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="pending",
               billed=True)
    row.extension_amount = 4258
    checkout, calls = _wire(
        monkeypatch,
        rows=[row],
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 25, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    written = dict(calls["rows"])["PETTY_CASH"]
    assert written["phase"] == "active"
    assert written["extension_amount"] is None
    assert written["extension_state"] is None
    assert written["app_access_until"] is None
    assert calls["charged"] == []   # nothing reversed, nothing charged


def test_in_house_uncancel_after_billing_charges_the_uncovered_remainder(monkeypatch):
    """The extension paid up to app_access_until and the renewal skipped this module for
    the rest of the period, so that window is covered by nobody. Reinstating without
    charging it hands the customer the rest of the month."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="invoiced",
               billed=True,
               app_access_until=datetime(2027, 1, 20, 13, tzinfo=UTC))
    row.extension_amount = 4258
    checkout, calls = _wire(
        monkeypatch,
        rows=[row],
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 25, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    charged = _wire_reinstate(monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"})

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert len(charged) == 1
    # Priced as an upgrade from what the entity still bills, so the customer pays the
    # MARGINAL difference rather than Petty Cash's standalone price.
    assert charged[0]["before"] == {"PAYMENT_REQUEST"}
    assert charged[0]["after"] == {"PAYMENT_REQUEST", "PETTY_CASH"}
    # Prorated from where the extension stopped covering, not from "now".
    assert charged[0]["at"] == datetime(2027, 1, 20, 13, tzinfo=UTC)
    # Charged on the card THIS company is nominated onto, never the account default.
    assert charged[0]["group"] is _GROUP

    code, fields = calls["rows"][-1]
    assert (code, fields["phase"]) == ("PETTY_CASH", "active")
    # The extension is NOT reversed: those days were used, at the rate they'd have cost.
    assert "extension_state" not in fields


def test_a_declined_reinstatement_does_not_give_the_module_back(monkeypatch):
    """Reinstating is a purchase. Charging at month end instead would let the customer
    take the module and walk away before the invoice lands."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="invoiced",
               billed=True,
               app_access_until=datetime(2027, 1, 20, 13, tzinfo=UTC))
    row.extension_amount = 4258
    checkout, calls = _wire(
        monkeypatch,
        rows=[row],
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 25, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    voided = _capture_voids(monkeypatch)
    _wire_reinstate(monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"}, paid=False)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert exc.value.status == 402
    assert calls["rows"] == []          # still cancelled
    # ...and the unpaid bill is withdrawn with it, or dunning would chase it.
    assert voided == ["in_r"]


def test_a_card_declined_while_reinstating_voids_the_invoice_it_left_open(monkeypatch):
    """How a decline really arrives: raised out of ``Invoice.pay`` with the invoice
    finalized and OPEN. The module stays cancelled, so the bill must go too — left open,
    dunning chases it and the portal offers it as *Retry payment*: a customer paying to
    restore a module that stays cancelled."""
    from billing.services.billing_gateway import BillingError

    checkout, calls = _reinstatable(monkeypatch)
    voided = _capture_voids(monkeypatch)
    _wire_reinstate(
        monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"},
        raises=BillingError("card_declined", invoice_id="in_declined"),
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert exc.value.status == 402
    assert voided == ["in_declined"]
    assert calls["rows"] == []          # still cancelled


def test_each_press_to_restore_is_a_new_attempt(monkeypatch):
    """The key is stable - the company, the day its access ends, the modules - and a declined
    attempt keeps it claimed, so without attempts every retry was refused unseen
    (``test_change_attempts`` pins the numbering)."""
    checkout, _calls = _reinstatable(monkeypatch)
    charged = _wire_reinstate(monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"})

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert charged[0]["attempts_of"] is not None


def test_a_second_press_while_the_first_is_charging_is_told_so(monkeypatch):
    """Both presses reach for the same attempt's key; the second is refused by the unique index.
    That is not a declined card, and "check your payment method" sent people to fix one."""
    from billing.services.billing_gateway import BillingError

    checkout, calls = _reinstatable(monkeypatch)
    voided = _capture_voids(monkeypatch)
    _wire_reinstate(
        monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"},
        raises=BillingError("already claimed", retryable=True, claimed=True),
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert exc.value.status == 409
    assert voided == [] and calls["rows"] == []


def test_a_processor_outage_while_restoring_is_not_a_declined_card(monkeypatch):
    from billing.services.billing_gateway import BillingError

    checkout, calls = _reinstatable(monkeypatch)
    _capture_voids(monkeypatch)
    _wire_reinstate(
        monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"},
        raises=BillingError("could not connect", retryable=True),
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert exc.value.status == 503
    assert "payment method" not in exc.value.message
    assert calls["rows"] == []


def test_a_reinstatement_whose_answer_was_lost_restores_the_module(monkeypatch):
    """The void finds the invoice PAID - the charge went through and its answer was lost. It
    was paid for, so the module comes back; refused, the customer paid and got nothing, and
    their retry charged them again."""
    from billing.services import billing_gateway
    from billing.services.billing_gateway import BillingError

    checkout, calls = _reinstatable(monkeypatch)
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: "paid")
    _wire_reinstate(
        monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"},
        raises=BillingError("no answer from the processor", invoice_id="in_lost"),
    )

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    code, fields = calls["rows"][-1]
    assert (code, fields["phase"]) == ("PETTY_CASH", "active")


def test_a_reinstatement_whose_void_fails_is_still_refused(monkeypatch):
    """The void is best effort. A processor error there is logged, never turned into
    something the customer reads as worse than the decline itself."""
    from billing.services.billing_gateway import BillingError

    checkout, calls = _reinstatable(monkeypatch)
    voided = _capture_voids(monkeypatch, fails=True)
    _wire_reinstate(
        monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"},
        raises=BillingError("card_declined", invoice_id="in_declined"),
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert exc.value.status == 402
    assert voided == ["in_declined"]
    assert calls["rows"] == []


def test_reinstating_a_company_with_no_card_is_refused_not_billed_elsewhere(monkeypatch):
    """A company nobody nominated a card for has nowhere to be charged. Without the
    company's card ``issue_change`` names none, and the processor bills the account's
    default: a card the payer never chose for it, on a document no billing account owns.
    That is not a fallback, it is the bug, so the purchase is refused."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="invoiced",
               billed=True,
               app_access_until=datetime(2027, 1, 20, 13, tzinfo=UTC))
    row.extension_amount = 4258
    checkout, calls = _wire(
        monkeypatch,
        rows=[row],
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 25, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    charged = _wire_reinstate(
        monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"}, group=None
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert exc.value.status == 409
    assert charged == []                # nothing billed, on any card
    assert calls["rows"] == []          # still cancelled


def test_an_extension_running_past_the_period_leaves_nothing_to_charge(monkeypatch):
    """Cancel late enough and the 30 free days already cover the rest of the period.
    Charging anyway would bill for days the extension paid for."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="invoiced",
               billed=True,
               app_access_until=datetime(2027, 3, 20, 13, tzinfo=UTC))
    row.extension_amount = 4258
    checkout, calls = _wire(
        monkeypatch,
        rows=[row],
        paid_through=datetime(2027, 2, 8, 13, tzinfo=UTC),
        now=datetime(2027, 1, 25, 13, tzinfo=UTC),
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    charged = _wire_reinstate(monkeypatch, checkout, billed_codes={"PAYMENT_REQUEST", "PETTY_CASH"})

    checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert charged == []
    assert calls["rows"][-1][1]["phase"] == "active"


def test_reactivate_rejects_a_module_not_scheduled_to_cancel(monkeypatch):
    checkout, calls = _wire(monkeypatch, rows=[_Row("PAYMENT_REQUEST", phase="active")])

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.reactivate_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")
    assert exc.value.status == 409


# --- rebuying inside the cancellation window ---------------------------------


def test_rebuying_a_module_inside_its_cancellation_window_is_refused(monkeypatch):
    """A cancelled module still has its extension queued and its access running. Buying
    it again would charge for the module a second time on top of that extension. Renew
    is the only correct move (it also reverses the extension)."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel",
               app_access_until=datetime.now(UTC) + timedelta(days=20))
    checkout, calls = _wire(monkeypatch, rows=[row])

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.start_modules_checkout(
            _FakeEntity(), _FakeUser(), "s", "c", ["PETTY_CASH"]
        )

    assert exc.value.status == 409
    assert "Renew" in exc.value.message
    assert calls["created"] == [] and calls["charged"] == []  # nothing billed


def test_rebuying_is_allowed_once_the_cancellation_window_has_passed(monkeypatch):
    """The window is over, so the module is genuinely gone and buying it is correct."""
    row = _Row("PETTY_CASH", phase="scheduled_cancel",
               app_access_until=datetime.now(UTC) - timedelta(days=1))  # expired
    checkout, calls = _wire(monkeypatch, rows=[row])

    checkout.start_modules_checkout(_FakeEntity(), _FakeUser(), "s", "c", ["PETTY_CASH"])

    assert calls["created"] == [["PETTY_CASH"]]


def test_cancelling_on_DIFFERENT_DAYS_keeps_each_date_but_prices_them_as_a_pair(monkeypatch):
    """Petty Cash today, Payment Request tomorrow — two clicks, one leaving set.

    Two things are decided separately and it is easy to conflate them:

      the DATE   each module's guarantee runs from its own cancellation, so the one
                 cancelled a day later keeps access a day longer;
      the PRICE  is what the module is worth against whoever still holds theirs — and
                 that CHANGES mid-window when they stop on different days.

    Anchor 8 Jan, paid_through 8 Feb (31 days). Petty Cash cancelled 20 Jan runs to
    19 Feb; Payment Request cancelled 21 Jan runs to 20 Feb. So:

      8 Feb -> 19 Feb   both there: the 400 bundle between them, Payment Request taking
                        its 280 and Petty Cash the 120 step
      19 Feb -> 20 Feb  Payment Request is alone, and worth its whole 280

    which makes its last day cost 280/mo rather than a bundle price nobody is in. Payment
    Request sorts first, so its own rate is 280 in BOTH pieces — segmentation only ever
    bites the later-sorted code, and here that one has already stopped.
    """
    day_one = datetime(2027, 1, 20, 13, tzinfo=UTC)
    day_two = datetime(2027, 1, 21, 13, tzinfo=UTC)
    paid_through = datetime(2027, 2, 8, 13, tzinfo=UTC)

    # Day one: Petty Cash leaves on its own, so it is priced on its own (28000 x 11/31).
    checkout, calls = _wire(
        monkeypatch,
        rows=[_Row("PETTY_CASH"), _Row("PAYMENT_REQUEST")],
        paid_through=paid_through,
        now=day_one,
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    first = dict(calls["rows"])["PETTY_CASH"]
    assert first["extension_amount"] == 9935
    assert first["app_access_until"] == datetime(2027, 2, 19, 13, tzinfo=UTC)

    # Day two: Payment Request follows it out. Same entity, with Petty Cash now winding
    # down and carrying the amount and date it was given yesterday.
    checkout, calls = _wire(
        monkeypatch,
        rows=[
            _Row("PAYMENT_REQUEST"),
            _Row("PETTY_CASH", phase="scheduled_cancel", ext_state="pending",
                 ext_amount=9935,
                 app_access_until=datetime(2027, 2, 19, 13, tzinfo=UTC)),
        ],
        paid_through=paid_through,
        now=day_two,
    )
    monkeypatch.setattr(checkout, "_set_module_access", lambda e, c, on: None)
    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    written = dict(calls["rows"])
    # Its own window — a day later than Petty Cash's — priced in two pieces at the same
    # 280 rate: 28000 x 11/31 = 9935 shared, then 28000 x 1/31 = 904 alone.
    assert written["PAYMENT_REQUEST"]["app_access_until"] == datetime(2027, 2, 20, 13, tzinfo=UTC)
    assert written["PAYMENT_REQUEST"]["extension_amount"] == 10839
    # Re-priced to the 120 step over the window it already had — one piece, since nothing
    # outlasts it.
    assert written["PETTY_CASH"]["extension_amount"] == 4258, "12000 x 11/31, was 9935"
