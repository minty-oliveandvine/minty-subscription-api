"""Minty's own billing arithmetic — periods, prorations and line text.

PHASE 1 (shadow): nothing here is wired into checkout or access yet. It computes what
an invoice SHOULD say so the result can be diffed against what Stripe actually produced.
Stripe is currently the oracle for this arithmetic; once billing moves off it that oracle
is gone, so the numbers get proven while it still exists.

Deliberately has NO Stripe import. Everything is plain data — integer minor units, aware
datetimes, entity names — so the engine is testable without the network and portable to
another processor, which is the point of owning it.

Two conventions, both matching Stripe so the shadow diff is meaningful:

* **Integer minor units** (cents) everywhere. Never floats for money; the fraction is
  applied to an integer and rounded once, at the end.
* **Prorations are charged by ELAPSED TIME, in seconds** — not by whole days. A period is
  whatever real interval the anchor produces (28-31 days), so "days" is not a constant
  and the fraction has to come from the actual span.

Rounding is **half away from zero**, by decision rather than by inference. Every case
observed from Stripe rounds identically under that rule and under truncate-toward-zero,
so the oracle could not distinguish them:

    28000 x  7/30 = 6533.33 -> 6533       40000 x 28/30 = 37333.33 -> 37333
    40000 x  7/30 = 9333.33 -> 9333       12000 x 23/31 =  8903.23 ->  8903

Floor is ruled out: Stripe's credit for the first case was -6533, and floor(-6533.33)
would be -6534. Since billing is moving in-house, this stops being a question about
Stripe's behaviour and becomes Minty's own rule — symmetric, so a credit and a charge of
the same magnitude round to the same number. See ``_round_money``.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class Period:
    """One billing period, half-open: ``start`` inclusive, ``end`` exclusive.

    Half-open so consecutive periods tile without overlap — the instant that ends one
    period is the instant that begins the next, and a charge at exactly the anchor
    belongs to the new period, not a zero-length tail of the old one.
    """

    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    def contains(self, at: datetime) -> bool:
        return self.start <= at < self.end

    def remaining_seconds(self, at: datetime) -> int:
        """Seconds from ``at`` to the end of the period; 0 outside it."""
        if at <= self.start:
            return self.seconds
        if at >= self.end:
            return 0
        return int((self.end - at).total_seconds())


#: ``billing_plan.code`` keeps the word BILL for the Payment Request module by decision
#: (schema item 20), while ``entity_function.function_code`` says PAYMENT_REQUEST. The
#: plan key is derived from module codes, so the module word is mapped to the plan word
#: here - in exactly one place - and back in ``plan_modules``.
PLAN_WORD_BY_MODULE = {"PAYMENT_REQUEST": "BILL"}
MODULE_BY_PLAN_WORD = {v: k for k, v in PLAN_WORD_BY_MODULE.items()}


def plan_code(codes) -> str:
    """Canonical key for the SET of modules a plan bills: upper-cased, deduped, sorted.

    Sorting is what makes it canonical - {PETTY_CASH, PAYMENT_REQUEST} and the reverse are
    the same plan and must not become two catalog rows. Derived in exactly one place so
    a lookup and a write can never disagree about the spelling. Module codes are mapped to
    the plan's words (``PAYMENT_REQUEST`` -> ``BILL``); an already-mapped word passes through,
    so a caller holding ``plan.code.split("+")`` gets the same key.

    Lives here, not on the model, so it can be imported without pulling in ``models.db``.
    """
    wanted = sorted({
        PLAN_WORD_BY_MODULE.get(w, w)
        for w in (str(c).strip().upper() for c in (codes or []))
        if w
    })
    if not wanted:
        raise ValueError("a plan needs at least one module code")
    return "+".join(wanted)


def plan_modules(code: str) -> tuple[str, ...]:
    """The module codes a ``billing_plan.code`` bills, sorted: ``'BILL+PETTY_CASH'`` ->
    ``('PAYMENT_REQUEST', 'PETTY_CASH')``. The inverse of ``plan_code``."""
    return tuple(sorted(
        MODULE_BY_PLAN_WORD.get(w, w)
        for w in (part.strip().upper() for part in (code or "").split("+"))
        if w
    ))


def _require_aware(name: str, value: datetime) -> datetime:
    """Reject naive datetimes rather than guessing a zone.

    A silent UTC assumption here would shift a period boundary by the host's offset,
    which moves what a customer is charged. Better to fail at the call site.
    """
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def add_months(moment: datetime, count: int) -> datetime:
    """``moment`` shifted by ``count`` whole months, clamping the day to month length.

    A 31st anchor has no counterpart in February, so it clamps to the 28th/29th — and
    clamping must NOT be sticky: the anchor keeps its original day-of-month and is
    re-derived each time, so 31 Jan -> 28 Feb -> 31 Mar, not -> 28 Mar. That is why this
    takes the anchor and a count rather than stepping one month at a time.

    Confirmed against Stripe twice, and the second run is the one that matters:

    * a 31 Jan anchor springs back to the 31st rather than sticking on 28 Feb. Stepping
      month-by-month would have left every later period three days short, forever;
    * a 29 FEB anchor produces 29 Mar, not 31 Mar. That distinguishes DAY-of-month
      clamping (this) from last-day-of-month tracking, which the 31 Jan run could not —
      31 Jan satisfies both readings, so it proved less than it appeared to.
    """
    moment = _require_aware("moment", moment)
    total = moment.month - 1 + count
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def months_between(anchor: datetime, at: datetime) -> int:
    """How many whole billing months from ``anchor`` to ``at`` (can be negative)."""
    months = (at.year - anchor.year) * 12 + (at.month - anchor.month)
    # Not a full month yet if the day-of-month hasn't come round. Compare against the
    # CLAMPED anchor day so a 31st anchor still turns over on 28 Feb.
    if at < add_months(anchor, months):
        months -= 1
    return months


def period_containing(anchor: datetime, at: datetime, *, interval_months: int = 1) -> Period:
    """The billing period that ``at`` falls in, for a subscription anchored at ``anchor``.

    The anchor is the ORIGINAL start, not the current period's start, so periods are
    always re-derived from it. Deriving them by repeatedly adding a month to the previous
    period would let month-length clamping accumulate (see ``add_months``).

    MONTHLY ONLY in practice. ``interval_months`` is honoured, but the business sells
    nothing but monthly plans and no other interval has been checked against Stripe — so
    a non-monthly value is untested arithmetic, not a supported feature. Verify it before
    relying on it rather than assuming the generality holds.
    """
    if interval_months < 1:
        raise ValueError("interval_months must be at least 1")
    anchor = _require_aware("anchor", anchor)
    at = _require_aware("at", at)
    elapsed = months_between(anchor, at)
    # Snap to a whole number of intervals so a quarterly plan doesn't start a period
    # part-way through one.
    index = (elapsed // interval_months) * interval_months
    start = add_months(anchor, index)
    return Period(start, add_months(anchor, index + interval_months))


def _round_money(value: float) -> int:
    """Round to whole minor units, half AWAY from zero.

    Python's ``round`` is banker's rounding (2.5 -> 2), which would disagree with Stripe
    on exact halves, and ``int()`` truncates. Neither is what's wanted by default. See
    the module docstring: this rule is not yet confirmed against Stripe, and the shadow
    diff exists partly to settle it.
    """
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def prorate(amount: int, period: Period, at: datetime) -> int:
    """``amount`` scaled to the part of ``period`` still remaining at ``at``.

    Used for a line that starts mid-period: the customer pays only for what they get.
    A charge at or before the period start is the full amount; at or after the end, zero.
    """
    at = _require_aware("at", at)
    total = period.seconds
    if total <= 0:
        return 0
    return _round_money(amount * period.remaining_seconds(at) / total)


def paid_from(period: Period, at: datetime) -> datetime:
    """The instant a line starting at ``at`` begins paying: ``at`` held inside ``period``.

    The same clamp ``prorate`` applies — at or before the start is the whole period, at or
    after the end is nothing — so the days a line records are the days it was charged for.
    """
    at = _require_aware("at", at)
    return min(max(at, period.start), period.end)


@dataclass(frozen=True)
class Adjustment:
    """What a mid-period price change costs: a credit, a charge, and the net.

    Both parts are kept rather than just the net because that is how the invoice reads —
    Stripe issues "Unused time on X" and "Remaining time on Y" as separate lines, and a
    net-only figure is unexplainable to a customer.
    """

    credit: int  # negative, or 0
    charge: int  # positive, or 0

    @property
    def net(self) -> int:
        return self.credit + self.charge


def change_line(old_amount: int, new_amount: int, period: Period, at: datetime) -> Adjustment:
    """Swap a line from ``old_amount`` to ``new_amount`` part-way through ``period``.

    The customer already paid ``old_amount`` for the whole period, so they are credited
    the unused remainder and charged the new price for that same remainder. Both sides
    use the SAME fraction, which is what makes the net come out as the price difference
    scaled to the time left.

    A downgrade (``new_amount < old_amount``) still produces a credit here. Whether that
    credit should be issued is a policy decision, not an arithmetic one — today's cancel
    path deliberately suppresses it because access continues to ``app_access_until``.
    Callers decide; this reports what the swap is worth.
    """
    credit = -prorate(old_amount, period, at)
    charge = prorate(new_amount, period, at)
    return Adjustment(credit=credit, charge=charge)


def start_line(amount: int, period: Period, at: datetime) -> Adjustment:
    """A NEW line joining an existing period — a trial converting, or a module bought.

    There is nothing to credit back: the entity was not billed for this period before,
    so this is a charge alone. Contrast ``change_line``, where an existing line's unused
    time has to be returned first.
    """
    return Adjustment(credit=0, charge=prorate(amount, period, at))


# --- Cancellation --------------------------------------------------------------
#
# Cancelling is where billing stops being arithmetic and becomes policy, so the rules
# live here explicitly rather than falling out of a proration default:
#
# * access is only ever EXTENDED, never cut short (``cancel_access_end``);
# * the extra days are priced against what is LEAVING, not against what survives. The set
#   leaving together is worth the plan that covers it, and that total is allocated in
#   sorted code order — the first code its standalone price, each later one the step it
#   adds (``checkout._leaving_marginal``, built on ``marginal_amount``). The shares
#   telescope, so they sum to the plan: Petty Cash alone is worth 280, and Petty Cash with
#   Payment Request is worth 400 between them (280 + 120), never 240;
# * the swap down issues NO credit. The customer keeps access to ``access_end``, so
#   refunding the unused time would be paying them for days they still get;
# * only the days BEYOND the anchor are charged (``extension_charge``) — everything up to
#   the anchor was already paid for.


def cancel_access_end(period_end: datetime, at: datetime, *, extension_days: int) -> datetime:
    """When access really ends after a cancellation — the LATER of the paid period and
    ``at + extension_days``.

    Never the earlier: a customer who has paid to the end of the period keeps it, and one
    cancelling early in a period still gets the full extension window.
    """
    period_end = _require_aware("period_end", period_end)
    at = _require_aware("at", at)
    horizon = at + timedelta(days=extension_days)
    return period_end if period_end >= horizon else horizon


def marginal_amount(line_amount: int, remaining_amount: int | None) -> int:
    """What the cancelled module was actually worth on the line.

    A bundled module is not worth its standalone price. Dropping Petty Cash from a 400
    bundle leaves Bill at 280, so Petty Cash's marginal value is 120 — charging 280 for
    the extension would bill the customer more than the module ever cost them. With no
    survivor, the whole line price is marginal.
    """
    if not remaining_amount:
        return max(0, int(line_amount))
    return max(0, int(line_amount) - int(remaining_amount))


def extension_charge(marginal: int, period: Period, access_end: datetime) -> int:
    """The cost of the days that fall AFTER the anchor, at the marginal rate.

    Everything up to ``period.end`` is already paid for, so only the overhang is billed —
    and at the same daily rate the period itself used, which is why the span is the
    period's own length rather than a nominal month.

    Zero when access ends at or before the anchor: there is no overhang to charge, and
    the period they are in was already covered.
    """
    access_end = _require_aware("access_end", access_end)
    overhang = (access_end - period.end).total_seconds()
    if marginal <= 0 or overhang <= 0 or period.seconds <= 0:
        return 0
    return _round_money(marginal * overhang / period.seconds)


def billed_days(period: Period, at: datetime | None = None) -> tuple[int, int]:
    """``(days_charged, days_in_period)`` for display — NEVER for money.

    The money is prorated in seconds (see ``prorate``), because a period is whatever the
    anchor produces and "days" is not a constant. But seconds are unreadable on an
    invoice, so the memo quotes whole days, rounded, purely as an explanation of a figure
    that was already computed exactly.

    Kept here, next to the arithmetic it describes, rather than in the gateway: derived
    twice is derived differently, and an invoice whose memo contradicts its own total is
    worse than one that explains nothing.
    """
    total = max(1, round(period.seconds / 86400))
    if at is None or at <= period.start:
        return total, total
    return max(0, round(period.remaining_seconds(at) / 86400)), total


def line_description(entity_name: str, product_name: str, *, kind: str = "full",
                     at: datetime | None = None) -> str:
    """Customer-facing line text: ``Entity - Product``, and nothing else.

    The entity leads because a payer's invoice spans entities and that is the only thing
    distinguishing two otherwise identical bundle lines. This is the whole reason billing
    moved in-house: Stripe composes subscription line text from the PRODUCT name and
    refuses to let it be rewritten, so the entity could never appear.

    A line IDENTIFIES; the memo EXPLAINS. These used to read "Remaining time on Entity A -
    Super Minty after 4 Feb 2027" — a sentence about proration in
    the field a customer scans to find out which of their companies a charge belongs to,
    while the memo said almost nothing. The proration story now lives in the memo, where
    it can also quote the arithmetic (see ``change_memo`` / ``join_memo``).

    ``unused`` keeps a parenthetical because it is the one case the entity+product pair
    cannot disambiguate: both lines of an upgrade name the SAME entity, so without it a
    credit and a charge differ only by sign.
    """
    label = f"{entity_name} - {product_name}"
    if kind == "unused":
        return f"{label} (unused time)"
    if kind in ("full", "remaining"):
        return label
    raise ValueError(f"unknown line kind {kind!r}")


# Minor units per major unit varies by currency: 2 for HKD, 0 for JPY, 3 for KWD. It is
# a PARAMETER rather than a lookup because this module is pure — no ORM, no clock — which
# is what lets every money rule below be tested without a database.
# ``money.decimal_places`` resolves it from ``currency_info``; callers thread it through.
DEFAULT_DECIMAL_PLACES = 2


def _money(amount: int, places: int = DEFAULT_DECIMAL_PLACES) -> str:
    """Minor units as a plain decimal for the memo. No currency symbol: the invoice
    already carries the currency, and repeating it invites the two disagreeing.

    Was a hardcoded ``/ 100``, which agreed with the rest of the app only because the
    business sells in HKD. On a zero-decimal currency the memo explaining a charge
    contradicted the card that quoted it.
    """
    value = abs(Decimal(int(amount))) / (Decimal(10) ** places)
    return f"{value:,.{places}f}"


def _span(period: Period, at: datetime | None = None) -> str:
    start = at if (at is not None and at > period.start) else period.start
    return f"{start.day} {start:%b} - {period.end.day} {period.end:%b %Y}"


def join_memo(entity_name: str, product_name: str, amount: int, period: Period,
              at: datetime, *, places: int = DEFAULT_DECIMAL_PLACES) -> str:
    """Memo for a module STARTING — a trial converting, or a purchase.

    States the days so "why 207.74 and not 280.00?" is answered on the invoice rather
    than in a support ticket.
    """
    charged, total = billed_days(period, at)
    when = f"{at.day} {at:%b %Y}"
    if charged >= total:
        return (f"{entity_name} started {product_name} on {when}. "
                f"Charged for {_span(period)} in full: {_money(amount, places)}.")
    return (f"{entity_name} started {product_name} on {when}. Charged for "
            f"{_span(period, at)}, {charged} of {total} days in the current period: "
            f"{_money(amount, places)}. Future periods bill in full.")


def change_memo(entity_name: str, old_product: str, new_product: str, credit: int,
                charge: int, period: Period, at: datetime, *,
                places: int = DEFAULT_DECIMAL_PLACES) -> str:
    """Memo for a mid-period price change, with both halves and the net.

    The net belongs here rather than on a line because it is a property of the INVOICE:
    neither line is the net, and a customer shown only "73.03" cannot reconcile it
    against the 280.00 they paid three weeks ago.
    """
    charged, total = billed_days(period, at)
    return (f"{entity_name} changed from {old_product} to {new_product} on "
            f"{at.day} {at:%b %Y}, with {charged} of {total} days left in the period. "
            f"Unused {old_product} credited {_money(credit, places)}; {new_product} "
            f"charged {_money(charge, places)} for the same days. "
            f"Net {_money(credit + charge, places)}.")


def renewal_memo(period: Period, entity_count: int, extension_count: int = 0) -> str:
    """Memo for the monthly bill.

    Only a summary is possible: a renewal spans entities, so one memo cannot carry
    per-entity arithmetic. The extension count is called out because a cancellation
    charge is the one line on a renewal a customer will not be expecting.
    """
    memo = (f"Minty subscription, {period.start.day} {period.start:%b} - "
            f"{period.end.day} {period.end:%b %Y}. "
            f"{entity_count} {'entity' if entity_count == 1 else 'entities'}.")
    if extension_count:
        plural = "" if extension_count == 1 else "s"
        memo += f" Includes {extension_count} access extension{plural}."
    return memo


# --- Invoice construction ------------------------------------------------------
#
# What a payer owes, as data. Nothing here talks to a payment processor: an Invoice is a
# plain value that ``billing_gateway`` hands to whoever collects it. That split is the
# point of owning billing — swapping processor should mean rewriting the gateway, not
# the arithmetic.


@dataclass(frozen=True)
class Line:
    """One charge on an invoice, always attributable to exactly one entity.

    ``entity_id`` travels with the line so a payment processor's own metadata is never
    needed to work out who a charge belongs to — the mistake that made Stripe's invoices
    unreadable, where every line inherited the subscription's entity.

    ``period_start`` / ``period_end`` / ``unit_amount`` say what the line PAID FOR: the
    days, half-open like ``Period``, and the price per period they were charged at —
    positive on a credit too, ``amount`` carries the sign. Set by whatever priced the line,
    because only that knows, and recorded on ``subscription_invoice_line`` so a breakdown
    never has to work them out again. None where there is no single answer (see
    ``checkout.pending_extension_terms``).
    """

    entity_id: str
    entity_name: str
    product_name: str
    amount: int
    kind: str = "full"
    at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    unit_amount: int | None = None

    @property
    def description(self) -> str:
        return line_description(
            self.entity_name, self.product_name, kind=self.kind, at=self.at
        )


@dataclass(frozen=True)
class Invoice:
    """Everything a payer owes for one event, ready to be collected."""

    currency: str
    period: Period
    lines: tuple[Line, ...]

    @property
    def total(self) -> int:
        return sum(line.amount for line in self.lines)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for line in self.lines:
            seen.setdefault(line.entity_id, None)
        return tuple(seen)


def renewal_invoice(entries, period: Period, currency: str) -> Invoice:
    """The whole-period bill for every entity a payer owns.

    ``entries`` are ``(entity_id, entity_name, product_name, amount)``. One line per
    entity, each naming its entity — which is the difference from the subscription-billed
    version, where two entities on the same bundle produced two identical lines.
    """
    lines = tuple(
        Line(entity_id=eid, entity_name=name, product_name=product, amount=amount,
             period_start=period.start, period_end=period.end, unit_amount=amount)
        for eid, name, product, amount in entries
    )
    return Invoice(currency=currency, period=period, lines=lines)


def join_invoice(entity_id: str, entity_name: str, product_name: str, amount: int,
                 period: Period, at: datetime, currency: str) -> Invoice:
    """A module STARTING mid-period — a trial converting, or a purchase.

    One charge, no credit: the entity was not billed for this period before.
    """
    adjustment = start_line(amount, period, at)
    return Invoice(
        currency=currency,
        period=period,
        lines=(
            Line(entity_id, entity_name, product_name, adjustment.charge,
                 kind="remaining", at=at, period_start=paid_from(period, at),
                 period_end=period.end, unit_amount=amount),
        ),
    )


def change_invoice(entity_id: str, entity_name: str, old_product: str, new_product: str,
                   old_amount: int, new_amount: int, period: Period, at: datetime,
                   currency: str) -> Invoice:
    """A price change mid-period: unused time returned, the new price charged.

    Both lines are kept rather than a single net figure — a customer shown only "28.00"
    cannot reconcile it against the 280.00 they paid three weeks ago.
    """
    adjustment = change_line(old_amount, new_amount, period, at)
    start = paid_from(period, at)
    return Invoice(
        currency=currency,
        period=period,
        lines=(
            Line(entity_id, entity_name, old_product, adjustment.credit,
                 kind="unused", at=at, period_start=start, period_end=period.end,
                 unit_amount=old_amount),
            Line(entity_id, entity_name, new_product, adjustment.charge,
                 kind="remaining", at=at, period_start=start, period_end=period.end,
                 unit_amount=new_amount),
        ),
    )


def cancellation_invoice(entity_id: str, entity_name: str, product_name: str,
                         marginal: int, period: Period, access_end: datetime,
                         currency: str) -> Invoice | None:
    """The extension owed for access continuing past the anchor, or None if nothing is.

    Deliberately has no credit line: access runs to ``access_end``, so returning the
    unused period would pay the customer for days they still get.
    """
    amount = extension_charge(marginal, period, access_end)
    if amount <= 0:
        return None
    return Invoice(
        currency=currency,
        period=period,
        lines=(
            Line(entity_id, entity_name, f"{product_name} (access extension)", amount,
                 kind="remaining", at=period.end, period_start=period.end,
                 period_end=_require_aware("access_end", access_end), unit_amount=marginal),
        ),
    )
