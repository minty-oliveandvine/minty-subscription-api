"""The panel's "charged that day" forecast equals what the conversion actually bills.

A trial converting beside modules already being paid for is a mid-period CHANGE: the
payer's cycle is running, so the remainder of it is billed on the spot (credit the old
price, charge the new). The panel used to quote only "/mo", so that charge landed with no
warning anywhere on the page.

``modules._forecast_conversion_charges`` forecasts it by calling the SAME
``changes.build_change`` that ``checkout._bill_module_change_in_house`` bills from, and
the tests below pin that they agree.

SEQUENTIAL, which is the reason this takes every card rather than one code. Trials that
end on different days convert on different days, and the FIRST to land pins the payer's
anchor and is billed in full; every later one is prorated against the cycle that first one
started. Forecasting each trial independently against today's state answers 0 for all of
them — there is no anchor yet — which is exactly the bug
``test_a_later_trial_is_prorated_against_the_cycle_the_first_one_starts`` guards.

The zero cases each matter for a different reason, and all are quiet on the page: the
conversion that starts the cycle, a downgrade, an unpriceable combination, and a trial
that will not convert at all.
"""
from __future__ import annotations

from datetime import UTC, datetime

from billing.services.billing import plan_code

# Anchor pinned so the proration is reproducible: a 30-day period, conversion 2 days in.
_ANCHOR = datetime(2026, 11, 8, 13, tzinfo=UTC)
_CONVERTS_AT = datetime(2026, 11, 10, 13, tzinfo=UTC)
_LATER = datetime(2026, 11, 24, 13, tzinfo=UTC)


class _Plan:
    def __init__(self, name, amount, currency="HKD"):
        self.display_name = name
        self.amount = amount
        self.currency = currency


PLANS = {
    "BILL": _Plan("Payment Request", 28000),
    "PETTY_CASH": _Plan("Petty Cash", 28000),
    "BILL+PETTY_CASH": _Plan("Super Minty", 40000),
}


def _card(code, *, at=_CONVERTS_AT, needs_card=False, pending_cancel=False,
          status="trialing"):
    """The subset of a module card the forecast reads."""
    return {
        "code": code,
        "subscription_status": status,
        "needs_card": needs_card,
        "pending_cancel": pending_cancel,
        "period_end": at,
    }


def _wire(app, monkeypatch, *, anchor=_ANCHOR, plans=None):
    """Point the change builder at a fixed catalog and the payer at a fixed anchor.

    Takes ``app`` purely to force the conftest fixture: importing
    ``blueprints.entity.services.modules`` cold trips the model graph's circular import,
    and creating the app is what resolves it.
    """
    from billing.services import entity_modules as modules
    from billing.services import store

    catalog = PLANS if plans is None else plans
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        # keyed by plan words (BILL), asked with module codes (PAYMENT_REQUEST): plan_code maps
        lambda codes: catalog.get(plan_code(codes)),
    )
    monkeypatch.setattr(
        store, "billing_cycle_for_user", lambda uid: (anchor, "HKD")
    )
    return modules


def test_forecast_is_the_upgrade_net_not_the_new_price(app, monkeypatch):
    """112.00 for 28 of 30 days: charge the bundle 373.33, credit Petty Cash 261.33.

    Both halves are kept by ``change_invoice`` rather than the net alone, and the net is
    what the customer's card is hit for. Quoting the 400 bundle price here — or the
    373.33 charge line without its credit — would overstate it by the credit.
    """
    modules = _wire(app, monkeypatch)

    out = modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST")]
    )

    assert out == {"PAYMENT_REQUEST": 37333 - 26133 == 11200 and 11200}


def test_forecast_equals_what_build_change_bills(app, monkeypatch):
    """The property, not the number: forecast and invoice come from one calculation.

    ``checkout._bill_module_change_in_house`` bills ``build_change`` over the period
    containing the conversion. If these ever diverge the panel is quoting a figure the
    customer is not charged.
    """
    modules = _wire(app, monkeypatch)
    from billing.services import changes
    from billing.services.billing import period_containing

    period = period_containing(_ANCHOR, _CONVERTS_AT)
    invoice = changes.build_change(
        "e1", "Alpha Co", {"PETTY_CASH"}, {"PETTY_CASH", "PAYMENT_REQUEST"}, period, _CONVERTS_AT
    )

    out = modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST")]
    )

    assert out["PAYMENT_REQUEST"] == invoice.total


def test_every_figure_comes_from_build_change(app, monkeypatch):
    """There is ONE calculation, and this pins it.

    ``checkout._bill_module_change_in_house`` bills through ``changes.issue_change``,
    which is ``build_change`` plus collection. The forecast calls ``build_change``
    directly. Neither branch of this function may reach a figure any other way — the
    anchoring conversion used to short-cut to ``billing_plan_for_codes(...).amount``,
    which agrees on the ordinary path and silently disagrees the moment the entity is
    already paying for something.

    Stubbing build_change to a sentinel proves nothing else is doing arithmetic here.
    """
    modules = _wire(app, monkeypatch, anchor=None)
    from billing.services import changes

    class _Sentinel:
        total = 999

    monkeypatch.setattr(changes, "build_change", lambda *a, **k: _Sentinel())

    out = modules._forecast_conversion_charges(
        "e1", "u1", set(),
        [_card("PETTY_CASH", at=_CONVERTS_AT), _card("PAYMENT_REQUEST", at=_LATER)],
    )

    # Both the anchoring conversion AND the later one, or one of them is doing its own maths.
    assert out == {"PETTY_CASH": 999, "PAYMENT_REQUEST": 999}


def test_forecast_prorates_against_the_period_the_conversion_LANDS_in(app, monkeypatch):
    """A trial ending next cycle prorates against THAT period, not today's.

    Reading the period from "now" would quote the wrong slice for any trial that
    outlives the current cycle — which is most of them on a 30-day trial. Checked
    against build_change over the landing period rather than by comparing the two
    figures: calendar periods differ in length, so "same offset" is NOT "same slice".
    """
    modules = _wire(app, monkeypatch)
    from billing.services import changes
    from billing.services.billing import period_containing

    next_cycle = datetime(2026, 12, 10, 13, tzinfo=UTC)  # one period on
    landing = period_containing(_ANCHOR, next_cycle)
    assert landing.start > _ANCHOR, "should be a LATER period, not the anchor's own"

    expected = changes.build_change(
        "e1", "Alpha Co", {"PETTY_CASH"}, {"PETTY_CASH", "PAYMENT_REQUEST"}, landing, next_cycle
    )

    out = modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST", at=next_cycle)]
    )

    assert out["PAYMENT_REQUEST"] == expected.total


# --- the sequence ---------------------------------------------------------------


def test_a_later_trial_is_prorated_against_the_cycle_the_first_one_starts(app, monkeypatch):
    """THE case this function exists for: two trials, two dates, no anchor yet.

    The first conversion anchors the payer and is charged a full period. The second lands
    mid-period and pays only the days left in it. Computing them independently reports the
    monthly rate for both, because at render time there is no anchor and nothing billed —
    and the second module's real, smaller charge is then never shown.
    """
    modules = _wire(app, monkeypatch, anchor=None)
    from billing.services import changes
    from billing.services.billing import period_containing

    out = modules._forecast_conversion_charges(
        "e1", "u1", set(),
        [_card("PAYMENT_REQUEST", at=_LATER), _card("PETTY_CASH", at=_CONVERTS_AT)],
    )

    # Petty Cash converts first, so it starts the cycle: a full period at its own price.
    assert out["PETTY_CASH"] == 28000
    # Payment Request then joins a cycle anchored on Petty Cash's conversion date, and
    # pays only the remainder — strictly less than a month of it.
    expected = changes.build_change(
        "e1", "e1", {"PETTY_CASH"}, {"PETTY_CASH", "PAYMENT_REQUEST"},
        period_containing(_CONVERTS_AT, _LATER), _LATER,
    )
    assert out["PAYMENT_REQUEST"] == expected.total
    assert 0 < out["PAYMENT_REQUEST"] < 28000, "a part-period must cost less than a whole one"


def test_order_is_by_date_not_by_card_order(app, monkeypatch):
    """The earliest conversion anchors, whichever module it happens to be.

    Sorted on the raw datetime — canonical card order would anchor on whichever module
    the catalog lists first, which is not the one that converts first.
    """
    modules = _wire(app, monkeypatch, anchor=None)

    out = modules._forecast_conversion_charges(
        "e1", "u1", set(),
        [_card("PETTY_CASH", at=_LATER), _card("PAYMENT_REQUEST", at=_CONVERTS_AT)],
    )

    assert out["PAYMENT_REQUEST"] == 28000, "BILL converts first, so BILL anchors at a full period"
    assert 0 < out["PETTY_CASH"] < 28000


def test_two_trials_converting_the_same_day_total_the_bundle(app, monkeypatch):
    """Started together, they end together — and that day costs the bundle price.

    The second is still a mid-period change, but against a period that started the same
    instant, so it charges the whole upgrade: 280 + 120 = the 400 bundle. Anything else
    would either double-charge or hand the bundle over for one module's price.
    """
    modules = _wire(app, monkeypatch, anchor=None)

    out = modules._forecast_conversion_charges(
        "e1", "u1", set(),
        [_card("PETTY_CASH", at=_CONVERTS_AT), _card("PAYMENT_REQUEST", at=_CONVERTS_AT)],
    )

    assert out["PETTY_CASH"] + out["PAYMENT_REQUEST"] == 40000


def test_a_trial_that_will_not_convert_is_skipped_and_does_not_anchor(app, monkeypatch):
    """One that expires charges nothing, so it must neither be quoted nor start a cycle.

    If it anchored, the trial that DOES convert would be forecast as a mid-period
    top-up against a cycle that never came into existence.
    """
    modules = _wire(app, monkeypatch, anchor=None)

    out = modules._forecast_conversion_charges(
        "e1", "u1", set(),
        [
            _card("PETTY_CASH", at=_CONVERTS_AT, needs_card=True),  # expires
            _card("PAYMENT_REQUEST", at=_LATER),                               # converts
        ],
    )

    assert "PETTY_CASH" not in out
    assert out["PAYMENT_REQUEST"] == 28000, "BILL is the first real conversion, so it anchors"


# --- the quiet cases -----------------------------------------------------------


def test_the_conversion_that_starts_the_cycle_is_charged_a_full_period(app, monkeypatch):
    """No anchor: the cycle starts here, so there is no earlier period to prorate against
    and the whole period is billed — arrived at through ``build_change`` like every other
    figure here, by anchoring on the conversion so a full period is left to bill.

    ``billed_now`` is empty deliberately: no anchor means nothing has ever been charged
    for this payer, so a module cannot already be billing forward. Pairing anchor=None
    with a billed module describes a state the writers cannot produce, and asserting on
    it would pin this function to something the real conversion never sees.
    """
    modules = _wire(app, monkeypatch, anchor=None)

    out = modules._forecast_conversion_charges("e1", "u1", set(), [_card("PAYMENT_REQUEST")])

    assert out == {"PAYMENT_REQUEST": 28000}


def test_an_entity_joining_an_existing_payer_IS_prorated(app, monkeypatch):
    """Nothing billed for THIS entity, but the payer's cycle is already running.

    This is the 373.33 join oracle, and it is a real charge. The forecast used to return
    0 whenever the entity had nothing billed yet, which silently dropped it — but
    ``checkout._bill_module_change_in_house`` is explicit that "a later entity joining an
    existing payer is prorated against the anchor already recorded". The guard belongs on
    the ANCHOR, not on what this entity happens to be paying.
    """
    modules = _wire(app, monkeypatch)
    from billing.services import changes
    from billing.services.billing import period_containing

    out = modules._forecast_conversion_charges("e1", "u1", set(), [_card("PAYMENT_REQUEST")])

    expected = changes.build_change(
        "e1", "e1", set(), {"PAYMENT_REQUEST"},
        period_containing(_ANCHOR, _CONVERTS_AT), _CONVERTS_AT,
    )
    assert out["PAYMENT_REQUEST"] == expected.total > 0


def test_a_downgrade_forecasts_nothing(app, monkeypatch):
    """build_change refuses to credit unused time the customer still gets, so there is
    nothing to collect and nothing to warn about."""
    cheaper = {
        "BILL": _Plan("Payment Request", 28000),
        "PETTY_CASH": _Plan("Petty Cash", 28000),
        "BILL+PETTY_CASH": _Plan("Super Minty", 20000),  # cheaper than either alone
    }
    modules = _wire(app, monkeypatch, plans=cheaper)

    out = modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST")]
    )

    assert out == {"PAYMENT_REQUEST": 0}


def test_an_unpriceable_combination_forecasts_nothing(app, monkeypatch):
    """A half-seeded catalog must not make the page invent a figure."""
    modules = _wire(app, monkeypatch, plans={"PETTY_CASH": _Plan("Petty Cash", 28000)})

    out = modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST")]
    )

    assert out == {"PAYMENT_REQUEST": 0}


def test_no_payer_or_no_trials_forecasts_nothing(app, monkeypatch):
    modules = _wire(app, monkeypatch)

    assert modules._forecast_conversion_charges(
        "e1", None, {"PETTY_CASH"}, [_card("PAYMENT_REQUEST")]
    ) == {}
    # A paid module is not a conversion, and a trial with no end date cannot be placed.
    assert modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST", status="active")]
    ) == {}
    assert modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST", at=None)]
    ) == {}


def test_a_broken_lookup_forecasts_nothing_rather_than_failing(app, monkeypatch):
    """A forecast must never cost anyone the settings page."""
    modules = _wire(app, monkeypatch)
    from billing.services import store

    def _boom(_uid):
        raise RuntimeError("billing cycle unavailable")

    monkeypatch.setattr(store, "billing_cycle_for_user", _boom)

    assert modules._forecast_conversion_charges(
        "e1", "u1", {"PETTY_CASH"}, [_card("PAYMENT_REQUEST")]
    ) == {}
