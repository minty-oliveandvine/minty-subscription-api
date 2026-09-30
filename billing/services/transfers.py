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

from billing.services import access, clock, store
from billing.services._log import logger
from billing.services.constants import (
    AUDIT_CANCEL,
    AUDIT_TRANSFER_ACCEPTED,
    AUDIT_TRANSFER_CANCELLED,
    AUDIT_TRANSFER_COLLECTED,
    AUDIT_TRANSFER_DECLINED,
    AUDIT_TRANSFER_OFFERED,
    EXT_PENDING,
    OUTCOME_ABORTED,
    OUTCOME_SUCCEEDED,
    PHASE_PAST_DUE,
    PHASE_SCHEDULED_CANCEL,
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


#: The ways an offer ends that the person who ASKED did not choose, and so has to be told.
#: ``cancelled`` is deliberately absent: it covers a withdrawal they performed themselves -
#: already answered by 07-K as they did it - and telling somebody their own click happened is
#: not news.
OUTCOME_STATUSES = (STATUS_DECLINED, STATUS_EXPIRED, STATUS_ACCEPTED)


def unseen_outcomes(from_user_id, *, limit: int = 5) -> list[dict]:
    """How this payer's offers ended, where they have not been shown yet.

    THE ONLY READ OF A FINISHED TRANSFER in the engine. Every other query filters on the
    three OPEN statuses, which is why a decline used to be invisible: ``pending_transfer``
    went null and the offering screen fell back to the picker exactly as though nothing had
    ever been asked. The payer learned by email or not at all.

    Oldest first, so several outcomes that piled up while they were away are told in the
    order they happened. ``limit`` is a sanity bound, not a page: a payer with more than a
    handful of unanswered handovers has a different problem, and the screen shows them one
    at a time anyway.
    """
    from shared_models.models import Entity

    if not from_user_id:
        return []
    rows = list(
        SubscriptionTransfer.objects.filter(
            from_user_id=str(from_user_id),
            status__in=OUTCOME_STATUSES,
            outcome_seen_at__isnull=True,
        ).order_by("responded_at", "created_at")[:limit]
    )
    out = []
    for offer in rows:
        entity = store._by_pk(Entity, offer.entity_id)
        out.append({
            "id": offer.id,
            "entity_id": offer.entity_id,
            "entity_name": getattr(entity, "name", None) or "",
            "status": offer.status,
            # The other party, named - the modal's title is "<Name> declined the transfer",
            # and an outcome that cannot name anybody is a worse sentence than none.
            "who": _display_name(store._by_pk(User, offer.to_user_id)),
            "responded_at": offer.responded_at,
        })
    return out


def mark_outcome_seen(user_id, transfer_id) -> tuple[bool, str]:
    """Record that this payer has been shown how their offer ended.

    IDEMPOTENT, and stamped from the person's own Done rather than from the read that drew
    the modal: a render is not evidence anybody saw it, and neither is the email
    (``subscription_email_log`` records that a message was SENT). Re-stamping is a no-op, so
    a double-click or a retry after a timeout costs nothing.
    """
    offer = store._by_pk(SubscriptionTransfer, transfer_id)
    if offer is None or str(offer.from_user_id) != str(user_id):
        # Same answer for "no such transfer" and "not yours", which is the rule the rest of
        # this module follows: an id that is not yours must not be confirmable as existing.
        return False, "That handover isn't yours."
    if offer.outcome_seen_at is None:
        offer.outcome_seen_at = clock.now()
        offer.save(update_fields=["outcome_seen_at"])
    return True, "Done."


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


def respond_to_transfer(
    user_id, transfer_id, *, accept: bool, codes=None
) -> tuple[bool, str, dict | None]:
    """Accept or decline an offer. Returns ``(ok, message, result)``.

    RE-ENTRANT. Called on an offer already in ``charging`` or ``charged`` it picks up at
    the adopt step instead of starting a second charge, which is what makes a retry after
    a crash safe rather than a double bill.

    ``codes`` is the modules the recipient is TAKING ON (07-D "Choose Modules"). Omitted
    means all of them, which is every caller that has not been updated and every handover
    made before the screen offered the choice. Anything the company has and this does not
    name is CANCELLED as part of accepting — see ``_decline_modules``.

    NOT STORED ON THE OFFER, deliberately. The choice is made and acted on in this one
    request: the modules are cancelled, the payer flips, and what remains is read back off
    the rows from then on. A column recording it would be a second copy of something the
    rows already say, and the deferred collection re-reads them anyway.
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

    return _accept(offer, user_id, now, codes=codes)


def _decline_modules(entity_id, keep, at, actor_user_id) -> list[str]:
    """End the modules the incoming payer is not taking on. Returns the codes ended.

    ENDED AT ``at`` — the instant the outgoing payer's money runs out — and that exact
    date is the whole design. A module declined at a handover is one nobody is going to
    pay for after that day: the days up to it are bought and are honoured, and there are
    no days after it to charge anybody for.

    WHICH IS WHY THIS DOES NOT CALL ``checkout.cancel_module``. That path derives its own
    access end, ``max(paid_through, now + paid_cancel_access_days)`` (``billing.
    cancel_access_end``), which for a handover is almost always LATER than
    ``paid_through`` — 24 Oct against a paid-through of 18 Oct on today's policy. Those
    extra days are real access, so it books a real extension charge, and that charge would
    land on the OUTGOING payer for days the recipient asked not to have. It would also
    refuse the very accept it is part of: ``_blockers_money_settled`` is re-evaluated
    inside the accept and rejects any row carrying a pending extension above zero.

    Ending exactly at ``at`` owes nobody anything. ``_segmented_extension`` returns 0 for
    ``end <= paid_through``, so no extension is stamped at all, and the row is invisible
    to the blocker. Written directly for that reason, matching what the ordinary cancel
    writes in every other respect: phase, the access date, and an audit row. Access is not
    revoked here — ``access.access_end`` reads ``app_access_until`` first and the ordinary
    cancel leaves the gate alone too.

    A TRIAL IS DIFFERENT and goes through ``checkout.cancel_module``, which has its own
    branch for it: the free days run to ``trial_end`` and the trial then expires instead of
    converting. Nobody is charged either way, so there is nothing to end early.
    """
    from billing.services import checkout

    ended: list[str] = []
    for row in store.rows_for_entity(entity_id):
        code = (row.function_code or "").upper()
        if code in keep:
            continue
        if row.phase == PHASE_TRIAL:
            try:
                checkout.cancel_module(_EntityRef(entity_id), _UserRef(actor_user_id), code)
                ended.append(code)
            except Exception:
                logger.exception(
                    "transfer: could not end the declined trial {} on {}", code, entity_id
                )
            continue
        if not access.is_billing_forward(phase=row.phase):
            continue  # already winding down or dead - nothing to decline
        try:
            store.upsert_module_row(
                entity_id, code, row.payer_user_id,
                phase=PHASE_SCHEDULED_CANCEL,
                app_access_until=at,
            )
            store.record_action(
                entity_id=entity_id,
                function_code=code,
                payer_user_id=row.payer_user_id,
                actor_user_id=actor_user_id,
                action=AUDIT_CANCEL,
                outcome=OUTCOME_SUCCEEDED,
                phase_before=row.phase,
                phase_after=PHASE_SCHEDULED_CANCEL,
                app_access_until=at,
                note="declined at handover; access runs to the paid-through date, no extension",
            )
            ended.append(code)
        except Exception:
            logger.exception(
                "transfer: could not end the declined module {} on {}", code, entity_id
            )
    if ended:
        logger.info(
            "transfer: entity {} handed over without {}", entity_id, sorted(ended)
        )
    return ended


class _EntityRef:
    """What ``checkout.cancel_module`` reads off an entity: its id. The real row is not
    needed and fetching one here would be a second read of something already in hand."""

    def __init__(self, entity_id):
        self.id = entity_id


class _UserRef:
    def __init__(self, user_id):
        self.id = user_id


def _accept(offer, user_id, now, *, codes=None) -> tuple[bool, str, dict | None]:
    from billing.services import checkout

    entity_id = offer.entity_id

    # WHAT THEY ARE TAKING ON. ``codes`` is the recipient's choice from 07-D; omitted means
    # the whole company, which is every caller that predates the screen offering one.
    held = {(row.function_code or "").upper() for row in store.rows_for_entity(entity_id)}
    if codes is None:
        keep = set(held)
    else:
        keep = {str(c).strip().upper() for c in codes if str(c).strip()} & held
        # Refused rather than read as "all of it". A request naming only modules this
        # company does not have is a mistake somewhere, and the generous reading of it
        # hands over a company nobody agreed to take.
        if not keep:
            return False, "Choose at least one module to take over.", None

    # PRICED ON WHAT THEY KEEP. The rest are cancelled below, but only once the handover
    # is certain - see the comment on ``_finish``.
    codes = _billable_codes(entity_id) & keep

    # READ THE HANDOVER INSTANT NOW, never the value quoted when the offer was made.
    # ``run_renewals`` advances ``paid_through`` on every successful renewal, so an offer
    # that outlived a cycle would otherwise bill a window the outgoing payer has since
    # paid for — a full duplicate charge.
    # Read off the ENTITY: the days already bought for this company belong to the card the
    # outgoing payer had it on, and their other cards say nothing about it.
    at = _aware(store.paid_through_for_entity(offer.entity_id)) or now

    def _finish():
        """End the declined modules, then move the pointer.

        LAST, and only here. Cancelling before the accept is certain would leave a company
        stripped of modules by a handover that then failed on a declined card — the rows
        changed, the payer unmoved, and nobody told. Every path out of this function that
        succeeds goes through here; every path that refuses returns before it.
        """
        _decline_modules(entity_id, keep, at, user_id)
        return _complete(offer, actor_user_id=user_id, now=now)

    # NOTHING TO CHARGE IS NOT A FAILURE. A trial-only entity has been paid for by nobody,
    # so there is no window to buy and no invoice to raise — the handover is just the flip.
    #
    # Skipping straight to ``_complete`` also skips the whole journal: no ``charging``
    # state, no idempotency key, no external call. That is safe precisely because the
    # crash window the journal exists to close is the gap between taking money and moving
    # the pointer, and here no money moves. The blockers have already refused the
    # genuinely empty entity, so reaching this with no codes means a live trial.
    if not codes:
        return _finish()

    # A CARD IS REQUIRED HERE AND NOWHERE EARLIER. The offer is allowed to reach someone
    # who has none — being asked is not being charged — so these two refusals are the only
    # place the requirement lives, and they are read by the recipient themselves. Worded in
    # the second person and as an instruction, because they are now the first time anybody
    # is told, rather than a restatement of something the offering screen already refused.
    customer_id = store.customer_id_for_user(offer.to_user_id)
    if not customer_id:
        return False, "Add a payment method before taking over the billing.", None

    # THE INCOMING PAYER'S CARD, nominated here if they have not chosen one.
    #
    # They cannot have chosen one BEFORE this point: the nomination is per (company,
    # payer), and until they accept, the company is not theirs — ``payment_methods``
    # refuses to let a non-payer point a card at somebody else's company. So the accept is
    # the first moment the choice can exist, and it is made from their account default —
    # the card the quote they are accepting was priced and shown against, saved either
    # before the offer arrived or on the accept screen itself.
    #
    # This is not the silent fallback the rest of the engine refuses. It is written, once,
    # as a consequence of an explicit "yes, bill me for this company" — recorded with its
    # own source so it can be told apart later — and they can move it afterwards. The
    # alternative is refusing an accept for want of a choice there was no way to make.
    if store.billing_group_for_entity(entity_id, offer.to_user_id) is None:
        from billing.services import stripe_client

        default_card = stripe_client.customer_default_payment_method(customer_id)
        if not default_card:
            return False, "Add a payment method before taking over the billing.", None
        store.nominate_card_for_entity(
            entity_id, offer.to_user_id, default_card, "transfer"
        )

    # NOTHING IS CHARGED FOR DAYS THAT HAVE NOT STARTED YET.
    #
    # ``at`` is where the outgoing payer's money runs out, and it is normally in the
    # FUTURE — they have bought days nobody has used. Charging here asked the incoming
    # payer for money weeks before the thing it pays for began: accepted on 24
    # September, "HKD 245.16 for 18 Oct to 6 Nov", taken today. A handover takes no
    # money.
    #
    # The window and the amount do not change. Only the DAY moves, to the start of the
    # window itself, where ``collect_due`` takes it. The handover still COMPLETES here —
    # the payer flips, consent is recorded, the old payer stops being liable — because
    # who owns the company and who has paid for it were never the same question. That is
    # also why this sits AFTER the card: the nomination is part of taking a company on,
    # and the collection weeks later has no screen to ask on.
    #
    # NO CLAIM IS WRITTEN with it, and that half is load-bearing. ``_complete`` passes
    # ``accepted_billed_through`` to ``transfer_entity_payer``, which stamps
    # ``billed_through`` on the rows, and ``renewals.entities_covered_into`` then
    # suppresses the new payer's renewal for every period that claim covers. Left set,
    # the company would run free AND be marked paid for — an absence with no invoice to
    # notice it by. Leaving it NULL is also what keeps the deferral collectable: the
    # card's ``paid_through`` is untouched, so ``due_renewals`` cannot reach this company
    # first, and the collection below is the only thing that will ever bill it.
    if at > now:
        offer.collect_at = at
        offer.save(update_fields=["collect_at"])
        return _finish()

    # Otherwise the window has already begun — the outgoing payer's money ran out before
    # anybody got round to accepting — so these are days being used right now. There is
    # no future date to defer to and they are owed, which is the path below.

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

    return _finish()


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


# --- the deferred charge --------------------------------------------------------------


def collect_due(now=None, *, limit=None) -> dict:
    """Charge the handovers whose parked first charge has come due.

    The other half of ``_accept``'s deferral. An accept takes no money for days that
    have not started; it parks the instant they do on ``collect_at`` and completes. This
    is what turns up on that day and takes it — the SAME charge, through the same
    ``_bill_transfer_in_house``, for the same window and the same amount. Only the day
    it happens is different.

    ``collect_at`` IS the marker, and clearing it is the only record of settled. There is
    no second flag to disagree with it, and nothing infers the state from an absent
    invoice id — a zero-total charge legitimately has none.

    WHAT A FAILURE DOES, and why it is not simply handed to dunning. A declined card
    here leaves the row UNCOLLECTED, so the next pass tries again with a fresh key: the
    counter keeps climbing and the key carries it, exactly as a re-attempted accept does,
    because voiding an invoice keeps its key claimed and a stable one would jam the retry
    forever. Dunning cannot do that job — it chases the payer's oldest OPEN invoice, and
    the failed charge voided its own. What dunning IS started for is the consequence:
    the rows go past due, the grace window begins, and access lapses if the card is never
    fixed. So the retry is here and the access story is dunning's, which is the split
    that actually works rather than the one that reads tidiest.

    Returns ``{"collected": [...], "failed": [...], "abandoned": [...]}``.
    """
    from billing.services import checkout

    now = now or clock.now()
    parked = SubscriptionTransfer.objects.filter(collect_at__isnull=False).order_by("collect_at")
    rows = list(parked[:limit] if limit else parked)
    result: dict[str, list] = {"collected": [], "failed": [], "abandoned": []}

    for offer in rows:
        try:
            # DUE-NESS IS DECIDED IN PYTHON, not in the query — the same rule
            # ``_expire_lapsed`` follows and for the same reason: pushing an aware
            # datetime at a column that may be naive is how a charge lands a day early.
            due = _aware(offer.collect_at)
            if due is None or due > now:
                continue
            outcome = _collect_one(offer, due, now, checkout)
            if outcome is not None:
                bucket, entry = outcome
                result[bucket].append(entry)
        except Exception:
            # One bad handover never stops the pass, and never loses its ``collect_at``:
            # an exception here leaves the row exactly as it was, still owed, still due.
            logger.exception("transfer: could not collect the deferred charge on {}", offer.id)

    if any(result.values()):
        logger.info("transfer: collected deferred handover charges {}", _summarise(result))
    return result


def _summarise(result: dict[str, list]) -> dict[str, int]:
    return {key: len(value) for key, value in result.items()}


def _abandon(offer, reason: str) -> tuple[str, dict]:
    """Stop asking for this one. The debt is not collectable and never will be."""
    offer.collect_at = None
    offer.save(update_fields=["collect_at"])
    logger.warning(
        "transfer: abandoned the deferred charge on {} ({})", offer.id, reason
    )
    return "abandoned", {"transfer_id": offer.id, "entity_id": offer.entity_id, "reason": reason}


def _collect_one(offer, due, now, checkout) -> tuple[str, dict] | None:
    entity_id = offer.entity_id
    to_user_id = offer.to_user_id

    # THE COMPANY MUST STILL BE THEIRS. A handover can be handed on again before this
    # falls due, and the second one reads the paid-through as it stands and parks its own
    # window. Charging the first recipient for days a third party now owns would bill
    # somebody for a company they no longer have.
    current = store.payer_for_entity(entity_id)
    if current and str(current) != str(to_user_id):
        return _abandon(offer, "the company has changed hands again")

    # RE-READ, never trust the set quoted at accept. Weeks have passed; a module may have
    # been cancelled, or gone to trial, and billing forward is the only thing to charge
    # for. Nothing left to bill is not a failure — it is a company that owes nothing.
    codes = _billable_codes(entity_id)
    if not codes:
        return _abandon(offer, "nothing is billing forward on this company any more")

    customer_id = store.customer_id_for_user(to_user_id)
    group = store.billing_group_for_entity(entity_id, to_user_id)
    if not customer_id or group is None:
        # Not abandoned. The card was there at accept and has gone since, so this is the
        # same shape as a decline: past due, and tried again when they put one back.
        return _fail(offer, group, now, "there is no card to charge for this company")

    # A FRESH KEY PER ATTEMPT, carrying the counter — the accept's rule, and the reason
    # is the same: the previous attempt's invoice was voided and its key stays claimed.
    offer.charge_attempt = int(offer.charge_attempt or 0) + 1
    offer.charge_key = f"transfer-{offer.id}-{offer.charge_attempt}"
    offer.save(update_fields=["charge_attempt", "charge_key"])

    result = checkout._bill_transfer_in_house(
        entity_id, to_user_id, customer_id, codes, at=due, idempotency_key=offer.charge_key,
    )
    if not result["paid"]:
        return _fail(offer, group, now, result["reason"])

    # PAID. The claim goes on NOW and not a moment earlier: it is the statement that
    # these days are covered, and until this instant they were not. Written through the
    # same one-statement writer the accept uses, which moves the claim forward only and
    # leaves the payer pointer where it already is.
    store.transfer_entity_payer(entity_id, to_user_id, billed_through=_aware(result["period_end"]))

    offer.collect_at = None
    offer.charge_invoice_id = result["invoice_id"]
    offer.accepted_billed_through = result["period_end"]
    offer.accepted_anchor_at = result["anchor"]
    offer.quoted_amount = result["amount"]
    offer.quoted_currency = result["currency"]
    offer.save(update_fields=[
        "collect_at", "charge_invoice_id", "accepted_billed_through",
        "accepted_anchor_at", "quoted_amount", "quoted_currency",
    ])

    # Out of dunning, if an earlier attempt put it there. Ordered after the money and
    # before the sweep: the rows have to be out of ``past_due`` for the sweep to restore
    # what it revoked.
    if getattr(group, "dunning_started_at", None) is not None:
        try:
            store.end_group_dunning(group.id, status="active")
        except Exception:
            logger.exception("transfer: could not end dunning for group {}", group.id)

    for code in sorted(codes):
        store.record_action(
            entity_id=entity_id,
            function_code=code,
            payer_user_id=to_user_id,
            action=AUDIT_TRANSFER_COLLECTED,
            outcome=OUTCOME_SUCCEEDED,
            actor_user_id=None,
            note=f"deferred handover charge collected for {due:%d %b %Y}",
        )

    try:
        from billing.services.access_sweep import sweep_expired_module_access

        sweep_expired_module_access(payer_user_id=to_user_id)
    except Exception:
        logger.exception("transfer: could not re-sync module access for {}", to_user_id)

    return "collected", {
        "transfer_id": offer.id,
        "entity_id": entity_id,
        "user_id": to_user_id,
        "invoice": result["invoice_id"],
        "amount": result["amount"],
        "currency": result["currency"],
    }


def _fail(offer, group, now, reason) -> tuple[str, dict]:
    """Left owed, and the company put past due — see ``collect_due``'s docstring.

    ``collect_at`` is deliberately NOT cleared: the next pass tries again with a fresh
    key, which is what lets a fixed card settle it without anybody re-accepting.
    """
    if group is not None:
        try:
            store.begin_group_dunning(group.id, now)
        except Exception:
            # The failure entry must survive a failure to record it — the same nested
            # try ``run_renewals`` uses on this exact call.
            logger.exception("transfer: could not start dunning for group {}", group.id)
    logger.warning(
        "transfer: deferred charge on {} was not collected ({})", offer.id, reason
    )
    return "failed", {
        "transfer_id": offer.id,
        "entity_id": offer.entity_id,
        "user_id": offer.to_user_id,
        "reason": reason,
    }


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
    from billing.services import billing_gateway, renewals

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
                # Except that a DRAFT cannot be settled by anybody: it was never
                # finalized, so it is not in any list a customer or dunning pays from.
                if status == "draft":
                    billing_gateway.stranded_draft(
                        getattr(store.invoice_for_key(offer.charge_key), "external_id", None),
                        offer.charge_key, "transfer", billing_gateway.NOT_RETRIED,
                    )
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

    A SAVED CARD IS NOT ASKED FOR HERE, deliberately. It used to be block 6, and it was
    the wrong question at the wrong moment:

      * it refused a person for a thing they could fix in the next ten seconds, and
        refused them BEFORE they had been asked whether they wanted the company at all;
      * the offering screen never showed it. ``portal.build_subscriber_options`` drops
        every reason starting "That person" and evaluates blockers against one arbitrary
        candidate, so a card-less admin rendered as selectable and the POST then refused —
        the screen and the API disagreeing about the same person;
      * on the recipient's own side it read as a sentence about somebody else ("That
        person needs...") describing themselves, above a disabled button.

    What genuinely cannot proceed without a card is the CHARGE, and that is guarded where
    it happens: ``_accept`` refuses with no customer and refuses with no default card to
    nominate. The accept screen collects one before it gets there.
    """
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
