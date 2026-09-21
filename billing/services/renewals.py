"""Build a payer's next invoice from Minty's own records.

PHASE 5, first slice. This is what replaces the Stripe subscription: instead of Stripe
deciding what a payer owes each period, the amount is assembled here from the module
rows and the in-house plan catalog, and handed to ``billing_gateway`` to collect.

Nothing here charges anyone. ``build_renewal`` is a pure-ish read: rows and catalog in,
a ``billing.Invoice`` out. Issuing it is a separate, deliberate call — which keeps this
safe to run in shadow against live data and diff against what Stripe actually billed.

TWO RULES THAT DECIDE THE AMOUNT, both already proven elsewhere:

* one line per ENTITY, priced by the SET of modules it bills. Two modules on one entity
  are the bundle price, not the sum — the bundle IS the discount, so summing standalone
  prices would overcharge. ``billing_plan`` is keyed by the code set for exactly this.
* only modules that will actually be charged again count
  (``access.is_billing_forward``). A trial is free, and a cancelling module is winding
  down; billing either would charge for something the customer was told was not coming.
"""
from __future__ import annotations

from datetime import UTC, datetime

from billing.services import access, store
from billing.services._log import logger
from billing.services.billing import (
    Invoice,
    Line,
    Period,
    period_containing,
    renewal_invoice,
    renewal_memo,
)


def _entity_names(entity_ids) -> dict[str, str]:
    """{entity_id: name} in one query — the invoice needs a name per line."""
    if not entity_ids:
        return {}
    from shared_models.models import Entity

    rows = Entity.objects.filter(id__in=[str(e) for e in entity_ids])
    return {str(e.id): (e.name or "").strip() or str(e.id) for e in rows}


def entities_billed_in(user_id, period: Period) -> set[str]:
    """Entities already charged for ``period`` by something other than a renewal.

    A purchase, a module change and a trial conversion all bill their own entity for the
    period they land in — in full at the boundary, prorated inside it. Renewing that
    entity for the same period would charge twice for the same days.

    ``first_billed_at`` is the FIRST charge and never moves afterwards, so it falls
    inside exactly one period: the one the entity started paying in, which is the one to
    skip. Every later period has it in the past and renews normally.
    """
    billed = set()
    for row in store.module_rows_for_payer(user_id):
        at = getattr(row, "first_billed_at", None)
        if at is None:
            continue
        # Rows read back from some drivers lose their tzinfo; comparing those against an
        # aware period raises rather than answering, and an exception here would stop the
        # payer being billed at all.
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if period.start <= at < period.end:
            billed.add(str(row.entity_id))
    return billed


def _covered_entities(user_id, period: Period, *, through_end: bool) -> set[str]:
    """Entities of this payer whose ``billed_through`` claim reaches into ``period``.

    The claim is money already collected for the entity from somewhere other than this
    payer's renewal cycle — today, only a subscriber transfer, which invoices the new
    payer at accept for the window the old payer's payment did not reach.

    ``through_end`` picks which of the two questions is being asked:

    * False — "does the claim reach INTO this period?" (``> period.start``). These must
      not be billed again. Strictly greater, so a claim landing exactly on the boundary
      does not suppress the period that begins there: half-open periods tile, and the
      instant that ends one begins the next.
    * True — "does the claim cover this period ENTIRELY?" (``>= period.end``). These are
      settled, so the account's cycle still has to advance over them.

    Billing-forward rows only. A cancelled or terminated row keeps whatever claim it had,
    and letting a dead row's stale date suppress a live entity's renewal would give the
    days away.
    """
    covered = set()
    for row in store.module_rows_for_payer(user_id):
        claim = getattr(row, "billed_through", None)
        if claim is None:
            continue
        if not access.is_billing_forward(phase=row.phase):
            continue
        # Same defence ``entities_billed_in`` needs: some drivers hand back naive
        # datetimes, and comparing one against an aware period raises rather than
        # answering — which here would stop the payer being billed at all.
        if claim.tzinfo is None:
            claim = claim.replace(tzinfo=UTC)
        reaches = claim >= period.end if through_end else claim > period.start
        if reaches:
            covered.add(str(row.entity_id))
    return covered


def entities_covered_into(user_id, period: Period) -> set[str]:
    """Entities this renewal must NOT bill — someone else's money already bought part of
    this period for them. See ``_covered_entities``."""
    return _covered_entities(user_id, period, through_end=False)


def entities_covered_past(user_id, period: Period) -> set[str]:
    """Entities whose claim covers this period outright, so the account's cycle must
    advance even though nothing was invoiced. See ``_covered_entities``."""
    return _covered_entities(user_id, period, through_end=True)


def billable_codes_by_entity(
    user_id, period: Period | None = None, *, group_id=None
) -> dict[str, set[str]]:
    """{entity_id: {module codes}} this payer will be charged for next period.

    ``period`` drops the entities already charged for it — see ``entities_billed_in`` for
    an entity that bought its own way into this period, and ``entities_covered_into`` for
    one carrying a transfer's claim over it. Omitting it answers the looser question "is
    there anything on this account at all", which is what ``due_renewals`` needs before a
    period has even been chosen.

    ``group_id`` narrows it to the companies nominated onto ONE card, which is what a
    renewal actually bills: one invoice per card, charged to that card. Omitting it
    answers for the whole account, which no longer corresponds to a single document and
    is used only for "is there anything here at all".

    A company nominated onto NO card is excluded either way and logged. There is
    deliberately no fallback to the account default — see ``store.card_for_entity`` — so
    the alternative to skipping it is charging a card the payer never chose for it.
    """
    already = (
        entities_billed_in(user_id, period) | entities_covered_into(user_id, period)
        if period is not None
        else set()
    )
    in_group = store.entity_ids_in_group(group_id) if group_id else None
    by_entity: dict[str, set[str]] = {}
    unnominated: set[str] = set()
    for row in store.module_rows_for_payer(user_id):
        if not access.is_billing_forward(phase=row.phase):
            continue
        entity_id = str(row.entity_id)
        if entity_id in already:
            continue
        if in_group is not None:
            if entity_id not in in_group:
                continue
        elif store.billing_group_for_entity(entity_id, user_id) is None:
            unnominated.add(entity_id)
            continue
        by_entity.setdefault(entity_id, set()).add(row.function_code.upper())
    for entity_id in sorted(unnominated):
        logger.error(
            "renewal: entity {} is billing forward but has no payment method "
            "nominated; it will not be billed until one is chosen",
            entity_id,
        )
    return by_entity


def build_renewal(user_id, period: Period, *, group_id=None) -> Invoice | None:
    """What this CARD owes for ``period``, or None if it owes nothing.

    Returns None rather than an empty invoice: a card whose companies have all lapsed or
    gone to trial has nothing to collect, and issuing a zero invoice would put a
    meaningless document in front of the payer every month.

    ``group_id`` is the card. One is built per group, so a payer with two cards gets two
    invoices for the same period — each priced from its own companies and charged to its
    own card. Omitting it prices the payer's whole account, which is what the shadow
    reports and the tests written before per-entity cards still ask for.

    A combination the catalog cannot price is SKIPPED and logged, never guessed at. The
    alternative — falling back to a sum of standalone prices — silently overcharges by
    the bundle discount, which is the kind of error nobody notices until a customer does.
    """
    # NOT an early return on "nothing renewing": a payer whose last entity was
    # cancelled has no billable modules but may still owe a cancel-extension, and
    # bailing here would give those days away.
    by_entity = billable_codes_by_entity(user_id, period, group_id=group_id)
    names = _entity_names(by_entity.keys())
    entries: list[tuple[str, str, str, int]] = []
    currency = None
    for entity_id, codes in sorted(by_entity.items(), key=lambda kv: names.get(kv[0], "")):
        plan = store.billing_plan_for_codes(codes)
        if plan is None:
            logger.error(
                "renewal: no plan prices {} for entity {}; skipping the line rather "
                "than guessing a price",
                ",".join(sorted(codes)),
                entity_id,
            )
            continue
        currency = currency or plan.currency
        entries.append((entity_id, names.get(entity_id, entity_id), plan.display_name,
                        plan.amount))

    extensions = _pending_extension_lines(user_id, names, group_id=group_id)
    if not entries and not extensions:
        return None

    if currency is None:
        # Nothing renewing — this invoice is extensions only, so the price catalog was
        # never consulted. Fall back to the currency the payer has been billed in.
        _anchor, currency = store.billing_cycle_for_user(user_id)
    # Cancel-extensions ride this invoice rather than being charged at cancellation
    # time, so that cancelling never depends on a card clearing. Under Stripe they were
    # a pending invoice item swept onto the anchor invoice; the runner plays that role
    # now — and unlike Stripe it fires even when this was the payer's LAST entity.
    invoice = renewal_invoice(entries, period, (currency or "").lower())
    if not extensions:
        return invoice
    return Invoice(
        currency=invoice.currency,
        period=invoice.period,
        lines=invoice.lines + tuple(extensions),
    )


def _extension_product(code) -> str:
    """What to call ONE module on an extension line — the name the catalog sells it under.

    This used to title-case the CODE, which is not a product name and only ever looked
    like one by accident: "PETTY_CASH" happens to come out "Petty Cash", so the bug was
    invisible until a Payment Request extension printed "Bill" — a word that appears
    nowhere in the catalog, on an invoice whose other lines said "Payment Request".

    Priced as a ONE-MODULE set rather than by the entity's whole set: the extension is
    for the single module that was cancelled, and the bundle name would claim the
    customer was charged for something they still hold. Falls back to the old title-cased
    code (and logs) rather than failing a renewal over a label — a missing catalog row
    must not stop the money being collected.
    """
    key = str(code or "").strip().upper()
    try:
        plan = store.billing_plan_for_codes([key])
    except Exception:
        plan = None
        logger.exception("renewal: could not read the catalog name for {}", key)
    if plan and plan.display_name:
        return plan.display_name
    logger.warning(
        "renewal: no catalog name for module {}; naming the extension line after the "
        "code instead",
        key,
    )
    return key.title().replace("_", " ")


def _pending_extension_lines(
    user_id, names: dict[str, str], *, group_id=None
) -> list[Line]:
    """Lines for cancel-extensions this payer owes but has not been billed for.

    ``group_id`` puts each one on the invoice of the card its OWN company is billed to.
    A cancellation fee belongs to the company it was charged for, so it rides that card's
    document — not whichever of the payer's invoices happens to be raised first.

    An extension for a company with no nomination left (the payer removed the card, or
    the company was never nominated) rides the payer's FIRST group, so the charge is not
    silently lost. It is money already promised in exchange for access already granted;
    dropping it because a pointer is missing would give those days away.
    """
    if group_id:
        in_group = store.entity_ids_in_group(group_id)
        groups = store.billing_groups_for_payer(user_id)
        is_first_group = bool(groups) and str(groups[0].id) == str(group_id)
    lines: list[Line] = []
    for row in store.pending_extensions_for_payer(user_id):
        entity_id = str(row.entity_id)
        if group_id:
            if entity_id not in in_group:
                homeless = store.billing_group_for_entity(entity_id, user_id) is None
                if not (homeless and is_first_group):
                    continue
        name = names.get(entity_id) or _entity_names({entity_id}).get(entity_id, entity_id)
        lines.append(
            Line(
                entity_id=entity_id,
                entity_name=name,
                product_name=f"{_extension_product(row.function_code)} "
                             "(access after cancellation)",
                amount=int(row.extension_amount or 0),
            )
        )
    return lines


def due_renewals(now: datetime) -> list:
    """Cards whose current period has ended and which need their next invoice.

    Returns ``(account, group, paid_through)`` per due CARD. A payer with two cards can
    have one due and one not — each buys its own periods for its own companies — so the
    work list is per group, while the anchor that shapes the period stays on the account.

    Driven off the group's stored ``paid_through``, not a date derived from the anchor and
    not a per-row copy. A derived period always contains "now" so it can never come due; a
    per-row copy drifts between a payer's entities, and taking the oldest of several
    disagreeing rows would re-bill a period already collected.
    """
    due = []
    anchors = {
        str(account.user_id): account for account in store.accounts_with_billing()
    }
    for group in store.groups_with_billing():
        account = anchors.get(str(group.payer_user_id))
        if account is None or account.anchor_at is None:
            continue  # nothing has ever been billed for this payer
        if group.paid_through is None:
            continue  # this card has never collected; nothing to renew from
        if group.paid_through > now:
            continue
        if not billable_codes_by_entity(
            account.user_id, group_id=group.id
        ) and not _pending_extension_lines(
            account.user_id, {}, group_id=group.id
        ):
            # Nothing renewing AND nothing owed on this card. A card whose LAST company
            # was cancelled has no billable modules but still owes the extension the
            # customer was promised access for — dropping it would give those days away.
            continue
        due.append((account, group, group.paid_through))
    return due


def period_key(user_id, period: Period, group_id=None) -> str:
    """Stable id for "this CARD's invoice for this period".

    Claimed under the UNIQUE index on ``subscription_invoice.idempotency_key`` before the
    charge, so a runner that crashes between charging and recording cannot bill the same
    period twice. Also Stripe's idempotency key and the invoice's ``renewal_key``
    metadata — the latter is what ``_already_invoiced`` falls back to when a reservation
    was never confirmed sent.

    THE GROUP IS PART OF THE KEY, and has to be: a payer with two cards raises two
    invoices for one period, and under a payer-and-period key the second would collide
    with the first and be refused as a double-bill — so one card's companies would simply
    never be charged.

    Omitting the group reproduces the old two-part key. That is what every invoice raised
    before per-entity cards carries, and those keys are claimed forever (a voided invoice
    keeps its row), so the two forms have to coexist rather than one replacing the other.

    Stable by construction: derived from the payer, the group and the period START, so it
    survives a process restart. Anything built off "now" would not.
    """
    stem = f"renewal-{user_id}-{period.start:%Y%m%d}"
    return f"{stem}-{group_id}" if group_id else stem


def _already_invoiced(
    customer_id, key: str, *, metadata_key: str = "renewal_key"
) -> str | None:
    """The status of the invoice already raised under ``key``, or None if there is none.

    ``metadata_key`` is the field the fallback scan matches on, and exists so the payer
    TRANSFER charge can reuse this rather than reimplement it. The three-way resolution
    below — confirmed / reserved-but-unknown / never existed — is the part that must not
    be written twice: getting it subtly wrong either charges someone twice or blocks a
    charge forever, and both failures are silent.

    ONE INDEXED LOOKUP, where this used to LIST the payer's Stripe invoices and scan
    their metadata — once per payer, every renewal run. The local row is written before
    the charge (``billing_gateway.issue_invoice``) under a UNIQUE index, so its presence
    is a stronger answer than a search that came back empty: a search can miss, a unique
    constraint cannot.

    THE ONE CASE THE ROW CANNOT ANSWER is a reservation with no ``external_id``: the key
    was claimed and then nothing came back, so we do not know whether Stripe created the
    invoice. Guessing either way is a real error — assume it exists and the payer is
    never billed for the period; assume it does not and they may be billed twice. So this
    is the one path that still asks the processor, and it costs a scan only after a run
    died mid-charge rather than on every renewal.

    If the processor has never heard of it, the reservation is DISCARDED so the retry can
    proceed. Leaving it would block that period's invoice permanently — the guard turned
    into a hold on a charge nobody ever made.
    """
    record = store.invoice_for_key(key)
    if record is None:
        return None
    if record.external_id:
        return record.status

    from billing.services import billing_gateway

    logger.warning(
        "renewal: {} was reserved but never confirmed sent; asking the processor", key
    )
    existing = billing_gateway.find_invoice_by_metadata(
        customer_id, metadata_key, key
    )
    if existing is None:
        store.discard_invoice(record.id)
        return None
    # It does exist — record what was found so the next run needs no scan at all.
    store.settle_invoice(
        record.id, external_id=existing.get("id"), status=existing.get("status")
    )
    return existing.get("status")


class _AllPayers:
    """Sentinel for "every payer" — see ``ALL_PAYERS``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ALL_PAYERS"


# Passed as ``scope`` to bill everybody. It exists so that billing the whole customer
# base has to be TYPED rather than obtained by leaving an argument out.
#
# This is not hypothetical caution. A global renewal run driven by an injected test
# clock charged a real payer twice for catch-up periods: the runner was correct, the
# clock belonged to a different customer, and nothing in the signature made the blast
# radius visible at the call site. A harness now cannot reach every payer by accident —
# it has to ask for them by name.
ALL_PAYERS = _AllPayers()


def run_renewals(now: datetime, *, scope, issue: bool = False,
                 limit: int | None = None) -> dict:
    """Bill the CARDS whose period has ended. SHADOW by default.

    ``scope`` is required and has no default: either an iterable of user ids, or
    ``ALL_PAYERS``. Omitting it is a TypeError rather than a full sweep. It still scopes
    by PAYER even though the unit of work is now a card — "bill this person" is the
    question an operator asks, and a payer's cards come along with them.

    With ``issue=False`` this computes what each card would be charged and changes
    nothing — the safe way to run it against live data for a cycle and compare with what
    Stripe actually billed, before it is ever allowed to take money.

    Returns ``{"planned": [...], "issued": [...], "failed": [...], "skipped": [...]}``,
    one entry per card rather than per payer: a payer with two cards appears twice, and
    can legitimately be in ``issued`` and ``failed`` at once. That is the containment
    working — the healthy card's companies are paid for and stay up.

    Ordering is deliberate and matters more than it looks:

    1. a local invoice already carrying this period's key means the money was collected
       on an earlier run that died before recording it — so ADOPT it, advance
       ``paid_through``, and charge nothing. This is the case a naive runner
       double-bills. See ``_already_invoiced``;
    2. issue and collect, charged to THIS card;
    3. only on success advance the card's ``paid_through``. Advancing first would skip the
       period forever if the charge then failed — the customer gets a free month and
       nothing ever notices;
    4. on failure start dunning FOR THIS CARD, which is what turns a declined renewal into
       the retry schedule rather than a silent lapse — and confines it to the companies
       that card actually pays for.
    """
    planned: list[dict] = []
    issued: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []

    wanted = None if scope is ALL_PAYERS else {str(u) for u in scope}
    candidates = [
        (account, group, paid_through)
        for account, group, paid_through in due_renewals(now)
        if wanted is None or str(account.user_id) in wanted
    ]

    for account, group, paid_through in candidates[: limit or None]:
        _out = _renew_one_group(account, group, paid_through, now, issue)
        planned.extend(_out["planned"])
        issued.extend(_out["issued"])
        failed.extend(_out["failed"])
        skipped.extend(_out["skipped"])

    # Emails go out only after the whole batch has been billed and committed. Sending
    # inside the loop would put SMTP latency between two payers' charges, and would tell
    # a customer about a charge that a later exception could still roll back.
    _notify_renewals(issued, failed)

    return {"planned": planned, "issued": issued, "failed": failed, "skipped": skipped}


def _notify_renewals(issued: list[dict], failed: list[dict]) -> None:
    """Mail the receipts and the declines. Never raises — see ``notify``.

    Deduped on the period key, which is what makes a re-run harmless: the same payer and
    period produce the same key, so a job that is run twice in a day charges once (by
    ``_already_invoiced``) and mails once (by the email log).
    """
    from billing.services import notify

    # The GROUP is in the dedupe key for the same reason it is in the idempotency key: a
    # payer with two cards has two outcomes for one period, and a payer-and-period key
    # would silence the second — most damagingly when one card was paid and the other
    # declined, which is exactly the message they need.
    events = []
    for entry in issued:
        period = Period(start=entry["period_start"], end=entry["period_end"])
        events.append(
            (entry["user_id"], notify.RENEWAL_PAID,
             period_key(entry["user_id"], period, entry.get("billing_group_id")), entry)
        )
    for entry in failed:
        period = Period(start=entry["period_start"], end=entry["period_end"])
        events.append(
            (entry["user_id"], notify.RENEWAL_FAILED,
             period_key(entry["user_id"], period, entry.get("billing_group_id")), entry)
        )
    notify.notify_many(events)


def next_period(anchor: datetime, paid_through: datetime) -> Period:
    """The period that follows what has been paid for.

    Derived from the anchor so month-end billing stays correct — 31 Jan clamps to 28 Feb
    and springs back to 31 Mar — rather than by adding a month to the previous end,
    which would peg a month-end payer to the 28th permanently.
    """
    return period_containing(anchor, paid_through)


def _renew_one_group(account, group, paid_through, now, issue) -> dict[str, list]:
    """Bill ONE card for the period it is due, or say why it was not billed.

    Returns the four outcome buckets for this card. They are append-only, so the
    caller simply extends its own -- a card can be ``skipped`` for one reason and
    never touch the others, and the split cannot reorder or drop an outcome.

    With ``issue=False`` this fills ``planned`` and writes nothing, which is the
    shadow mode the caller documents: run it against live data and compare with what
    Stripe actually billed before letting it take money.

    ``continue`` in the loop this came from meant "done with this card", which is a
    ``return`` now the body is a function.
    """
    from billing.services import billing_gateway

    out: dict[str, list] = {"planned": [], "issued": [], "failed": [], "skipped": []}
    user_id = account.user_id
    period = next_period(account.anchor_at, paid_through)
    invoice = build_renewal(user_id, period, group_id=group.id)
    entry = {
        "user_id": user_id,
        "billing_group_id": group.id,
        "period_start": period.start,
        "period_end": period.end,
        "total": invoice.total if invoice else 0,
        "lines": [line.description for line in invoice.lines] if invoice else [],
        # Carried so the receipt / decline email can state the amount in the right
        # currency without re-deriving the payer's invoice from scratch.
        "currency": invoice.currency if invoice else None,
    }
    if invoice is None:
        # Two different nothings. "Nothing left on this account" leaves the cycle
        # alone — advancing it would hand a lapsed payer free periods forever. "This
        # period was already paid for, just not by a renewal" has to advance it: the
        # entity that converted or bought on the boundary covered the period in its
        # own invoice, and leaving ``paid_through`` behind would make the account
        # permanently due, re-checked every day, and — once past the grace window —
        # revoked for non-payment it had actually made.
        #
        # A transferred entity reaches this the same way: excluded from the invoice
        # by ``entities_covered_into`` because it was already paid for at accept. The
        # exclusion MUST be paired with the advance — suppressing the line while
        # leaving ``paid_through`` behind turns one avoided double-charge into a
        # guaranteed one, because the account stays due and is re-billed the next day.
        #
        # Both questions are asked of THIS CARD's companies only. Another card's
        # entity covering its own period says nothing about whether this one is
        # settled, and advancing on it would hand this card's companies a free month.
        in_group = store.entity_ids_in_group(group.id)
        if issue and (
            (entities_billed_in(user_id, period) & in_group)
            or (entities_covered_past(user_id, period) & in_group)
        ):
            store.set_group_paid_through(group.id, period.end)
            out["skipped"].append({**entry, "reason": "already covered this period"})
        else:
            out["skipped"].append({**entry, "reason": "nothing billable"})
        return out
    if not issue:
        out["planned"].append(entry)
        return out

    key = period_key(user_id, period, group.id)
    # Captured BEFORE issuing, so the rows closed out afterwards are exactly the ones
    # whose lines rode this invoice. Re-querying after would also catch anything
    # cancelled while the charge was in flight and mark it paid for free.
    #
    # Matched against the LINES rather than against the payer's whole pending set:
    # with several cards in play an extension belonging to another card's company is
    # still pending and must not be closed out by this document.
    riding = {line.entity_id for line in invoice.lines}
    extension_ids = [
        row.id
        for row in store.pending_extensions_for_payer(user_id)
        if str(row.entity_id) in riding
    ]
    try:
        status = _already_invoiced(account.stripe_customer_id, key)
        if status is not None:
            # Charged on a previous run that failed to record it. Catching up costs
            # nothing; re-issuing would bill the customer twice for one month.
            # The extensions rode THAT invoice; leaving them pending would put them on
            # the next one too. True whether or not it has been PAID yet — an unpaid
            # one is being chased by dunning with those lines still on it.
            store.mark_extensions_invoiced(extension_ids)
            if status == "paid":
                store.set_group_paid_through(group.id, period.end)
                out["skipped"].append({**entry, "reason": "already invoiced; adopted"})
            else:
                out["skipped"].append({**entry, "reason": "already invoiced; unpaid"})
            return out

        result = billing_gateway.issue_invoice(
            account.stripe_customer_id,
            invoice,
            # Counted off the invoice itself rather than off the rows, so the memo
            # describes what is actually being charged. An extension is called out
            # because it is the one line on a renewal nobody is expecting.
            memo=renewal_memo(
                period,
                len(invoice.entity_ids),
                sum(1 for line in invoice.lines
                    if "access after cancellation" in line.product_name),
            ),
            # ``billing_group`` travels with the document so dunning can pick this
            # one out of the payer's other open invoices later — a question that did
            # not exist while a payer had one invoice per period.
            metadata={"renewal_key": key, "billing_group": str(group.id)},
            idempotency_key=key,
            # Known here, so the gateway doesn't have to look up the payer the
            # customer id came from in the first place.
            payer_user_id=user_id,
            # THE CARD. Set on the invoice rather than by moving the customer default,
            # which would repoint every other company of this payer mid-run.
            payment_method=group.stripe_payment_method_id,
            billing_group_id=group.id,
        )
        # Closed out because the invoice CARRYING them was raised — not because it
        # was paid. An unpaid renewal is not a dropped charge: the invoice exists and
        # dunning chases that same document, extension lines and all. Marking only on
        # payment left them pending through the whole episode, so when dunning finally
        # collected, the next renewal added them a SECOND time and the customer paid
        # for the same cancellation twice.
        #
        # The trade is deliberate. If the account never recovers and dunning gives up,
        # the extension is closed without being collected — but there is no next
        # renewal on a closed account to collect it on either, so nothing is actually
        # lost, and the alternative overcharges every customer who does recover.
        # Skipped entirely when issuing raised, because then no invoice exists.
        if result.get("id"):
            store.mark_extensions_invoiced(extension_ids)
        if result.get("status") == "paid":
            store.set_group_paid_through(group.id, period.end)
            out["issued"].append({**entry, "invoice": result.get("id")})
        else:
            # THIS CARD only. The payer's other cards have their own periods and
            # their own companies, and a decline here says nothing about them.
            store.begin_group_dunning(group.id, now)
            out["failed"].append({**entry, "invoice": result.get("id"),
                           "status": result.get("status")})
    except Exception:
        logger.exception(
            "renewal: failed to bill payer {} on group {}", user_id, group.id
        )
        try:
            store.begin_group_dunning(group.id, now)
        except Exception:
            logger.exception(
                "renewal: could not start dunning for group {}", group.id
            )
        out["failed"].append({**entry, "status": "error"})
    return out
