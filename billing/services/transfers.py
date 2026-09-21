"""Handing one entity's subscription to a different payer.

The current payer offers; another admin of the same entity accepts; at accept the new
payer is charged for the window the old payer's money does not reach, and the payer
pointer moves.

=============================================================================
WHY ACCEPT IS ORDERED RATHER THAN ATOMIC

Accepting has to charge and then flip, and those cannot be one transaction. Every helper
in ``store`` commits its own unit of work — the anchor by ``start_billing_cycle``, the
invoice row by ``reserve_invoice`` BEFORE the processor is contacted, its ``open`` status
by ``settle_invoice`` before the payment is even attempted. By the time a card declines,
three writes are already on disk, and ``reserve_invoice``'s collision path issues a
session-wide ``rollback()`` that would silently discard anything staged.

So the guarantee is ORDER, not atomicity:

    charge first, flip second — a decline leaves the entity exactly where it was.

and one invariant that must not be broken later:

    NOTHING MAY BE STAGED IN THE SESSION ACROSS THE CHARGE.

The cost of ordering is a window between "money taken" and "pointer moved". The offer row
is the journal that closes it: ``charging`` is committed before the charge is attempted,
``charged`` once the money is in, ``accepted`` once the pointer has moved. A process that
dies in between leaves a row saying exactly how far it got, and two paths finish it — a
retried accept (``respond_to_transfer`` is re-entrant) and ``repair_stranded``. Both
ADOPT the invoice already paid under the stored key; neither ever charges again.

``run_renewals`` solves the same shape the same way. The difference is that a renewal
recomputes its key on the next pass anyway, so a crash repairs itself; an accept is a
one-shot user action that nothing would revisit.

=============================================================================
THE KEY

``transfer-{transfer_id}-{attempt}``, with the counter incremented and committed before
each attempt. Identical within an attempt, so a double-click is refused by the unique
index on ``subscription_invoice.idempotency_key``; different across attempts, so a
declined card can be fixed and retried. ``changes.change_key`` cannot be reused: it embeds
a timestamp of ``at``, and ``at`` here is stored and does not move between retries — so
the key would be stable in both directions and JAM the retry, because voiding an invoice
deliberately keeps its row and its key claimed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.db import IntegrityError, transaction

from billing.services import clock, store
from billing.services._log import logger
from billing.services.constants import (
    AUDIT_TRANSFER_ACCEPTED,
    AUDIT_TRANSFER_CANCELLED,
    AUDIT_TRANSFER_DECLINED,
    AUDIT_TRANSFER_OFFERED,
    EXT_PENDING,
    OUTCOME_ABORTED,
    OUTCOME_SUCCEEDED,
    PHASE_PAST_DUE,
    PHASE_TRIAL,
)
from billing.services.constants import (
    TRANSFER_ACCEPTED as STATUS_ACCEPTED,
)
from billing.services.constants import (
    TRANSFER_CANCELLED as STATUS_CANCELLED,
)
from billing.services.constants import (
    TRANSFER_CHARGED as STATUS_CHARGED,
)
from billing.services.constants import (
    TRANSFER_CHARGING as STATUS_CHARGING,
)
from billing.services.constants import (
    TRANSFER_DECLINED as STATUS_DECLINED,
)
from billing.services.constants import (
    TRANSFER_EXPIRED as STATUS_EXPIRED,
)
from billing.services.constants import (
    TRANSFER_OPEN_STATUSES as OPEN_STATUSES,
)
from billing.services.constants import (
    TRANSFER_PENDING as STATUS_PENDING,
)
from billing.services.constants import (
    TRANSFER_STRANDED_STATUSES as STRANDED_STATUSES,
)
from shared_models.models import SubscriptionTransfer, User, UserEntity

#: How long an unanswered offer stays open. Checked at ACCEPT as well as by any sweep,
#: so an expired offer cannot be accepted merely because nothing has swept it yet —
#: otherwise "expires in 7 days" means "expires whenever the sweep next runs".
TRANSFER_TTL_DAYS = 7


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _aware(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a datetime read back from the row.

    Some drivers return naive datetimes for a ``timezone=True`` column, and the billing
    layer REFUSES them — ``billing._require_aware`` raises rather than guess a zone, and
    so does ``store.transfer_entity_payer``. Reached from here that refusal would land
    AFTER the charge, stranding a handover whose money has already been taken, which is
    the one outcome this whole flow is ordered to avoid.

    ``renewals.entities_billed_in`` carries the same defence for the same reason.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# --- refusals -------------------------------------------------------------------


def transfer_blockers(entity_id, *, from_user_id, to_user_id) -> list[str]:
    """Every reason this handover must be refused, as complete sentences.

    Sentences rather than codes because the payer portal shows the server's words
    verbatim only when they look like prose — a lowercase machine token is replaced with
    generic copy on the way through, so a code here becomes "something went wrong".

    ORDER IS PRIORITY, and callers rely on it. The refusal paths already answer with
    ``reasons[0]``, and the portal shows only the first, so the list is built most-blocking
    first: who you are, then whether the money is settled, then whether the recipient can
    take it on. Someone shown four problems at once cannot tell which to fix, and fixing
    the wrong one first often is not even possible — there is no point adjusting a card for
    a person who is not an admin yet.

    EVALUATED TWICE: at offer, and again inside accept. Every one of these can change in
    the days between — the payer can enter dunning, a module can be cancelled or go to
    trial, the nominee can be demoted or deactivated, a card can be removed — and nothing
    else would notice.
    """
    # Built in FOUR groups, appended in the order the docstring above commits to. The
    # sequence is the contract: callers answer with ``reasons[0]`` and the portal shows
    # only the first, so reordering these lines changes what a refused payer is told.
    reasons: list[str] = []
    rows = store.rows_for_entity(entity_id)

    reasons += _blockers_caller(entity_id, from_user_id)

    reasons += _blockers_money_settled(rows, from_user_id, to_user_id)

    # 4. A TRIAL IS NO LONGER A REFUSAL. It used to be, for three reasons, and all three
    #    have since been dealt with:
    #
    #      * the conversion would charge the new card on the OLD payer's consent — fixed
    #        when consent became per (entity, payer); the accept records the incoming
    #        payer's own row, and ``_convert_due_trials`` asks about the payer on the row;
    #      * the amount was never shown — now disclosed by ``trial_disclosure`` on the
    #        accept screen, priced by the same function that charges it;
    #      * the warning email could never reach them, because the trial dedupe key was
    #        entity-scoped and the outgoing payer had already consumed it — the key now
    #        carries the payer.
    #
    #    What makes the handover coherent rather than merely permitted is that the free
    #    days genuinely travel: ``trial_end`` has one writer and no path moves it, and the
    #    trial stays once-per-entity, so the incoming payer inherits a spent trial rather
    #    than minting a fresh one.

    reasons += _blockers_anything_to_hand_over(entity_id)

    reasons += _blockers_recipient(entity_id, to_user_id)

    return reasons


#: Blockers that make an offer permanently invalid rather than temporarily refused: the
#: payer changed, or the nominee is no longer eligible. Everything else may clear on its
#: own (a debt is settled, a trial converts), so the offer is left standing.
_FATAL_PREFIXES = (
    "Only the person currently being billed",
    "Nobody is being billed",
    "That person needs to be an admin",
    "That account isn't active",
)


def _is_fatal(reasons: list[str]) -> bool:
    return any(r.startswith(p) for r in reasons for p in _FATAL_PREFIXES)


# --- reads ------------------------------------------------------------------------


def pending_transfer_for_entity(entity_id) -> SubscriptionTransfer | None:
    """The open offer on this entity, if any. At most one — see the partial unique index."""
    if not entity_id:
        return None
    return (
        SubscriptionTransfer.objects.filter(entity_id=str(entity_id), status__in=OPEN_STATUSES)
        .order_by("-created_at")
        .first()
    )


def incoming_transfers(user_id) -> list[SubscriptionTransfer]:
    """Offers addressed to this user and still open.

    The one recipient-scoped read in the subscription domain. Every other portal query
    filters on ``payer_user_id``, which is exactly wrong here: the whole point is the
    companies this person does NOT pay for yet.
    """
    if not user_id:
        return []
    return list(
        SubscriptionTransfer.objects.filter(to_user_id=str(user_id), status__in=OPEN_STATUSES)
        .order_by("-created_at")
    )


def incoming_transfers_payload(user_id) -> list[dict]:
    """Offers addressed to this user, shaped for the portal inbox.

    Carries the COMPANY NAME, who is asking, and a live quote — an offer that says only
    "someone wants you to take over something" cannot be answered. The quote is re-derived
    here rather than read off the row because the stored one is an estimate taken when the
    offer was made, and the outgoing payer's ``paid_through`` advances on every renewal.
    """
    from shared_models.models import Entity

    out = []
    for offer in incoming_transfers(user_id):
        entity = store._by_pk(Entity, offer.entity_id)
        asker = store._by_pk(User, offer.from_user_id)
        try:
            quote = quote_transfer(offer.entity_id, to_user_id=offer.to_user_id)
        except Exception:
            # A quote that cannot be priced must not hide the request itself — the
            # accept screen re-quotes anyway, and an invisible offer is worse than one
            # with a missing figure.
            logger.exception("transfer: could not quote offer {}", offer.id)
            quote = None
        out.append(
            {
                **_as_dict(offer),
                "entity_name": getattr(entity, "name", None) or "",
                # Where the company's subscription can be looked at before answering.
                # A PATH, not a URL, and built here rather than in the frontend for the
                # same reason the entity list's is: the route belongs to this app.
                # MODULE_VIEW gates it at CASHIER, so the admin being asked to pay can
                # read it — deciding is not the same as managing.
                "settings_path": f"/entity/settings/module/{offer.entity_id}",
                "from_name": _display_name(asker),
                "quote": quote,
                # What they inherit that is not being charged for today — free days now,
                # a charge on their card at its own date. Accepting commits them to it.
                "trials": trial_disclosure(offer.entity_id, offer.to_user_id),
                # One reason, the most blocking — same rule as the offering screen, and
                # the same one ``respond_to_transfer`` answers with.
                "blockers": transfer_blockers(
                    offer.entity_id,
                    from_user_id=offer.from_user_id,
                    to_user_id=offer.to_user_id,
                )[:1],
            }
        )
    return out


def _display_name(person) -> str:
    if person is None:
        return "an admin"
    full = " ".join(
        filter(None, [getattr(person, "first_name", ""), getattr(person, "last_name", "")])
    ).strip()
    return full or getattr(person, "username", None) or "an admin"


def quote_transfer(entity_id, *, to_user_id, at=None) -> dict | None:
    """What taking this company over would cost, for the accept screen.

    ``at`` defaults to the outgoing payer's ``paid_through`` — the instant their money
    stops covering the entity. An ESTIMATE while the offer is open: that date advances on
    every successful renewal, so it is re-read at accept and this figure is not trusted.
    """
    from billing.services import checkout

    payer = store.payer_for_entity(entity_id)
    if at is None:
        # What the OUTGOING payer's money covers for THIS company — the card it is billed
        # on, not the account, which cannot answer for two cards at once.
        at = store.paid_through_for_entity(entity_id) if payer else None
    if at is None:
        at = clock.now()

    codes = _billable_codes(entity_id)
    if not codes:
        return None
    return checkout.quote_transfer_charge(entity_id, to_user_id, codes, at=at)


def _billable_codes(entity_id) -> set[str]:
    """The module codes the new payer is CHARGED FOR at accept — billing forward only.

    Deliberately excludes trials. A trial is worth something to the recipient and does
    move with the entity, but its days are free, so charging for them at the handover
    would bill somebody for a period nobody is paying for. It becomes billable the moment
    it converts, on their own card, at its own end date.
    """
    from billing.services import access

    return {
        row.function_code.upper()
        for row in store.rows_for_entity(entity_id)
        if access.is_billing_forward(phase=row.phase)
    }


def trial_disclosure(entity_id, to_user_id) -> list[dict]:
    """What the incoming payer will be charged for trials they inherit, and when.

    Accepting a handover now commits someone to a charge that lands weeks later, so the
    screen has to name it. Returns one entry per CONVERSION DATE:
    ``{codes, trial_end, amount, currency, anchor_is_new}``.

    GROUPED BY ``trial_end`` AND PRICED AS A SET, because pricing is per module set. Two
    modules ending on the same day convert in one charge at the bundle rate; quoting them
    separately would disclose figures that sum to MORE than the customer is charged.
    Modules ending on different days are genuinely separate charges, so they are separate
    lines. ``notify_trials_ending`` groups the same way, and following it keeps the
    accept screen and the warning email saying the same thing.

    Priced by ``quote_transfer_charge`` — the same function that takes the money — with
    ``at=trial_end``. It is pure: it reads the incoming payer's cycle and writes nothing,
    so opening the screen cannot anchor anybody.

    AN ESTIMATE, and the caller must present it as one. It is priced against the incoming
    payer's anchor as it stands today; if they have none, the conversion will anchor them
    at conversion time and bill a full period instead. ``anchor_is_new`` says which case
    this is so the screen can word it honestly.
    """
    from billing.services import checkout

    by_date: dict[datetime, set[str]] = {}
    for row in _trial_rows(entity_id):
        by_date.setdefault(_aware(row.trial_end), set()).add(row.function_code.upper())

    # WHAT THEY ALREADY PAY FOR on this entity, which is what the conversion will price
    # against. A trial landing beside an active module is an upgrade to the bundle, not a
    # fresh join, and the two differ by more than double on a two-module entity.
    #
    # Accumulated across dates, in order: a trial converting in August is active by the
    # time one converting in September does, so it belongs in the second one's "before".
    # ``_forecast_conversion_charges`` simulates the same sequence for the settings page.
    billed = {str(c).upper() for c in checkout._billed_codes_in_house(entity_id)}

    out = []
    for trial_end, codes in sorted(by_date.items()):
        try:
            quote = checkout.quote_transfer_charge(
                entity_id, to_user_id, codes, at=trial_end, before=billed
            )
        except Exception:
            # A price we cannot compute must not hide the trial itself — being told a
            # module is on trial with the amount missing beats being told nothing.
            logger.exception(
                "transfer: could not price the trial ending {} on entity {}",
                trial_end, entity_id,
            )
            quote = None
        out.append({
            "codes": sorted(codes),
            # NAMED BY THE SET, because it is PRICED by the set. Two modules converting
            # together are one plan — "Super Minty", not "Petty Cash and Payment Request"
            # — and that is the name that will appear on the invoice, so it is the name
            # to disclose. Falls back to the module phrase the emails use if the catalog
            # cannot price the combination, which is the same fallback shape as
            # ``build_renewal``: name it rather than leave a blank.
            "label": _plan_label(codes),
            "trial_end": trial_end,
            "amount": (quote or {}).get("amount"),
            "currency": (quote or {}).get("currency"),
            "anchor_is_new": (quote or {}).get("anchor_is_new"),
        })
        billed |= {str(c).upper() for c in codes}
    return out


def _plan_label(codes) -> str:
    """What this set of modules is called — the plan's own name where one prices them.

    ``{PETTY_CASH, BILL}`` is "Super Minty", not two modules listed with an "and". The
    bundle IS a product, it is what the invoice line says, and naming it any other way
    describes a purchase the customer will not recognise on their statement.
    """
    from billing.services import notify as notifier

    try:
        plan = store.billing_plan_for_codes(set(codes))
        if plan is not None and (plan.display_name or "").strip():
            return plan.display_name.strip()
    except Exception:
        logger.exception("transfer: could not name the plan for {}", sorted(codes))
    return notifier.modules_phrase(sorted(codes))


def _trial_rows(entity_id) -> list:
    """Rows still running a free trial — inherited whole, and charged for later.

    Kept apart from ``_billable_codes`` because the two answer different questions: what
    is charged at accept, and what is being handed over. A trial-only entity has nothing
    in the first and something real in the second.
    """
    return [
        row
        for row in store.rows_for_entity(entity_id)
        if row.phase == PHASE_TRIAL and row.trial_end is not None
    ]


# --- offer / cancel / decline -------------------------------------------------------


def offer_transfer(user_id, entity_id, to_user_id) -> tuple[bool, str, dict | None]:
    """Open an offer to hand ``entity_id`` to ``to_user_id``. Returns ``(ok, message, offer)``."""
    if str(user_id) == str(to_user_id):
        return False, "You're already the one being billed for this company.", None

    reasons = transfer_blockers(
        entity_id, from_user_id=user_id, to_user_id=to_user_id
    )
    if reasons:
        return False, reasons[0], None

    if pending_transfer_for_entity(entity_id) is not None:
        return False, "There's already a handover waiting on this company.", None

    now = clock.now()
    quote = quote_transfer(entity_id, to_user_id=to_user_id)
    offer = SubscriptionTransfer(
        id=_uuid(),
        entity_id=str(entity_id),
        from_user_id=str(user_id),
        to_user_id=str(to_user_id),
        status=STATUS_PENDING,
        expires_at=now + timedelta(days=TRANSFER_TTL_DAYS),
        quoted_amount=(quote or {}).get("amount"),
        quoted_currency=(quote or {}).get("currency"),
    )
    try:
        # A savepoint, so the refused insert cannot poison an enclosing transaction.
        with transaction.atomic():
            offer.save(force_insert=True)
    except IntegrityError:
        # The partial unique index is the real guard against two open offers; this is the
        # race the pre-check above cannot close.
        logger.warning("transfer: a concurrent offer already exists for {}", entity_id)
        return False, "There's already a handover waiting on this company.", None

    _record(offer, AUDIT_TRANSFER_OFFERED, actor=user_id, outcome=OUTCOME_SUCCEEDED)
    _notify(offer, "requested")
    return True, "The handover request has been sent.", _as_dict(offer)


def cancel_transfer(user_id, transfer_id) -> tuple[bool, str]:
    """Withdraw an offer. The initiator's escape hatch — without it their own exit
    depends indefinitely on someone else answering."""
    offer = store._by_pk(SubscriptionTransfer, transfer_id)
    if offer is None or offer.status not in OPEN_STATUSES:
        return False, "That handover isn't open any more."
    if str(offer.from_user_id) != str(user_id):
        return False, "Only the person who started this handover can cancel it."
    if offer.status != STATUS_PENDING:
        # Mid-charge. Cancelling now would strand money already being collected.
        return False, "That handover is already being processed."

    offer.status = STATUS_CANCELLED
    offer.responded_at = clock.now()
    offer.save(update_fields=["status", "responded_at"])
    _record(offer, AUDIT_TRANSFER_CANCELLED, actor=user_id, outcome=OUTCOME_ABORTED)
    return True, "The handover request has been withdrawn."


def _decline(offer, user_id) -> tuple[bool, str, dict | None]:
    offer.status = STATUS_DECLINED
    offer.responded_at = clock.now()
    offer.save(update_fields=["status", "responded_at"])
    _record(offer, AUDIT_TRANSFER_DECLINED, actor=user_id, outcome=OUTCOME_ABORTED)
    # After the commit, like every other send here. Until this existed a decline was
    # silent to the payer who asked: the status flipped and the audit row was written, so
    # a request that had actually been answered looked exactly like one nobody had opened.
    _notify(offer, "declined")
    return True, "You've declined the handover.", None


# --- accept -------------------------------------------------------------------------


def respond_to_transfer(user_id, transfer_id, *, accept: bool) -> tuple[bool, str, dict | None]:
    """Accept or decline an offer. Returns ``(ok, message, result)``.

    RE-ENTRANT. Called on an offer already in ``charging`` or ``charged`` it picks up at
    the adopt step instead of starting a second charge, which is what makes a retry after
    a crash safe rather than a double bill.
    """
    offer = store._by_pk(SubscriptionTransfer, transfer_id)
    if offer is None or offer.status not in OPEN_STATUSES:
        return False, "That handover isn't open any more.", None
    if str(offer.to_user_id) != str(user_id):
        return False, "That handover was sent to someone else.", None

    now = clock.now()
    expires_at = _aware(offer.expires_at)
    # Checked HERE, not only by a sweep. Otherwise "expires in 7 days" quietly means
    # "expires whenever something next looks at it".
    if expires_at is not None and expires_at <= now and offer.status == STATUS_PENDING:
        offer.status = STATUS_EXPIRED
        offer.save(update_fields=["status"])
        return False, "That handover request has expired.", None

    if not accept:
        return _decline(offer, user_id)

    reasons = transfer_blockers(
        offer.entity_id,
        from_user_id=offer.from_user_id,
        to_user_id=offer.to_user_id,
    )
    if reasons:
        if _is_fatal(reasons):
            offer.status = STATUS_CANCELLED
            offer.responded_at = now
            offer.save(update_fields=["status", "responded_at"])
            _record(offer, AUDIT_TRANSFER_CANCELLED, actor=user_id,
                    outcome=OUTCOME_ABORTED, note=reasons[0][:500])
        return False, reasons[0], None

    return _accept(offer, user_id, now)


def _accept(offer, user_id, now) -> tuple[bool, str, dict | None]:
    from billing.services import checkout

    entity_id = offer.entity_id
    codes = _billable_codes(entity_id)

    # NOTHING TO CHARGE IS NOT A FAILURE. A trial-only entity has been paid for by nobody,
    # so there is no window to buy and no invoice to raise — the handover is just the flip.
    #
    # Skipping straight to ``_complete`` also skips the whole journal: no ``charging``
    # state, no idempotency key, no external call. That is safe precisely because the
    # crash window the journal exists to close is the gap between taking money and moving
    # the pointer, and here no money moves. The blockers have already refused the
    # genuinely empty entity, so reaching this with no codes means a live trial.
    if not codes:
        return _complete(offer, actor_user_id=user_id, now=now)

    # READ THE HANDOVER INSTANT NOW, never the value quoted when the offer was made.
    # ``run_renewals`` advances ``paid_through`` on every successful renewal, so an offer
    # that outlived a cycle would otherwise bill a window the outgoing payer has since
    # paid for — a full duplicate charge.
    # Read off the ENTITY: the days already bought for this company belong to the card the
    # outgoing payer had it on, and their other cards say nothing about it.
    at = _aware(store.paid_through_for_entity(offer.entity_id)) or now

    customer_id = store.customer_id_for_user(offer.to_user_id)
    if not customer_id:
        return False, "That billing account isn't set up to be charged.", None

    # THE INCOMING PAYER'S CARD, nominated here if they have not chosen one.
    #
    # They cannot have chosen one BEFORE this point: the nomination is per (company,
    # payer), and until they accept, the company is not theirs — ``payment_methods``
    # refuses to let a non-payer point a card at somebody else's company. So the accept is
    # the first moment the choice can exist, and it is made from their account default,
    # which the blockers already required them to have and which the quote they are
    # accepting was priced and shown against.
    #
    # This is not the silent fallback the rest of the engine refuses. It is written, once,
    # as a consequence of an explicit "yes, bill me for this company" — recorded with its
    # own source so it can be told apart later — and they can move it afterwards. The
    # alternative is refusing an accept for want of a choice there was no way to make.
    if store.billing_group_for_entity(entity_id, offer.to_user_id) is None:
        from billing.services import stripe_client

        default_card = stripe_client.customer_default_payment_method(customer_id)
        if not default_card:
            return False, "That billing account has no payment method to charge.", None
        store.nominate_card_for_entity(
            entity_id, offer.to_user_id, default_card, "transfer"
        )

    # --- the journal write, committed BEFORE the external call ----------------------
    # Same shape as ``notify._claim``: a process that dies between the two must leave a
    # claim with no side effect, never a side effect with no claim.
    if offer.status == STATUS_PENDING:
        offer.charge_attempt = int(offer.charge_attempt or 0) + 1
        offer.charge_key = f"transfer-{offer.id}-{offer.charge_attempt}"
        offer.status = STATUS_CHARGING
        offer.accepted_billed_through = at
        offer.save(update_fields=["charge_attempt", "charge_key", "status", "accepted_billed_through"])

    # --- the charge. NOTHING may be inside a transaction across this ------------------
    if offer.status == STATUS_CHARGING:
        result = checkout._bill_transfer_in_house(
            entity_id,
            offer.to_user_id,
            customer_id,
            codes,
            at=at,
            idempotency_key=offer.charge_key,
        )
        if not result["paid"]:
            # Back to pending so it can be retried — with a FRESH key, because the
            # counter stays incremented and the voided one stays claimed.
            offer.status = STATUS_PENDING
            offer.save(update_fields=["status"])
            # No email. This path is reached from the accept request itself, so the person
            # whose card was declined is looking at ``result["reason"]`` in the browser as
            # it happens; a second telling by email minutes later added nothing. The offer
            # stays ``pending``, so they can fix the card and accept again.
            return False, result["reason"], None

        offer.status = STATUS_CHARGED
        offer.charge_invoice_id = result["invoice_id"]
        offer.accepted_billed_through = result["period_end"]
        # The cycle they actually landed on. Recorded in the SAME commit as ``charged``,
        # so a crash after this leaves the anchor with the money rather than a repair pass
        # having to re-derive it — by which point the payer's anchor may have moved on.
        offer.accepted_anchor_at = result["anchor"]
        offer.quoted_amount = result["amount"]
        offer.quoted_currency = result["currency"]
        offer.save(update_fields=[
            "status", "charge_invoice_id", "accepted_billed_through", "accepted_anchor_at",
            "quoted_amount", "quoted_currency",
        ])

    return _complete(offer, actor_user_id=user_id, now=now)


def _complete(offer, *, actor_user_id, now) -> tuple[bool, str, dict | None]:
    """Move the pointer. Everything before this is reversible by doing nothing; this is
    the step that makes the handover real, and it is ONE transaction - literally, here:
    the payer flip, the consent, the nomination clear and the offer's ``accepted`` land
    together or not at all (``transaction.atomic``). Flask's copy could only ORDER those
    four commits, because its store helpers each committed; the journal repair still
    tolerates the ordered version, so the atomic one is a strict improvement.

    Shared by ``_accept`` and ``repair_stranded`` so a handover finished by the nightly
    pass is identical to one finished by the customer clicking twice.
    """
    from billing.services.access_sweep import sweep_expired_module_access

    entity_id = offer.entity_id
    from_user_id = offer.from_user_id
    to_user_id = offer.to_user_id
    codes = [row.function_code for row in store.rows_for_entity(entity_id)]

    with transaction.atomic():
        store.transfer_entity_payer(
            entity_id, to_user_id, billed_through=_aware(offer.accepted_billed_through)
        )
        # Their own agreement, recorded against THEM. The old payer's row stays as history —
        # "why was I billed for this in June" is asked most often by the person who no
        # longer pays — and it no longer authorises anything, because consent is asked per
        # payer.
        store.record_billing_consent(entity_id, to_user_id, "transfer")
        # The outgoing payer's CARD stops paying for it, for the same reason their consent
        # stops authorising it. Deleted rather than left, because the nomination is what
        # the charge paths resolve: leaving it would let a company that has changed hands
        # go on naming a card belonging to someone who no longer pays for it. Their GROUP
        # is left alone — other companies may still be on that card. Its own savepoint, so
        # a failure here is logged without taking the flip down with it.
        try:
            with transaction.atomic():
                store.clear_nomination_for_entity(entity_id, from_user_id)
        except Exception:
            logger.exception(
                "transfer: could not clear the old payer's card nomination for {}", entity_id
            )

        offer.status = STATUS_ACCEPTED
        offer.responded_at = now
        offer.save(update_fields=["status", "responded_at"])

    for code in codes:
        store.record_action(
            entity_id=entity_id,
            function_code=code,
            payer_user_id=from_user_id,
            action=AUDIT_TRANSFER_ACCEPTED,
            outcome=OUTCOME_SUCCEEDED,
            actor_user_id=actor_user_id,
            payer_before=from_user_id,
            payer_after=to_user_id,
        )

    # AFTER the commit. Nothing else re-syncs ``entity_function_map`` at a flip — dunning
    # does it on recovery, the light pass only for payers it touched — and this is only
    # safe once the claim is on the row, or it would revoke what it just transferred.
    try:
        sweep_expired_module_access(payer_user_id=to_user_id)
    except Exception:
        logger.exception(
            "transfer: could not re-sync module access for {} after the handover",
            to_user_id,
        )

    _notify(offer, "accepted")

    logger.info(
        "transfer: entity {} moved from payer {} to {} (covered to {})",
        entity_id, from_user_id, to_user_id, offer.accepted_billed_through,
    )
    return True, "You're now the subscriber for this company.", _as_dict(offer)


# --- the repair step ------------------------------------------------------------------


def repair_stranded(now=None, *, limit=None) -> dict:
    """Finish handovers whose accept got part-way and stopped.

    NORMALLY DOES NOTHING — an accept that completes leaves no row in ``charging`` or
    ``charged``, and the partial index it reads is empty. It exists for the one window
    the ordering cannot remove: money collected, pointer not yet moved, and a one-shot
    user action that nothing would otherwise revisit.

    It charges nothing. A ``charged`` row is finished from what is already paid; a
    ``charging`` row is resolved by ASKING the processor through the same adopt path the
    accept uses, and released back to ``pending`` only when the processor confirms it
    never saw the invoice.

    ALSO retires lapsed offers, which is not a repair but has to live on a schedule for
    the same reason: ``respond_to_transfer`` expires an offer only when somebody touches
    it, so without a sweep a request nobody ever opened stays ``pending`` for ever and the
    payer who sent it is never told it ran out.
    """
    from billing.services import renewals

    now = now or clock.now()
    stranded = SubscriptionTransfer.objects.filter(status__in=STRANDED_STATUSES).order_by("created_at")
    rows = list(stranded[:limit] if limit else stranded)
    result = {"completed": [], "released": [], "waiting": [],
              "expired": _expire_lapsed(now, limit=limit)}

    for offer in rows:
        try:
            if offer.status == STATUS_CHARGED:
                _complete(offer, actor_user_id=offer.to_user_id, now=now)
                result["completed"].append(offer.id)
                continue

            customer_id = store.customer_id_for_user(offer.to_user_id)
            status = renewals._already_invoiced(
                customer_id, offer.charge_key, metadata_key="transfer_key"
            )
            if status == "paid":
                record = store.invoice_for_key(offer.charge_key)
                offer.charge_invoice_id = getattr(record, "external_id", None)
                offer.status = STATUS_CHARGED
                offer.save(update_fields=["charge_invoice_id", "status"])
                _complete(offer, actor_user_id=offer.to_user_id, now=now)
                result["completed"].append(offer.id)
            elif status is None:
                # The processor never saw it, and the reservation has been discarded, so
                # the key is free again. Back to pending for a clean retry.
                offer.status = STATUS_PENDING
                offer.save(update_fields=["status"])
                result["released"].append(offer.id)
            else:
                # Raised and unpaid. Leave it — the customer can still settle it, and
                # forcing it either way here would either bill twice or give it away.
                result["waiting"].append(offer.id)
        except Exception:
            logger.exception("transfer: could not repair stranded handover {}", offer.id)

    if any(result.values()):
        logger.info("transfer: repaired stranded handovers {}", result)
    return result


def _expire_lapsed(now, *, limit=None) -> list:
    """Retire pending offers past their deadline and tell the payer who sent them.

    The time comparison is done in PYTHON, not in the query. ``expires_at`` is compared
    against ``clock.now()`` everywhere else in this module through ``_aware``, and pushing
    an aware datetime into a filter against a column that may be naive is how an offer
    silently expires an hour early or late. The candidate set is every open request in the
    system, which is bounded by the TTL and small.

    Never raises per offer: one bad row must not stop the rest of the sweep, and this runs
    inside the same daily pass that moves money.
    """
    pending = (
        SubscriptionTransfer.objects.filter(status=STATUS_PENDING, expires_at__isnull=False)
        .order_by("created_at")
    )
    rows = list(pending[:limit] if limit else pending)

    expired = []
    for offer in rows:
        deadline = _aware(offer.expires_at)
        if deadline is None or deadline > now:
            continue
        try:
            offer.status = STATUS_EXPIRED
            offer.save(update_fields=["status"])
            # After the write, so a send can never describe an expiry that rolled back.
            _notify(offer, "expired")
            expired.append(offer.id)
        except Exception:
            logger.exception("transfer: could not expire lapsed handover {}", offer.id)
    return expired


# --- helpers ---------------------------------------------------------------------------


def _notify(offer, kind: str) -> None:
    """Send the handover email. ALWAYS after the commit it reports.

    ``notify`` commits twice of its own — it claims a dedupe row before sending and
    settles it after — so calling it mid-transaction publishes a half-finished flip and
    can take the caller's uncommitted work with it.

    Never raises. An email that fails to send must not undo a handover that has already
    happened, and the audit row is the durable record either way.
    """
    from billing.services import notify as notifier

    events = {
        # The paths are minty-web's payer portal (Part 2 step 4); ``notify.portal_url``
        # routes them through Flask's re-handoff. Flask's copy pointed at billing-frontend's
        # ``/profile/subscriptions[/incoming]`` with a minted token in the link.
        "requested": (
            notifier.SUBSCRIBER_TRANSFER_REQUESTED, offer.to_user_id, "/subscription/subscriptions/incoming"
        ),
        "accepted": (
            notifier.SUBSCRIBER_TRANSFER_ACCEPTED, offer.from_user_id, "/subscription/subscriptions"
        ),
        # Both of these go to the payer who ASKED. The recipient already knows what they
        # did — declining is their own click, and an expiry is a request they chose not
        # to answer. The person left waiting is the one who learns nothing otherwise.
        "declined": (
            notifier.SUBSCRIBER_TRANSFER_DECLINED, offer.from_user_id, "/subscription/subscriptions"
        ),
        "expired": (
            notifier.SUBSCRIBER_TRANSFER_EXPIRED, offer.from_user_id, "/subscription/subscriptions"
        ),
    }
    event, recipient, path = events[kind]

    try:
        from shared_models.models import Entity

        entity = store._by_pk(Entity, offer.entity_id)
        names = {}
        for key, uid in (("from_name", offer.from_user_id), ("to_name", offer.to_user_id)):
            person = store._by_pk(User, uid)
            names[key] = (
                " ".join(
                    filter(None, [getattr(person, "first_name", ""), getattr(person, "last_name", "")])
                ).strip()
                or getattr(person, "username", None)
                or "an admin"
            )

        notifier.notify(
            recipient,
            event,
            # Per TRANSFER, not per entity: a re-offer after a decline is a new request
            # and must send, where an entity-scoped key would swallow it. The trial keys
            # used to make exactly that mistake — which is why a transferred entity's
            # trial warning could never reach its new payer — and now carry the payer too.
            dedupe_key=f"transfer-{offer.id}-{kind}",
            context={
                "entity_id": offer.entity_id,
                "entity_name": getattr(entity, "name", None) or "your company",
                "amount": offer.quoted_amount,
                "currency": offer.quoted_currency,
                "billed_through": offer.accepted_billed_through,
                "expires_at": offer.expires_at,
                "portal_url": notifier.portal_url(path),
                **names,
            },
        )
    except Exception:
        logger.exception(
            "transfer: could not send the {} email for handover {}", kind, offer.id
        )


def _record(offer, action, *, actor, outcome, note=None) -> None:
    """One audit row per module code — ``function_code`` is NOT NULL and the payer sits on
    every row of the entity, so a handover is recorded the same way a cancellation is."""
    try:
        for row in store.rows_for_entity(offer.entity_id):
            store.record_action(
                entity_id=offer.entity_id,
                function_code=row.function_code,
                payer_user_id=offer.from_user_id,
                action=action,
                outcome=outcome,
                actor_user_id=actor,
                payer_before=offer.from_user_id,
                payer_after=offer.to_user_id,
                note=note,
            )
    except Exception:
        # An audit failure must not undo a handover that has already happened.
        logger.exception("transfer: could not record {} for {}", action, offer.id)


def _as_dict(offer) -> dict:
    return {
        "id": offer.id,
        "entity_id": offer.entity_id,
        "from_user_id": offer.from_user_id,
        "to_user_id": offer.to_user_id,
        "status": offer.status,
        "expires_at": offer.expires_at,
        "amount": offer.quoted_amount,
        "currency": offer.quoted_currency,
        "billed_through": offer.accepted_billed_through,
        "invoice_id": offer.charge_invoice_id,
    }


def _blockers_recipient(entity_id, to_user_id) -> list[str]:
    """Whether the person being handed the bill can actually take it on.

    Blocks 5 and 6 are ONE ``if/elif/else`` and stay together: the card is only worth
    asking about once the recipient is an admin with a live account.
    """
    from billing.services import stripe_client
    from core.policy import Role, role_at_least

    reasons: list[str] = []
    # 5. The bill can only sit with someone who could act on it — and who can sign in.
    #    ``_admin_candidates`` checks the membership flags but not the account one, so a
    #    deactivated admin still appears on the list this validates against.
    membership = (
        UserEntity.objects.filter(
            entity_id=str(entity_id), user_id=str(to_user_id), approved=True
        )
        .values_list("role", flat=True)
        .first()
    )
    account = store._by_pk(User, to_user_id)
    if membership is None or not role_at_least(membership, Role.ADMIN.value):
        reasons.append("That person needs to be an admin of this company first.")
    elif account is None or not getattr(account, "approved", False):
        reasons.append("That account isn't active, so it can't take on the billing.")

    # 6. Without a saved card nothing can be charged AT ALL: ``start_billing_cycle``
    #    silently no-ops for a payer with no ``user_stripe_customer`` row, so the accept
    #    would fail at the charge having promised to succeed.
    #
    #    Having a card SAVED is checked here; having one nominated for this company is
    #    not, and cannot be — the incoming payer chooses that as part of accepting, and
    #    demanding it beforehand would ask them to point a card at a company they have not
    #    yet agreed to take on. The accept refuses if they still have not.
    else:
        customer_id = store.customer_id_for_user(to_user_id)
        if not customer_id or not stripe_client.customer_default_payment_method(customer_id):
            reasons.append(
                "That person needs a saved payment method before they can take over "
                "the billing."
            )
    return reasons

def _blockers_anything_to_hand_over(entity_id) -> list[str]:
    """Whether the company has anything worth handing over at all."""
    reasons: list[str] = []
    # 4b. NOTHING LEFT AT ALL. An entity whose only modules are expired trials or finished
    #     cancellations still names a payer on those dead rows, so every check above
    #     passes — but there is nothing to hand over and nothing to charge.
    #
    #     A RUNNING TRIAL COUNTS as something to hand over even though it is not billing
    #     forward: it is worth real money to the recipient and converts on their card. So
    #     this asks the broader question than ``_billable_codes`` alone.
    #
    #     Caught HERE rather than at the accept because the difference is who finds out.
    #     Left to the accept, the offer is allowed, the email goes out, and the recipient
    #     is the one told it cannot happen — for a reason already true when it was sent.
    if not _billable_codes(entity_id) and not _trial_rows(entity_id):
        reasons.append(
            "There's nothing active on this company to hand over. "
            "Subscribe a module first, then it can be handed over."
        )
    return reasons

def _blockers_money_settled(rows, from_user_id, to_user_id) -> list[str]:
    """Whether any money is still in flight on either side of the handover."""
    reasons: list[str] = []
    # 2. Debt splits in half. The arrears sit on the PAYER's account while the phase sits
    #    on the row, so transferring mid-dunning gives the new payer a "payment due" card
    #    with nothing owed on their account, and leaves the real debt uncollectable.
    if store.payer_is_dunning(from_user_id):
        reasons.append(
            "There's a payment still being collected on this account. "
            "Once that's settled the handover can go ahead."
        )
    elif any(row.phase == PHASE_PAST_DUE for row in rows):
        reasons.append(
            "This company has a payment outstanding. Settle it first, then hand it over."
        )
    if store.payer_is_dunning(to_user_id):
        reasons.append(
            "That person has a payment still being collected, so they can't take on "
            "another company right now."
        )

    # 3. A pending cancel-extension is money the OUTGOING payer owes. It rides the module
    #    row, so moving the row moves the debt onto the new payer's invoice — and makes it
    #    uncollectable from the person who actually incurred it.
    #    The remedy is UN-CANCELLING, not waiting. While the extension is still pending
    #    nobody has been billed for it, so ``_reactivate_module_in_house`` simply deletes
    #    the number — "no invoice item to remove, no credit note, no money moved in either
    #    direction". Telling someone to wait for the next invoice sends them away for up
    #    to a month to reach the same place one click would.
    cancelling = [
        row.function_code
        for row in rows
        if row.extension_state == EXT_PENDING and (row.extension_amount or 0) > 0
    ]
    if cancelling:
        reasons.append(
            "A module here is cancelling, and the charge for its last days hasn't been "
            "billed yet. Un-cancel it and the charge goes away, then you can hand the "
            "company over."
        )
    return reasons

def _blockers_caller(entity_id, from_user_id) -> list[str]:
    """Whether the caller is the person entitled to give the bill away."""
    reasons: list[str] = []
    # 1. Only the established payer may give the bill away. Strict identity, NOT
    #    ``may_manage_subscription`` — that answers True when there is no payer at all,
    #    which is the one case where there is nothing to hand over.
    payer = store.payer_for_entity(entity_id)
    if payer is None:
        reasons.append("Nobody is being billed for this company yet, so there's nothing to hand over.")
    elif str(payer) != str(from_user_id):
        reasons.append("Only the person currently being billed can hand this company over.")
    return reasons
