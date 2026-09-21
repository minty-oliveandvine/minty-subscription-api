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

from datetime import datetime

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


def change_key(entity_id, at: datetime, after_codes) -> str:
    """Stable id for "this entity's change, at this moment, to this module set".

    Doubles as the idempotency key and the invoice metadata, so a retry after a crash
    cannot bill the same change twice. Includes the target modules because a customer
    can legitimately make two different changes to one entity in the same minute — a
    key on time alone would silently swallow the second.
    """
    codes = "+".join(sorted(str(c).upper() for c in after_codes))
    return f"change-{entity_id}-{at:%Y%m%d%H%M%S}-{codes}"


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
                 *, collect: bool = True, group=None) -> dict | None:
    """Build and collect the change. Returns the invoice, or None if nothing was owed.

    Refuses to bill the same change twice: an invoice already carrying this change's key
    means an earlier run charged it and died before recording the result.

    ``group`` is the billing group this company is on, and therefore the CARD the change
    is charged to. It is passed rather than looked up because the caller has already
    resolved it — and has already refused to charge anything when there is none, which is
    the only correct answer: there is no default to fall back to.
    """
    from billing.services import billing_gateway

    invoice = build_change(entity_id, entity_name, before_codes, after_codes, period, at)
    if invoice is None or not invoice.total:
        return None

    key = change_key(entity_id, at, after_codes)
    existing = billing_gateway.find_invoice_by_metadata(customer_id, "change_key", key)
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
    )
