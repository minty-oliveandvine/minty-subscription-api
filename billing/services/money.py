"""Minor-unit money helpers, keyed off ``currency_info``.

ONE place decides how many decimal places a currency has. It used to be decided in
three, and they disagreed: ``entity.services.modules`` read ``currency_info.decimal_places``
correctly, while the invoice memo (``billing._money``) and the first-charge confirm
dialog both divided by a hardcoded 100. On HKD the three agree by luck — it is a
two-decimal currency — so the split went unnoticed. On a zero-decimal currency the
module card would read 280 while the memo explaining that very charge read 2.80, and
the dialog asking the customer to authorise it read 2.80 as well.

How an amount is WRITTEN is every backend's rule, in ``minty_shared.money``: "HKD 1,234.50" -
the code, a non-breaking space, the currency's own places - as the apps write it
(``@minty/shared/money``). This module is the half that is this service's own: resolving the
places from ``currency_info``.

Deliberately NOT in ``billing.py``. That module is pure arithmetic — no ORM, no clock,
no Stripe — which is what makes the money rules testable without a database. So it takes
``decimal_places`` as a plain parameter and this module is what resolves one.

The lookup is cached on the request scope (``billing.services._context``), like
``clock.database_now``: per-request, thread-safe, and enough to keep a renewal that
prices many entities from re-reading one tiny table per line.
"""
from __future__ import annotations

from decimal import Decimal

from minty_shared import money as shared_money

from billing.services import _context
from billing.services._log import logger

# Used when the currency is unknown or unreadable. Two is right for almost every
# currency in circulation, so it keeps money rendering rather than blowing up a page —
# but it is a GUESS, so it logs. A currency missing from ``currency_info`` is a data
# problem to fix, not a condition to absorb silently.
FALLBACK_DECIMAL_PLACES = shared_money.FALLBACK_DECIMAL_PLACES

_G_KEY = "_subscription_decimal_places"


def decimal_places(currency_code: str | None) -> int:
    """How many minor units make one major unit, from ``currency_info``.

    Falls back to :data:`FALLBACK_DECIMAL_PLACES` (loudly) when the code is missing,
    unknown, or the table cannot be read. Never raises: a currency lookup must not be
    the thing that breaks an invoice or a settings page.
    """
    if not currency_code:
        logger.warning("money: no currency code given; assuming 2 decimal places")
        return FALLBACK_DECIMAL_PLACES

    code = str(currency_code).strip().upper()
    cache = _context.get(_G_KEY)
    if cache is None:
        cache = {}
        _context.set(_G_KEY, cache)
    if code in cache:
        return cache[code]

    places = FALLBACK_DECIMAL_PLACES
    try:
        # Imported here, not at module scope: the subscription services are imported by
        # the Stripe client, and pulling the model layer in at import time drags the
        # whole entity model graph along with it (same reason as ``clock.database_now``).
        from shared_models.models import CurrencyInfo

        row = CurrencyInfo.objects.filter(currency_code=code).first()
        if row is None:
            logger.warning(
                "money: {} is not in currency_info; assuming {} decimal places",
                code, FALLBACK_DECIMAL_PLACES,
            )
        elif row.decimal_places is not None:
            places = int(row.decimal_places)
    except Exception:
        logger.exception(
            "money: could not read currency_info for {}; assuming {} decimal places",
            code, FALLBACK_DECIMAL_PLACES,
        )

    cache[code] = places
    return places


def symbol(currency_code: str | None) -> str:
    """The label in front of an amount: the currency's ISO code, upper-cased ("HKD"), "" with
    no currency. Never a symbol from ``currency_info`` (owner, 2026-10-21); the name stays
    because the API's ``currency_symbol`` / ``currency`` fields carry it."""
    return shared_money.label(currency_code)


def join(currency_symbol: str, text: str) -> str:
    """The code, ONE non-breaking space, an already-formatted amount (``minty_shared.money``)."""
    return shared_money.join(currency_symbol, text)


def to_major(amount_minor, currency_code: str | None) -> Decimal:
    """Minor units to a major-unit Decimal. HKD 28000 -> 280.00; JPY 280 -> 280."""
    return shared_money.to_major(amount_minor, decimal_places(currency_code))


def format_minor(amount_minor, currency_code: str | None) -> str:
    """Minor units as a plain grouped decimal - no code. For the invoice memo and the confirm
    dialog, both of which state the currency separately."""
    return shared_money.format_minor(amount_minor, decimal_places(currency_code))


def format_with_symbol(amount_minor, currency_code: str | None) -> str:
    """Minor units as "HKD 400.00" -- the code, a space, the currency's own places."""
    return join(symbol(currency_code), format_minor(amount_minor, currency_code))


def format_major(currency_symbol: str, amount_major, places: int = FALLBACK_DECIMAL_PLACES) -> str:
    """MAJOR units as "HKD 400.00" / "HKD 400.50" / "JPY 1,200" - always the currency's places
    (the invoice document, as the payer portal and the apps' ``formatMoney``)."""
    return shared_money.format_major(currency_symbol, amount_major, places)


def format_price(currency_symbol: str, amount_major, places: int = FALLBACK_DECIMAL_PLACES) -> str:
    """A PRICE: "HKD 400" when whole, "HKD 400.50" when not (owner, 2026-10-21). Invoices
    keep the places (:func:`format_major`)."""
    return shared_money.format_price(currency_symbol, amount_major, places)
