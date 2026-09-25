"""Every invoice line records what it PAID FOR: its days and the price per period they were
charged at (``billing.Line.period_start`` / ``period_end`` / ``unit_amount``, stored on
``subscription_invoice_line`` - schema item 23).

What is pinned:

* **Whatever priced a line says what it priced.** A renewal is the whole period at its plan's
  price; a mid-period start, an upgrade and the credit for the plan it replaced run from the
  change to the period's end, each at its own plan's price - the same clamp ``prorate`` uses.
* **An access extension carries the overhang and ITS rate.** From what the company was paid
  through to the module's access end, at the rate the cancellation priced it at - the 120
  step for a module leaving a bundle, not the catalogue's 280.
* **No single rate, no rate.** A window whose rate stepped part-way (a pair ending on different
  days), or a re-derivation that does not reproduce the amount being billed, records the days
  and leaves the rate None rather than a blend.
* **A label never costs a collection.** ``pending_extension_terms`` answers what it knows and
  never raises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
PAID_THROUGH = datetime(2027, 2, 8, 13, tzinfo=UTC)  # the period before it is 31 days
END = datetime(2027, 3, 8, 13, tzinfo=UTC)
FIRST_BILLED = datetime(2026, 12, 8, 13, tzinfo=UTC)


class _Row:
    def __init__(self, code, phase="scheduled_cancel", until=None, amount=None,
                 first_billed_at=FIRST_BILLED):
        self.entity_id = "e1"
        self.function_code = code
        self.payer_user_id = "u1"
        self.phase = phase
        self.app_access_until = until
        self.first_billed_at = first_billed_at
        self.extension_state = "pending" if amount else None
        self.extension_amount = amount


class _Plan:
    def __init__(self, amount, display_name):
        self.amount = amount
        self.display_name = display_name
        self.currency = "HKD"


def _catalog(codes):
    codes = set(codes)
    if len(codes) > 1:
        return _Plan(40000, "Super Minty")
    return _Plan(28000, "Petty Cash" if codes == {"PETTY_CASH"} else "Payment Request")


def _wire(monkeypatch, rows):
    from billing.services import store

    monkeypatch.setattr(store, "module_rows_for_entity", lambda _e: rows)
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: PAID_THROUGH)
    monkeypatch.setattr(store, "billing_cycle_for_user", lambda _u: (ANCHOR, "HKD"))
    monkeypatch.setattr(store, "billing_plan_for_codes", _catalog)


def _priced(checkout, rows, code):
    """What the cancellation recorded for ``code`` - the same arithmetic, at cancel time."""
    from billing.services.billing import Period

    ends = {r.function_code: r.app_access_until for r in rows if r.app_access_until}
    return checkout._segmented_extension(code, ends, Period(ANCHOR, PAID_THROUGH), PAID_THROUGH)


# --- renewals and mid-period changes ----------------------------------------------------


def test_a_renewal_line_is_the_whole_period_at_its_plans_price():
    from billing.services.billing import Period, renewal_invoice

    invoice = renewal_invoice(
        [("e1", "Acme", "Super Minty", 40000)], Period(PAID_THROUGH, END), "hkd"
    )

    line = invoice.lines[0]
    assert (line.period_start, line.period_end, line.unit_amount) == (PAID_THROUGH, END, 40000)
    assert line.amount == 40000


def test_a_mid_period_start_runs_from_the_day_it_started_at_the_full_price():
    from billing.services.billing import Period, join_invoice

    period = Period(PAID_THROUGH, END)
    joined = PAID_THROUGH + timedelta(days=14)

    line = join_invoice("e1", "Acme", "Petty Cash", 28000, period, joined, "hkd").lines[0]

    assert (line.period_start, line.period_end, line.unit_amount) == (joined, END, 28000)
    assert 0 < line.amount < 28000


@pytest.mark.parametrize(
    ("at", "start"),
    [
        (PAID_THROUGH - timedelta(days=3), PAID_THROUGH),  # before the period: all of it
        (END + timedelta(days=3), END),                    # after it: none of it
    ],
)
def test_a_start_outside_the_period_is_held_to_it_as_the_charge_is(at, start):
    from billing.services.billing import Period, join_invoice

    line = join_invoice("e1", "Acme", "Petty Cash", 28000, Period(PAID_THROUGH, END), at,
                        "hkd").lines[0]

    assert line.period_start == start
    assert line.amount == (28000 if start == PAID_THROUGH else 0)


def test_an_upgrade_and_its_credit_cover_the_same_days_each_at_its_own_price():
    from billing.services.billing import Period, change_invoice

    changed = PAID_THROUGH + timedelta(days=10)
    credit, charge = change_invoice(
        "e1", "Acme", "Petty Cash", "Super Minty", 28000, 40000,
        Period(PAID_THROUGH, END), changed, "hkd",
    ).lines

    assert (credit.kind, credit.unit_amount) == ("unused", 28000)
    assert credit.amount < 0 < credit.unit_amount
    assert (charge.kind, charge.unit_amount) == ("remaining", 40000)
    assert (credit.period_start, credit.period_end) == (changed, END)
    assert (charge.period_start, charge.period_end) == (changed, END)


# --- access extensions ----------------------------------------------------------------


def test_a_module_leaving_alone_is_charged_its_list_price_for_the_overhang(monkeypatch):
    from billing.services import checkout

    until = PAID_THROUGH + timedelta(days=10)
    rows = [_Row("PETTY_CASH", until=until), _Row("PAYMENT_REQUEST", phase="active")]
    _wire(monkeypatch, rows)
    rows[0].extension_amount = _priced(checkout, rows, "PETTY_CASH")

    assert checkout.pending_extension_terms(rows[0]) == (PAID_THROUGH, until, 28000)


def test_a_pair_leaving_together_records_each_ones_share_of_the_bundle(monkeypatch):
    """Sorted order: Payment Request takes the 280, Petty Cash the 120 step. Quoting the
    catalogue's 280 beside Petty Cash's charge would make a row that does not add up."""
    from billing.services import checkout

    until = PAID_THROUGH + timedelta(days=10)
    rows = [_Row("PETTY_CASH", until=until), _Row("PAYMENT_REQUEST", until=until)]
    _wire(monkeypatch, rows)
    for row in rows:
        row.extension_amount = _priced(checkout, rows, row.function_code)

    assert checkout.pending_extension_terms(rows[0]) == (PAID_THROUGH, until, 12000)
    assert checkout.pending_extension_terms(rows[1]) == (PAID_THROUGH, until, 28000)


def test_an_overhang_priced_at_two_rates_records_its_days_and_no_rate(monkeypatch):
    """Payment Request stops first, so Petty Cash's days are the 120 step while the pair
    lasts and its own 280 after. There is no single rate to record - and a blend would
    claim a price nobody set."""
    from billing.services import checkout
    from billing.services.billing import Period

    first, last = PAID_THROUGH + timedelta(days=5), PAID_THROUGH + timedelta(days=12)
    rows = [_Row("PETTY_CASH", until=last), _Row("PAYMENT_REQUEST", until=first)]
    _wire(monkeypatch, rows)
    rows[0].extension_amount = _priced(checkout, rows, "PETTY_CASH")

    pieces = checkout._extension_pieces(
        "PETTY_CASH", {"PETTY_CASH": last, "PAYMENT_REQUEST": first},
        Period(ANCHOR, PAID_THROUGH), PAID_THROUGH,
    )
    assert [rate for _s, _e, rate, _c in pieces] == [12000, 28000]
    assert sum(c for *_x, c in pieces) == rows[0].extension_amount
    assert checkout.pending_extension_terms(rows[0]) == (PAID_THROUGH, last, None)


def test_a_rate_that_does_not_reproduce_the_billed_amount_is_not_recorded(monkeypatch):
    """The row holds what will be collected. A re-derivation that disagrees with it - a
    catalogue price changed since the cancellation, say - is evidence of nothing."""
    from billing.services import checkout

    until = PAID_THROUGH + timedelta(days=10)
    rows = [_Row("PETTY_CASH", until=until)]
    _wire(monkeypatch, rows)
    rows[0].extension_amount = _priced(checkout, rows, "PETTY_CASH") + 1

    assert checkout.pending_extension_terms(rows[0]) == (PAID_THROUGH, until, None)


def test_a_leaver_the_sweep_already_closed_still_shares_the_pieces(monkeypatch):
    """A renewal that runs late can find the other leaver already ``cancelled`` (its access
    ran out). It was in the pair the whole time the pieces were priced, so it still counts:
    without it Petty Cash would re-price alone at 280 and fail the check."""
    from billing.services import checkout

    until = PAID_THROUGH + timedelta(days=10)
    rows = [_Row("PETTY_CASH", until=until), _Row("PAYMENT_REQUEST", until=until)]
    _wire(monkeypatch, rows)
    rows[0].extension_amount = _priced(checkout, rows, "PETTY_CASH")
    rows[1].phase = "cancelled"

    assert checkout.pending_extension_terms(rows[0]) == (PAID_THROUGH, until, 12000)


def test_a_trial_that_was_never_paid_for_does_not_share_the_pieces(monkeypatch):
    from billing.services import checkout

    until = PAID_THROUGH + timedelta(days=10)
    rows = [_Row("PETTY_CASH", until=until),
            _Row("PAYMENT_REQUEST", until=until, first_billed_at=None)]
    _wire(monkeypatch, rows)
    rows[0].extension_amount = _priced(checkout, rows[:1], "PETTY_CASH")

    assert checkout.pending_extension_terms(rows[0]) == (PAID_THROUGH, until, 28000)


def test_no_access_end_is_no_terms_and_no_reads(monkeypatch):
    from billing.services import checkout, store

    def boom(*_a, **_k):
        raise AssertionError("nothing to look up without an access end")

    monkeypatch.setattr(store, "paid_through_for_entity", boom)
    assert checkout.pending_extension_terms(_Row("PETTY_CASH", amount=900)) == (None, None, None)


def test_a_failure_is_answered_with_what_is_known_never_raised(monkeypatch):
    from billing.services import checkout, store

    until = PAID_THROUGH + timedelta(days=10)
    row = _Row("PETTY_CASH", until=until, amount=9032)
    _wire(monkeypatch, [row])

    def gone(_e):
        raise RuntimeError("the rows table is gone")

    monkeypatch.setattr(store, "module_rows_for_entity", gone)

    assert checkout.pending_extension_terms(row) == (PAID_THROUGH, until, None)


def test_the_renewal_puts_the_terms_on_the_extension_line(monkeypatch):
    from billing.services import checkout, renewals, store

    until = PAID_THROUGH + timedelta(days=10)
    rows = [_Row("PETTY_CASH", until=until)]
    _wire(monkeypatch, rows)
    rows[0].extension_amount = _priced(checkout, rows, "PETTY_CASH")
    monkeypatch.setattr(store, "pending_extensions_for_payer", lambda _u: rows)

    (line,) = renewals._pending_extension_lines("u1", {"e1": "Acme"})

    assert line.product_name == "Petty Cash (access after cancellation)"
    assert line.amount == rows[0].extension_amount
    assert (line.period_start, line.period_end, line.unit_amount) == (PAID_THROUGH, until, 28000)


# --- what the row records --------------------------------------------------------------


@pytest.mark.django_db
def test_the_invoice_line_row_records_what_each_line_paid_for():
    """``store.reserve_invoice`` writes the three columns as the lines carry them - and a line
    that had no single rate is stored with none, never a blend."""
    from billing.services import store
    from billing.services.billing import Line, Period
    from billing.tests.engine.conftest import make_entity, make_user, seed_currency
    from shared_models.models import SubscriptionInvoiceLine

    currency = seed_currency("HKD")
    payer = make_user("terms@payer.test")
    entity = make_entity(payer, name="Terms Co", currency=currency)
    until = PAID_THROUGH + timedelta(days=21)
    lines = (
        Line(str(entity.id), "Terms Co", "Super Minty", 40000,
             period_start=PAID_THROUGH, period_end=END, unit_amount=40000),
        Line(str(entity.id), "Terms Co", "Petty Cash (access after cancellation)", 8129,
             period_start=PAID_THROUGH, period_end=until),
    )

    record = store.reserve_invoice(
        payer_user_id=payer.id, stripe_customer_id="cus_terms",
        period=Period(PAID_THROUGH, END), currency="hkd", lines=lines,
        idempotency_key="terms-1",
    )

    stored = {
        row.product_name: (row.period_start, row.period_end, row.unit_amount)
        for row in SubscriptionInvoiceLine.objects.filter(invoice_id=record.id)
    }
    assert stored == {
        "Super Minty": (PAID_THROUGH, END, 40000),
        "Petty Cash (access after cancellation)": (PAID_THROUGH, until, None),
    }
