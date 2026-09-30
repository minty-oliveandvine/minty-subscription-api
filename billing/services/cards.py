"""The module cards: what the settings page shows per module, per entity.

Moved out of ``entity.services.modules``. A card is almost entirely subscription state --
phase, trial end, access window, what converting would charge -- wrapped in the catalogue
copy for the module it describes. It lived in the entity blueprint because the page that
renders it does.

``_forecast_conversion_charges`` travels with it: it exists only to fill the
``conversion_charge`` field these cards carry, and it answers the question the card asks
("what does saying yes cost today?") rather than any question about entities.

WHAT IT READS BACK, and why through the module object: ``_enabled_state`` and
``_entity_customer_id`` are the entity-side gate and stay behind, and so do the constants.
``_entity_customer_id`` in particular is patched by ``test_module_card_lapsed``,
``test_onboarding_payment_method`` and ``test_subscription_trials`` -- binding it with a
``from ... import`` here would capture the real one and ignore all three silently.

``money`` is deliberately NOT imported at module scope: ``get_module_cards`` already
imports it inside the function, alongside the rest of the subscription services it needs,
and a second binding at module level would only shadow that one.
"""
from __future__ import annotations

from decimal import Decimal

from billing.services import entity_modules, money
from billing.services._log import logger
from billing.services.display import day as fmt_day
from billing.services.display import day_month as fmt_day_month
from shared_models.models import EntityFunction


def get_module_cards(entity_id: str) -> list[dict]:
    """Build the module-settings cards for an entity, backend-driven.

    Merges three sources into one card per canonical module:
      * catalog copy — entity_function.function_name / description (the code
        itself is the last-resort fallback when the catalog row is missing);
      * presentation metadata — entity_modules.MODULE_DISPLAY (image, learn_more);
      * LIVE Stripe state — the available plan's price, and the entity's
        subscription status / period end / grace-aware access end, read from
        the module rows plus ``access.py`` — one source, so the card and checkout
        cannot disagree about what the entity holds.

    Cards come back in the canonical entity_modules.MODULE_CODES order. Subscription state is
    None when the entity isn't subscribed or Stripe isn't configured.
    """
    from billing.services import access, catalog, clock, policy
    from billing.services import store as sub_store
    from billing.services.constants import (
        EXT_PENDING,
        PHASE_ACTIVE,
        PHASE_PAST_DUE,
        PHASE_SCHEDULED_CANCEL,
        PHASE_TRIAL,
    )

    catalog_by_code = {
        fn.function_code: fn
        for fn in EntityFunction.objects.filter(function_code__in=entity_modules.MODULE_CODES)
    }

    from billing.services.stripe_client import (
        customer_default_payment_method,
    )

    customer_id = entity_modules._entity_customer_id(entity_id)
    plans_by_code = {p.function_code.upper(): p for p in catalog.available_plans()}
    now = clock.now()

    # A trial without a card CANCELS at the end of its term (the card is optional at
    # signup), so those cards get an "add a card to keep this module" nudge. One lookup
    # for the whole page - it is a customer-level fact, not a per-module one.
    has_payment_method = bool(
        customer_id and customer_default_payment_method(customer_id)
    )

    has_billing_consent = _billing_consent_or_assume(entity_id, sub_store)

    will_convert = has_payment_method and has_billing_consent

    # THE module state, and now the only source of it.
    #
    # This used to merge live Stripe subscription views with these rows, which meant a
    # module's status could be read two ways and the two could disagree: an app-level
    # trial was invisible to Stripe, and a module cancelled out of a bundle had no
    # Stripe view at all because its line had been swapped down. The row plus
    # ``access.py`` answers every question this card asks, from one source.
    rows, payer_id, paid_through = _module_rows_for_cards(entity_id, sub_store)
    # Past due is "payment failed" only once the customer has been told: a card held in its
    # grace while the payment PROCESSOR was failing was never asked (``dunning.customer_told``).
    # Read only when something is past due - most companies, most of the time, nothing is.
    told = not any(getattr(r, "phase", None) == PHASE_PAST_DUE for r in rows.values()) or (
        _told_of_failure(entity_id, now)
    )

    # What this period was ALREADY PAID FOR. A trial converting alongside these is a
    # mid-period change priced against them; converting with nothing here just starts the
    # cycle. Mirrors checkout._billed_codes_in_house exactly — the forecast and the
    # invoice must not be computed two ways — INCLUDING its treatment of a module that is
    # winding down: cancelled, but paid up to the period end, so for the days before that
    # end it is still on the line and still part of a bundle. Only until the renewal that
    # drops it, though, which is what the extension state and access end below are for:
    # the payer's paid_through moves on without the module, and reading the date alone
    # kept it on the line for a period it was never billed for.
    billed_now = {
        c
        for c, r in rows.items()
        if access.is_covered_this_period(
            phase=getattr(r, "phase", None) or "",
            first_billed_at=getattr(r, "first_billed_at", None),
            paid_through=paid_through,
            now=now,
            extension_state=getattr(r, "extension_state", None),
            app_access_until=getattr(r, "app_access_until", None),
        )
    }

    # Read once for the whole card set, not per module: every card on this page must
    # answer against the same window, and re-reading it per row is a lookup for nothing.
    grace_days = policy.current().past_due_window_days

    access_state = _access_state_or_deny(entity_id)

    cards: list[dict] = []
    for code in entity_modules.MODULE_CODES:
        display = entity_modules.MODULE_DISPLAY.get(code, {})
        fn = catalog_by_code.get(code)
        row = rows.get(code.upper())
        phase = getattr(row, "phase", None) or ""

        granted = row is not None and access.grants_access(
            now,
            phase=phase,
            trial_end=getattr(row, "trial_end", None),
            app_access_until=getattr(row, "app_access_until", None),
            period_end=paid_through,
            past_due_grace_days=grace_days,
        )
        # A CANCELLED trial is still a trial: cancelling one does not end it, it only
        # stops it converting to paid, so the free days keep running (see
        # checkout.cancel_module). It must still present as trialing - otherwise the
        # card reads "Not subscribed" while the user demonstrably still has access.
        # ``first_billed_at`` is what separates it from a cancelled PAID module, which
        # is winding down for an entirely different reason.
        never_billed = getattr(row, "first_billed_at", None) is None
        app_trial_running = bool(row is not None and phase == PHASE_TRIAL and granted)
        app_trial_cancelled = bool(
            row is not None
            and phase == PHASE_SCHEDULED_CANCEL
            and never_billed
            and getattr(row, "trial_end", None) is not None
            and granted
        )
        app_trial = app_trial_running or app_trial_cancelled

        # Whether the request gate would let this entity into the module right now.
        # Normally implied by ``granted`` — access only projects the row — but the two
        # can part company between a write and the sweep that repairs it, and the card
        # must side with the gate.
        has_access = bool(access_state.get(code))

        # THE WINDOW BETWEEN THE TERM ENDING AND THE JOB CLOSING IT OUT.
        #
        # ``trial_end`` passes unattended. Until the subscription pass runs, the row is
        # still ``phase = trial`` while ``granted`` has already gone false — so the card
        # dropped out of "trialing", failed the paid branch too, and landed on
        # ``trial_expired``: it told a customer their free trial was used up while the
        # gate was still letting them work, and while a trial with a card and consent was
        # in fact about to CONVERT. Premature, and for the converting case the opposite of
        # what was coming.
        #
        # Bounded by construction, which is what makes it safe to show. It needs the gate
        # to still say yes, and the next pass ends it either way — close-trials converts
        # or expires the row, and failing that the sweep revokes access because
        # ``grants_access`` is already false. A scheduler that stopped cannot leave a card
        # stuck here; it resolves to the honest ``trial_expired`` as soon as anything runs.
        _trial_end = getattr(row, "trial_end", None)
        app_trial_closing = bool(
            row is not None
            and phase == PHASE_TRIAL
            and not granted
            and has_access
            # BOUNDED BY TIME, and this is the whole safety of the state.
            #
            # It was first written as "phase is trial, access is still on" and justified
            # as self-limiting: the next pass converts or expires the row within the hour,
            # so it could not persist. That is only true where the pass RUNS. On an
            # environment with no scheduler — which production was, and which any
            # environment becomes the moment the scheduler is off — a trial past its term
            # keeps its phase and its access indefinitely, and the unbounded version
            # matched every one of them, forever: a trial that ended three weeks ago
            # rendered as a running trial with a past date, and ``needs_card`` turned on
            # with it, which displaced the billing-portal banner with a nudge quoting a
            # deadline in the past.
            #
            # So the window is explicit. Past it, the trial is not "being closed out", it
            # is simply over and nothing came for it — which is what ``trial_expired`` has
            # always said.
            and _trial_end is not None
            and (now - _trial_end) <= entity_modules.TRIAL_CLOSING_WINDOW
        )
        # Folded into ``app_trial`` rather than given a status of its own. Everything
        # downstream — the panel's enabled set, the notices, the badge, the row styling —
        # asks "is this trialing", and answering differently for the hour before the pass
        # runs would rearrange the whole page around a state the customer cannot act on and
        # which resolves by itself. It IS still a trial: nothing has closed it out. The
        # closing flag is carried separately and used for exactly one thing, a label.
        app_trial = app_trial or app_trial_closing

        # Trial eligibility: a module this entity has NEVER held. The trial is
        # once-per-module, so ANY history disqualifies it - including a lapsed one - and
        # those go through paid checkout instead. The row existing at all IS that
        # history, which is exactly what ``checkout.start_module_trial`` checks.
        #
        # Access with no row disqualifies it too. Offering a trial there produced the
        # contradiction this whole model exists to remove: a clickable "Start free trial"
        # on a module the user was already working inside.
        trial_eligible = fn is not None and row is None and not has_access

        # ...and the other side of that coin: a trial this entity USED UP. It held one,
        # it is over, and nothing was ever charged — so the module is off and cannot be
        # trialled again. "not active" on its own reads as "never had this", which leaves
        # the customer wondering why the card offers Subscribe instead of a free trial.
        # A module whose PAID subscription ended is a different sentence and is excluded
        # by never_billed.
        trial_expired = bool(
            row is not None
            and never_billed
            and getattr(row, "trial_end", None) is not None
            and not granted
            # Not yet: the term is up but the pass has not closed it out, and the customer
            # is still working inside the module. Calling that "expired" is a guess about
            # an outcome that has not been decided — see ``app_trial_closing``.
            and not app_trial_closing
        )

        plan = plans_by_code.get(code.upper())
        amount = (
            money.to_major(plan.amount, plan.currency_code)
            if plan
            else Decimal(0)
        )

        # Winding down: a scheduled cancellation still inside its paid days, or a
        # renewal that failed and is inside its grace. Both show "access until ... /
        # Renew" rather than Subscribe - offering Subscribe would double-charge, since
        # the queued extension bills on the anchor AND the module is billed afresh.
        winding_down = phase in (PHASE_SCHEDULED_CANCEL, PHASE_PAST_DUE) and granted
        access_end = (
            access.access_end(
                phase=phase,
                trial_end=getattr(row, "trial_end", None),
                app_access_until=getattr(row, "app_access_until", None),
                # From the ACCOUNT, not the row: one payer has one cycle, and the
                # per-row copy drifts apart between their entities.
                period_end=paid_through,
                past_due_grace_days=grace_days,
            )
            if row is not None
            else None
        )
        access_end_date = (
            access_end.strftime("%B %d, %Y")
            if (winding_down and access_end and access_end > now)
            else None
        )
        pending_cancel = bool(access_end_date)

        # What the customer is told about the next date. A trial runs to its term; a
        # paid module runs to what the payer is paid through.
        #
        # ``granted`` gates the paid branch as well as the trial one. The phase alone is
        # not "is this live": a phase stays ``active`` until something writes it, while
        # access ends on a DATE that passes unattended (the sweep is what reconciles the
        # gate afterwards — see sweep_expired_module_access). Reading the phase by itself
        # left a module whose period had lapsed, and whose access had just been swept
        # off, still badged "active" with "valid until <a date in the past>" and a Cancel
        # button — the card claiming a subscription the request gate would refuse.
        if app_trial:
            subscription_status = "trialing"
            period_end = getattr(row, "trial_end", None)
        elif granted and phase in (PHASE_ACTIVE, PHASE_PAST_DUE, PHASE_SCHEDULED_CANCEL):
            subscription_status = "past_due" if phase == PHASE_PAST_DUE and told else "active"
            period_end = paid_through
        else:
            # Never held it, or held it and lost it. Either way there is nothing live to
            # cancel and the card offers the way back in.
            subscription_status = None
            period_end = None
        formatted_period_end = period_end.strftime("%B %d, %Y") if period_end else None

        cards.append(
            {
                "code": code,
                "name": (fn.function_name if fn and fn.function_name else code),
                "description": (fn.description if fn and fn.description else ""),
                "image": display.get("image", ""),
                "learn_more": display.get("learn_more", "#"),
                # The double-buy guard: a live paid module OR a running trial.
                # ``access.is_subscribed`` owns that rule, so the card and checkout
                # cannot disagree about whether the entity already has this module.
                "is_subscribed": bool(
                    row is not None and access.is_subscribed(phase=phase)
                ),
                "trial_eligible": trial_eligible,
                # The term is up and the pass has not closed it out yet. Presentation
                # ONLY: the card is otherwise a running trial in every respect, and this
                # adds a line saying the outcome is being settled. Never gate behaviour on
                # it — see where it is set.
                "trial_closing": app_trial_closing,
                # Held a trial, used it up, never paid: the card says "free trial
                # expired" under its status so "not active" is not the whole story.
                "trial_expired": trial_expired,
                # The other half of that story: PAID for, and now out of access — with
                # the DATE it ran out, and deliberately without a verdict on why.
                #
                # "Subscription ended" was wrong here. A module whose paid period lapses
                # keeps ``phase = active`` until something writes it (see
                # sweep_expired_module_access), so nothing has ended: nobody cancelled,
                # and the renewal simply never replaced the period. Stating the date is
                # true whether it lapsed, was cancelled to completion, or is waiting on a
                # renewal that has not run.
                "lapsed_long": (
                    fmt_day(
                        getattr(row, "app_access_until", None) or paid_through
                    )
                    if (
                        row is not None
                        and not never_billed
                        and not granted
                        and (getattr(row, "app_access_until", None) or paid_through)
                    )
                    else None
                ),
                # What the request gate answers for this module. The card's "is it on"
                # branch reads THIS, not subscription_status, so what the page shows and
                # what the user can actually open are the same question.
                "has_access": has_access,
                # No Stripe subscription exists any more. Kept as a key because the
                # template still reads it; Renew and Cancel act on the module CODE.
                "subscription_id": None,
                "subscription_status": subscription_status,
                # Whether the user can cancel this module right now. An already-cancelled
                # trial is still trialing (access runs on) but must not offer Cancel
                # again - its card shows "access until ... / Renew" instead.
                #
                # ``granted`` for the same reason it gates subscription_status above: a
                # module whose period has lapsed keeps its ``active`` phase until
                # something writes it, and there is nothing live to cancel.
                "can_cancel": bool(
                    app_trial_running
                    or (
                        granted
                        and phase in (PHASE_ACTIVE, PHASE_PAST_DUE)
                        and not pending_cancel
                    )
                ),
                "amount": amount,
                "formatted_amount": (
                    money.format_minor(plan.amount, plan.currency_code)
                    if plan
                    else "0.00"
                ),
                "currency_code": (plan.currency_code if plan else None),
                "billing_interval": (plan.billing_interval if plan else "month"),
                "cancel_at_period_end": phase == PHASE_SCHEDULED_CANCEL,
                "pending_cancel": pending_cancel,
                # A cancelled free trial, still running. Distinct from a cancelled PAID
                # module: no money changed hands, so the copy is "will not convert"
                # rather than "you paid for these days", and resuming it costs nothing.
                "trial_cancelled": app_trial_cancelled,
                "formatted_period_end": formatted_period_end,
                # Compact / long variants for the redesigned card + panel: "19 Aug"
                # for the trial pill, "19 Aug 2026" for the "valid until" line.
                "period_end_short": fmt_day_month(period_end),
                "period_end_long": fmt_day(period_end),
                # Raw datetime as well as the formatted strings: the panel needs to
                # compare dates to pick the EARLIEST next-invoice date across modules,
                # and re-parsing "19 Aug 2026" to do it would be absurd.
                "period_end": period_end,
                # What converting THIS trial will charge on the spot, on top of the
                # monthly rate. Filled by _forecast_conversion_charges AFTER this loop:
                # the answer depends on the OTHER trials, because whichever converts
                # first starts the cycle every later one is then prorated against.
                "conversion_charge": Decimal(0),
                # A cancel-extension already recorded on this module and not yet billed.
                # It is a real line on the payer's next invoice (renewals.
                # _pending_extension_lines), so a panel quoting only the recurring price
                # would understate what is about to be charged.
                "extension_amount": (
                    money.to_major(
                        getattr(row, "extension_amount", None),
                        plan.currency_code if plan else None,
                    )
                    if row is not None
                    and getattr(row, "extension_state", None) == EXT_PENDING
                    and getattr(row, "extension_amount", None)
                    else Decimal(0)
                ),
                # Ready to print. The card states what the cancellation costs, and money
                # on a card is formatted by the same rule as money in the panel.
                "extension_formatted": (
                    money.format_trimmed(
                        money.symbol(plan.currency_code if plan else None),
                        money.to_major(
                            row.extension_amount, plan.currency_code if plan else None
                        ),
                        money.decimal_places(plan.currency_code if plan else None),
                    )
                    if row is not None
                    and getattr(row, "extension_state", None) == EXT_PENDING
                    and getattr(row, "extension_amount", None)
                    else None
                ),
                "access_end_date": access_end_date,
                "access_end_long": (
                    fmt_day(access_end)
                    if (winding_down and access_end and access_end > now)
                    else None
                ),
                # A trial that will not convert CANCELS when the term ends - warn while
                # there is still time to fix it. Two reasons it will not: no card at
                # all, or a card the payer never authorised for THIS entity.
                "needs_card": bool(
                    app_trial and not will_convert and not pending_cancel
                ),
                # Which of the two it is, so the banner can say "add a card" vs
                # "confirm billing for this company" - the fix differs.
                "needs_consent_only": bool(
                    has_payment_method and not has_billing_consent
                ),
            }
        )
    _fill_conversion_charges(cards, entity_id, payer_id, billed_now)
    return cards

def _forecast_conversion_charges(entity_id, payer_id, billed_now, cards) -> dict[str, int]:
    """{code: minor units} actually charged ON each converting trial's date.

    The whole charge for that day, not a top-up on a monthly rate: the row it feeds reads
    "Petty Cash converts · 28 Aug · HKD 280", and that figure is what leaves the card.

    SEQUENTIAL, and that is the whole point. Trials that end on different days convert on
    different days, and the FIRST one to land pins the payer's billing anchor and bills a
    full period. Every later conversion is then a mid-period CHANGE against the cycle that
    first one started — credit the old price, charge the new, prorated over the days left
    (the 373.33 / 28.00 oracles in tests/test_change_billing.py).

    Forecasting each trial independently against today's state answers 0 for all of them,
    because today there is no anchor and nothing billed — which is exactly the bug this
    replaces: two trials a fortnight apart both quoted a plain "/mo" and the second
    module's prorated charge arrived with no warning anywhere on the page.

    Calls the same ``changes.build_change`` the conversion itself does
    (``checkout._bill_module_change_in_house``), so the forecast and the invoice cannot be
    computed two ways. What it does NOT mirror is the anchoring side effect: this reads.

    Two shapes, matching ``_bill_module_change_in_house`` exactly:
      * the conversion that STARTS the payer's cycle is charged a FULL period for the
        modules it turns on — the plan price, not a proration, because the payer was
        never part of an earlier period to prorate against;
      * every later one is a mid-period change: credit the old price for the unused days,
        charge the new for them, and the NET is what the card is hit for.

    A code maps to 0 only when there is genuinely nothing to take — a downgrade, or a
    combination the catalog cannot price, both of which make ``build_change`` return None
    rather than guess.
    """
    if not payer_id:
        return {}

    # Only trials that will actually convert. One that expires charges nothing, so
    # forecasting it would invent a number for an event that never happens — and it must
    # not advance the simulated anchor either.
    converting = sorted(
        (
            c
            for c in cards
            if c.get("subscription_status") == "trialing"
            and not c.get("needs_card")
            and not c.get("pending_cancel")
            and c.get("period_end")
        ),
        key=lambda c: c["period_end"],
    )
    if not converting:
        return {}

    try:
        from billing.services import changes
        from billing.services import store as sub_store
        from billing.services.billing import period_containing

        anchor, _currency = sub_store.billing_cycle_for_user(payer_id)
    except Exception:
        logger.exception("modules: could not read the billing cycle for {}", entity_id)
        return {}

    billed: set[str] = {str(c).upper() for c in (billed_now or ())}
    out: dict[str, int] = {}
    for card in converting:
        code = (card["code"] or "").upper()
        at = card["period_end"]
        if anchor is None:
            # Nothing has ever been billed for this payer, so THIS conversion starts the
            # cycle and anchors it on its own date — exactly the branch
            # _bill_module_change_in_house takes when it finds no anchor. Anchoring HERE
            # rather than short-cutting to the plan price keeps the line below the only
            # arithmetic in this function: with the period starting at the conversion,
            # build_change has a whole period left to bill and returns the full charge by
            # the same route it prorates a later one. Later trials in this loop then
            # prorate against the cycle it just created, which is what a per-card
            # forecast cannot see.
            anchor = at
        try:
            # The period containing the CONVERSION, not today's — a trial ending next
            # month prorates against the period it lands in.
            period = period_containing(anchor, at)
            invoice = changes.build_change(
                entity_id, str(entity_id), billed, billed | {code}, period, at
            )
            out[code] = int(getattr(invoice, "total", 0) or 0) if invoice else 0
        except Exception:
            # A forecast must never cost anyone the page. Silence here only drops the
            # "charged on the day" line; the conversion itself bills from its own path.
            logger.exception(
                "modules: could not forecast the conversion charge for {} {}",
                entity_id,
                code,
            )
            out[code] = 0
        billed.add(code)
    return out


def get_trial_modules_for_entities(entity_ids: list[str]) -> dict[str, set[str]]:
    """Map each entity id to the set of module codes currently on a FREE TRIAL.

    One query for the whole list. The per-entity path (``get_module_cards``) answers
    the same question far more thoroughly, but it reads Stripe, the billing policy
    and the payer's cycle for every entity it is asked about — repeating that once
    per row on the "select company" page would put a payment-provider round trip
    behind a page that only wants to draw a badge.

    "On a free trial" is the module card's claim, minus the states a badge cannot
    express:

      * the row was NEVER billed (``first_billed_at is None``) — that is what
        separates a trial from a paid module that is winding down, and both share
        the ``scheduled_cancel`` phase;
      * it is either running (``phase = trial``) or a CANCELLED trial, which stops
        the conversion to paid without ending the free days (see
        ``checkout.cancel_module``) and so is still a trial on screen;
      * the free days have not run out — with the same ``entity_modules.TRIAL_CLOSING_WINDOW``
        slack a running trial gets while it waits for the pass that closes it out,
        so the badge does not blink off in the hour before the job converts it.

    Fail-soft: any error yields no trials rather than an exception. A missing badge
    costs a hint; a raise here costs the user the whole entity list.
    """
    if not entity_ids:
        return {}

    # Imported here, not at module scope: the subscription package imports back into
    # the entity models, and this module is loaded early enough for that to bite.
    from billing.services import clock
    from billing.services.constants import PHASE_SCHEDULED_CANCEL, PHASE_TRIAL
    from shared_models.models import EntityModuleSubscription

    try:
        now = clock.now()
        rows = list(
            EntityModuleSubscription.objects.filter(
                entity_id__in=[str(e) for e in entity_ids],
                first_billed_at__isnull=True,
                trial_end__isnull=False,
                phase__in=(PHASE_TRIAL, PHASE_SCHEDULED_CANCEL),
            )
        )
    except Exception:
        # (Flask rolled its session back here; autocommit needs no reset.)
        logger.exception("modules: could not read trial state for the entity list")
        return {}

    trials: dict[str, set[str]] = {}
    for row in rows:
        # Precedence rule 1 of ``access.access_end``: the app's own promise outranks
        # the term. A cancelled trial carries its remaining days there.
        ends_at = row.app_access_until or row.trial_end
        if ends_at <= now and not (
            row.phase == PHASE_TRIAL and (now - ends_at) <= entity_modules.TRIAL_CLOSING_WINDOW
        ):
            continue
        trials.setdefault(row.entity_id, set()).add(
            (row.function_code or "").upper()
        )
    return trials

def get_module_plan_catalog() -> dict:
    """The module price list for the onboarding wizard, live from Stripe.

    Entity-independent — no customer, no subscriptions, just what each canonical
    module costs plus the bundle price. Onboarding Step 2 uses it to preview the same
    subtotal / discount / total the settings page shows, and that checkout will
    actually bill once the app-level trial converts to paid.

    A module with no live Stripe plan is omitted rather than priced at zero, so a
    half-seeded catalog can't invent a price. Amounts come back as JSON numbers,
    already converted out of Stripe's smallest currency unit.
    """
    from billing.services import catalog, money, policy

    catalog_by_code = {
        fn.function_code: fn
        for fn in EntityFunction.objects.filter(function_code__in=entity_modules.MODULE_CODES)
    }
    plans_by_code = {
        plan.function_code.upper(): plan for plan in catalog.available_plans()
    }

    plans: list[dict] = []
    for code in entity_modules.MODULE_CODES:
        plan = plans_by_code.get(code.upper())
        if plan is None:
            continue
        fn = catalog_by_code.get(code)
        amount = money.to_major(plan.amount, plan.currency_code)
        plans.append(
            {
                "code": code,
                "name": fn.function_name if fn and fn.function_name else code,
                "amount": float(amount),
                "formatted_amount": money.format_minor(plan.amount, plan.currency_code),
                "currency_code": plan.currency_code,
                "currency_symbol": money.symbol(plan.currency_code),
                "billing_interval": plan.billing_interval or "month",
            }
        )

    bundle = catalog.bundle_plan()
    bundle_amount = (
        money.to_major(bundle.amount, bundle.currency_code)
        if bundle
        else Decimal("0")
    )

    return {
        "plans": plans,
        # The bundle price + its modules: the wizard bills the bundle price when the
        # picked set is exactly these, else the sum of the standalone plans — exactly
        # as get_subscription_summary and checkout do. The bundle IS the discount.
        "bundle_amount": float(bundle_amount),
        "bundle_codes": sorted(bundle.function_codes) if bundle else [],
        "bundle_currency": (bundle.currency_code or None) if bundle else None,
        "trial_period_days": policy.current().trial_days,
    }


def _access_state_or_deny(entity_id) -> dict:
    """What the request gate would answer for this entity right now, per module."""
    # What the request gate would actually answer for this entity right now. The card
    # has to agree with it: a module the user can already open must never render the
    # "Start free trial" button, whatever the subscription rows say.
    try:
        access_state = entity_modules._enabled_state(entity_id)
    except Exception:
        # Same posture as the consent read above — the page must not 500 over one
        # lookup. Assume NO access: that only ever suppresses a trial button, where
        # assuming access would offer a trial on a module the user is already inside,
        # which is the contradiction this field exists to prevent.
        logger.exception(
            "modules: could not read module access for {}; assuming none", entity_id
        )
        access_state = {code: False for code in entity_modules.MODULE_CODES}
    return access_state

def _module_rows_for_cards(entity_id, sub_store):
    """``(rows, payer_id, paid_through)`` for the entity, or empties on a bad read."""
    rows = {}
    paid_through = None
    payer_id = None
    try:
        rows = {
            row.function_code.upper(): row
            for row in sub_store.module_rows_for_entity(entity_id)
        }
        # What THIS company is paid through, read once. It lives on the card the company
        # is billed on: a payer may hold several, each buying its own periods for its own
        # companies. Not the per-row copy, which drifted apart between a payer's entities
        # because each was only refreshed when its own entity was touched.
        payer_id = next(
            (row.payer_user_id for row in rows.values() if row.payer_user_id), None
        )
        if payer_id:
            paid_through = sub_store.paid_through_for_entity(entity_id)
    except Exception:
        logger.exception("modules: could not read module rows for entity {}", entity_id)
    return rows, payer_id, paid_through

def _told_of_failure(entity_id, now) -> bool:
    """``dunning.told_of_failure`` - imported at call time, like the rest of this module."""
    from billing.services import dunning

    return dunning.told_of_failure(entity_id, now)


def _billing_consent_or_assume(entity_id, sub_store) -> bool:
    """Whether THIS entity is authorised to bill its payer's card.

    Assumes consent when the read fails: the answer only drives a nudge, and nagging
    someone who has already consented is the worse of the two wrong answers.
    """
    # ...but a card is not sufficient. The payer's card is shared across every entity
    # they pay for, so THIS entity also needs its own billing consent before a trial
    # here may convert to a charge (see checkout._convert_due_trials). An entity with a
    # card but no consent gets the same nudge - otherwise its trial would quietly expire
    # and the user would never learn why.
    try:
        # Asked about the entity's PAYER — the person whose card the nudge is about.
        # After a handover the previous payer's consent is history and says nothing
        # about whether this one has agreed, so a card-but-no-consent entity would
        # otherwise stop showing the nudge and let its trial lapse unexplained.
        has_billing_consent = sub_store.has_billing_consent(
            entity_id, sub_store.payer_for_entity(entity_id)
        )
    except Exception:
        # Best-effort: never fail the page over the nudge. Assume consent so we do not
        # nag someone who has already given it. (Flask rolled its session back here -
        # on Postgres a failed statement aborted the whole request's transaction; under
        # Django's autocommit a failed read poisons nothing.)
        logger.exception(
            "modules: could not read billing consent for {}; assuming consent", entity_id
        )
        has_billing_consent = True
    return has_billing_consent


def _fill_conversion_charges(cards, entity_id, payer_id, billed_now) -> None:
    """Stamp each card's ``conversion_charge``, in MAJOR units, in place."""

    # Second pass, once every card exists: the conversion charges are SEQUENTIAL and a
    # per-card computation cannot see the trials it depends on.
    for code, charge in _forecast_conversion_charges(
        entity_id, payer_id, billed_now, cards
    ).items():
        for card in cards:
            if (card["code"] or "").upper() == code:
                card["conversion_charge"] = money.to_major(
                    charge, card.get("currency_code")
                )
