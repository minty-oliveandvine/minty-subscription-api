"""Money is scaled by the CURRENCY, not by a hardcoded 100.

The bug these pin: three places decided how many minor units make a major unit, and
only one of them asked ``currency_info``. ``entity.services.modules`` read
``decimal_places`` correctly; ``billing._money`` (the invoice memo) and the
first-charge confirm dialog both divided by 100. On HKD — a two-decimal currency —
all three agreed by luck, so the split was invisible. On a zero-decimal currency the
module card said 280 while the memo explaining that same charge said 2.80, and the
dialog asking the customer to authorise it said 2.80 too.

The HKD cases below are therefore NOT the interesting ones: they only prove nothing
regressed. The zero- and three-decimal cases are what would have failed before.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from billing.services import money
from billing.services.billing import Period, change_memo, join_memo


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


PERIOD = Period(dt(2027, 2, 1), dt(2027, 3, 1))


class _FakeCurrency:
    def __init__(self, decimal_places, symbol=None):
        self.decimal_places = decimal_places
        self.symbol = symbol


class _FakeQuery:
    """Stands in for ``CurrencyInfo.objects`` — the test database is SQLite, where the
    ``pettycashv3`` schema the real model points at is not materialised."""

    def __init__(self, table):
        self._table = table
        self._wanted = None

    def filter(self, currency_code=None):
        self._wanted = currency_code
        return self

    def first(self):
        return self._table.get(self._wanted)


@pytest.fixture
def currencies(monkeypatch):
    """Patch ``currency_info`` with a known table. Returns the dict so a test can
    add to it — key is the UPPER-CASED code, as ``decimal_places`` normalises."""
    import minty_db.models

    table = {
        "HKD": _FakeCurrency(2, "HK$"),   # a GLYPH: not spaced off the amount
        "JPY": _FakeCurrency(0),          # zero-decimal, and no symbol recorded
        "KWD": _FakeCurrency(3),          # three-decimal
    }

    class _FakeModel:
        objects = _FakeQuery(table)

    monkeypatch.setattr(minty_db.models, "CurrencyInfo", _FakeModel)
    return table


# --- the lookup ------------------------------------------------------------------


def test_decimal_places_comes_from_currency_info(app, currencies):
    with app.app_context():
        assert money.decimal_places("HKD") == 2
        assert money.decimal_places("JPY") == 0
        assert money.decimal_places("KWD") == 3


def test_the_code_is_normalised_before_lookup(app, currencies):
    """``billing_plan.currency`` is upper-case but callers pass Stripe's lower-case
    form too — ``changes.build_change`` lower-cases it before it reaches the invoice."""
    with app.app_context():
        assert money.decimal_places("jpy") == 0
        assert money.decimal_places(" jpy ") == 0


def test_an_unknown_currency_falls_back_to_two_rather_than_raising(app, currencies):
    """A currency missing from the table is a data fault, but money still has to
    render — a settings page must not 500 because a seed row is absent."""
    with app.app_context():
        assert money.decimal_places("ZZZ") == money.FALLBACK_DECIMAL_PLACES
        assert money.decimal_places(None) == money.FALLBACK_DECIMAL_PLACES
        assert money.decimal_places("") == money.FALLBACK_DECIMAL_PLACES


def test_the_lookup_is_cached_per_request(app, currencies):
    """One tiny table, read once per request. A renewal prices many entities and would
    otherwise re-read it per line."""
    with app.app_context():
        assert money.decimal_places("HKD") == 2
        # Change the table underneath: a second read inside the SAME context must not
        # see it, which is only true if the first answer was cached.
        currencies["HKD"] = _FakeCurrency(3)
        assert money.decimal_places("HKD") == 2
    # A new context is a new request, so it picks the change up.
    with app.app_context():
        assert money.decimal_places("HKD") == 3


# --- conversion and formatting ---------------------------------------------------


def test_minor_units_scale_by_the_currency(app, currencies):
    """ONE amount, three currencies, three different major values — the whole point.
    A hardcoded /100 gives 280 for all three.

    Compared numerically, not by ``str``: Decimal division normalises the exponent
    (``Decimal(28000) / 100`` is ``280``, not ``280.00``), which is also exactly what
    the ``_normalize_price_amount`` this replaced returned. Scale belongs to the
    FORMATTER — see ``format_minor``."""
    with app.app_context():
        assert money.to_major(28000, "HKD") == Decimal("280")
        assert money.to_major(28000, "JPY") == Decimal("28000")
        assert money.to_major(28000, "KWD") == Decimal("28")


def test_format_minor_renders_the_currencys_own_precision(app, currencies):
    with app.app_context():
        assert money.format_minor(28000, "HKD") == "280.00"
        # THE REGRESSION: /100 would have rendered this "2.80".
        assert money.format_minor(280, "JPY") == "280"
        assert money.format_minor(280000, "KWD") == "280.000"


def test_format_minor_groups_thousands_and_drops_the_sign(app, currencies):
    """The memo states credits in words ("credited 65.33"), so the sign is carried by
    the sentence rather than the number."""
    with app.app_context():
        assert money.format_minor(-1234567, "HKD") == "12,345.67"
        assert money.format_minor(1234567, "JPY") == "1,234,567"


# --- the memo, which is where the bug was visible to customers -------------------


def test_a_join_memo_quotes_the_amount_in_the_invoices_currency():
    """Pure: ``billing`` takes ``places`` as a parameter rather than reading a table,
    which is what keeps the arithmetic testable without a database."""
    hkd = join_memo("Entity A", "Super Minty", 40000, PERIOD, dt(2027, 2, 1), places=2)
    assert "400.00" in hkd

    # Same integer, zero-decimal currency: 40000 yen, not 400.00 of anything.
    jpy = join_memo("Entity A", "Super Minty", 40000, PERIOD, dt(2027, 2, 1), places=0)
    assert "40,000" in jpy
    assert "400.00" not in jpy


def test_a_change_memo_scales_every_figure_it_quotes():
    """Credit, charge AND net — a memo that scaled only some of them would not add up."""
    memo = change_memo(
        "Entity B", "Payment Request", "Super Minty",
        -6533, 9333, PERIOD, dt(2027, 2, 8), places=0,
    )
    assert "6,533" in memo
    assert "9,333" in memo
    assert "2,800" in memo          # the net
    assert "65.33" not in memo


def test_the_default_stays_two_places_so_existing_callers_are_unaffected():
    """``places`` is keyword-only with a default: the oracle-figure tests in
    test_billing_engine call these positionally and must keep passing."""
    assert "400.00" in join_memo("E", "P", 40000, PERIOD, dt(2027, 2, 1))


# --- the label, and how it joins the amount ----------------------------------------
#
# The second half of the same split. ``entity.services.modules`` and the payer portal
# each resolved the label themselves and each applied the spacing from its own copy. One
# rule now (owner, 2026-10-21): the ISO code, a space, the currency's places - never the
# glyph ``currency_info`` records ("HK$"), never a trimmed whole amount.


def test_the_label_is_the_code_even_where_a_glyph_is_recorded(app, currencies):
    with app.app_context():
        assert money.symbol("HKD") == "HKD"


def test_the_label_is_the_code_for_any_currency_and_empty_for_none(app, currencies):
    with app.app_context():
        assert money.symbol("JPY") == "JPY"
        assert money.symbol("ZZZ") == "ZZZ"
        assert money.symbol(None) == ""
        assert money.symbol("") == ""


def test_the_label_is_trimmed_and_upper_cased(app, currencies):
    with app.app_context():
        assert money.symbol("hkd") == "HKD"
        assert money.symbol(" hkd ") == "HKD"


def test_the_code_is_spaced_off_the_amount_and_no_code_leaves_it_alone(app, currencies):
    """"HKD400" scans as a single token rather than a currency and an amount."""
    assert money.join("HKD", "400.00") == "HKD\u00a0400.00"
    assert money.join("", "400.00") == "400.00"


def test_format_with_symbol_uses_the_currencys_own_places(app, currencies):
    """Code, spacing and precision together - what the payer portal prints."""
    with app.app_context():
        assert money.format_with_symbol(40000, "HKD") == "HKD\u00a0400.00"
        assert money.format_with_symbol(280, "JPY") == "JPY\u00a0280"
        assert money.format_with_symbol(400000, "KWD") == "KWD\u00a0400.000"


def test_format_major_always_shows_the_places(app, currencies):
    """The cards, the panel and the invoice document: "HKD 400.00", once "HK$400"."""
    assert money.format_major("HKD", Decimal("400"), 2) == "HKD\u00a0400.00"
    assert money.format_major("HKD", Decimal("400.5"), 2) == "HKD\u00a0400.50"
    assert money.format_major("HKD", Decimal("1234567.89"), 2) == "HKD\u00a01,234,567.89"
    assert money.format_major("JPY", Decimal("1200"), 0) == "JPY\u00a01,200"
    assert money.format_major("KWD", Decimal("1.5"), 3) == "KWD\u00a01.500"
    assert money.format_major("", Decimal("5"), 2) == "5.00"


def test_format_price_drops_only_a_trailing_zero_cents(app, currencies):
    """A price on a screen (the panel, the cards): "HKD 400", but "HKD 400.50" keeps its
    cents (owner, 2026-10-21). Invoices keep the places - :func:`format_major`."""
    assert money.format_price("HKD", Decimal("400"), 2) == "HKD\u00a0400"
    assert money.format_price("HKD", Decimal("400.00"), 2) == "HKD\u00a0400"
    assert money.format_price("HKD", Decimal("400.5"), 2) == "HKD\u00a0400.50"
    assert money.format_price("HKD", Decimal("1500"), 2) == "HKD\u00a01,500"
    assert money.format_price("JPY", Decimal("1200"), 0) == "JPY\u00a01,200"
    assert money.format_price("KWD", Decimal("1.5"), 3) == "KWD\u00a01.500"
    assert money.format_price("", Decimal("5"), 2) == "5"

