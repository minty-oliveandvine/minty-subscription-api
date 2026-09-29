"""Collect a ``billing.Invoice`` through a payment processor.

The ONLY Stripe-aware part of in-house billing, and deliberately thin: it takes lines
that are already priced and described, and does nothing but present and charge them.
Swapping processor should mean rewriting this file and nothing in ``billing``.

Why invoice ITEMS rather than subscription items — the whole reason billing moved here:

    a subscription line's description is composed by Stripe from the PRODUCT name and
    cannot be changed, through any endpoint:
        POST /v1/invoices/{inv}/lines/{line} -> 400 You may only update `tax_rates`,
                                               `tax_amounts`, or `discounts` for a
                                               subscription typed line item.

    an invoice ITEM carries whatever description it is given.

So a payer with two entities on the same bundle sees two lines naming their entities,
instead of two identical "1 x Super Minty 400.00" lines that no one can tell apart.

The subscription is NOT the biller here. If one still exists it is only a payment
instrument and a cycle marker; every amount and every period on the invoice came from
``billing``.

It does one thing besides talk to Stripe: every invoice it sends is RECORDED locally
first, in ``subscription_invoice``. That is not bookkeeping tacked onto the wrong layer —
this is the only place that knows what was actually sent (zero-amount lines are dropped
here) and the only place with a before-and-after around the charge, which is what a
double-billing guard has to have. The write goes through ``store``, so the processor and
the persistence stay separable.
"""
from __future__ import annotations

from datetime import UTC, datetime

from billing.services._log import logger
from billing.services.billing import Invoice
from billing.services.stripe_client import get_stripe


class BillingError(Exception):
    """Collection failed. Carries a customer-safe message where Stripe gave one.

    ``invoice_id`` is the document that was raised before the failure, when there was
    one. A decline arrives as an exception from ``Invoice.pay``, by which point the
    invoice is finalized and OPEN — so the caller has to be told what exists in order to
    decide what to do with it. Those decisions differ: a declined renewal keeps its
    invoice, because dunning chases exactly that document; a declined conversion must
    withdraw its own, because nothing was granted. Only the caller knows which it is,
    which is why this is reported rather than acted on here.
    """

    def __init__(self, message: str, *, user_message: str | None = None,
                 invoice_id: str | None = None):
        super().__init__(message)
        self.user_message = user_message
        self.invoice_id = invoice_id


def find_invoice_by_metadata(customer_id: str, key: str, value: str, *,
                             live_only: bool = False) -> dict | None:
    """An existing invoice for this customer carrying ``metadata[key] == value``.

    ``live_only`` skips VOID invoices. Off by default, and deliberately: for a renewal or a
    transfer a void invoice is still evidence the period was dealt with - re-billing a period
    somebody voided on purpose is the wrong answer. ``refresh_invoice`` wants the opposite: a
    replacement that was withdrawn is not one to collect on.

    NO LONGER the routine double-billing guard. That is now the UNIQUE index on
    ``subscription_invoice.idempotency_key``, claimed before the charge (see
    ``issue_invoice``) and read by ``renewals.run_renewals`` in one indexed lookup — this
    used to LIST a customer's invoices and scan them, once per payer per renewal.

    It survives for the one case a local row cannot answer: a reservation whose
    ``external_id`` is still NULL, meaning we claimed the key and then never heard back.
    Only the processor knows whether that invoice exists, and metadata is how to ask —
    Stripe expires idempotency keys after 24 hours, metadata never.

    Still the guard for ``changes.issue_change``, which has not been moved over.
    """
    # ``auto_paging_iter``, not the first page: ``limit`` is a PAGE SIZE, so a bare
    # list() stops at 100 and quietly reports "no such invoice" for a long-lived payer —
    # which here means re-issuing an invoice that already exists.
    listing = get_stripe().Invoice.list(customer=customer_id, limit=100)
    for candidate in listing.auto_paging_iter():
        if live_only and candidate.get("status") == "void":
            continue
        if (candidate.get("metadata") or {}).get(key) == value:
            return candidate
    return None


def _reserve(customer_id, invoice: Invoice, lines, memo, idempotency_key,
             payer_user_id, billing_group_id=None):
    """Claim the key and record what is about to be sent. See ``issue_invoice``.

    Returns the local row, or None if there is none to update — either because it could
    not be written (no key, so record-keeping only) or because there was no payer to
    attribute it to. Raises when a KEYED invoice cannot be reserved: that is the guard
    refusing to let the same period be charged twice, and it has to fail closed.
    """
    from billing.services import store

    try:
        payer = payer_user_id or store.user_for_customer(customer_id)
        if not payer:
            # Nothing to attribute the row to. An unmapped customer is a real problem,
            # but it is not one to discover by refusing a charge the payer is waiting on.
            raise ValueError(f"no payer mapped to customer {customer_id}")
        record = store.reserve_invoice(
            payer_user_id=payer,
            stripe_customer_id=customer_id,
            period=invoice.period,
            currency=invoice.currency,
            lines=lines,
            memo=memo,
            idempotency_key=idempotency_key,
            billing_group_id=billing_group_id,
        )
    except Exception as exc:
        if idempotency_key:
            logger.exception(
                "billing: could not reserve {} for customer {}; refusing to charge "
                "without the guard", idempotency_key, customer_id,
            )
            raise BillingError(
                f"could not reserve invoice {idempotency_key} for {customer_id}"
            ) from exc
        logger.exception(
            "billing: could not record the invoice for customer {}; charging anyway "
            "because nothing about this guards the customer", customer_id,
        )
        return None

    if record is None and idempotency_key:
        # The unique index refused it: this period is already being, or has been,
        # invoiced. Charging now is the exact double-bill the table exists to prevent.
        raise BillingError(
            f"invoice {idempotency_key} is already claimed for {customer_id}"
        )
    return record


def _settle(record, **fields) -> None:
    """Update the local row, never at the expense of the charge.

    Called after money may already have moved, so a failure here is logged and swallowed:
    losing the record of a payment is bad, but raising would turn a collected payment
    into an exception the caller reads as "it failed" — and that is how a customer gets
    charged a second time.
    """
    if record is None:
        return
    from billing.services import store

    try:
        store.settle_invoice(record.id, **fields)
    except Exception:
        logger.exception(
            "billing: charged, but could not update local invoice record {}", record.id
        )


def _local(external_id):
    """The local row for a processor invoice, or None — never a reason to fail.

    An invoice raised before these tables existed, or straight from the Stripe dashboard,
    has no local row. Collecting it must still work.
    """
    from billing.services import store

    try:
        return store.invoice_for_external_id(external_id)
    except Exception:
        logger.exception("billing: could not look up local record for {}", external_id)
        return None


def _moment(timestamp) -> datetime | None:
    """A Stripe unix timestamp as an aware UTC datetime, or None.

    Stripe's own times are used rather than the local clock so the recorded moments line
    up with the invoice they describe — which is also what keeps test-clock runs
    readable, where "now" and the billed period are years apart.
    """
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _record_of(result: dict) -> dict:
    """The settle fields describing ``result`` — status, total, when it moved, and the
    processor's own copies of the document.

    The two URLs are free here: Stripe puts them on the invoice from finalization
    onwards, so storing them costs nothing and saves the invoice list from being N round
    trips later. They are absent on a draft, and ``settle_invoice`` ignores None rather
    than blanking what an earlier settle recorded.
    """
    transitions = result.get("status_transitions") or {}
    return {
        "status": result.get("status") or "draft",
        "total": result.get("total"),
        "issued_at": _moment(transitions.get("finalized_at") or result.get("created")),
        "paid_at": _moment(transitions.get("paid_at")),
        "hosted_invoice_url": result.get("hosted_invoice_url"),
    }


def _capture_payment_method(record, invoice_id: str) -> None:
    """Record WHICH card paid this invoice, for the history. Never raises.

    Deliberately a SEPARATE call made AFTER the charge, not an ``expand`` on ``pay()``.
    The card here decorates a receipt; the pay call moves money. Folding an expand into
    it would put a display nicety on the path that must not fail, and an API that
    rejected the parameter would take the payment down with it. This one is wrapped, so
    the worst case is the column stays null and the invoice list says "not recorded".

    Only fills a blank — a re-settle must not overwrite the card that actually paid with
    whatever is on the charge later.
    """
    if record is None or getattr(record, "payment_method", None):
        return
    try:
        from billing.services import store
        from billing.services.stripe_client import payment_method_display

        invoice = get_stripe().Invoice.retrieve(invoice_id, expand=["charge"])
        charge = invoice.get("charge")
        if not isinstance(charge, dict):
            return
        card = (charge.get("payment_method_details") or {}).get("card") or {}
        brand, last4 = card.get("brand"), card.get("last4")
        label = None
        if brand and last4:
            label = f"{str(brand).title()} •••• {last4}"
        else:
            # A wallet (Link) exposes no card on the charge; name what it was instead.
            shown = payment_method_display(charge.get("payment_method"))
            label = shown.get("label") if shown else None
        if label:
            store.settle_invoice(record.id, payment_method=label)
    except Exception:
        logger.warning(
            "billing: could not record which card paid invoice {} (display only)",
            invoice_id,
        )


def _item_period(line, period) -> dict:
    """The days ONE line pays for, as the processor prints them under it.

    Every item used to carry the invoice's whole period, so Stripe's PDF, its hosted page
    and its revenue recognition dated a prorated start, an upgrade's credit and an access
    extension as if each covered the full month. A line records its own span when it is
    priced (``billing.Line.period_start`` / ``period_end``, schema item 23); the invoice's
    period is the fallback only for a line that recorded none.

    A recorded span that is empty or runs backwards is a pricing bug, not a reason to refuse
    a charge over a display date: it is logged and the invoice's period is sent instead.
    """
    start, end = line.period_start, line.period_end
    if start is None or end is None:
        start, end = period.start, period.end
    elif end <= start:
        logger.warning(
            "billing: line for entity {} records an empty span ({} - {}); sending the "
            "invoice's period instead",
            line.entity_id, start, end,
        )
        start, end = period.start, period.end
    return {"start": int(start.timestamp()), "end": int(end.timestamp())}


def issue_invoice(customer_id: str, invoice: Invoice, *, memo: str | None = None,
                  collect: bool = True, metadata: dict[str, str] | None = None,
                  idempotency_key: str | None = None,
                  payer_user_id: str | None = None,
                  payment_method: str | None = None,
                  billing_group_id: str | None = None) -> dict:
    """Create, finalize and (by default) charge ``invoice`` for ``customer_id``.

    ``payment_method`` NAMES THE CARD. Without it Stripe charges the customer's account
    default, which is what every invoice used to do and is now wrong: a company is billed
    to the card it was nominated onto, and its payer may hold several. It is set on the
    invoice itself rather than by moving the customer default, because moving the default
    to charge one company would repoint every other one mid-run.

    ``billing_group_id`` records WHICH card locally, so dunning can find this document
    again among the payer's other open invoices.

    The order matters. The invoice is created FIRST and each item attached to it by id,
    rather than letting items sit pending and be swept up later: a pending item lands on
    whatever invoice Stripe next generates, which is fine for a cancel extension riding
    an anchor but wrong here, where these lines are the invoice.

    ``pending_invoice_items_behavior="exclude"`` keeps unrelated pending items — a queued
    extension, say — off this one for the same reason.

    Zero-amount lines are skipped: Stripe rejects them, and a 0.00 line says nothing a
    customer needs. A wholly empty invoice is not created at all.

    Pass ``collect=False`` to leave it as a DRAFT for inspection — useful while the
    subscription path is still the one actually billing.

    EVERY invoice sent from here is also recorded locally, and the local row is written
    BEFORE the processor is called. Two different things depend on that ordering:

    * with an ``idempotency_key``, the write is the double-billing GUARD — a unique
      index, so a second attempt at the same period cannot even reach Stripe. If the key
      is already claimed this refuses to charge, which is the whole point;
    * without one (a mid-period purchase, guarded by the user waiting for the response),
      it is only a record, and a failure to write it must never cost the customer their
      purchase. So that case is logged and the charge proceeds.

    Recording only what came BACK would leave the window this table exists to close:
    charge succeeds, runner dies, nothing on disk, next run bills the period again.
    """
    lines = [line for line in invoice.lines if line.amount]
    if not lines:
        logger.info("billing: nothing to invoice for customer {}", customer_id)
        return {}

    record = _reserve(
        customer_id, invoice, lines, memo, idempotency_key, payer_user_id,
        billing_group_id,
    )

    stripe = get_stripe()
    # Bound before the try so the failure path can name the document it left behind: a
    # decline raises from ``Invoice.pay`` with the invoice already finalized and open.
    draft = None
    try:
        options = {"idempotency_key": idempotency_key} if idempotency_key else {}
        # Omitted rather than passed as None: an explicit null would CLEAR the field, and
        # a caller that has no card to name wants Stripe's own resolution (the customer
        # default), not a document with the field wiped.
        if payment_method:
            options["default_payment_method"] = payment_method
        draft = stripe.Invoice.create(
            customer=customer_id,
            currency=invoice.currency,
            auto_advance=False,
            collection_method="charge_automatically",
            pending_invoice_items_behavior="exclude",
            description=memo,
            metadata=metadata or {},
            **options,
        )
        # Stamped the moment Stripe acknowledges the invoice exists, so the "we claimed
        # the key but never heard back" window is exactly the create call and nothing
        # more. Everything after this point is recoverable by id.
        _settle(record, external_id=draft.get("id"), status=draft.get("status"))
        # Created in REVERSE, because Stripe renders invoice items newest-first: items
        # made in order come out backwards, which puts a swap's charge above the credit
        # that explains it. Cosmetic only -- order never changes what is owed -- and the
        # newest-first behaviour is observed rather than documented, so the worst case if
        # it ever changes is that lines read in the other order again.
        for line in reversed(lines):
            stripe.InvoiceItem.create(
                customer=customer_id,
                invoice=draft["id"],
                currency=invoice.currency,
                amount=line.amount,
                description=line.description,
                # The entity travels ON the line. Stripe stamps a subscription's metadata
                # onto every line it owns, which names one entity for all of them; this
                # is written per item and stays correct.
                metadata={"entity_id": line.entity_id},
                # The days THIS line pays for, not the invoice's (``_item_period``).
                period=_item_period(line, invoice.period),
            )
        if not collect:
            held = stripe.Invoice.retrieve(draft["id"])
            _settle(record, **_record_of(held))
            return held

        finalized = stripe.Invoice.finalize_invoice(draft["id"])
        # Recorded BEFORE the payment attempt, because a decline raises: without this the
        # local row would still say "draft" for an invoice that is really open and owed,
        # and dunning chases what is open.
        _settle(record, **_record_of(finalized))
        # A finalized invoice with charge_automatically is normally collected by Stripe,
        # but not synchronously — pay() makes the outcome available now, so a decline
        # surfaces to the caller instead of arriving by webhook later.
        if finalized.get("status") == "open":
            finalized = stripe.Invoice.pay(draft["id"])
            _settle(record, **_record_of(finalized))
        # After the money has moved and been recorded, never before: this is history for
        # the invoice list and must not be able to affect the charge. See the function.
        if finalized.get("status") == "paid":
            _capture_payment_method(record, draft["id"])
        return finalized
    except Exception as exc:
        logger.exception(
            "billing: could not issue invoice for customer {} ({} line(s), total {})",
            customer_id, len(lines), invoice.total,
        )
        raise BillingError(
            f"could not issue invoice for {customer_id}",
            user_message=getattr(exc, "user_message", None),
            invoice_id=(draft or {}).get("id"),
        ) from exc


#: What ``retry_invoice`` reports for an invoice the processor will no longer collect: a
#: marker for ``dunning``, which re-issues the invoice (``refresh_invoice``) and charges the
#: replacement instead. Never a message - nothing a customer reads may carry it.
DEAD_PAYMENT = "payment_intent_canceled"

#: The metadata a replacement carries naming the invoice it replaces.
REPLACES = "replaces"


def _payment_is_dead(invoice) -> bool:
    """Whether the processor has cancelled this OPEN invoice's payment for good.

    Stripe cancels a PaymentIntent once it has been confirmed too many times - "a variable
    upper limit on how many times a PaymentIntent can be confirmed", ten declines in our
    account - and cancellation "can't be undone": the invoice keeps pointing at the dead one,
    and every later pay is refused ("This invoice can no longer be paid"). ``payment_intent``
    has to be EXPANDED for this to see it.
    """
    if (invoice.get("status") or "") != "open":
        return False
    intent = invoice.get("payment_intent")
    return isinstance(intent, dict) and intent.get("status") == "canceled"


def _died(invoice_id: str) -> bool:
    """Re-read after a failed pay: did THAT call cancel the payment? Never raises.

    The call that crosses the processor's limit is the one that cancels - and it fails with an
    invalid-request error, not a decline - so the invoice's STATE decides, not the error.
    """
    try:
        return _payment_is_dead(
            get_stripe().Invoice.retrieve(invoice_id, expand=["payment_intent"])
        )
    except Exception:
        logger.warning("dunning: could not re-check invoice {} after a failed retry", invoice_id)
        return False


def retry_invoice(invoice_id: str,
                  payment_method: str | None = None) -> tuple[bool, str | None]:
    """Attempt payment on an already-finalized invoice. Returns ``(paid, reason)``.

    Used by the dunning run. Returns rather than raises because a decline is the EXPECTED
    outcome here, not an error — a failed retry is data the schedule acts on, and raising
    would make the caller treat "the card was declined" the same as "the processor is
    down", which need opposite responses.

    ``payment_method`` IS THE CARD TO TRY NOW, and passing it is what makes recovery
    possible at all. ``issue_invoice`` pins the card onto the document, so Stripe would
    otherwise keep retrying the very card that declined — for the whole retry schedule,
    however many times the payer replaced it. (Before invoices named a card, ``pay`` fell
    back to the customer default, and replacing that default healed the account by
    accident.) The caller passes the group's CURRENT card, so a replacement is picked up
    on the next retry. Omitted, Stripe falls back to whatever the invoice already names.

    An invoice that is already paid returns ``(True, None)``: someone may have paid it
    out of band between the attempt being scheduled and it running.

    An invoice the processor will NEVER collect returns ``(False, DEAD_PAYMENT)`` - checked
    before the attempt, and again after a failed one, because the call that crosses the
    processor's confirmation limit is itself the one that cancels. Nothing is paid or
    recorded for it here; ``dunning`` re-issues it and charges the replacement.

    Whatever the outcome, the local record is brought up to date. A recovered invoice
    that still reads "open" months later would make the table lie about the one thing it
    is for — a declined renewal is precisely the case someone asks "was I charged?" of.
    """
    stripe = get_stripe()
    try:
        invoice = stripe.Invoice.retrieve(invoice_id, expand=["payment_intent"])
        if invoice.get("status") == "paid":
            record = _local(invoice_id)
            _settle(record, **_record_of(invoice))
            _capture_payment_method(record, invoice_id)
            return True, None
        if _payment_is_dead(invoice):
            # Not paid, and not settled either: an earlier refresh may already have marked
            # the local row void, and that must not be overwritten with "open".
            logger.info("dunning: invoice {} can no longer be paid (its payment was "
                        "cancelled by the processor)", invoice_id)
            return False, DEAD_PAYMENT
        paid = stripe.Invoice.pay(
            invoice_id, **({"payment_method": payment_method} if payment_method else {})
        )
        record = _local(invoice_id)
        _settle(record, **_record_of(paid))
        # Recorded HERE as well as on the first charge, and this is the case that needs
        # it most: a renewal that declined and later recovered was paid by a DIFFERENT
        # card than the one that failed — replacing it is usually how it recovered. An
        # invoice with no card against it is exactly the one somebody asks "what
        # eventually paid this?" of.
        if paid.get("status") == "paid":
            _capture_payment_method(record, invoice_id)
        return paid.get("status") == "paid", None
    except Exception as exc:
        reason = getattr(exc, "user_message", None) or str(exc)
        logger.info("dunning: retry of invoice {} failed: {}", invoice_id, reason)
        if _died(invoice_id):
            return False, DEAD_PAYMENT
        return False, reason


def open_invoices(customer_id: str) -> list[dict]:
    """The customer's finalized-but-unpaid invoices, oldest first — what dunning chases.

    Pages in full. ``limit`` is Stripe's PAGE SIZE, not a cap, and reading only the first
    page truncated silently in the worst possible direction: dunning chases
    ``invoices[0]``, the OLDEST open invoice, so a payer with more than a page of history
    could have their genuine oldest debt fall outside the window entirely. Worse, an
    empty first page reads as "settled elsewhere" and ends dunning as recovered — see
    the no-invoices branch in ``dunning.collect_due``.
    """
    listing = get_stripe().Invoice.list(customer=customer_id, status="open", limit=100)
    return sorted(listing.auto_paging_iter(), key=lambda i: i.get("created") or 0)


def void_invoice(invoice_id: str) -> None:
    """Void a finalized invoice, or delete it if still a draft. Mirrors it locally.

    Finalized invoices cannot be deleted — only voided — and neither their lines nor
    their memo can be edited afterwards. So anything needing correction has to be caught
    while it is still a draft.

    The LOCAL row is marked too, and that is not bookkeeping. ``dunning.collect_due``
    chases whatever the processor still reports as open, and the invoice list, the
    revenue figures and ``_already_invoiced`` all read the local row — so voiding in one
    place only would leave a document that is dead to Stripe and live to Minty.

    The row is kept rather than deleted. ``discard_invoice`` exists for a reservation the
    processor never saw; this one WAS raised, and may well have been seen by the customer
    before it was withdrawn. That is history, not a mistake to erase.
    """
    stripe = get_stripe()
    invoice = stripe.Invoice.retrieve(invoice_id)
    if invoice.get("status") == "draft":
        stripe.Invoice.delete(invoice_id)
    else:
        stripe.Invoice.void_invoice(invoice_id)
    _settle(_local(invoice_id), status="void")


def _items_of(stripe, invoice_id: str) -> list:
    """Every invoice item on ``invoice_id``, as the processor lists them (newest first)."""
    return list(stripe.InvoiceItem.list(invoice=invoice_id, limit=100).auto_paging_iter())


def _signature(item) -> tuple:
    """What makes two invoice items the same charge, for not copying one twice."""
    return (int(item.get("amount") or 0), item.get("description"),
            (item.get("metadata") or {}).get("entity_id"))


def refresh_invoice(invoice_id: str, payment_method: str | None = None) -> dict | None:
    """Re-issue an invoice the processor will no longer collect. Returns the replacement.

    Stripe cancels an invoice's payment for good once it has been confirmed too many times
    (see ``_payment_is_dead``), and from then on the invoice can never be paid - so the
    customer who fixes their card on day eleven could not pay us at all. The only way to
    collect is a NEW invoice: the same lines, the same period, the same metadata (the renewal
    key dunning and the renewal run find it by) plus ``replaces``, on the card named here.

    The steps, in the ORDER that makes every interruption resumable:

      1. claim the replacement locally under ``store.refresh_key`` - a copy of the dead one's
         recorded lines, never re-priced (a cancellation's extension, already marked
         invoiced, would be lost to a rebuild);
      2. create it at the processor (found by ``replaces`` if an earlier attempt already did),
         copy the dead invoice's items onto it, and finalize it - which does not charge;
      3. hand the dead one's key to it and mark the dead one void (``store.supersede_invoice``);
      4. only THEN void the dead one at the processor.

    The dead invoice is voided only after the replacement is open, so the processor's open
    list always holds one of the two: dunning's "nothing open, so the debt was settled
    elsewhere" can never see a half-finished refresh and restore access unpaid. And every
    interrupted state still has the dead invoice open with its payment cancelled, so the next
    attempt - scheduled or pressed - walks back in through the same door and finishes the job.

    Returns the replacement, open and not yet charged (or already paid, when an interrupted
    refresh is resumed after the payment went through); ``None`` when it cannot be re-issued
    automatically - no local record of what was sent, or a record that disagrees with the
    processor's - which is logged as an ERROR naming the invoice to fix by hand. Raises when
    the processor or the database cannot be reached; calling again resumes.
    """
    from collections import Counter

    from billing.services import store
    from billing.services.billing import Line, Period

    stripe = get_stripe()
    claim = store.refresh_key(invoice_id)
    dead = stripe.Invoice.retrieve(invoice_id, expand=["payment_intent"])
    record = _local(invoice_id)
    if record is None:
        logger.error(
            "billing: invoice {} can no longer be paid and has no local record to re-issue "
            "it from - void and re-issue it by hand", invoice_id,
        )
        return None
    customer = dead.get("customer") or record.stripe_customer_id

    row = store.replacement_of(record)
    if row is None:
        if not _payment_is_dead(dead):
            logger.warning(
                "billing: asked to re-issue invoice {}, but it is {} and its payment is not "
                "cancelled; leaving it", invoice_id, dead.get("status"),
            )
            return None
        lines = store.invoice_lines(record.id)
        items = _items_of(stripe, invoice_id)
        recorded = sum(int(line.amount) for line in lines)
        sent = sum(int(item.get("amount") or 0) for item in items)
        if not lines or len(items) != len(lines) or recorded != sent \
                or recorded != int(record.total or 0):
            logger.error(
                "billing: invoice {} can no longer be paid, and what was recorded does not "
                "match what was sent ({} line(s) totalling {} here, {} item(s) totalling {} at "
                "the processor) - void and re-issue it by hand",
                invoice_id, len(lines), recorded, len(items), sent,
            )
            return None
        row = store.reserve_invoice(
            payer_user_id=record.payer_user_id,
            stripe_customer_id=record.stripe_customer_id,
            period=Period(record.period_start, record.period_end),
            currency=record.currency,
            lines=[
                Line(
                    entity_id=str(line.entity_id), entity_name=line.entity_name,
                    product_name=line.product_name, amount=int(line.amount), kind=line.kind,
                    at=line.at, period_start=line.period_start, period_end=line.period_end,
                    unit_amount=line.unit_amount,
                )
                for line in lines
            ],
            memo=record.memo,
            idempotency_key=claim,
            billing_group_id=record.billing_group_id,
        )
        # Claimed, THEN looked at again. A run that finished the whole refresh between the
        # check above and this claim has moved the key off ``record`` - and released the
        # refresh key doing it, which is exactly why this claim could succeed. Ours is then a
        # second claim on a replacement that already exists: dropped, since the processor
        # never saw it.
        fresh = store.invoice_for_external_id(invoice_id)
        if row is not None and fresh is not None \
                and fresh.idempotency_key != record.idempotency_key:
            store.discard_invoice(row.id)
            row = None
        if row is None:
            row = store.replacement_of(fresh or record)
        if row is None:
            raise BillingError(f"could not claim a replacement for invoice {invoice_id}")

    if not row.external_id:
        replacement = find_invoice_by_metadata(customer, REPLACES, invoice_id, live_only=True)
        if replacement is None:
            options = {"idempotency_key": claim}
            if payment_method:
                options["default_payment_method"] = payment_method
            replacement = stripe.Invoice.create(
                customer=customer,
                currency=dead.get("currency"),
                auto_advance=False,
                collection_method="charge_automatically",
                pending_invoice_items_behavior="exclude",
                description=dead.get("description"),
                metadata={**dict(dead.get("metadata") or {}), REPLACES: invoice_id},
                **options,
            )
        # Straight to the store, not ``_settle``: no money has moved, so a failure to record
        # is a reason to STOP - the reservation stays, and the next attempt resumes from it.
        store.settle_invoice(row.id, external_id=replacement["id"],
                             status=replacement.get("status"))
    else:
        replacement = stripe.Invoice.retrieve(row.external_id)

    if replacement.get("status") == "draft":
        # Created in the dead invoice's order (it lists newest first), so the replacement
        # reads exactly as the original did. Items already on it - an interrupted copy - are
        # skipped; the per-item keys stop a concurrent copy from adding them twice.
        on_it = Counter(_signature(item) for item in _items_of(stripe, replacement["id"]))
        for index, item in enumerate(reversed(_items_of(stripe, invoice_id))):
            signature = _signature(item)
            if on_it[signature]:
                on_it[signature] -= 1
                continue
            period = item.get("period") or {}
            stripe.InvoiceItem.create(
                customer=customer,
                invoice=replacement["id"],
                currency=item.get("currency") or dead.get("currency"),
                amount=item["amount"],
                description=item.get("description"),
                metadata=dict(item.get("metadata") or {}),
                period={"start": period.get("start"), "end": period.get("end")},
                idempotency_key=f"{claim}-item-{index}",
            )
        # Finalizing does not charge: the replacement is ``auto_advance=False``, like every
        # invoice raised here. The caller charges it, in the same attempt.
        replacement = stripe.Invoice.finalize_invoice(
            replacement["id"], idempotency_key=f"{claim}-finalize"
        )

    store.settle_invoice(row.id, **_record_of(replacement))
    if replacement.get("status") not in ("open", "paid"):
        logger.error(
            "billing: the replacement {} for invoice {} is {}, not collectable - void and "
            "re-issue it by hand", replacement["id"], invoice_id, replacement.get("status"),
        )
        return None

    store.supersede_invoice(record.id, row.id)
    if (dead.get("status") or "") == "open":
        try:
            stripe.Invoice.void_invoice(invoice_id)
        except Exception:
            # It can never be paid, and the replacement is the debt now; the next attempt
            # that meets it voids it again.
            logger.exception(
                "billing: re-issued invoice {} as {}, but could not void the original",
                invoice_id, replacement["id"],
            )
    logger.info("billing: invoice {} could no longer be paid; re-issued as {}",
                invoice_id, replacement["id"])
    return replacement
