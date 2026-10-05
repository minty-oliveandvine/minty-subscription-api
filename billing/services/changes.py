"""Bill a mid-period module change — a purchase, a trial converting, an upgrade.

The renewal runner handles the periodic charge. This handles everything that happens
BETWEEN renewals, where the customer only owes for the part of the period they will
actually get.

Which shape applies depends entirely on whether the entity was already being billed:

* nothing before  -> ONE prorated charge. There is no unused time to return, because the
  entity was not paying for this period at all (``billing.join_invoice``);
* something before -> a credit for the unused remainder of the old price AND a charge at
  the new one (``billing.change_invoice``). Both lines are kept: a customer shown only
  the net "28.00" cannot reconcile it against the 280.00 they paid three weeks ago.

Getting that distinction wrong is expensive in both directions — crediting a customer
who never paid, or charging the full new price on top of one they already settled.

Nothing here decides WHETHER to bill; callers do that. This turns "these modules, from
this moment, for this period" into money.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from billing.services import money, store
from billing.services._log import logger
from billing.services.billing import (
    Invoice,
    Period,
    change_invoice,
    change_memo,
    join_invoice,
    join_memo,
)


def change_key(entity_id, at: datetime, after_codes, *, kind: str = "change") -> str:
    """Stable id for "this entity's change, at this moment, to this module set".

    Doubles as the idempotency key and the invoice metadata, so a retry after a crash
    cannot bill the same change twice. Includes the target modules because a customer
    can legitimately make two different changes to one entity in the same minute — a
    key on time alone would silently swallow the second.

    ``kind`` names what the change is when its attempts have to be found again: a trial's
    conversion is ``convert`` (``checkout._resolve_prior_conversions`` reads them back by
    that prefix). Everything else is a ``change``.
    """
    return f"{kind}-{entity_id}-{at:%Y%m%d%H%M%S}-{codes_key(after_codes)}"


def codes_key(codes) -> str:
    """A module set as it is written into a change's key: upper-cased, sorted, ``+``-joined."""
    return "+".join(sorted(str(c).upper() for c in codes))


def attempt_key(base: str, number: int) -> str:
    """The key of attempt ``number`` at a charge retried under a FRESH key each time.

    The first attempt is the bare key. Later ones carry the number after a ``~`` - never a
    ``-``, which ``dunning._names_period`` reads as the same period, and not the
    ``~in_…`` a refresh retires a dead key to (``store.retired_key``), which is no attempt.
    """
    return base if number <= 1 else f"{base}~{number}"


#: How long a reservation the processor has not confirmed may still be ANOTHER request's
#: charge in flight - a second press of the same button. Stripe's own request timeout times
#: the SDK's retries is well inside it; a reservation older than this was left by an attempt
#: that died, and only then is it the last attempt's to settle.
IN_FLIGHT = timedelta(minutes=10)


def _in_flight(record) -> bool:
    """Whether ``record``, a reservation the processor has not confirmed, is young enough to
    be another request's charge still on its way (``IN_FLIGHT``)."""
    made = record.created_at
    if made is not None and made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    return made is not None and datetime.now(UTC) - made < IN_FLIGHT


def _attempt_number(key: str, base: str) -> int | None:
    """Which attempt ``key`` is at the charge keyed ``base``, or None if it is none of them."""
    if key == base:
        return 1
    tail = key[len(base) + 1:] if key.startswith(f"{base}~") else ""
    return int(tail) if tail.isdigit() else None


def _next_attempt(customer_id: str, payer_user_id, base: str) -> tuple[str, dict | None]:
    """The key to raise the next attempt under - or an earlier attempt that was PAID.

    For a charge the customer may retry after a decline (a reinstatement). Its key is
    stable - the company, the day its access ends, the modules - so every retry used to
    reach for the same key; the declined attempt's invoice is voided, its row keeps the key,
    and the unique index refused every retry until the cancellation window ran out: "check
    your payment method", with the card never even tried.

    So each attempt takes the next number, worked out from the attempts already on record.
    The latest is settled first, because it decides what the customer owes:

    * PAID (a reply that was lost, or a payment made elsewhere) is returned, to be adopted -
      never charged again;
    * OPEN is withdrawn - and if the withdrawal finds it paid after all, adopted;
    * a DRAFT (a crash before it was finalized) is deleted;
    * a reservation the processor never confirmed is looked for, and discarded if it never
      got there.

    A new attempt always takes the next number, never an old key again: Stripe replays a
    keyed request's ERROR for 24 hours, so a key that once failed fails again all day.

    Raises ``billing_gateway.BillingError`` (``retryable``) when the processor cannot be read -
    nothing may be charged on a guess about whether the last attempt was paid.
    """
    from billing.services import billing_gateway

    family = [
        (number, row)
        for row in store.invoices_with_key_prefix(payer_user_id, base)
        if (number := _attempt_number(row.idempotency_key or "", base)) is not None
    ]
    if not family:
        return base, None
    number, latest = max(family, key=lambda pair: pair[0])
    try:
        if latest.external_id:
            found = billing_gateway.recheck(latest)
            if found is None:
                # A draft deleted by hand at the processor: that attempt is over.
                return attempt_key(base, number + 1), None
        else:
            if _in_flight(latest):
                # Another press is raising THIS attempt right now: its create has not answered
                # yet, so the processor cannot show it. Discarding it and raising the next
                # attempt charged the customer twice the moment the first one landed.
                raise billing_gateway.BillingError(
                    f"attempt {latest.idempotency_key} is still in flight",
                    retryable=True, claimed=True,
                )
            found = billing_gateway.find_invoice_by_metadata(
                customer_id, "change_key", latest.idempotency_key
            )
            if found is None:
                # Claimed, and the processor never heard of it.
                store.discard_invoice(latest.id)
                return attempt_key(base, number + 1), None
            billing_gateway.record_found_invoice(latest, found)
            # Read again: the store settles a fresh copy, and this one still has no id.
            latest = store.invoice_for_key(latest.idempotency_key) or latest
        status = found.get("status")
        if status in ("open", "draft"):
            if billing_gateway.void_invoice(found["id"]) == "paid":
                found, status = billing_gateway.recheck(latest), "paid"
    except billing_gateway.BillingError:
        raise
    except Exception as exc:
        raise billing_gateway.BillingError(
            f"could not settle the last attempt at {base}",
            retryable=billing_gateway.retryable(exc),
        ) from exc
    if status == "paid":
        logger.info(
            "change: {} was already paid as {} (attempt {}); not charging again",
            base, found.get("id"), number,
        )
        return latest.idempotency_key, found
    return attempt_key(base, number + 1), None


def _invoiced_under(customer_id: str, key: str) -> dict | None:
    """The processor's invoice already raised under ``key``, or None if there is none.

    From OUR row first - ``store.invoice_for_key``, one indexed lookup, as renewals do
    (``renewals._already_invoiced``). This used to LIST every invoice the customer ever had
    and scan their metadata, on every change. The row is claimed before the charge under a
    UNIQUE index (``billing_gateway.issue_invoice``), so no row means nothing was raised, and
    a change never raised before costs no processor call at all.

    * A row the processor confirmed is read back by its id (``recheck``): its status NOW,
      which the caller decides on.
    * A row it never confirmed is the one case the row cannot answer, so the processor is
      asked by metadata - but not while it may be ANOTHER press's charge still on its way
      (``IN_FLIGHT``): that is refused as claimed, never discarded. Found, it is recorded;
      never there, the reservation is discarded, or it would hold this change for good.

    Raises ``billing_gateway.BillingError`` when the processor cannot be read - nothing may
    be charged on a guess about whether it already was.
    """
    from billing.services import billing_gateway

    record = store.invoice_for_key(key)
    if record is None:
        return None
    if record.external_id:
        return billing_gateway.recheck(record)
    if _in_flight(record):
        raise billing_gateway.BillingError(
            f"change {key} is still in flight", retryable=True, claimed=True
        )
    try:
        found = billing_gateway.find_invoice_by_metadata(customer_id, "change_key", key)
    except Exception as exc:
        raise billing_gateway.BillingError(
            f"could not look for change {key}", retryable=billing_gateway.retryable(exc)
        ) from exc
    if found is None:
        store.discard_invoice(record.id)               # it never reached the processor
        return None
    billing_gateway.record_found_invoice(record, found)
    return found


def build_change(entity_id, entity_name: str, before_codes, after_codes,
                 period: Period, at: datetime) -> Invoice | None:
    """What the entity owes for changing its modules mid-period, or None if nothing.

    Returns None when the change costs nothing to bill now — a pure DOWNGRADE, where the
    new price is lower and the customer keeps access they have already paid for. That is
    a policy decision the cancel path owns (it charges a prorated extension instead), so
    inventing a credit here would refund days the customer still gets.

    A combination the catalog cannot price returns None and logs. Guessing — summing
    standalone prices — silently overcharges by the bundle discount.
    """
    after = {str(c).upper() for c in after_codes if str(c).strip()}
    before = {str(c).upper() for c in before_codes if str(c).strip()}
    if not after or after == before:
        return None

    new_plan = store.billing_plan_for_codes(after)
    if new_plan is None:
        logger.error(
            "change: no plan prices {} for entity {}; refusing to guess a price",
            ",".join(sorted(after)),
            entity_id,
        )
        return None
    currency = (new_plan.currency or "").lower()

    if not before:
        return join_invoice(
            str(entity_id), entity_name, new_plan.display_name, new_plan.amount,
            period, at, currency,
        )

    old_plan = store.billing_plan_for_codes(before)
    if old_plan is None:
        logger.error(
            "change: no plan prices the entity's CURRENT modules {}; cannot work out "
            "what to credit for entity {}",
            ",".join(sorted(before)),
            entity_id,
        )
        return None

    if new_plan.amount <= old_plan.amount:
        # A downgrade. Access continues to what was already paid for, so there is
        # nothing to collect now — and the unused time must NOT be credited, or the
        # customer is refunded days they keep. The cancel path handles the rest.
        return None

    return change_invoice(
        str(entity_id), entity_name, old_plan.display_name, new_plan.display_name,
        old_plan.amount, new_plan.amount, period, at, currency,
    )


def _memo_for(entity_name: str, before_codes, after_codes, invoice: Invoice,
              period: Period, at: datetime) -> str:
    """The memo that matches the invoice ``build_change`` actually produced.

    Derived from the SAME before/after sets, so a join can never be described as a change
    or vice versa. Quotes the invoice's own line amounts rather than recomputing them —
    a memo that disagrees with the total it explains is worse than no memo.

    Falls back to the bare product name if the catalog cannot be read a second time.
    Getting the memo wrong must not stop a charge that has already been priced.
    """
    before = {str(c).upper() for c in before_codes if str(c).strip()}
    after = {str(c).upper() for c in after_codes if str(c).strip()}
    new_plan = store.billing_plan_for_codes(after)
    new_name = new_plan.display_name if new_plan else ", ".join(sorted(after))
    # From the INVOICE, not the plan: the memo explains the invoice's own figures, so it
    # has to scale them the way that invoice's currency does.
    places = money.decimal_places(invoice.currency)

    if not before:
        return join_memo(entity_name, new_name, invoice.total, period, at, places=places)

    old_plan = store.billing_plan_for_codes(before)
    old_name = old_plan.display_name if old_plan else ", ".join(sorted(before))
    credit = sum(line.amount for line in invoice.lines if line.amount < 0)
    charge = sum(line.amount for line in invoice.lines if line.amount > 0)
    if not credit:
        # No credit line means nothing was being paid for yet — a join in all but name.
        return join_memo(entity_name, new_name, invoice.total, period, at, places=places)
    return change_memo(
        entity_name, old_name, new_name, credit, charge, period, at, places=places
    )


def issue_change(customer_id: str, entity_id, entity_name: str, before_codes,
                 after_codes, period: Period, at: datetime,
                 *, collect: bool = True, group=None, attempts_of=None,
                 kind: str = "change") -> dict | None:
    """Build and collect the change. Returns the invoice, or None if nothing was owed.

    Refuses to bill the same change twice: an invoice already carrying this change's key
    means an earlier run charged it and died before recording the result.

    ``group`` is the billing group this company is on, and therefore the CARD the change
    is charged to. It is passed rather than looked up because the caller has already
    resolved it — and has already refused to charge anything when there is none, which is
    the only correct answer: there is no default to fall back to.

    ``attempts_of`` (the payer) makes each call a new ATTEMPT at the change, under its own
    key (``_next_attempt``) - for a charge whose key is stable across the customer's retries,
    so that a declined one can be tried again at all.
    """
    from billing.services import billing_gateway

    invoice = build_change(entity_id, entity_name, before_codes, after_codes, period, at)
    if invoice is None or not invoice.total:
        return None

    key = change_key(entity_id, at, after_codes, kind=kind)
    if attempts_of is not None:
        key, paid = _next_attempt(customer_id, attempts_of, key)
        if paid is not None:
            return paid
    existing = _invoiced_under(customer_id, key)
    # A VOIDED invoice is not evidence of a charge — it is evidence of one withdrawn.
    # A failed conversion voids its invoice (see ``_bill_module_change_in_house``), and
    # treating that as "already invoiced" would make the next genuine attempt at the same
    # change return the dead document and collect nothing, while the caller reads success
    # and hands over the module.
    if existing is not None and (existing.get("status") or "") in ("void", "deleted"):
        logger.info(
            "change: {} was invoiced as {} and voided; charging again",
            key,
            existing.get("id"),
        )
        existing = None
    if existing is not None:
        logger.info(
            "change: {} was already invoiced as {}; not charging again",
            key,
            existing.get("id"),
        )
        # (Its row is already brought up to date - ``_invoiced_under`` records what it
        # finds, or the list would show that invoice with no paid date or link for good.)
        if existing.get("status") == "draft":
            # Never finalized, so never charged. Every caller refuses anything unpaid and
            # withdraws it, but that it happened at all has to be said.
            billing_gateway.stranded_draft(
                existing.get("id"), key, "change", billing_gateway.WITHDRAWN
            )
        return existing

    metadata = {"change_key": key, "entity_id": str(entity_id)}
    if group is not None:
        metadata["billing_group"] = str(group.id)
    return billing_gateway.issue_invoice(
        customer_id,
        invoice,
        memo=_memo_for(entity_name, before_codes, after_codes, invoice, period, at),
        metadata=metadata,
        idempotency_key=key,
        collect=collect,
        payment_method=(
            group.stripe_payment_method_id if group is not None else None
        ),
        billing_group_id=(group.id if group is not None else None),
        # Known when the caller counts attempts; otherwise the gateway looks it up.
        payer_user_id=attempts_of,
    )
