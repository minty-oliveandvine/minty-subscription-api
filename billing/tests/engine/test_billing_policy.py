"""The tunable windows come from ``billing_policy``, and a bad row cannot hurt anyone.

Two things are being pinned here, and the second matters more than the first.

1. A valid row is USED — that is the point of the table.
2. An invalid row is IGNORED, loudly, in favour of the shipped defaults. These numbers
   decide when a customer loses access and when their card stops being charged, and they
   are now editable by anyone with an UPDATE. The database can check that
   ``past_due_window_days`` is a positive integer; it cannot check that the last retry
   lands two days inside it, because that needs a subquery a CHECK constraint cannot
   have. So the loader is the enforcement point, and it has to fail safe.

The fallback is per-GROUP: a broken retry schedule must not silently revert a trial
length somebody set deliberately.
"""
from __future__ import annotations

from datetime import UTC

import pytest

from billing.services import policy


class _Row:
    """A ``billing_policy`` row. The real model is not used: the test app is SQLite,
    where the ``pettycashv3`` schema it points at is not materialised."""

    def __init__(self, trial=30, cancel=30, window=10, offsets="1,3,5,7"):
        self.trial_days = trial
        self.paid_cancel_access_days = cancel
        self.past_due_window_days = window
        self.retry_offsets_days = offsets


@pytest.fixture
def row(monkeypatch):
    """Install a policy row and clear the per-request cache. Returns a setter."""
    import shared_models.models

    holder = {"row": _Row()}

    class _FakeQuery:
        def filter(self, **_kw):
            return self

        def first(self):
            return holder["row"]

    class _FakeModel:
        objects = _FakeQuery()

    monkeypatch.setattr(shared_models.models, "BillingPolicy", _FakeModel)

    def _set(**kwargs):
        holder["row"] = _Row(**kwargs) if kwargs else None
        return holder["row"]

    return _set


def load(app):
    """Read the policy in a fresh scope, so the per-request cache never leaks."""
    with app.app_context():
        return policy.current()


# --- a valid row is used ---------------------------------------------------------


def test_a_valid_row_replaces_every_shipped_default(app, row):
    row(trial=14, cancel=7, window=21, offsets="2,4,8,16")
    live = load(app)

    assert live.trial_days == 14
    assert live.paid_cancel_access_days == 7
    assert live.past_due_window_days == 21
    assert live.retry_offsets_days == (2, 4, 8, 16)


def test_the_attempt_count_is_the_schedules_length(app, row):
    """``MAX_ATTEMPTS`` used to be ``len(RETRY_OFFSETS_DAYS)`` at import time. Editing
    the list therefore changes how many times a card is charged, not just when."""
    row(offsets="1,3,5,7")
    assert load(app).max_attempts == 4

    row(window=30, offsets="1,3,5,7,9,11")
    assert load(app).max_attempts == 6


def test_a_zero_day_trial_is_allowed(app, row):
    """Turning the trial offer off is a legitimate commercial decision, not a fault."""
    row(trial=0)
    assert load(app).trial_days == 0


def test_whitespace_and_stray_separators_in_the_schedule_are_tolerated(app, row):
    row(offsets=" 1, 3 ,5,7, ")
    assert load(app).retry_offsets_days == (1, 3, 5, 7)


# --- a missing or unreadable row falls back --------------------------------------


def test_no_row_yields_the_shipped_defaults(app, row):
    """An app running ahead of the migration, or a test app with no schema."""
    row()  # no kwargs -> None
    assert load(app) == policy.DEFAULTS


def test_an_unreadable_table_falls_back_rather_than_raising(app, monkeypatch):
    """A policy lookup must never be the thing that breaks a settings page."""
    import shared_models.models

    class _ExplodingQuery:
        def filter(self, **_kw):
            raise RuntimeError("no such table")

    class _Exploding:
        objects = _ExplodingQuery()

    monkeypatch.setattr(shared_models.models, "BillingPolicy", _Exploding)
    assert load(app) == policy.DEFAULTS


# --- an incoherent row is refused ------------------------------------------------


@pytest.mark.parametrize(
    "offsets, why",
    [
        ("1,3,5,12", "last retry falls outside a 10-day window"),
        ("1,3,5,10", "last retry lands exactly on the deadline"),
        ("1,3,5,9", "under the two-day settle gap"),
        ("1,5,3,7", "not strictly increasing — would schedule a retry in the past"),
        ("1,3,3,7", "duplicated offset"),
        ("0,3,5,7", "day 0 is the failure itself, not a retry"),
        ("", "no retries at all means a failed renewal is never chased"),
        ("1,x,5", "not a list of integers"),
    ],
)
def test_an_incoherent_schedule_reverts_to_the_shipped_dunning_policy(
    app, row, offsets, why
):
    row(window=10, offsets=offsets)
    live = load(app)

    assert live.retry_offsets_days == policy.DEFAULT_RETRY_OFFSETS_DAYS, why
    # The WINDOW reverts too. Half a dunning policy is still incoherent: keeping a
    # customer-set window with a defaulted schedule can put the last retry back outside
    # it, which is the exact failure the validation exists to prevent.
    assert live.past_due_window_days == policy.DEFAULT_PAST_DUE_WINDOW_DAYS


def test_a_nonsense_window_reverts_the_whole_dunning_pair(app, row):
    row(window=0, offsets="1,3,5,7")
    live = load(app)

    assert live.past_due_window_days == policy.DEFAULT_PAST_DUE_WINDOW_DAYS
    assert live.retry_offsets_days == policy.DEFAULT_RETRY_OFFSETS_DAYS


def test_a_negative_day_count_reverts_that_field_alone(app, row):
    row(trial=-5, cancel=7)
    live = load(app)

    assert live.trial_days == policy.DEFAULT_TRIAL_DAYS
    # Independent field, deliberately set, left alone.
    assert live.paid_cancel_access_days == 7


def test_a_broken_schedule_does_not_revert_a_deliberate_trial_length(app, row):
    """THE per-group rule. Reverting wholesale would quietly undo an unrelated change
    somebody made on purpose, and they would have no reason to look."""
    row(trial=14, cancel=7, window=10, offsets="1,3,5,99")
    live = load(app)

    assert live.trial_days == 14
    assert live.paid_cancel_access_days == 7
    assert live.retry_offsets_days == policy.DEFAULT_RETRY_OFFSETS_DAYS


# --- the coupling the single column removes --------------------------------------


def test_access_and_collection_read_the_same_number(app, row):
    """The whole reason ``past_due_window_days`` is ONE column rather than two.

    ``access.access_end`` extends access by it and ``dunning.give_up_at`` stops charging
    at it. When they were separate constants they had to be kept equal by a test; now
    there is nothing to keep in step.
    """
    from datetime import datetime, timedelta

    from billing.services import access, dunning

    row(window=21, offsets="1,3,5,7")
    window = load(app).past_due_window_days

    at = datetime(2027, 3, 1, tzinfo=UTC)
    access_ends = access.access_end(
        phase="past_due", period_end=at, past_due_grace_days=window
    )
    collection_ends = dunning.give_up_at(at, window)

    assert access_ends == collection_ends == at + timedelta(days=21)


def test_the_defaults_still_satisfy_the_rules_they_are_the_fallback_for(app):
    """The fallback must itself be valid, or a bad row would fall back to another bad
    policy. Guards against someone lowering a default without checking the others."""
    live = policy.DEFAULTS
    assert live.retry_offsets_days
    assert list(live.retry_offsets_days) == sorted(set(live.retry_offsets_days))
    assert min(live.retry_offsets_days) > 0
    assert max(live.retry_offsets_days) < live.past_due_window_days
    assert (
        live.past_due_window_days - max(live.retry_offsets_days)
        >= policy.MIN_SETTLE_GAP_DAYS
    )
