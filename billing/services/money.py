"""Minor-unit money helpers, keyed off ``currency_info``.

ONE place decides how many decimal places a currency has. It used to be decided in
three, and they disagreed: ``entity.services.modules`` read ``currency_info.decimal_places``
correctly, while the invoice memo (``billing._money``) and the first-charge confirm
dialog both divided by a hardcoded 100. On HKD the three agree by luck — it is a
two-decimal currency — so the split went unnoticed. On a zero-decimal currency the
module card would read 280 while the memo explaining that very charge read 2.80, and
the dialog asking the customer to authorise it read 2.80 as well.

The SYMBOL half was split the same way and is now here too. ``entity.services.modules``
looked the code up without upper-casing it and let a database error escape; the payer
portal upper-cased and fell back. Both then applied the same code-vs-glyph spacing rule
from their own copy of it. One currency renders one way now, wherever it is printed.

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
    """The display symbol for a currency, from ``currency_info``.

    Falls back to the upper-cased CODE ("HKD") when no symbol is recorded, and to "" when
    there is no currency at all. Never raises and never leaves a poisoned session behind:
    a symbol lookup must not be the thing that costs the page its amount.

    The code is upper-cased before the lookup. One of the two callers this replaces did
    not, so a lowercase code missed the row and rendered the bare code even where a
    symbol existed.
    """
    if not currency_code:
        return ""
    code = str(currency_code).strip().upper()

    # Imported here for the same reason as in ``decimal_places``.
    from shared_models.models import CurrencyInfo

    try:
        row = CurrencyInfo.objects.filter(currency_code=code).first()
        if row is not None and row.symbol:
            return row.symbol
    except Exception:  # noqa: BLE001 - a missing symbol must not cost the amount
        logger.exception("money: could not read a symbol for {}; using the code", code)
        # (Flask rolled the SQLAlchemy session back here; under Django's autocommit a
        # failed read poisons nothing.)
    return code


def join(currency_symbol: str, text: str) -> str:
    """Put a symbol in front of an already-formatted amount, spaced correctly.

    A CODE is spaced off the number, a GLYPH is not: "HKD 400", but "HK$400". Without
    this the panel read "HKD400", which scans as one token rather than a currency and an
    amount. The same rule the onboarding app's ``money()`` applies.
    """
    space = " " if currency_symbol[-1:].isalpha() else ""
    return f"{currency_symbol}{space}{text}"


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
    """Minor units as "HK$400.00" / "HKD 400.00" -- symbol resolved, never hardcoded.

    Always shows the currency's decimal places. For the surface that trims a whole amount
    to "HK$400" instead, see :func:`format_trimmed`: the two share the symbol and the
    spacing, and differ only in that trailing-zero rule.
    """
    return join(symbol(currency_code), format_minor(amount_minor, currency_code))


def format_trimmed(currency_symbol: str, amount_major, places: int = FALLBACK_DECIMAL_PLACES) -> str:
    """MAJOR units as "HK$400" when whole, "HK$400.50" when not.

    The settings page and the module cards print money this way: cents are shown only
    when they mean something, because a column of "HK$400.00" reads as noise where every
    price is whole. The payer portal's :func:`format_with_symbol` always prints the
    places instead -- that is a real difference in what the two SHOW, not a duplicate,
    which is why both exist here rather than one being folded into the other.

    Takes ``amount_major`` and ``places`` rather than minor units and a currency code:
    its callers have already scaled through :func:`to_major` and resolved
    :func:`decimal_places`, often for several amounts at once. ``places`` was once fixed
    at 2, which rounds a three-decimal currency wrong and invents a ".00" on a
    zero-decimal one.
    """
    if places <= 0:
        return join(currency_symbol, f"{int(Decimal(amount_major).to_integral_value()):,}")
    quantized = Decimal(amount_major).quantize(Decimal(1).scaleb(-places))
    if quantized == quantized.to_integral_value():
        return join(currency_symbol, f"{int(quantized):,}")
    return join(currency_symbol, f"{quantized:,.{places}f}")
