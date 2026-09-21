"""A module whose period has lapsed must stop presenting as a live subscription.

The phase is not "is this live". A row stays ``active`` until something writes it, while
access ends on a DATE that passes unattended — nothing fires at the boundary, which is
why ``sweep_expired_module_access`` exists to reconcile the gate afterwards.

Reading the phase alone meant a module that had lapsed, and whose access the sweep had
just revoked, still rendered as:

    Petty Cash                       active
                          [ Cancel Subscription ]
                          valid until 28 Jul 2026     <- a date in the PAST

— the card claiming a subscription the request gate would refuse, and offering to cancel
something already gone. ``granted`` now gates the paid branch the same way it always
gated the trial one.

NOTE: imports are done INSIDE each test and the catalog is patched BY DOTTED PATH — the
conftest ``app`` fixture clears and re-imports project modules mid-session, so a module
object captured at import time is not the one the code under test calls.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .fakes import fake_model

_CATALOG = "billing.services.catalog"
UTC = UTC


class _Fn:
    id = "fn_pc"
    function_code = "PETTY_CASH"
    function_name = "Petty Cash"
    description = "d"
    is_active = True


# The catalog row, in the shape the cards read it (``EntityFunction.objects``).
_FakeEntityFunction = fake_model([_Fn()])


class _PaidRow:
    """A module that was converted and has been billing ever since."""

    entity_id = "e1"
    function_code = "PETTY_CASH"
    payer_user_id = "u1"
    phase = "active"
    trial_end = None
    app_access_until = None
    extension_amount = None
    extension_state = None

    def __init__(self):
        self.first_billed_at = datetime.now(UTC) - timedelta(days=60)


class _TrialRow:
    """A trial whose term has passed and that no pass has closed out yet.

    Nothing has been billed, the phase is still ``trial`` because only ``close-trials``
    rewrites it, and ``app_access_until`` was stamped equal to ``trial_end`` when the trial
    started — so the date rules already say access is over while the gate has not been
    told.
    """

    entity_id = "e1"
    function_code = "PETTY_CASH"
    payer_user_id = "u1"
    phase = "trial"
    extension_amount = None
    extension_state = None
    first_billed_at = None

    def __init__(self, *, ended_ago=timedelta(minutes=20)):
        self.trial_end = datetime.now(UTC) - ended_ago
        self.app_access_until = self.trial_end


def _card(app, monkeypatch, *, paid_through, row=None, has_access=False):
    import billing.services.entity_modules as modules_mod
    from billing.services import cards as cards_mod
    from billing.services import store

    # The cards bind EntityFunction themselves (Flask's tests patched the shim AND cards).
    monkeypatch.setattr(cards_mod, "EntityFunction", _FakeEntityFunction)
    monkeypatch.setattr(modules_mod, "MODULE_CODES", ("PETTY_CASH",))
    monkeypatch.setattr(modules_mod, "_entity_customer_id", lambda eid: None)
    monkeypatch.setattr(modules_mod, "_enabled_state",
                        lambda eid: {"PETTY_CASH": has_access})
    monkeypatch.setattr(store, "module_rows_for_entity",
                        lambda eid: [row if row is not None else _PaidRow()])
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    monkeypatch.setattr(f"{_CATALOG}.available_plans", lambda: [])
    monkeypatch.setattr(
        "billing.services.stripe_client.customer_default_payment_method",
        lambda cid: None,
    )

    with app.app_context():
        return modules_mod.get_module_cards("e1")[0]


def test_a_lapsed_paid_module_does_not_present_as_active(app, monkeypatch):
    """Period ended six days ago and the phase was never rewritten."""
    card = _card(app, monkeypatch, paid_through=datetime.now(UTC) - timedelta(days=6))

    assert card["subscription_status"] is None, "not a live subscription any more"
    assert card["can_cancel"] is False, "nothing live to cancel"
    # No stale promise: "valid until <past date>" came from period_end_long.
    assert card["period_end_long"] is None
    assert card["formatted_period_end"] is None


def test_a_paid_module_inside_its_period_still_presents_as_active(app, monkeypatch):
    """The mirror case — the fix must not switch off a healthy subscription."""
    card = _card(app, monkeypatch, paid_through=datetime.now(UTC) + timedelta(days=20))

    assert card["subscription_status"] == "active"
    assert card["can_cancel"] is True
    assert card["period_end_long"] is not None


def test_a_payer_with_no_paid_through_has_no_live_paid_module(app, monkeypatch):
    """Nothing has ever been billed, so there is no period the phase can be live within.

    Reached when a row is written by hand or a conversion half-completed; the card must
    not invent a subscription from the phase alone.
    """
    card = _card(app, monkeypatch, paid_through=None)

    assert card["subscription_status"] is None
    assert card["can_cancel"] is False


def test_a_trial_past_its_term_is_closing_not_expired(app, monkeypatch):
    """The window between the term ending and the pass closing it out.

    ``trial_end`` passes unattended, so until the subscription pass runs the row is still
    ``phase = trial`` while the date rules have already gone false. The card used to fall
    straight through to ``trial_expired`` and tell the customer their free trial was used
    up — while the request gate was still letting them work, and while a trial with a card
    and consent was about to CONVERT rather than expire.
    """
    card = _card(
        app, monkeypatch, paid_through=None, row=_TrialRow(), has_access=True
    )
    # STILL "trialing", deliberately. Everything downstream — the panel's enabled set, the
    # notices, the badge, the row styling — asks that question, and answering differently
    # would rearrange the page around a state nobody can act on. The difference is carried
    # by a flag used for one label and nothing else.
    assert card["subscription_status"] == "trialing"
    assert card["trial_closing"] is True
    # Not "expired": nothing has decided yet, and the module still opens.
    assert card["trial_expired"] is False
    assert card["has_access"] is True
    # Not offered as a fresh trial either — they are inside it.
    assert card["trial_eligible"] is False
    # The pill's date survives, so the card reads exactly as it did an hour ago.
    assert card["period_end_short"] is not None


def test_a_trial_past_its_term_with_access_gone_is_expired(app, monkeypatch):
    """The bound on the transitional state.

    It needs the gate to still say yes. Once anything reconciles — close-trials expiring
    the row, or the sweep revoking a module whose ``grants_access`` is already false — the
    card must go back to telling the plain truth. Without this, a scheduler that stopped
    would leave "finalising" on screen forever.
    """
    card = _card(
        app, monkeypatch, paid_through=None, row=_TrialRow(), has_access=False
    )
    assert card["subscription_status"] is None
    assert card["trial_closing"] is False
    assert card["trial_expired"] is True


def test_a_running_trial_is_untouched(app, monkeypatch):
    """The new state must not swallow trials that are simply still running."""
    card = _card(
        app,
        monkeypatch,
        paid_through=None,
        row=_TrialRow(ended_ago=timedelta(days=-3)),  # ends in three days
        has_access=True,
    )
    assert card["subscription_status"] == "trialing"
    assert card["trial_expired"] is False


def _trial_card(**overrides):
    card = {
        "code": "PETTY_CASH", "name": "Petty Cash", "description": "", "image": "x.png",
        "learn_more": "#", "subscription_status": "trialing", "amount": 280,
        "pending_cancel": False, "trial_cancelled": False, "trial_eligible": False,
        "trial_expired": False, "needs_card": False, "needs_consent_only": False,
        "period_end_long": None, "period_end_short": "15 Aug", "access_end_long": None,
        "has_access": True, "trial_closing": False,
    }
    card.update(overrides)
    return card




def test_a_long_stale_trial_is_expired_not_closing(app, monkeypatch):
    """The bound that production needed, and the regression that found it.

    ``trial_closing`` was first written as "phase is trial, access is still on", justified
    as self-limiting because the next pass resolves it within the hour. That holds only
    where the pass RUNS. With no scheduler — production, until it is enabled there — a
    trial keeps its phase and its access indefinitely, so the unbounded version matched
    every stale trial forever.

    The visible damage was not the card. ``needs_card`` is ``app_trial and not
    will_convert``, so folding closing into ``app_trial`` switched it on for every stale
    trial, and the settings banner swapped from the billing-portal text to a nudge quoting
    a deadline weeks in the past.
    """
    card = _card(
        app,
        monkeypatch,
        paid_through=None,
        row=_TrialRow(ended_ago=timedelta(days=21)),
        has_access=True,  # nothing ever revoked it, because nothing ran
    )
    assert card["trial_closing"] is False
    assert card["subscription_status"] is None
    assert card["trial_expired"] is True
    # The one that displaced the banner.
    assert card["needs_card"] is False


def test_the_closing_window_is_wide_enough_for_a_missed_pass(app, monkeypatch):
    """A pass that was skipped by a deploy must not flip the card to "expired".

    The window has to absorb an outage of a few hours without the customer seeing their
    trial declared over while they are still working inside it.
    """
    import billing.services.entity_modules as modules_mod

    assert modules_mod.TRIAL_CLOSING_WINDOW >= timedelta(hours=3)
    card = _card(
        app,
        monkeypatch,
        paid_through=None,
        row=_TrialRow(ended_ago=timedelta(hours=4)),
        has_access=True,
    )
    assert card["trial_closing"] is True
    assert card["trial_expired"] is False
