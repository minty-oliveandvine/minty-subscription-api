"""The double-buy guard asks about NOW, not about the phase alone.

``access.is_subscribed`` answers "does this phase mean they hold it" and is right about
that. What ``_has_active_subscription`` has to answer is "do they hold it *now*", and the
phase cannot say: nothing transitions a lapsed paid module to ``expired``. The sweep
revokes access without touching the phase, and ``end_group_dunning(status="closed")``
deliberately leaves a given-up account ``past_due`` because the debt is real.

So a customer whose subscription lapsed for non-payment was told, forever after, that they
"already have an active subscription" — and could never buy it back. ``is_subscribed``'s
own reasoning is the fix: *cancelled / expired — nothing is held any more, so re-buying is
right*. A lapsed module holds nothing either.

Every intended block survives, because each one still grants access. The past-due-inside-
grace case matters most: selling that again is what the rules call the worst version of a
double buy, "because it looks like new revenue".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)

FUTURE = NOW + timedelta(days=20)
JUST_LAPSED = NOW - timedelta(days=2)      # inside a 10-day past-due grace
LONG_LAPSED = NOW - timedelta(days=40)     # well past it


class _Row:
    def __init__(self, phase, *, trial_end=None, app_access_until=None):
        self.entity_id = "e1"
        self.function_code = "PETTY_CASH"
        self.payer_user_id = "u1"
        self.phase = phase
        self.trial_end = trial_end
        self.app_access_until = app_access_until
        self.first_billed_at = None


class _Policy:
    trial_days = 30
    paid_cancel_access_days = 30
    past_due_window_days = 10
    retry_offsets_days = (1, 3, 5, 7)


def _blocked(app, monkeypatch, *, row, paid_through):
    from billing.services import checkout, clock, policy, store

    monkeypatch.setattr(clock, "now", lambda: NOW)
    monkeypatch.setattr(policy, "current", lambda: _Policy())
    monkeypatch.setattr(store, "module_row", lambda eid, code: row)
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)

    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    with app.app_context():
        return checkout._has_active_subscription("e1", "PETTY_CASH")


# --- still blocked, and each for its own reason --------------------------------


def test_a_live_paid_module_still_blocks_a_second_purchase(app, monkeypatch):
    assert _blocked(app, monkeypatch, row=_Row("active"), paid_through=FUTURE) is True


def test_a_running_trial_still_blocks_a_purchase(app, monkeypatch):
    """It converts on its own; selling it would take money for something being given
    free and leave two claims on one module."""
    row = _Row("trial", trial_end=FUTURE, app_access_until=FUTURE)
    assert _blocked(app, monkeypatch, row=row, paid_through=None) is True


def test_past_due_INSIDE_its_grace_still_blocks(app, monkeypatch):
    """The worst version of a double buy — a second charge for a module they already
    have and have not paid for — so this one must not regress."""
    assert _blocked(app, monkeypatch, row=_Row("past_due"),
                    paid_through=JUST_LAPSED) is True


# --- no longer blocked, because nothing is held --------------------------------


def test_past_due_PAST_its_grace_can_be_bought_again(app, monkeypatch):
    """Dunning gave up and access lapsed. The phase stays past_due on purpose (the debt
    is real), which is exactly why the phase cannot be the whole answer."""
    assert _blocked(app, monkeypatch, row=_Row("past_due"),
                    paid_through=LONG_LAPSED) is False


def test_an_active_module_whose_period_lapsed_can_be_bought_again(app, monkeypatch):
    """The reported case: "Module PETTY_CASH already has an active subscription" on a
    module the request gate had already stopped letting anyone into."""
    assert _blocked(app, monkeypatch, row=_Row("active"),
                    paid_through=LONG_LAPSED) is False


def test_an_expired_module_was_never_blocked(app, monkeypatch):
    assert _blocked(app, monkeypatch, row=_Row("expired"),
                    paid_through=FUTURE) is False


def test_a_scheduled_cancellation_is_left_to_the_cancellation_window_guard(app, monkeypatch):
    """Refused a moment later with "use Renew", which tells the customer what to do."""
    row = _Row("scheduled_cancel", app_access_until=FUTURE)
    assert _blocked(app, monkeypatch, row=row, paid_through=FUTURE) is False


def test_no_row_is_not_a_subscription(app, monkeypatch):
    assert _blocked(app, monkeypatch, row=None, paid_through=FUTURE) is False
