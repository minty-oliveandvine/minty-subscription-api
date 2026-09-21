"""The "Free Trial" badge on the select-company list.

The list draws one badge per card, left of the module icons, when a module the entity
actually holds is running on a free trial. Two things it must not do:

  * claim a trial for a module that has been BILLED. A paid module that is winding down
    shares the ``scheduled_cancel`` phase with a cancelled trial, and only
    ``first_billed_at`` separates them;
  * blink off in the gap between a trial's term ending and the pass that closes it out —
    the same window ``get_module_cards`` covers with ``TRIAL_CLOSING_WINDOW``.

NOTE: the fake query returns whatever rows the test hands it. The SQL predicates
(never billed, trial_end present, phase in trial/scheduled_cancel) are not exercised here,
so tests only supply rows that clause would really return; what IS exercised is the
date arithmetic afterwards, which SQL does not do.

Imports are done INSIDE each test — the conftest ``app`` fixture clears and re-imports
project modules mid-session, so a module captured at import time is not the one under test.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .fakes import fake_model

UTC = UTC
_EMS = "shared_models.models"


class _Row:
    def __init__(self, entity_id, code, *, phase="trial", ends_in=timedelta(days=5),
                 app_access_until=None, first_billed_at=None):
        self.entity_id = entity_id
        self.function_code = code
        self.phase = phase
        self.trial_end = datetime.now(UTC) + ends_in
        self.app_access_until = app_access_until
        self.first_billed_at = first_billed_at


def _trials(app, monkeypatch, rows, entity_ids=("e1",)):
    import billing.services.entity_modules as modules_mod

    # The rows, behind the model the cards import at call time. The fake's filter
    # applies the same field tests the real query does.
    monkeypatch.setattr(f"{_EMS}.EntityModuleSubscription", fake_model(rows))
    with app.app_context():
        return modules_mod.get_trial_modules_for_entities(list(entity_ids))


def test_a_running_trial_is_reported(app, monkeypatch):
    assert _trials(app, monkeypatch, [_Row("e1", "PETTY_CASH")]) == {
        "e1": {"PETTY_CASH"}
    }


def test_a_cancelled_trial_is_still_a_trial(app, monkeypatch):
    """Cancelling a trial stops it converting; it does not end the free days."""
    row = _Row(
        "e1", "PAYMENT_REQUEST", phase="scheduled_cancel",
        app_access_until=datetime.now(UTC) + timedelta(days=3),
    )
    assert _trials(app, monkeypatch, [row]) == {"e1": {"PAYMENT_REQUEST"}}


def test_a_trial_past_its_term_but_awaiting_the_pass_still_counts(app, monkeypatch):
    """``trial_end`` passes unattended — the row keeps its phase until close-trials runs."""
    row = _Row("e1", "PETTY_CASH", ends_in=-timedelta(minutes=30))
    assert _trials(app, monkeypatch, [row]) == {"e1": {"PETTY_CASH"}}


def test_a_trial_nothing_ever_closed_out_stops_counting(app, monkeypatch):
    """Past the closing window the trial is simply over — the badge must not stick.

    This is the environment with no scheduler, which production was: without the bound
    every stale trial would read as a live one forever.
    """
    row = _Row("e1", "PETTY_CASH", ends_in=-timedelta(days=9))
    assert _trials(app, monkeypatch, [row]) == {}


def test_a_lapsed_cancelled_trial_stops_counting(app, monkeypatch):
    """No closing-window slack outside ``phase = trial`` — nothing is due to run for it."""
    row = _Row(
        "e1", "PAYMENT_REQUEST", phase="scheduled_cancel",
        app_access_until=datetime.now(UTC) - timedelta(minutes=30),
    )
    assert _trials(app, monkeypatch, [row]) == {}


def test_rows_are_grouped_per_entity(app, monkeypatch):
    rows = [_Row("e1", "PETTY_CASH"), _Row("e1", "PAYMENT_REQUEST"), _Row("e2", "PAYMENT_REQUEST")]
    assert _trials(app, monkeypatch, rows, entity_ids=("e1", "e2")) == {
        "e1": {"PETTY_CASH", "PAYMENT_REQUEST"},
        "e2": {"PAYMENT_REQUEST"},
    }


def test_no_entities_asks_nothing(app, monkeypatch):
    """An empty list must not build a ``WHERE entity_id IN ()``."""
    import billing.services.entity_modules as modules_mod

    with app.app_context():
        assert modules_mod.get_trial_modules_for_entities([]) == {}


