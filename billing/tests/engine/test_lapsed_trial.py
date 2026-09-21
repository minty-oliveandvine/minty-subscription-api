"""When a lapsed trial takes the subscription settings page over.

THE MODE MATRIX is what this file is for. ``consent.lapsed_trial_for_entity`` answers
one of three things, and each has a different cost of being wrong:

* ``takeover`` on an entity that is fine  -> a working page replaced by a sales screen
* ``None`` on an entity that went dark    -> the silent lapse this feature exists to end
* ``takeover`` on a trial about to convert -> a customer CHARGED for something that was
  seconds from converting on its own

That last one is why the trigger reads the ACCESS GATE rather than ``trial_end``. The
date passes unattended; only ``close-trials`` (or the sweep) flips the gate, so between
the two the customer is still working. ``modules.get_module_cards`` already carries a
comment about a date-keyed reading that told customers their trial was used up while the
gate still let them work — here the same mistake would take their money, so
``test_a_trial_past_its_term_with_the_gate_still_on_is_left_alone`` is the load-bearing
case in this file.

NOTE: imports are done INSIDE each test and patching is BY DOTTED PATH — the conftest
``app`` fixture clears and re-imports project modules mid-session, so a module object
captured at import time is not the one the code under test calls.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

_STORE = "billing.services.store"


class _Row:
    """One ``entity_module_subscription`` row, only the fields the trigger reads."""

    def __init__(self, code, phase, *, ended_ago=None, payer="u1"):
        self.entity_id = "e1"
        self.function_code = code
        self.payer_user_id = payer
        self.phase = phase
        self.trial_end = (
            datetime.now(UTC) - ended_ago if ended_ago is not None else None
        )


def _state(app, monkeypatch, rows, access, *, consent=False):
    from billing.services import consent as consent_mod

    monkeypatch.setattr(f"{_STORE}.module_rows_for_entity", lambda eid: rows)
    monkeypatch.setattr(f"{_STORE}.payer_for_entity", lambda eid: "u1")
    monkeypatch.setattr(f"{_STORE}.has_billing_consent", lambda eid, uid=None: consent)
    monkeypatch.setattr(
        "billing.services.entity_modules.module_display_names",
        lambda codes: {c: c.replace("_", " ").title() for c in codes},
    )
    monkeypatch.setattr(consent_mod, "_has_card", lambda uid: False)

    with app.app_context():
        return consent_mod.lapsed_trial_for_entity("e1", "u1", access_state=access)


# --- the matrix ---------------------------------------------------------------


def test_every_module_lapsed_takes_the_page_over(app, monkeypatch):
    state = _state(
        app, monkeypatch,
        [_Row("PETTY_CASH", "expired", ended_ago=timedelta(days=2))],
        {"PETTY_CASH": False},
    )
    assert state["mode"] == "takeover"
    assert [item["code"] for item in state["lapsed"]] == ["PETTY_CASH"]


def test_a_lapse_beside_a_running_trial_is_a_panel_not_a_takeover(app, monkeypatch):
    """The takeover would replace the very cards the live trial still needs."""
    state = _state(
        app, monkeypatch,
        [
            _Row("PETTY_CASH", "expired", ended_ago=timedelta(days=20)),
            _Row("PAYMENT_REQUEST", "trial", ended_ago=timedelta(days=-10)),
        ],
        {"PETTY_CASH": False, "PAYMENT_REQUEST": True},
    )
    assert state["mode"] == "panel"
    assert [item["code"] for item in state["lapsed"]] == ["PETTY_CASH"]


def test_a_lapse_beside_a_paid_module_shows_nothing(app, monkeypatch):
    """A paying entity's page is not broken — that is an upsell, and the ordinary
    subscribe flow already covers it."""
    state = _state(
        app, monkeypatch,
        [
            _Row("PETTY_CASH", "expired", ended_ago=timedelta(days=20)),
            _Row("PAYMENT_REQUEST", "active"),
        ],
        {"PETTY_CASH": False, "PAYMENT_REQUEST": True},
    )
    assert state["mode"] is None


def test_a_lapse_beside_a_past_due_module_shows_nothing(app, monkeypatch):
    """``past_due`` is still a billing relationship — dunning owns that conversation."""
    state = _state(
        app, monkeypatch,
        [
            _Row("PETTY_CASH", "expired", ended_ago=timedelta(days=20)),
            _Row("PAYMENT_REQUEST", "past_due"),
        ],
        {"PETTY_CASH": False, "PAYMENT_REQUEST": True},
    )
    assert state["mode"] is None


def test_nothing_lapsed_shows_nothing(app, monkeypatch):
    state = _state(
        app, monkeypatch,
        [_Row("PETTY_CASH", "trial", ended_ago=timedelta(days=-5))],
        {"PETTY_CASH": True},
    )
    assert state["mode"] is None


# --- the gate, not the date ---------------------------------------------------


def test_a_trial_past_its_term_with_the_gate_still_on_is_left_alone(app, monkeypatch):
    """THE case a date-keyed trigger would get wrong, and it would get it wrong by
    charging: ``close-trials`` has not run, so this trial may be about to CONVERT."""
    state = _state(
        app, monkeypatch,
        [_Row("PETTY_CASH", "trial", ended_ago=timedelta(minutes=20))],
        {"PETTY_CASH": True},
    )
    assert state["mode"] is None


def test_a_trial_row_whose_gate_went_off_first_is_a_lapse(app, monkeypatch):
    """The sweep can revoke access before ``close-trials`` rewrites the phase. The row
    still says ``trial``, but the customer is locked out — same state to them as
    ``expired``, so it is treated identically."""
    state = _state(
        app, monkeypatch,
        [_Row("PETTY_CASH", "trial", ended_ago=timedelta(days=3))],
        {"PETTY_CASH": False},
    )
    assert state["mode"] == "takeover"


def test_a_trial_inside_the_closing_window_is_left_to_the_pass(app, monkeypatch):
    """Gate off but the term only just ended: give the pass its window to convert
    rather than selling something that may be about to be charged anyway."""
    state = _state(
        app, monkeypatch,
        [_Row("PETTY_CASH", "trial", ended_ago=timedelta(minutes=30))],
        {"PETTY_CASH": False},
    )
    assert state["mode"] is None


def test_an_expired_row_whose_gate_is_still_on_sides_with_the_gate(app, monkeypatch):
    """A repair the sweep has not made yet. The card sides with the gate; so does this."""
    state = _state(
        app, monkeypatch,
        [_Row("PETTY_CASH", "expired", ended_ago=timedelta(days=2))],
        {"PETTY_CASH": True},
    )
    assert state["mode"] is None


# --- cancellation is the way out ----------------------------------------------


def test_a_cancelled_trial_is_not_a_lapse(app, monkeypatch):
    """Cancelling is the ONLY way out of a screen with no dismiss control, so taking the
    page over about a subscription the customer themselves ended would leave them
    no recourse at all."""
    for phase in ("scheduled_cancel", "cancelled"):
        state = _state(
            app, monkeypatch,
            [_Row("PETTY_CASH", phase, ended_ago=timedelta(days=5))],
            {"PETTY_CASH": False},
        )
        assert state["mode"] is None, phase


def test_a_row_with_no_trial_end_is_not_a_lapsed_trial(app, monkeypatch):
    """``expired`` with no ``trial_end`` never had a free period that ran out, so there
    is nothing this screen knows how to sell back."""
    state = _state(
        app, monkeypatch, [_Row("PETTY_CASH", "expired")], {"PETTY_CASH": False}
    )
    assert state["mode"] is None


# --- staggered trials ---------------------------------------------------------


def test_staggered_trials_report_a_date_each(app, monkeypatch):
    """Two modules trialed at different times went dark on different days, and the copy
    has to be able to say so — one date for the entity would be wrong for one of them."""
    state = _state(
        app, monkeypatch,
        [
            _Row("PETTY_CASH", "expired", ended_ago=timedelta(days=30)),
            _Row("PAYMENT_REQUEST", "expired", ended_ago=timedelta(days=5)),
        ],
        {"PETTY_CASH": False, "PAYMENT_REQUEST": False},
    )
    assert state["mode"] == "takeover"
    dates = {item["code"]: item["lapsed_on"] for item in state["lapsed"]}
    assert dates["PETTY_CASH"] != dates["PAYMENT_REQUEST"]
    assert [item["code"] for item in state["lapsed"]] == ["PETTY_CASH", "PAYMENT_REQUEST"]


def test_the_mode_does_not_depend_on_which_half_was_missing(app, monkeypatch):
    """Card but no consent, or consent but no card — either way the trial expired and
    the entity has to buy its modules back."""
    rows = [_Row("PETTY_CASH", "expired", ended_ago=timedelta(days=2))]
    for consent in (True, False):
        state = _state(app, monkeypatch, rows, {"PETTY_CASH": False}, consent=consent)
        assert state["mode"] == "takeover", consent
        assert state["has_consent"] is consent


# --- failure ------------------------------------------------------------------


def test_a_read_failure_answers_closed(app, monkeypatch):
    """The most intrusive screen in the app, and it charges. Anything unknown resolves
    to "do not". (Flask's version also asserted a session rollback, which is what stopped
    one bad statement poisoning the rest of the request on Postgres; under Django's
    autocommit there is no session to reset, so only the answer is asserted.)"""
    from billing.services import consent as consent_mod

    def boom(_eid):
        raise RuntimeError("db is unhappy")

    monkeypatch.setattr(f"{_STORE}.module_rows_for_entity", boom)

    with app.app_context():
        state = consent_mod.lapsed_trial_for_entity("e1", "u1", access_state={})

    assert state["mode"] is None
    assert state["lapsed"] == []


# --- what a restart may charge for --------------------------------------------


def test_codes_for_restart_keeps_only_what_lapsed(app, monkeypatch):
    """THE validation for the route that charges. The list comes from the browser, so a
    module the entity never lapsed must not become a charge."""
    from billing.services import consent as consent_mod

    state = {"lapsed": [{"code": "PETTY_CASH"}, {"code": "PAYMENT_REQUEST"}]}

    with app.app_context():
        assert consent_mod.codes_for_restart(state, ["PETTY_CASH"]) == ["PETTY_CASH"]
        assert consent_mod.codes_for_restart(state, ["PAYMENT_REQUEST", "PETTY_CASH"]) == [
            "PETTY_CASH",
            "PAYMENT_REQUEST",
        ], "canonical order, not the order submitted"
        assert consent_mod.codes_for_restart(state, []) == []
        assert consent_mod.codes_for_restart(state, None) == []


def test_codes_for_restart_refuses_a_set_it_cannot_honour(app, monkeypatch):
    """Refused OUTRIGHT rather than filtered down. Silently dropping the bad code would
    charge for a different set than the payer submitted, which on a screen that takes
    money is the worst of both."""
    from billing.services import consent as consent_mod

    state = {"lapsed": [{"code": "PETTY_CASH"}]}

    with app.app_context():
        assert consent_mod.codes_for_restart(state, ["PAYMENT_REQUEST"]) == []
        assert consent_mod.codes_for_restart(state, ["PETTY_CASH", "PAYMENT_REQUEST"]) == []
        assert consent_mod.codes_for_restart(state, ["NOT_A_MODULE"]) == []
        assert consent_mod.codes_for_restart({"lapsed": []}, ["PETTY_CASH"]) == []
