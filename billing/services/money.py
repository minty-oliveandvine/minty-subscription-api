"""Minor-unit money helpers, keyed off ``currency_info``.

ONE place decides how many decimal places a currency has. It used to be decided in
three, and they disagreed: ``entity.services.modules`` read ``currency_info.decimal_places``
correctly, while the invoice memo (``billing._money``) and the first-charge confirm
dialog both divided by a hardcoded 100. On HKD the three agree by luck — it is a
two-decimal currency — so the split went unnoticed. On a zero-decimal currency the
module card would read 280 while the memo explaining that very charge read 2.80, and
the dialog asking the customer to authorise it read 2.80 as well.

The LABEL half was split the same way and is now here too. Every amount is written with
the ISO code, never a symbol (owner, 2026-10-21): "HKD 1,234.50" - the code, a space, the
currency's own places - on every card, panel, portal row and invoice, as the apps write it
(``@minty/shared/money``). ``currency_info.symbol`` ("HK$") is no longer read for display.

Deliberately NOT in ``billing.py``. That module is pure arithmetic — no ORM, no clock,
no Stripe — which is what makes the money rules testable without a database. So it takes
``decimal_places`` as a plain parameter and this module is what resolves one.

The lookup is cached on the request scope (``billing.services._context``), like
``clock.database_now``: per-request, thread-safe, and enough to keep a renewal that
prices many entities from re-reading one tiny table per line.
"""
from __future__ import annotations

from decimal import Decimal

from billing.services import _context
from billing.services._log import logger

# Used when the currency is unknown or unreadable. Two is right for almost every
# currency in circulation, so it keeps money rendering rather than blowing up a page —
# but it is a GUESS, so it logs. A currency missing from ``currency_info`` is a data
# problem to fix, not a condition to absorb silently.
FALLBACK_DECIMAL_PLACES = 2

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
        from minty_db.models import CurrencyInfo

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
    """The label in front of an amount: the currency's ISO code, upper-cased ("HKD").

    "" when there is no currency at all. Never a symbol from ``currency_info`` - every amount
    is written with its code (owner, 2026-10-21) - so the name stays only because the API's
    ``currency_symbol`` / ``currency`` fields carry it.
    """
    if not currency_code:
        return ""
    return str(currency_code).strip().upper()


def join(currency_symbol: str, text: str) -> str:
    """Put the code in front of an already-formatted amount: "HKD 400.00".

    Always one NON-BREAKING space (owner, 2026-10-21), so a line never splits the code from its
    amount - "HKD400" scans as one token rather than a currency and an amount. With
    no code the amount stands alone (never a "$").
    """
    return f"{currency_symbol}\u00a0{text}" if currency_symbol else text


def to_major(amount_minor, currency_code: str | None) -> Decimal:
    """Minor units to a major-unit Decimal. HKD 28000 -> 280.00; JPY 280 -> 280."""
    if amount_minor is None:
        return Decimal("0")
    return Decimal(int(amount_minor)) / (Decimal(10) ** decimal_places(currency_code))


def format_minor(amount_minor, currency_code: str | None) -> str:
    """Minor units as a plain grouped decimal — no symbol, no currency code.

    For the invoice memo and the confirm dialog, both of which state the currency
    separately. Repeating it here invites the two disagreeing.
    """
    places = decimal_places(currency_code)
    value = abs(Decimal(int(amount_minor or 0))) / (Decimal(10) ** places)
    return f"{value:,.{places}f}"


def format_with_symbol(amount_minor, currency_code: str | None) -> str:
    """Minor units as "HKD 400.00" -- the code, a space, the currency's own places."""
    return join(symbol(currency_code), format_minor(amount_minor, currency_code))


def format_major(currency_symbol: str, amount_major, places: int = FALLBACK_DECIMAL_PLACES) -> str:
    """MAJOR units as "HKD 400.00" / "HKD 400.50" / "JPY 1,200" -- always the currency's places.

    The invoice document prints money this way: a document of a charge always shows its
    places, the same as :func:`format_with_symbol` (the payer portal) and the apps'
    ``formatMoney``. A PRICE on a screen is :func:`format_price` instead.

    Takes ``amount_major`` and ``places`` rather than minor units and a currency code:
    its callers have already scaled through :func:`to_major` and resolved
    :func:`decimal_places`, often for several amounts at once. ``places`` was once fixed
    at 2, which rounds a three-decimal currency wrong and invents a ".00" on a
    zero-decimal one.
    """
    if places <= 0:
        return join(currency_symbol, f"{int(Decimal(amount_major).to_integral_value()):,}")
    quantized = Decimal(amount_major).quantize(Decimal(1).scaleb(-places))
    return join(currency_symbol, f"{quantized:,.{places}f}")


def format_price(currency_symbol: str, amount_major, places: int = FALLBACK_DECIMAL_PLACES) -> str:
    """A PRICE as the settings page, the module cards and the panel show it: "HKD 400" when
    whole, "HKD 400.50" when not (owner, 2026-10-21) - cents only when they mean something,
    because a column of "HKD 400.00" reads as noise where every price is whole. The apps'
    ``formatPrice`` is the same rule. Invoices keep the places (:func:`format_major`).
    """
    if places > 0:
        quantized = Decimal(amount_major).quantize(Decimal(1).scaleb(-places))
        if quantized == quantized.to_integral_value():
            return join(currency_symbol, f"{int(quantized):,}")
    return format_major(currency_symbol, amount_major, places)

