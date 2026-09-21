"""The settings card names a date that is still ahead, not the anchor behind it.

The card used to read "Payment anchor date · 28 Jul 2026". The anchor is the payer's
FIRST charge and is immutable by design — every period is re-derived from it — so from
the day after the first renewal it is a date in the past sitting under a label that
promises a future one. On 13 Aug the page told a paying customer they are billed on
28 Jul.

Two functions answer it, in that order:

``next_payment_from_panel`` reads the first row of the panel's own upcoming charges, so
the card and the list under it cannot name different days. ``get_next_payment_date`` is
the fallback for an entity with nothing scheduled, projecting the payer's cycle to the
boundary ``now`` is inside through the SAME ``billing.period_containing`` the renewal
runner bills on — month-end clamp included.

The anchor is still read; it is just never shown. ``get_billing_anchor`` survives as the
panel's mode switch ("has this payer ever been billed"), which is a different question
from "when are they billed next" and has a different answer while a payer is anchored but
has nothing renewing.
"""
from __future__ import annotations

from datetime import UTC, datetime

UTC = UTC


def _wire(app, monkeypatch, *, payer="u1", anchor=None, now=None):
    """Point the projection at a fixed payer, anchor and clock.

    Takes ``app`` to force the conftest fixture: importing
    ``blueprints.entity.services.modules`` cold trips the model graph's circular import,
    and creating the app is what resolves it. Imported HERE rather than at module level
    for the same reason — conftest drops ``blueprints.*`` from ``sys.modules``, so a
    top-level import would be a stale object and patching it would patch nothing.
    """
    from billing.services import clock, store
    from billing.services import entity_modules as modules

    monkeypatch.setattr(store, "payer_for_entity", lambda eid: payer)
    monkeypatch.setattr(store, "billing_cycle_for_user", lambda uid: (anchor, "HKD"))
    if now is not None:
        monkeypatch.setattr(clock, "now", lambda: now)
    return modules


def test_the_date_shown_is_ahead_of_today_not_the_anchor(app, monkeypatch):
    """The reported bug. Anchored 28 Jul, read on 13 Aug: the answer is 28 Aug."""
    modules = _wire(
        app, monkeypatch,
        anchor=datetime(2026, 7, 28, 13, tzinfo=UTC),
        now=datetime(2026, 8, 13, 9, tzinfo=UTC),
    )

    assert modules.get_next_payment_date("e1") == "28 Aug 2026"


def test_the_anchor_itself_is_still_ahead_inside_the_first_period(app, monkeypatch):
    """Before the first renewal the projection and the anchor agree — nothing regressed
    for a payer who has only just been charged."""
    modules = _wire(
        app, monkeypatch,
        anchor=datetime(2026, 7, 28, 13, tzinfo=UTC),
        now=datetime(2026, 8, 2, 9, tzinfo=UTC),
    )

    assert modules.get_next_payment_date("e1") == "28 Aug 2026"


def test_the_payment_day_itself_still_names_that_day(app, monkeypatch):
    """Morning of the 28th, billed that afternoon: the card says 28 Aug, not 28 Sep.

    ``period_containing`` returns the period ``now`` is INSIDE, and the payment closes it
    — so the day only rolls forward once the charge has actually happened.
    """
    modules = _wire(
        app, monkeypatch,
        anchor=datetime(2026, 7, 28, 13, tzinfo=UTC),
        now=datetime(2026, 8, 28, 9, tzinfo=UTC),
    )

    assert modules.get_next_payment_date("e1") == "28 Aug 2026"


def test_a_month_end_anchor_clamps_the_way_the_renewal_does(app, monkeypatch):
    """31 Jan in a February: 28 Feb, and it springs back to 31 Mar rather than sticking.

    This is the reason the date is derived from the anchor rather than by adding a month
    to the last one — the latter would peg a month-end payer to the 28th permanently, and
    the card would then disagree with the invoice.
    """
    anchor = datetime(2026, 1, 31, 13, tzinfo=UTC)

    modules = _wire(app, monkeypatch, anchor=anchor,
                    now=datetime(2026, 2, 10, 9, tzinfo=UTC))
    assert modules.get_next_payment_date("e1") == "28 Feb 2026"

    modules = _wire(app, monkeypatch, anchor=anchor,
                    now=datetime(2026, 3, 10, 9, tzinfo=UTC))
    assert modules.get_next_payment_date("e1") == "31 Mar 2026"


def test_a_trial_has_no_date_to_name(app, monkeypatch):
    """No anchor means nothing has ever been charged, so there is no cycle to project.
    The card shows "To Be Decided" — inventing the trial's end date here would name a day
    that charges nothing when the trial will not convert."""
    modules = _wire(app, monkeypatch, anchor=None,
                    now=datetime(2026, 8, 13, 9, tzinfo=UTC))

    assert modules.get_next_payment_date("e1") is None


def test_an_entity_with_no_payer_has_no_date(app, monkeypatch):
    """Nobody is billed for it, so the cycle lookup is not even attempted."""
    modules = _wire(app, monkeypatch, payer=None,
                    now=datetime(2026, 8, 13, 9, tzinfo=UTC))

    assert modules.get_next_payment_date("e1") is None


# --- what the card reads off the panel ------------------------------------------
#
# The card's real source. The projection above is only what it falls back to.


def _panel(*rows):
    return {"upcoming_charges": list(rows)}


def _row(label, date, *, overdue=False):
    return {"at": None, "date": date, "label": label, "amount": "HKD 280",
            "overdue": overdue, "note": None}


def test_the_card_names_the_first_charge_not_the_renewal(app):
    """The disagreement this replaced. A trial converting on the 20th is charged eight
    days before the renewal on the 28th, and the card — computed from the payer's cycle,
    which knows nothing about conversions — named the 28th as the next payment."""
    from billing.services import entity_modules as modules

    panel = _panel(
        _row("Petty Cash converts", "20 Aug 2026"),
        _row("Renewal", "28 Aug 2026"),
    )

    assert modules.next_payment_from_panel(panel) == "20 Aug 2026"


def test_an_overdue_charge_is_not_named_as_the_next_payment(app):
    """``past_due`` carries a ``paid_through`` already behind us, so the earliest row can
    be a PAST date — the exact thing this card was rewritten to stop showing, and with
    none of the red that makes the panel's own "Renewal — overdue" legible as arrears.
    The next date that is genuinely ahead is named instead."""
    from billing.services import entity_modules as modules

    panel = _panel(
        _row("Renewal", "28 Jul 2026", overdue=True),
        _row("Petty Cash converts", "20 Aug 2026"),
    )

    assert modules.next_payment_from_panel(panel) == "20 Aug 2026"


def test_nothing_scheduled_falls_through_to_the_projection(app):
    """A trial that will not convert, or every module cancelled: the panel has no charge
    to point at, and the caller drops to the payer's projected cycle."""
    from billing.services import entity_modules as modules

    assert modules.next_payment_from_panel(_panel()) is None
    assert modules.next_payment_from_panel(None) is None
    # An overdue-only panel is "nothing ahead", not "the overdue date".
    assert modules.next_payment_from_panel(
        _panel(_row("Renewal", "28 Jul 2026", overdue=True))
    ) is None


def test_a_row_with_no_date_is_skipped_not_returned_blank(app):
    """An extension on a module whose period end never made it onto the card sorts last
    and carries no date. Returning it would blank the card."""
    from billing.services import entity_modules as modules

    panel = _panel(_row("Petty Cash cancellation", None),
                   _row("Renewal", "28 Aug 2026"))

    assert modules.next_payment_from_panel(panel) == "28 Aug 2026"


def test_a_broken_projection_does_not_cost_the_page(app, monkeypatch):
    """A naive anchor — the one shape ``period_containing`` refuses — must degrade to
    "To Be Decided" rather than 500 the settings page. The panel below the card carries
    the same dates per module."""
    modules = _wire(
        app, monkeypatch,
        anchor=datetime(2026, 7, 28, 13),  # no tzinfo
        now=datetime(2026, 8, 13, 9, tzinfo=UTC),
    )

    assert modules.get_next_payment_date("e1") is None
