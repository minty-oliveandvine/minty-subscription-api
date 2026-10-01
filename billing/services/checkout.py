"""Per-module checkout and trials for entity subscriptions.

The local rows are the source of truth: the plan catalog comes from ``billing_plan``
via ``catalog.py``, and the "already subscribed?" guards read the module rows via
``store``/``access``. Stripe is the payment RAIL only — it captures the card and the
invoices are issued against the payer's customer; there is no Stripe subscription
behind any of this.

Billing is ONE cycle per payer, priced per entity by the module SET it bills:

* **One module** → that module's own price.
* **Both modules** → the single bundle price (the bundle IS the discount — there
  is no coupon), so adding the second module bills the MARGINAL difference.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from billing.services import access, catalog, clock, money, policy, store
from billing.services._log import logger
from billing.services.catalog import PlanView
from billing.services.constants import (
    AUDIT_CANCEL,
    AUDIT_TERMINATE,
    AUDIT_UNCANCEL,
    EXT_INVOICED,
    EXT_PENDING,
    OUTCOME_SUCCEEDED,
    PHASE_ACTIVE,
    PHASE_CANCELLED,
    PHASE_EXPIRED,
    PHASE_PAST_DUE,
    PHASE_SCHEDULED_CANCEL,
    PHASE_TRIAL,
)

# Stripe is the payment RAIL, not the biller. Nothing that creates or edits a Stripe
# SUBSCRIPTION is imported any more, and nothing here opens a Stripe-hosted page: a card
# is only ever added in-app, through a billing account (``payment_methods.confirm_setup``),
# and the setup-mode Checkout and Billing Portal paths were deleted on 2026-10-01.
from billing.services.stripe_client import (
    customer_default_payment_method,
    find_customer_by_user,
    payment_method_display,
)

# Post-cancellation access window for an active paid subscription, in days.
#
# DEFAULTS, both of them. The live values are ``billing_policy.paid_cancel_access_days``
# and ``billing_policy.trial_days`` — see ``services.policy``. They remain here as the
# fallback when the row cannot be read, and because a handful of tests assert against
# the shipped behaviour rather than a database.
#
# Both are safe to change at any time, for the same reason: neither is consulted after
# the fact. The trial's end is stamped into ``trial_end`` when it starts and no path
# moves it; the cancellation window is captured into ``app_access_until`` at
# cancellation. So an edit applies to new trials and new cancellations only, and can
# never shorten a window a customer is already inside.
PAID_CANCEL_ACCESS_DAYS = 30

# Length of the card-free onboarding trial, in days.
TRIAL_PERIOD_DAYS = 30

# What ``modules.set_entity_module`` returns on success. It answers with a Flask-style
# ``(payload, status)`` pair rather than raising, so the access write has to be checked
# against the code. Named because a bare 200 in a billing path reads as a magic number.
HTTP_OK = 200

#: The refusal when billing is authorised for a company on no billing account. The web
#: matches it to open the Billing Accounts picker.
NO_ACCOUNT_FOR_COMPANY = "Choose a billing account for this company."


class CheckoutError(Exception):
    """Raised with a user-safe message when checkout can't proceed."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ChargeDeferred(Exception):
    """A trial's conversion the PAYMENT PROCESSOR failed to make - an outage, a timeout, our
    own key - rather than one the card refused. Nothing is known about whether the customer
    can pay, so the trial is not expired: it keeps running and the next pass tries again
    (the user's rule, 2026-09-30), for as long as the past-due grace would have lasted."""


def _has_active_subscription(entity_id, function_code: str) -> bool:
    """True if THIS ENTITY already bills the module — the double-buy guard.

    Scoped to the entity, not the payer: one customer can bill the same module for
    several entities, so a payer-wide check would wrongly block a second entity from
    subscribing to something the first already has.

    Read from the module row rather than live Stripe. That fixes a real gap: Stripe
    reports a failing card as ``past_due`` rather than ``active``, so the old check let
    a past-due module be purchased AGAIN — a second charge for something the customer
    already has and has not paid for.

    Only a live paid module blocks a purchase. A trial does not (buying converts it
    early, which is legitimate), and neither does a cancelled one — that is refused just
    below by ``_in_cancellation_window``, which can say "use Renew" instead. See
    ``access.is_subscribed``.

    "Already has it" is asked of the PHASE **and** of whether access is still granted.
    The phase alone answers a question about the past: nothing transitions a lapsed paid
    module to ``expired`` — the sweep revokes access without touching the phase, and
    ``end_group_dunning(status="closed")`` deliberately leaves a given-up card ``past_due``
    because the debt is real. So a customer whose subscription lapsed for non-payment was
    told forever after that they "already have an active subscription", and could never
    buy it back. ``is_subscribed``'s own reasoning is the fix: *cancelled / expired —
    nothing is held any more, so re-buying is right*. A lapsed module holds nothing.

    Every intended block survives, because each one still grants access: a live paid
    module, a running trial, and a past-due module inside its grace — that last being the
    case the docstring calls the worst version of a double buy.
    """
    row = store.module_row(entity_id, (function_code or "").strip().upper())
    if row is None or not access.is_subscribed(phase=row.phase):
        return False
    return access.grants_access(
        clock.now(),
        phase=row.phase,
        trial_end=getattr(row, "trial_end", None),
        app_access_until=getattr(row, "app_access_until", None),
        # From the CARD this company is billed on — its own money bought its own days.
        # Not the per-row copy, which drifts, and no longer the account: a payer with two
        # cards has two answers, and one of them can be past due while the other is not.
        period_end=store.paid_through_for_entity(entity_id),
        past_due_grace_days=policy.current().past_due_window_days,
    )


def resolve_target_plan(entity_id, requested_code: str | None) -> PlanView:
    """Choose the plan to subscribe to.

    With an explicit module code, use that module's available plan. Without one,
    fall back to the single available module the entity isn't already subscribed
    to; raise if the choice is ambiguous or there's nothing left to buy.
    """
    if requested_code:
        plan = catalog.plan_for_module(requested_code)
        if plan is None:
            raise CheckoutError(
                f"No available subscription plan for module {requested_code!r}."
            )
        return plan

    candidates = [
        plan
        for plan in catalog.available_plans()
        if not _has_active_subscription(entity_id, plan.function_code)
    ]
    if not candidates:
        raise CheckoutError(
            "This entity is already subscribed to all available modules.", status=409
        )
    if len(candidates) > 1:
        raise CheckoutError("Please choose which module to subscribe to.")
    return candidates[0]


# NOTE: there is deliberately no "get or create the payer's customer" helper here, and
# nothing in this module creates a Stripe Customer. A customer must not exist until a
# card has actually been saved; the one place that makes one is the in-app card form's
# confirm (``payment_methods._payer_customer_for_confirm``), after Stripe has said the
# card exists.
#
# To READ the payer's customer use ``_resolve_customer_id`` (or
# ``_customer_id_for_entity``), which never creates.


def _resolve_customer_id(user_id) -> str | None:
    """The payer's Stripe customer: the local mapping first, then Stripe's own record.

    The mapping row is a CACHE, not the source of truth. ``_seed_user_customer_mapping``
    swallows its own write failures, so a payer can have a real, card-bearing customer in
    Stripe and no row here. Stripe knows which one: every customer we create carries a
    ``metadata.user_id`` stamp (``stripe_client.create_customer_for_user``) — precisely so
    this lookup can recover from a lost mapping.

    Skipping that second step is not a smaller answer, it is a WRONG one, and every
    caller draws a different bad conclusion from it: the card form's confirm mints a
    DUPLICATE customer, and the trial-end job expires a trial whose card was fine. The
    search is what makes the swallow above safe.

    Re-seeds the mapping on a hit. That is NOT just a cache warm-up — it is load-bearing.
    The anchor and ``paid_through`` live on the very same ``user_stripe_customer`` row, so
    without the row ``store.start_billing_cycle`` no-ops (it has nothing to write to) and
    ``_bill_module_change_in_house`` gives up with "could not anchor" — expiring the trial
    it just resolved a perfectly good customer for. Returning the id without writing the
    row would fix the lookup and leave the billing broken one step later.

    It also means a broken payer costs one search rather than one per request, and the
    log line below is how you find out the seed is failing at all.

    A Stripe failure PROPAGATES here rather than degrading to None — do not wrap this in
    a bare except. The search only runs when the mapping row is already missing, so the
    choice is between raising and answering "no customer" WRONGLY, and every caller
    handles the raise better than the wrong answer: the trial job catches per entity and
    retries tomorrow (instead of expiring a paid module today), the payment-method status
    endpoint already degrades to "no card", and checkout failing beats minting a
    duplicate. On the healthy path the mapping hits and Stripe is never called at all.

    NOT used on render paths (see ``modules._entity_customer_id``): a payer who genuinely
    has no customer would search on every page load and always find nothing.
    """
    mapped = store.customer_id_for_user(user_id)
    if mapped:
        return mapped
    if not user_id:
        return None
    found = find_customer_by_user(str(user_id))
    customer_id = (found or {}).get("id")
    if not customer_id:
        return None
    logger.warning(
        "subscription: payer {} had customer {} in Stripe but no mapping row; "
        "recovered it by search and re-seeded the mapping",
        user_id,
        customer_id,
    )
    _seed_user_customer_mapping(user_id, customer_id)
    return customer_id


def _seed_user_customer_mapping(user_id, customer_id) -> None:
    """Record the payer->customer link locally (best-effort).

    Lets ``_resolve_customer_id`` answer without a Stripe search. A failure here must
    never block customer resolution — the customer already exists in Stripe, which is
    the source of truth, and the ``metadata.user_id`` stamp keeps it findable — so it's
    logged and swallowed.
    """
    if not user_id or not customer_id:
        return
    try:
        store.upsert_customer_mapping(user_id, customer_id)
    except Exception:
        logger.exception(
            "subscription: failed to seed user->customer mapping for user {}", user_id
        )


def _plan_for_codes(codes):
    """The catalog plan that bills exactly ``codes`` for ONE entity.

    A single module bills at its own price; two or more bill as the single bundle price
    — the bundle IS the discount, so there's no coupon and no per-module share of it.
    Raises ``CheckoutError`` when the live catalog can't express the combination.
    """
    wanted = sorted({str(c).strip().upper() for c in (codes or []) if str(c).strip()})
    if not wanted:
        raise CheckoutError("No modules to bill.")
    if len(wanted) == 1:
        plan = catalog.plan_for_module(wanted[0])
        if plan is None:
            raise CheckoutError(
                f"No available subscription plan for module {wanted[0]!r}."
            )
        return plan
    bundle = catalog.bundle_plan()
    if bundle is None or not bundle.covers(wanted):
        raise CheckoutError(
            "No bundle plan is configured for that combination of modules.", status=409
        )
    return bundle


def _create_paid_subscriptions(
    entity, user, customer_id: str, plans: list[PlanView], payment_method: str,
    idempotency_scope: str,
) -> list[str]:
    """Bill ``plans``' modules for this entity on the payer's ONE subscription.

    The entity has a single line whose price is decided by the modules it bills for, so
    this reconciles that line (add / swap 280 <-> 400) rather than creating a
    subscription per module. Modules already billed are skipped. Returns the codes newly
    billed.

    This is the BUY path, so it bills now: the customer just asked for the module and is
    expecting a charge for today-to-the-anchor, not a surprise on their next invoice.
    """
    current = _billed_codes_in_house(entity.id)
    new_codes = {plan.function_code.upper() for plan in plans} - current
    if not new_codes:
        return []

    # Unlike a trial conversion, this is interactive: the customer is waiting, so a
    # failure has to surface a message rather than quietly leave them unsubscribed.
    paid_through = _bill_module_change_in_house(
        entity.id, getattr(user, "id", None), customer_id, current, new_codes
    )
    if paid_through is None:
        raise CheckoutError(
            "We couldn't set up the subscription. Please check your payment "
            "method and try again.",
            status=402,
        )
    # Write the rows and open the access gate. Easy to miss, because under Stripe this
    # was done afterwards by the subscription webhook: without it the customer was
    # charged and the module never switched on.
    _grant_purchased_modules(entity.id, getattr(user, "id", None), new_codes)
    return sorted(new_codes)


def _in_cancellation_window(entity_id, function_code) -> bool:
    """True while a cancelled module still has the access it was charged an extension for.

    Stripe can't answer this: cancelling swaps the entity's line down to the survivor's
    price (or parks it on the £0 grace price), so the cancelled module has no view at all
    — yet its access runs to ``app_access_until`` and its extension is queued for the
    anchor invoice. That state is the app's, and it lives on the mirror row.
    """
    try:
        row = store.module_row(entity_id, function_code)
    except Exception:
        logger.exception(
            "checkout: could not read the cancellation window for {} {}",
            entity_id,
            function_code,
        )
        return False
    return bool(
        row is not None
        and row.phase == PHASE_SCHEDULED_CANCEL
        and row.app_access_until is not None
        and row.app_access_until > clock.now()
    )


def _customer_id_for_entity(entity_id) -> str | None:
    """The Stripe customer billing this entity, or None before one exists.

    entity -> payer -> customer. The customer belongs to the PAYER, not the entity: one
    card covers every entity they own. The first hop is a local read; the second goes
    through ``_resolve_customer_id``, which falls back to Stripe when the mapping row is
    missing.

    Stripe is still the payment RAIL — invoices are issued against this customer — so
    this survives the removal of the subscription biller.
    """
    payer_id = store.payer_for_entity(entity_id)
    return _resolve_customer_id(payer_id) if payer_id else None


def _resolve_checkout_plans(entity_id, requested_codes) -> list[PlanView]:
    """Validate the requested module codes into plans to subscribe to.

    Raises ``CheckoutError`` for an unknown plan, a module that already has an active
    subscription, or one still inside a paid cancellation window (which must be renewed,
    not re-bought). With no codes, falls back to the single unsubscribed module."""
    codes = [c.strip().upper() for c in (requested_codes or []) if c and c.strip()]
    if not codes:
        return [resolve_target_plan(entity_id, None)]
    plans: list[PlanView] = []
    for code in dict.fromkeys(codes):  # dedupe, preserve order
        plan = catalog.plan_for_module(code)
        if plan is None:
            raise CheckoutError(f"No available subscription plan for module {code!r}.")
        if _has_active_subscription(entity_id, plan.function_code):
            # A trial is refused for the same reason as a paid module — the entity
            # already has it — but saying "active subscription" to someone mid-trial is
            # simply untrue, and hides the fact that they need do nothing.
            row = store.module_row(entity_id, plan.function_code.upper())
            if row is not None and row.phase == PHASE_TRIAL:
                raise CheckoutError(
                    f"Module {code} is already on a free trial — it becomes a paid "
                    "subscription automatically when the trial ends.",
                    status=409,
                )
            raise CheckoutError(
                f"Module {code} already has an active subscription.", status=409
            )
        # A module cancelled out of a bundle has NO billing line, so the guard above is
        # blind to it — but its extension is queued and its access still runs. Buying it
        # again would charge for the module a second time on top of that extension.
        # Renewing is the only correct move, and it also reverses the extension.
        if _in_cancellation_window(entity_id, plan.function_code):
            raise CheckoutError(
                f"Module {code} is still active until its cancellation date — "
                "use Renew instead.",
                status=409,
            )
        plans.append(plan)
    return plans


def start_modules_checkout(
    entity,
    user,
    requested_codes: list[str] | None = None,
):
    """Subscribe an entity to one or more modules — ONE paid subscription per module.

    Decided on the card NOMINATED FOR THIS COMPANY (``store.card_for_entity``), never the
    account default — the default is only what pickers offer first. Three outcomes:

    * **A nominated card AND this entity has billing consent** → create the paid
      subscriptions directly on that card. Returns ``{"created": [codes]}``.
    * **A nominated card but NO consent for this entity** → returns
      ``{"needs_confirmation": {...}}`` and charges nothing. See below.
    * **No card nominated** → ``CheckoutError("Choose a card before subscribing.", 402)``.
      There is no hosted fallback any more: a card is only ever added in-app, through a
      billing account (the user's rule, 2026-10-01), and the caller nominates one before
      asking again.

    The consent gate exists because the card is the PAYER's, shared by every entity
    they pay for. Without it, saving a card while setting up entity #1 silently
    authorises charges for every entity created afterwards — click Subscribe on a brand
    new entity and it just bills, with no prompt. Having a card is necessary to be
    charged; agreeing to be billed for THIS entity is also required. (The same gate
    guards the bigger surface, ``convert_or_expire_due_trials``, where a trial would
    otherwise convert to paid with no user action at all.)

    Either way the entity ends up with ONE line on the payer's subscription, priced
    by the modules it bills (standalone, or the bundle for both).

    Raises ``CheckoutError`` when a module is unavailable, already actively
    subscribed, or the choice is ambiguous.
    """
    # Asked about the payer whose card is about to be charged — which is the acting user.
    # Every route reaching here carries the payer gate, so they are either the
    # established payer or the one establishing the relationship.
    user_id = getattr(user, "id", None)
    plans = _resolve_checkout_plans(entity.id, requested_codes)

    payment_method = store.card_for_entity(entity.id, user_id)
    if not payment_method:
        raise CheckoutError("Choose a card before subscribing.", status=402)

    # A nominated card is a card on the payer's customer, so a missing customer here is
    # an inconsistency in our own records, not the customer's mistake. Refused with the
    # same answer (re-choosing the card re-attaches it), and logged so it is seen.
    customer_id = _customer_id_for_entity(entity.id) or _resolve_customer_id(user_id)
    if not customer_id:
        logger.error(
            "checkout: entity {} is nominated onto {} but payer {} has no Stripe customer",
            entity.id, payment_method, user_id,
        )
        raise CheckoutError("Choose a card before subscribing.", status=402)

    if not store.has_billing_consent(entity.id, user_id):
        # Charge NOTHING. The caller shows "you'll be billed X on card ending Y" and
        # calls confirm_modules_checkout if the payer accepts.
        return {"needs_confirmation": _billing_confirmation(entity, plans, payment_method)}
    # A fresh idempotency scope per attempt guards Stripe SDK network retries without
    # blocking a later re-subscribe of the same module.
    created = _create_paid_subscriptions(
        entity, user, customer_id, plans, payment_method, uuid.uuid4().hex
    )
    return {"created": created}


def _billing_confirmation(entity, plans: list[PlanView], payment_method: str) -> dict:
    """What the payer needs to see before a first charge on this entity is authorised.

    The amount is the price of the whole combination, not a per-module sum — two modules
    bill as the single bundle price (the bundle IS the discount), so summing the plans
    would overstate it.
    """
    plan = _plan_for_codes([p.function_code for p in plans])
    return {
        "entity_id": str(entity.id),
        "entity_name": entity.name,
        "codes": [p.function_code.upper() for p in plans],
        "amount": plan.amount,
        # Formatted HERE, not in the dialog. The browser has no way to know how many
        # minor units a currency has, so the script divided by a hardcoded 100 — which
        # would quote a customer 2.80 while authorising a charge of 280 in a zero-decimal
        # currency. The one number a payer is asked to approve is not a place to guess.
        "formatted_amount": money.format_minor(plan.amount, plan.currency_code),
        "currency": plan.currency_code,
        "interval": plan.billing_interval,
        "card": payment_method_display(payment_method),
    }


def authorize_entity_billing(entity, user) -> dict:
    """Record consent to bill this entity WITHOUT subscribing or charging anything.

    For a module still inside its app-level trial. The trial has paid time left, so the
    right outcome is "let it run and convert at term end", not "charge now" — which is
    what going through ``confirm_modules_checkout`` would do, since an app-level trial
    has no Stripe object and therefore doesn't look like an active subscription to
    ``_resolve_checkout_plans``.

    Idempotent (consent is once per entity), so a double-click is harmless.

    THE COMPANY MUST ALREADY BE ON A BILLING ACCOUNT. The caller puts it there first - the
    account picker's move, or ``payment_method`` on the route - and with none this refuses
    402 BEFORE consent is written, so the screen can open the Billing Accounts picker. There
    is no fallback to the Stripe customer's default card any more (the user's rule,
    2026-10-01: a card is chosen through a billing account, never inferred). Consent with no
    account would be an agreement to be billed to nothing: the trial would expire at term
    end having been told it would convert.
    """
    user_id = getattr(user, "id", None)
    if store.billing_group_for_entity(entity.id, user_id) is None:
        raise CheckoutError(NO_ACCOUNT_FOR_COMPANY, status=402)
    store.record_billing_consent(entity.id, user_id, "confirmed")
    return {"ok": True}


def confirm_modules_checkout(
    entity,
    user,
    requested_codes: list[str] | None = None,
):
    """Record the payer's consent to be billed for THIS entity, then subscribe.

    The second half of the ``needs_confirmation`` handshake: the payer has been shown
    the amount and the card, and accepted. Consent is per entity and permanent, so
    subsequent module purchases on this entity go straight through.

    Delegates to ``start_modules_checkout``, which re-validates the modules — the codes
    come back from the client, so they can't be trusted to still be buyable — and, now
    that consent exists, takes the charge path. It still refuses with 402 "Choose a card
    before subscribing." if no card is nominated for the company by then.
    """
    user_id = getattr(user, "id", None)
    # The company's billing account was chosen before this call (the picker's move, or
    # ``payment_method`` on the route); ``start_modules_checkout`` refuses 402 without one.
    store.record_billing_consent(entity.id, user_id, "confirmed")
    return start_modules_checkout(entity, user, requested_codes)


def _named_account(user_id):
    """The payer's oldest NAMED billing account, or None.

    NEVER RAISES, and that is the point. This is only ever asked in order to put a
    display name on a Stripe customer, and the calls that need one are saving a card or
    opening a subscription. A billing account that cannot be read — no app context, the
    table not yet migrated, the database briefly unavailable — must not turn "save this
    card" into an error. The user record is the fallback, and it is the same answer every
    account gave before identities existed.

    Oldest first, and unnamed accounts skipped: see ``_payer_identity``.
    """
    try:
        from billing.services import store as sub_store

        accounts = sub_store.billing_groups_for_payer(user_id)
    except Exception:  # noqa: BLE001 - see the docstring; a name is never worth a failure
        logger.exception(
            "subscription: could not read billing accounts for payer {}", user_id
        )
        return None

    return next(
        (
            a for a in accounts
            if (a.billing_company or "").strip() or (a.billing_email or "").strip()
        ),
        None,
    )


def _payer_identity(user_id) -> dict[str, str]:
    """The payer's display fields for their Stripe customer: ``name``/``email``/``description``.

    The customer is the PAYER, not the entity — one customer can bill several entities,
    so an entity name here would be wrong the moment a second entity is added. (Which
    entity a charge is for lives on the subscription ITEM metadata and on the invoice
    line labels, not on the customer.)

    ``name`` is the human name so invoices read properly; ``description`` carries the
    username because ``first_name``/``last_name`` are not unique and two payers sharing a
    name are otherwise indistinguishable in the Stripe dashboard list.

    THE BILLING ACCOUNT OUTRANKS THE USER RECORD, AND THAT IS A REVERSAL.

    This function used to say the local ``user`` row overwrites whatever the payer typed,
    full stop — "our record is the source of truth for who they are". It still is for who
    they ARE. It is not for what their INVOICES SHOULD SAY, and those are different
    questions: a finance lead paying for a company does not want their own name on the
    document.

    So ``v1a01_billing_account`` gave ``payer_billing_group`` a ``billing_email`` and a
    ``billing_company``, and where an account carries them they win here. The user record
    is the fallback, which is what every account that predates them uses.

    WHICH ACCOUNT, when a payer has several? The oldest — ``billing_groups_for_payer``
    orders that way — because there is exactly ONE Stripe customer per payer and it can
    hold exactly one name. This is a display field on a customer that bills for several
    companies; the per-company truth is on the invoice lines, and always was. Do not
    "fix" this by making it the most recent account: the customer's name would then
    change under the payer every time they opened another one.

    Two accounts opened in the same tick share a ``created_at``, and the tie falls to the
    id. That is arbitrary but STABLE — the same account wins every time it is asked — and
    stability is the property that matters here. A name that churns is the failure; a
    name picked from two simultaneous candidates is not.

    ``email`` remains nullable at every level: when neither the account nor the user has
    one we leave Stripe's own collected address rather than blanking it.

    Returns {} if the user can't be loaded; the ``user_id`` stamp still goes on, since
    resolvability matters more than a display name.
    """
    from shared_models.models import User

    user = store._by_pk(User, user_id) if user_id else None
    if user is None:
        logger.warning(
            "subscription: no user {} to name their Stripe customer after", user_id
        )
        return {}

    named = _named_account(user_id)

    fields: dict[str, str] = {}

    company = (named.billing_company or "").strip() if named else ""
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if company:
        fields["name"] = company
    elif name:
        fields["name"] = name

    # ``description`` stays the USERNAME whatever the account says. It is what tells two
    # payers sharing a name apart in the Stripe dashboard, and a billing company in that
    # field would make the customer unidentifiable the moment two payers bill for the
    # same company.
    if user.username:
        fields["description"] = f"@{user.username}"

    email = (named.billing_email or "").strip() if named else ""
    if email:
        fields["email"] = email
    elif user.email:
        fields["email"] = user.email

    return fields


def entity_has_payment_method(entity) -> bool:
    """True if the entity has a card on file, read live from Stripe.

    Onboarding Step 2 gates "Save & Next" on this. Trials do NOT need it — they're
    app-level and card-free (see ``trial_payment_method``); the card is only consulted
    when a trial ends, to decide convert-to-paid vs expire.
    """
    customer_id = _customer_id_for_entity(entity.id)
    if not customer_id:
        return False
    return customer_default_payment_method(customer_id) is not None


def trial_payment_method(customer_id: str | None) -> str | None:
    """The card a converting trial should bill, or None when the payer hasn't added one.

    The card is OPTIONAL during an app-level trial — it's only consulted when the trial
    ENDS, where its presence decides convert-to-paid vs expire (see
    ``convert_or_expire_due_trials``). So this reports its absence rather than raising.
    """
    return customer_default_payment_method(customer_id)


def _set_module_access(entity_id, function_code: str, enabled: bool) -> None:
    """Flip a module's access gate. Best-effort: logged, never raised — the trial row
    is the record of truth and a later resync/sweep self-heals the flag."""
    from billing.services.entity_modules import set_entity_module

    try:
        _data, status = set_entity_module(
            entity_id, function_code, enabled, actor="subscription"
        )
        if status != HTTP_OK:
            logger.warning(
                "trial: access write for {} {} returned {}: {}",
                entity_id,
                function_code,
                status,
                _data,
            )
    except Exception:
        logger.exception(
            "trial: failed to set access for {} {}", entity_id, function_code
        )


def start_module_trial(entity, user, plan: PlanView):
    """Start the APP-LEVEL trial for one module. No Stripe object is created.

    The trial clock lives entirely in the app (``entity_module_subscription``): access
    runs until ``trial_end = now + TRIAL_PERIOD_DAYS``. Stripe only gets involved when
    the trial ENDS (see ``convert_or_expire_due_trials``), so a trial needs neither a
    customer nor a card — whether one exists by then decides convert vs expire.

    The acting ``user`` becomes the module's payer. Returns the trial row, or None if
    the module already has a trial or a subscription (the trial is once per module).
    """
    code = plan.function_code.upper()
    if store.module_row(entity.id, code) is not None:
        return None

    now = clock.now()
    trial_end = now + timedelta(days=policy.current().trial_days)
    row = store.upsert_module_row(
        entity.id,
        code,
        getattr(user, "id", None),
        phase=PHASE_TRIAL,
        trial_end=trial_end,
        app_access_until=trial_end,
    )
    _set_module_access(entity.id, code, True)
    return row


def start_module_trials(entity, user, requested_codes) -> list[str]:
    """Start app-level trials for never-used modules; return their codes.

    Every requested module must be trial-eligible — the trial is once per module, so a
    module with any existing trial or subscription row is rejected and the caller routes
    it to paid checkout instead. Raises ``CheckoutError`` for an unknown plan too.

    No Stripe customer and no card are needed: the trial is tracked entirely in the app
    and only reaches Stripe when it ends.
    """
    codes = [c.strip().upper() for c in (requested_codes or []) if c and c.strip()]
    if not codes:
        raise CheckoutError("No modules specified for a trial.")

    plans: list[PlanView] = []
    for code in dict.fromkeys(codes):  # dedupe, preserve order
        plan = catalog.plan_for_module(code)
        if plan is None:
            raise CheckoutError(
                f"No available subscription plan for module {code!r}."
            )
        if store.module_row(entity.id, plan.function_code) is not None:
            raise CheckoutError(
                f"Module {code} has already used its free trial.", status=409
            )
        plans.append(plan)

    started: list[str] = []
    for plan in plans:
        if start_module_trial(entity, user, plan) is not None:
            started.append(plan.function_code.upper())
    return started


def convert_or_expire_due_trials(limit: int | None = None) -> dict:
    """Close out app-level trials whose term has ended (run on a schedule).

    For each due trial: if the payer has a card on file the module CONVERTS to a paid
    subscription; otherwise the trial EXPIRES and access is revoked. This is the only
    thing that ends an app-level trial — the access sweep deliberately leaves modules
    with no Stripe subscription alone.

    A conversion the PROCESSOR failed to make (``ChargeDeferred``) is neither: the trial
    keeps running - and its access with it (``access_sweep``) - and the next pass tries
    again. Not for ever: past the same grace a failed renewal gets, it expires as a decline
    would, loudly.

    Returns ``{"converted": [...], "expired": [...], "deferred": [...]}``.
    """
    now = clock.now()
    converted: list[dict] = []
    expired: list[dict] = []
    deferred: list[dict] = []
    # Grouped by ENTITY, because an entity bills on ONE line: two modules whose trials
    # end together are a single swap to the bundle price, not two. Converting them
    # row-by-row cut two invoices seconds apart — the first billing a standalone price
    # the customer never chose, the second immediately crediting it back.
    by_entity: dict[str, list] = {}
    for row in store.due_trials(now, limit=limit):
        by_entity.setdefault(row.entity_id, []).append(row)

    for entity_id, rows in by_entity.items():
        try:
            billed, unbilled = _convert_due_trials(entity_id, rows)
        except ChargeDeferred:
            ended = _ended(rows) or now
            if now < ended + timedelta(days=policy.current().past_due_window_days):
                logger.error(
                    "trial: the payment processor failed converting entity {}; the trial "
                    "keeps running and the next pass tries again", entity_id,
                )
                deferred.extend(
                    {"entity_id": entity_id, "code": row.function_code} for row in rows
                )
                continue
            logger.error(
                "trial: the payment processor has failed converting entity {} since {} - "
                "past the grace window; expiring as a decline would", entity_id, ended,
            )
            billed, unbilled = [], rows
        except Exception:
            logger.exception(
                "trial: failed to close out trials for entity {}", entity_id
            )
            continue
        converted.extend(
            {"entity_id": entity_id, "code": row.function_code} for row in billed
        )
        for row in unbilled:
            try:
                _expire_due_trial(row)
            except Exception:
                logger.exception(
                    "trial: failed to expire trial for {} {}",
                    entity_id,
                    row.function_code,
                )
                continue
            expired.append({"entity_id": entity_id, "code": row.function_code})

    # Nothing is mailed about how a trial ENDED, by decision (2026-09-30): neither the
    # "trial expired" notice nor a conversion note is in the approved designs, and the
    # receipt that used to announce a conversion's first charge is retired too. A lapse
    # shows on the module page. See the retired-events block in ``notify``.
    return {"converted": converted, "expired": expired, "deferred": deferred}


def _ended(rows):
    """When the earliest of these trials ended - where their conversion's attempts start."""
    return min((row.trial_end for row in rows if getattr(row, "trial_end", None)),
               default=None)


def _entity_names_for_notice(entity_ids) -> dict[str, str]:
    """{entity_id: name} in one query. Empty on any failure — an email that says
    "your company" is a smaller loss than a trial-close job that dies looking up a name.
    """
    if not entity_ids:
        return {}
    try:
        from shared_models.models import Entity

        rows = Entity.objects.filter(id__in=[str(i) for i in entity_ids])
        return {str(e.id): (e.name or "").strip() for e in rows if (e.name or "").strip()}
    except Exception:
        logger.exception("trial: could not resolve entity names for notification")
        return {}


def notify_trials_ending(days_before: int = 7, limit: int | None = None) -> dict:
    """Warn payers about trials that end in ``days_before`` days.

    THE ONLY NOTIFICATION IN THE SYSTEM THAT CAN PREVENT A LAPSE. Every other billing email
    reports something that has already happened — a payment that failed or recovered, a
    handover — and a trial that has ENDED is not mailed at all. This one arrives while the
    customer can still act, and the case it exists for is the trial that will NOT convert
    because no card is saved or billing for the entity was never confirmed: without it,
    that customer's first news is their module going dark.

    Reads the SAME three conditions ``_convert_due_trials`` will apply on the day
    (customer, card, consent) rather than a simplified version, so the warning cannot
    promise a conversion the conversion job then refuses. Where they must disagree it is
    in the safe direction: this runs days earlier, so a card saved in between turns a
    warned trial into a quiet one, never the reverse.

    Warns about the next ``days_before`` days, not the single day exactly that far out.
    Run daily that re-matches each trial on each of those days and the email log dedupes
    all but the first — which is the price of surviving a day the job does not run. See
    the window below for why that trade is worth making.

    Returns ``{"warned": [...], "skipped": [...]}``. Idempotent by the email log — the
    dedupe key is the entity, module set and trial-end date, so re-running warns nobody
    twice even if the daily window is widened or the job is run by hand.
    """
    now = clock.now()
    # A one-day window, not "everything within N days": run daily this tiles the calendar
    # exactly once per trial. A cumulative "<= N days" filter would re-match the same
    # trial on each of the N days before it ends and rely entirely on the email log to
    # stay quiet — correct, but it makes the dedupe row load-bearing for basic sanity
    # rather than a backstop.
    #
    # Anchored to CALENDAR MIDNIGHT, not to ``now``. A window of
    # ``[now + 3d, now + 4d)`` only tiles if the job runs at precisely 24-hour intervals,
    # and cron does not: a run at 08:10 followed by one at 08:15 leaves a five-minute
    # hole, and any trial ending inside it is never warned about at all. Found against
    # real data — a trial ending 13:00 HKT (05:00 UTC) fell before a window that opened
    # at 08:10 UTC and was silently skipped. Day-aligned, the windows tile whatever time
    # the job runs, and a second run the same day re-derives the SAME window (which the
    # email log then dedupes) instead of a shifted one.
    # THE NEXT ``days_before`` DAYS, not the single day exactly that far out. The tiling
    # above is only perfect while the job runs every day, and a day it does not run is a
    # hole nothing ever revisits — no watermark, no backlog. The trials whose tile fell in
    # that hole are never warned at all, and this is the one notice that arrives while the
    # customer can still prevent the lapse, so losing it costs them the module.
    #
    # Re-matching is what the day-aligned window was written to avoid, and the objection
    # was fair: it makes the email log load-bearing rather than a backstop. But that log
    # is durable, indexed, and already load-bearing for the renewal and dunning notices,
    # and its key here is the entity, module set and trial-end DATE — so a trial scanned
    # on three consecutive days is still mailed exactly once.
    #
    # It bounds itself. An outage of up to ``days_before`` is covered completely (with
    # less notice than intended, which is the point); a longer one loses only trials that
    # ENDED while it was down, and those are not warnable — ``convert_or_expire_due_trials``
    # has already closed them out, and since the ended-trial email was retired
    # (2026-09-30) the module page is the only place they hear about it.
    #
    # Starts TOMORROW, not today. ``notify-trial-ending`` runs before ``close-trials`` on
    # the same schedule, so including today would mail "your trial ends today" minutes
    # before the trial is closed out — a warning nobody has the time to act on.
    today = now.astimezone(UTC).date()
    start = datetime(
        today.year, today.month, today.day, tzinfo=UTC
    ) + timedelta(days=1)
    end = start + timedelta(days=days_before)

    warned: list[dict] = []
    skipped: list[dict] = []
    by_entity: dict[str, list] = {}
    for row in store.trials_ending_between(start, end, limit=limit):
        by_entity.setdefault(row.entity_id, []).append(row)

    events = []
    names = _entity_names_for_notice(set(by_entity))
    for entity_id, rows in by_entity.items():
        try:
            payer = rows[0].payer_user_id
            codes = sorted(row.function_code.upper() for row in rows)
            amount, currency = _trial_line_price(entity_id, codes)
            context = {
                "entity_id": entity_id,
                "entity_name": names.get(str(entity_id)),
                "codes": codes,
                "trial_end": min(row.trial_end for row in rows),
                "amount": amount,
                "currency": currency,
                # TWO reasons a trial will not convert. The email does not word them
                # differently — one "action needed" body covers both (the in-app trial
                # notices that told them apart were removed 2026-10-01). Both flags stay
                # because the SEND gates on them: either one means the trial lapses,
                # and neither means there is nothing to say.
                "needs_card": not _trial_has_card(payer),
                "needs_consent": not store.has_billing_consent(entity_id, payer),
            }
            if not (context["needs_card"] or context["needs_consent"]):
                # Card saved and this company authorised, so the trial converts on its
                # own. Mailing "your trial ends soon, do nothing" trains people to skim
                # past the one trial email that does need acting on, so it is not sent
                # at all — and with the receipt retired, neither is the charge itself.
                skipped.append({"entity_id": entity_id, "reason": "will_convert"})
                continue
            events.append((
                payer,
                _notify_module().TRIAL_ENDING,
                # THE PAYER IS PART OF THE KEY, and has to be. The uniqueness constraint
                # is (event, dedupe_key) with ``user_id`` deliberately outside it, so the
                # key is the only thing that can distinguish two recipients.
                #
                # Without the payer, a subscription handed over mid-trial silences the
                # warning for the person who will actually be charged: the entity, the
                # codes and ``trial_end`` are all unchanged by a handover, so the key
                # regenerates identically, ``notify._claim`` finds the row already sent to
                # the OUTGOING payer, and the incoming one hears nothing until the money
                # leaves. This warning is the only notice that can PREVENT the lapse,
                # so losing it to a stale key costs the customer the module.
                f"{payer}:{entity_id}:{','.join(codes)}:{context['trial_end']:%Y-%m-%d}",
                context,
            ))
            warned.append({"entity_id": entity_id, "codes": codes,
                           "needs_card": context["needs_card"]})
        except Exception:
            logger.exception(
                "trial: could not prepare ending-soon notice for entity {}", entity_id
            )
            skipped.append({"entity_id": entity_id, "reason": "error"})

    _notify_module().notify_many(events)
    return {"warned": warned, "skipped": skipped}


def _notify_module():
    """Imported lazily: ``notify`` reaches into the model layer, and importing it at
    module scope would drag the entity model graph in behind the Stripe client (same
    reason as ``money.decimal_places``)."""
    from billing.services import notify

    return notify


def _trial_has_card(payer_user_id, entity_id=None) -> bool:
    """Whether THIS company has a card the trial conversion could charge.

    One half of what a trial conversion needs (the other is consent), asked on its own so
    the ending-soon email can name WHICH half is missing. Any failure answers False:
    nagging a customer who was fine is a far smaller harm than letting a trial they
    wanted lapse in silence.

    Asked of the COMPANY once one is named. A payer with three cards saved and none of
    them put on this company still cannot be billed for it — "they have a card somewhere"
    was the right question only while every payer had exactly one.
    """
    try:
        if entity_id is not None:
            return bool(store.card_for_entity(entity_id, payer_user_id))
        customer_id = _resolve_customer_id(payer_user_id)
        return bool(customer_id and trial_payment_method(customer_id))
    except Exception:
        logger.exception(
            "trial: could not determine whether payer {} has a card", payer_user_id
        )
        return False


def _trial_line_price(entity_id, converting_codes) -> tuple[int | None, str | None]:
    """What this entity will bill per month once these trials convert.

    Priced on the FULL resulting module set, because an entity bills one line priced by
    the set it carries — quoting the converting module's standalone price would overstate
    the bill for an entity that ends up on the bundle. Returns ``(None, None)`` when the
    catalog can't price the combination, and the email simply omits the figure rather
    than guessing at it.
    """
    try:
        codes = {str(code).upper() for code in converting_codes}
        for row in store.module_rows_for_entity(entity_id):
            if access.is_billing_forward(phase=row.phase):
                codes.add(row.function_code.upper())
        plan = _plan_for_codes(codes)
        return plan.amount, plan.currency_code
    except Exception:
        logger.info(
            "trial: no catalog price for entity {}; omitting amount from notice",
            entity_id,
        )
        return None, None


def _convert_due_trials(entity_id, rows) -> tuple[list, list]:
    """Bill ONE entity's due trials as a single line change.

    Returns ``(converted, to_expire)``. Everything the entity can't be billed for comes
    back in ``to_expire`` for the caller to close out — a cancelled trial, no customer,
    no card, no consent for this entity, no live plan, or a card that wouldn't take the
    charge.

    All the module codes ending together go into ONE billing call, so the entity is
    priced straight at its final module set and the customer gets one invoice for one
    event — billing them row-by-row cut two invoices seconds apart, the first at a
    standalone price the customer never chose.

    The charge lands IMMEDIATELY, on the conversion day: converting is the moment the
    customer starts paying, so that is when the bill should arrive. The phase is moved
    off ``trial`` here so a re-run can't convert the same module twice.
    """
    # A cancelled trial ran its free days out but must never become a charge — that IS
    # what cancelling a trial means. Checked before anything else, so a payer with a
    # card and consent still doesn't get billed for one they cancelled.
    doomed = [row for row in rows if row.phase == PHASE_SCHEDULED_CANCEL]
    candidates = [row for row in rows if row.phase != PHASE_SCHEDULED_CANCEL]
    if not candidates:
        return [], doomed

    payer_user_id = candidates[0].payer_user_id
    # Resolved, not read locally. "No customer" ends this trial and revokes access, and
    # this job runs unattended — a payer whose mapping row went missing would lose a
    # module their card would have paid for, with nobody in the loop to notice.
    customer_id = _resolve_customer_id(payer_user_id)
    if not customer_id:
        return [], rows
    # The card THIS company was put on. Not the account default: there is no fallback, so
    # an unnominated company has nothing to charge and its trial ends rather than
    # converting onto a card nobody chose for it. ``_bill_module_change_in_house`` refuses
    # it a second time; this is the early exit that keeps the row out of the biller.
    payment_method = store.card_for_entity(entity_id, payer_user_id)
    if not payment_method:
        return [], rows
    # The payer's card is shared across every entity they pay for, so a card alone is
    # NOT authorisation to bill this one. Without this check a brand-new entity's trial
    # converts to a real charge with no user action whatsoever, purely because a card
    # was saved for a different entity. Expire instead; the settings page nudges for
    # consent while the trial is still running (see modules.get_module_cards).
    #
    # Asked about THIS payer, read off the row above. On an entity whose subscription
    # was handed over, the previous payer's consent says nothing about this card — and
    # this is the job that would otherwise charge it unattended.
    if not store.has_billing_consent(entity_id, payer_user_id):
        logger.info(
            "trial: entity {} has a card but no billing consent; expiring {} rather "
            "than charging",
            entity_id,
            ",".join(sorted(row.function_code for row in candidates)),
        )
        return [], rows

    billable = [
        row
        for row in candidates
        if catalog.plan_for_module(row.function_code) is not None
    ]
    for row in candidates:
        if row not in billable:
            logger.warning(
                "trial: no live plan for {}; expiring instead of converting",
                row.function_code,
            )
            doomed.append(row)
    if not billable:
        return [], doomed

    # Priced as a CHANGE from what the entity already bills to what it will bill: if
    # it is already paying for the other module, converting costs the marginal step up
    # to the bundle (400 - 280 = 120), not the converting module's standalone price.
    #
    # The charge is raised and collected here, on the conversion day, rather than left
    # to ride the next renewal — a charge that surfaces weeks later bundled with the
    # next period is impossible for the customer to recognise.
    #
    # A failure to collect returns None rather than raising, and the caller expires the
    # trial: a decline must not leave the rows stuck in ``trial`` with a past
    # ``trial_end``, or every future run of this job retries the same dead card forever.
    codes = {row.function_code.upper() for row in billable}

    paid_through = _bill_module_change_in_house(
        entity_id, payer_user_id, customer_id,
        _billed_codes_in_house(entity_id), codes,
        # A conversion is its own kind of change: its earlier attempts are found again by
        # it, and a processor failure defers it rather than expiring the trial.
        kind="convert", defer_transient=True,
        prior_since=_ended(billable),
    )
    if paid_through is None:
        # Could not collect. Treat it exactly like "no card": the caller expires the
        # trial rather than handing over modules nobody paid for.
        return [], rows
    _finish_conversion(entity_id, billable)
    return billable, doomed


def _billed_codes_in_house(entity_id) -> set[str]:
    """The modules this entity is ALREADY billed for, from Minty's own rows.

    The in-house counterpart of ``_entity_billing_line``, and it exists because that
    function cannot answer this question once the cutover is on: it reads the payer's
    Stripe SUBSCRIPTION items, and in-house there is no subscription, so it returns an
    empty set for every entity no matter what they are paying.

    That is not a cosmetic difference. An empty "before" makes ``changes.build_change``
    treat every upgrade as a fresh JOIN and charge the new module's STANDALONE price on
    top of what the entity already paid this period — the bundle discount is lost for the
    remainder of the period. Adding Payment Request to an entity already on Petty Cash was
    billed 65.33 (280 prorated) instead of 28.00 (the 120 bundle margin prorated).

    A module winding down IS counted, as long as it was billed and the period it was
    billed for has not ended. That pricing decision — left open here originally — is
    settled: the customer paid for that module through the period end, so for the days
    before that end the line genuinely holds it, and adding a second module to it is an
    upgrade to the bundle rather than a fresh join. Petty Cash converting on 20 Aug beside
    a Payment Request paid to 28 Aug is 30.97 (credit 72.26 unused, charge 103.23 of
    bundle for the 8 days), not 72.26 (its standalone price for the same 8 days). The
    re-price to what survives happens at 28 Aug, on the renewal, which is where the
    cancelled module actually leaves — and ``is_covered_this_period`` now SEES that
    renewal happen, which it did not before. The account's ``paid_through`` moves for the
    whole payer while the cancelling module is left off the invoice, so the extension
    state and the access end are passed with it; without them this counted a module for a
    period nobody billed it for. See the predicate's docstring.

    ``first_billed_at`` is what keeps a cancelled TRIAL out of this: it is winding down
    too, and nothing was ever paid for it, so there is no covered period to price against.

    This deliberately does NOT match the set ``_paid_cancel_terms`` uses. The two answer
    different questions: this one prices a change INSIDE a period already paid for, that
    one prices access BEYOND it, where a module winding down is no longer on the line.
    """
    now = clock.now()
    rows = list(store.module_rows_for_entity(entity_id))
    # Best-effort: a failure here must not cost anyone their purchase. Falling back to
    # None drops the winding-down module from the set, which is the pre-existing
    # behaviour — it can overcharge a wind-down overlap, never undercharge.
    try:
        paid_through = store.paid_through_for_entity(entity_id)
    except Exception:
        logger.exception(
            "billing: could not read paid_through for entity {}; pricing its change "
            "without the modules that are winding down",
            entity_id,
        )
        paid_through = None

    return {
        row.function_code.upper()
        for row in rows
        if access.is_covered_this_period(
            phase=row.phase,
            first_billed_at=getattr(row, "first_billed_at", None),
            paid_through=paid_through,
            now=now,
            extension_state=getattr(row, "extension_state", None),
            app_access_until=getattr(row, "app_access_until", None),
        )
    }


def _void_unpaid_invoice(invoice_id, entity_id, what: str) -> str | None:
    """Withdraw the invoice of a purchase the customer is not getting.

    Every purchase that is refused when its charge fails lands here — a trial conversion,
    a handover, a reinstatement (``what`` names which, for the log). Refusing to grant and
    leaving the invoice open bills them for something explicitly not given: for a
    conversion the row's ``first_billed_at`` stays NULL, which is this code recording that
    no real charge happened, beside an open document saying one did.

    It does not merely sit there. ``dunning.collect_due`` chases the payer's OLDEST open
    invoice, so on any later episode this is the first thing retried — for a module whose
    row has since gone ``expired``, or one still cancelled because its restore was
    declined — and the portal offers it to the customer as *Retry payment*. Either way
    they would pay for something never given.

    Reached from BOTH failure paths, which look different and are the same event: a
    declined card RAISES out of ``Invoice.pay``, while a non-paid status is returned.
    Only the first happens in practice, and fixing only the second is how this bug
    survived its first fix.

    Never raises. The refusal has already happened; a void that cannot be completed must
    not turn it into a crash the caller reads as something worse.

    Returns ``"paid"`` when the invoice turns out to have been PAID - the charge went through
    and its answer never arrived - and the caller must then GRANT what it paid for: refused,
    the customer retries and is charged a second time. ``"voided"`` when it was withdrawn,
    None when there was nothing to withdraw or the void could not be completed (logged).
    """
    if not invoice_id:
        return None
    from billing.services import billing_gateway

    try:
        outcome = billing_gateway.void_invoice(invoice_id)
    except Exception:
        logger.exception(
            "{}: could not void the unpaid invoice {} for entity {}",
            what, invoice_id, entity_id,
        )
        return None
    if outcome == "paid":
        logger.warning(
            "{}: invoice {} for entity {} was PAID after all - its answer was lost on the way "
            "back; granting what it paid for rather than refusing it",
            what, invoice_id, entity_id,
        )
        return "paid"
    logger.info("{}: voided unpaid invoice {} for entity {}", what, invoice_id, entity_id)
    return "voided"


def _entity_invoice_name(entity_id) -> str:
    """The entity's name for an invoice line, falling back to its id.

    Never raises. A cosmetic lookup must not be able to stop a charge — a line reading as
    a uuid is bad, and silently not billing is worse.
    """
    from shared_models.models import Entity

    try:
        entity = store._by_pk(Entity, entity_id)
        if entity is not None and (entity.name or "").strip():
            return entity.name.strip()
        logger.error("billing: entity {} has no name for its invoice line", entity_id)
    except Exception:
        logger.exception(
            "billing: could not read the name for entity {}; billing it as its id",
            entity_id,
        )
    return str(entity_id)


def quote_transfer_charge(entity_id, payer_user_id, codes, *, at, before=None):
    """What taking over ``entity_id`` costs the new payer, without charging anything.

    Returns ``{"amount", "currency", "period_start", "period_end", "anchor_at",
    "anchor_is_new"}``, or None if the combination cannot be priced.

    THE SAME CALCULATION the charge uses — ``_transfer_invoice`` below is called by both,
    rather than mirrored by hand. That is the rule ``_paid_cancel_terms`` already sets and
    states: the figure shown before confirming and the figure actually taken cannot be
    computed two ways. ``preview_reinstate_modules`` is what happens when they are, and
    it shipped quoting zero.

    Reads nothing and writes nothing — in particular it does NOT anchor an unanchored
    payer, it only reports what their anchor WOULD become, so opening the accept screen
    has no side effect.

    ``before`` is what they are already billed for on this entity — empty for a handover
    (a join), and the entity's billed codes for a trial converting later (an upgrade).
    See ``_transfer_invoice``; getting it wrong more than doubled a quoted figure.
    """
    anchor, _currency = store.billing_cycle_for_user(payer_user_id)
    anchor_is_new = anchor is None
    if anchor_is_new:
        # Not written. See the charge for why the anchor lands on ``at``.
        anchor = at

    invoice, period = _transfer_invoice(entity_id, anchor, codes, at=at, before=before)
    if invoice is None:
        return None
    return {
        "amount": invoice.total,
        "currency": (invoice.currency or "").upper(),
        # THE WINDOW ACTUALLY CHARGED — from the handover instant, not from the period
        # start. These differ by design: the period is the new payer's whole cycle, and
        # they are only billed the part of it after the old payer's money runs out. A
        # screen that quotes ``period_start`` tells the customer they are paying for days
        # somebody else already paid for, which is both wrong and alarming.
        # ``preview_reinstate_modules`` names the same pair covers_from / covers_to.
        "covers_from": at,
        "covers_to": period.end,
        "period_start": period.start,
        "period_end": period.end,
        "anchor_at": anchor,
        "anchor_is_new": anchor_is_new,
    }


def _transfer_invoice(entity_id, anchor, codes, *, at, before=None):
    """Price ``codes`` starting at ``at`` for this payer. Returns ``(invoice, period)``.

    THE PERIOD COMES FROM ``at``, not from ``now``, and that is the whole reason this is
    not ``_bill_module_change_in_house``. A transfer's ``at`` is the instant the old
    payer's money runs out, which is in the FUTURE when the offer is accepted. Derive the
    period from ``now`` instead and ``at`` lands past ``period.end``, so
    ``Period.remaining_seconds`` returns 0, ``prorate`` returns 0, ``build_change``
    returns None — and the caller reads None as "nothing was owed", which is a success.
    The entity would be handed over free, silently.

    ``before`` is WHAT THE PAYER IS ALREADY BILLED FOR on this entity, and it changes the
    price a great deal:

    * For the HANDOVER charge it is empty. The incoming payer pays for this entity for the
      first time, whatever it was to the outgoing one, so the line is a join.
    * For a TRIAL CONVERTING LATER it is not. By then they are already paying for whatever
      was active, so the trial is an UPGRADE — priced at its margin inside the resulting
      bundle, not at its standalone rate. On an entity with one active module and one on
      trial the difference was 94.03 against 219.41: quoting the join would have promised
      a charge more than double the real one.

    Pass what ``_bill_module_change_in_house`` would see, i.e. ``_billed_codes_in_house``.
    """
    from billing.services import changes
    from billing.services.billing import period_containing

    period = period_containing(anchor, at)
    current = {str(c).upper() for c in (before or set())}
    invoice = changes.build_change(
        entity_id,
        _entity_invoice_name(entity_id),
        current,
        current | {str(c).upper() for c in codes},
        period,
        at,
    )
    return invoice, period


def _bill_transfer_in_house(entity_id, payer_user_id, customer_id: str, codes, *,
                            at, idempotency_key, keep_open: bool = False):
    """Charge the NEW payer for taking over ``entity_id`` from ``at``.

    Returns ``{"paid", "period_end", "invoice_id", "amount", "currency", "anchor",
    "reason"}``. ``paid`` False means nothing was collected and the caller must not move
    the payer pointer — the entity stays where it is.

    ``keep_open`` is the DEFERRED collection's (``transfers.collect_due``): nothing is
    withdrawn on a failure. A decline leaves the invoice open for dunning to chase, like a
    renewal's (the user's call, 2026-09-30), and a failure of the PROCESSOR leaves whatever
    was raised for the next pass to settle by its key. The unpaid answer then also says which
    it was - ``declined`` or ``transient`` - and names the ``invoice_id`` left open. The
    accept, with the customer in front of the decline, withdraws it as before.

    ``anchor`` is the incoming payer's cycle anchor as it stood for THIS charge — the one
    they already had, or the one established here on a first charge. It is returned so the
    accept can record it: the anchor is per payer, so a handover moves the entity onto a
    different cycle, and the offer screen has already told the nominee which case they are
    in (``anchor_is_new``). Only meaningful when ``paid``; None on every failure, where
    there is no charge for an anchor to belong to.

    Called at ACCEPT, before anything about the entity has been changed, and that ordering
    is the whole safety story. The accept cannot be one transaction (``store``'s helpers
    each commit their own unit of work, and ``reserve_invoice`` commits the invoice row
    before the processor is even contacted), so a decline is survived by having moved
    nothing yet rather than by rolling back. **Nothing may be staged in the session when
    this is called** — ``reserve_invoice`` commits the whole session on success and rolls
    the whole session back on an idempotency collision, so staged work would either land
    early or vanish.

    ``idempotency_key`` is supplied by the caller rather than derived here, and it carries
    the offer's attempt number. Within one attempt it is identical, so a double-click is
    refused by the unique index on ``subscription_invoice.idempotency_key``; across
    attempts it differs, so a declined card can be fixed and retried. A key derived from
    ``at`` — which is what ``changes.change_key`` would give, since ``at`` is stored and
    does not move — would be stable in both directions and JAM the retry, because voiding
    an invoice deliberately keeps its row and its key claimed.
    """
    from billing.services import billing_gateway, renewals
    from billing.services.billing import join_memo

    def _failed(reason, **left):
        return {"paid": False, "period_end": None, "invoice_id": None,
                "amount": 0, "currency": None, "anchor": None, "reason": reason, **left}

    anchor, _currency = store.billing_cycle_for_user(payer_user_id)
    first_charge = anchor is None
    if first_charge:
        # ANCHOR AT ``at``, NOT AT ``now``. Their cycle then begins exactly where the old
        # payer's money ends, so the handover has no seam and this first charge is one
        # clean period (``prorate`` returns the full amount when ``at <= period.start``).
        # Anchoring at ``now`` instead — which is what every other caller of
        # ``start_billing_cycle`` does — would start their cycle on whichever day they
        # happened to click accept, and bill a part-month matching neither payer's cycle.
        plan = store.billing_plan_for_codes(set(codes))
        store.start_billing_cycle(payer_user_id, at, (plan.currency if plan else ""))
        anchor, _currency = store.billing_cycle_for_user(payer_user_id)
        if anchor is None:
            # No ``user_stripe_customer`` row at all, so nothing can be charged. The
            # accept blockers refuse this case up front; reaching it means the account
            # went away between the check and here.
            logger.error("transfer: could not anchor payer {}", payer_user_id)
            return _failed("That billing account isn't set up to be charged.")

    # WHICH CARD the incoming payer is charged on: the one they nominated for THIS
    # company. Read before anything can succeed, because every paid return below has to
    # establish that card's cycle (``_establish_card_cycle``).
    group = store.billing_group_for_entity(entity_id, payer_user_id)

    invoice, period = _transfer_invoice(entity_id, anchor, codes, at=at)
    if invoice is None or not invoice.total:
        # Nothing to collect — the window is empty or the plan prices it at zero. Treat it
        # as settled rather than failed: the days are covered and there is no document to
        # chase. The caller still records the period end, so the renewal stays suppressed.
        logger.info(
            "transfer: nothing to charge for entity {} from {}; period ends {}",
            entity_id, at, period.end,
        )
        _establish_card_cycle(group, first_charge, period)
        return {"paid": True, "period_end": period.end, "invoice_id": None,
                "amount": 0, "currency": None, "anchor": anchor, "reason": None}

    # ADOPT before charging. A previous attempt under this exact key may have collected
    # the money and died before the pointer moved — the crash window this whole design is
    # ordered around. ``renewals._already_invoiced`` is the three-way resolution for that
    # (confirmed / reserved-but-unknown / never existed) and is reused rather than
    # reimplemented, because getting it subtly wrong charges someone twice.
    existing = renewals._already_invoiced(
        customer_id, idempotency_key, metadata_key="transfer_key"
    )
    if existing == "paid":
        logger.info(
            "transfer: {} was already collected; adopting it rather than charging again",
            idempotency_key,
        )
        _establish_card_cycle(group, first_charge, period)
        return {"paid": True, "period_end": period.end,
                "invoice_id": store.invoice_for_key(idempotency_key).external_id,
                "amount": invoice.total, "currency": invoice.currency,
                "anchor": anchor, "reason": None}
    if existing is not None:
        # Raised but unpaid. Do not raise a second document against the same window.
        if existing == "draft":
            # Never finalized, so there is nothing anybody can pay: said, loudly.
            billing_gateway.stranded_draft(
                getattr(store.invoice_for_key(idempotency_key), "external_id", None),
                idempotency_key, "transfer", billing_gateway.NOT_RETRIED,
            )
        return _failed("There's already an unpaid invoice for this handover.")

    # The card is required, not defaulted — the accept blockers ask for it up front, and
    # reaching here without one means charging a card they never chose for a company they
    # are only now taking on.
    if group is None:
        logger.error(
            "transfer: entity {} has no payment method nominated for payer {}",
            entity_id, payer_user_id,
        )
        return _failed("Choose a payment method for this company before taking it on.")

    # The memo states the actual span — "12 Sept to 1 Oct, 19 of 30 days" — which the
    # invoice's own period_start/period_end cannot, because they record the payer's whole
    # period. Without it the customer sees a part-month charge with no explanation of why.
    plan = store.billing_plan_for_codes(set(codes))
    product_name = plan.display_name if plan else "Subscription"

    def _paid(invoice_id):
        _establish_card_cycle(group, first_charge, period)
        return {"paid": True, "period_end": period.end, "invoice_id": invoice_id,
                "amount": invoice.total, "currency": invoice.currency,
                "anchor": anchor, "reason": None}

    try:
        result = billing_gateway.issue_invoice(
            customer_id,
            invoice,
            memo=join_memo(
                _entity_invoice_name(entity_id),
                product_name,
                invoice.total,
                period,
                at,
            ),
            metadata={"transfer_key": idempotency_key, "entity_id": str(entity_id),
                      "billing_group": str(group.id)},
            idempotency_key=idempotency_key,
            payer_user_id=payer_user_id,
            payment_method=group.stripe_payment_method_id,
            billing_group_id=group.id,
        )
    except Exception as exc:
        logger.exception(
            "transfer: could not bill the handover of entity {} to {}",
            entity_id, payer_user_id,
        )
        # A decline arrives HERE, as an exception out of ``Invoice.pay``, by which point
        # the document is finalized and OPEN. Void it: an abandoned open invoice becomes
        # the first thing ``dunning.collect_due`` chases on any later episode, for a
        # handover that never happened. Unless it was PAID after all - the lost reply.
        raised = getattr(exc, "invoice_id", None)
        reason = (getattr(exc, "user_message", None)
                  or "That payment didn't go through. Check the card and try again.")
        if keep_open:
            transient = billing_gateway.retryable(exc)
            return _failed(reason, invoice_id=raised, declined=not transient,
                           transient=transient)
        if _void_unpaid_invoice(raised, entity_id, "transfer") == "paid":
            return _paid(raised)
        return _failed(reason)

    if (result or {}).get("status") != "paid":
        logger.warning(
            "transfer: invoice {} for entity {} is {}; not moving the payer",
            (result or {}).get("id"), entity_id, (result or {}).get("status"),
        )
        if keep_open:
            return _failed("That payment didn't go through. Check the card and try again.",
                           invoice_id=(result or {}).get("id"), declined=True, transient=False)
        if _void_unpaid_invoice((result or {}).get("id"), entity_id, "transfer") == "paid":
            return _paid(result.get("id"))
        return _failed("That payment didn't go through. Check the card and try again.")

    return _paid(result.get("id"))


def _establish_card_cycle(group, first_charge: bool, period) -> None:
    """Start THIS CARD's cycle on a paid charge - never advance one that exists.

    ``paid_through`` is the CARD's marker for every company on it, and ``due_renewals`` reads
    it to decide whether the card owes anything at all. Moving it forward for one company's
    charge announces that every other company on the card is settled too, silently
    cancelling their renewal - so only a renewal may advance it. But a card with NO date is
    skipped by ``due_renewals`` outright, and measured as no access at all: left unset it
    never renews, and its companies are switched off at the next sweep.

    So it is set when the payer has never been charged (``first_charge``) OR when this card
    never has - judged on the CARD, not the payer. The handover biller used to ask only the
    first: a payer whose first attempt had failed (the anchor is written before the charge),
    or one already paying on another card, took the company onto a card with no date, and
    it went dark straight after the handover. The trial rule, which always asked both.
    """
    if group is not None and (first_charge or group.paid_through is None):
        store.set_group_paid_through(group.id, period.end)


def _resolve_prior_conversions(entity_id, payer_user_id, customer_id, group, after,
                               since, now=None):
    """Settle every earlier attempt at THIS trial's conversion before a new one is raised.

    A conversion the processor failed to make is retried on the next pass under a fresh key
    (a fixed one would replay the processor's error for a day, and move the anchor and the
    proration with it), so its earlier attempts are found again by their ``convert-`` key,
    raised since the trial ended. Each is settled by what the processor says of it:

    * PAID - the answer was lost, or it was paid since: that is the conversion. Returned as
      its period, to be adopted - never charged again;
    * OPEN and never tried: charged now, on the company's current card - and if that is
      refused, withdrawn and the trial expires, as a decline does;
    * OPEN and tried and refused: withdrawn, and the trial expires;
    * a DRAFT, or a reservation the processor never saw: withdrawn, and a fresh attempt goes on;
    * void or uncollectible: dealt with.

    A paid attempt whose period is already OVER by ``now`` paid for that period, not this one:
    it is left, and the current period is billed afresh. Adopted, the company went into the new
    period marked as billed in it (``renewals.entities_billed_in``) and its renewal skipped it -
    a free month.

    Returns the adopted ``Period``, ``"declined"`` (expire the trial), or None (raise a fresh
    attempt). Raises ``ChargeDeferred`` when the processor cannot say - charging again on a
    guess is how a trial is paid for twice.
    """
    from billing.services import billing_gateway, changes
    from billing.services.billing import Period

    prefix = f"convert-{entity_id}-"
    wanted = changes.codes_key(after)
    for record in store.invoices_with_key_prefix(payer_user_id, prefix):
        if record.status in ("void", "uncollectible"):
            continue
        # ``convert-<entity>-<when>-<codes>[-<replay scope>]``. WHEN is read off the key, not
        # the row's ``created_at``: it is the app's clock, the one ``trial_end`` is on - the
        # database's differs wherever the app runs on a test clock.
        stamp, _, tail = record.idempotency_key[len(prefix):].partition("-")
        try:
            made = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        # Keys carry whole seconds: an attempt made in the second the trial ended is this
        # trial's, however many microseconds ``since`` holds.
        if made < since.replace(microsecond=0):
            continue                      # an earlier conversion, not this trial's
        codes = tail.split("-")[0]
        try:
            if record.external_id:
                found = billing_gateway.recheck(record)
                if found is None:                 # a draft deleted by hand
                    continue
            else:
                found = billing_gateway.find_invoice_by_metadata(
                    customer_id, "change_key", record.idempotency_key
                )
                if found is None:
                    store.discard_invoice(record.id)
                    continue
                billing_gateway.record_found_invoice(record, found)
                # Read again: the store settles a fresh copy, and this one still has no id.
                record = store.invoice_for_key(record.idempotency_key) or record
            status = found.get("status")
            if status in ("open", "draft") and (
                status == "draft" or billing_gateway.declined(found) or codes != wanted
            ):
                # Not one to finish: withdrawn - unless it turns out paid after all.
                if billing_gateway.void_invoice(found["id"]) == "paid":
                    status = "paid"
                elif status == "open" and codes == wanted:
                    return "declined"
                else:
                    continue
            elif status == "open":
                try:
                    found = billing_gateway.resume_invoice(
                        record, payment_method=group.stripe_payment_method_id, where="trial"
                    )
                except billing_gateway.BillingError as exc:
                    if billing_gateway.retryable(exc):
                        raise
                    if _void_unpaid_invoice(record.external_id, entity_id, "trial") != "paid":
                        return "declined"
                    found = {"status": "paid"}
                status = (found or {}).get("status")
        except billing_gateway.BillingError as exc:
            if billing_gateway.retryable(exc):
                raise ChargeDeferred(f"conversion of {entity_id} deferred") from exc
            raise
        except Exception as exc:
            if billing_gateway.retryable(exc):
                raise ChargeDeferred(f"conversion of {entity_id} deferred") from exc
            raise
        if status != "paid":
            continue
        ended = record.period_end
        if now is not None and ended is not None:
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=UTC)
            if ended <= now:
                logger.info(
                    "trial: conversion {} was paid for a period that has ended; the current "
                    "one is billed afresh", record.idempotency_key,
                )
                continue
        if codes != wanted:
            # Paid for a DIFFERENT set of modules than is due now - another trial's
            # conversion inside the same outage. Guessing either way charges twice or gives
            # modules away, so it waits for a person, and the trial runs on meanwhile.
            logger.error(
                "trial: conversion {} was PAID for {} while {} is due on entity {} - resolve "
                "it by hand", record.idempotency_key, codes, wanted, entity_id,
            )
            raise ChargeDeferred(f"conversion of {entity_id} needs a person")
        logger.warning(
            "trial: conversion {} was already PAID; adopting it rather than charging again",
            record.idempotency_key,
        )
        return Period(record.period_start, record.period_end)
    return None


def _bill_module_change_in_house(entity_id, payer_user_id, customer_id: str,
                                 current, codes, *, kind: str = "change",
                                 defer_transient: bool = False, prior_since=None):
    """Bill a module change from Minty's own arithmetic. Returns the new paid-through.

    Shared by the trial conversion and the BUY path — the money is the same either way,
    only the caller's response to failure differs (expire the trial vs tell the user).

    Returns None if nothing could be collected, which the caller treats exactly like a
    declined card: expire the trial rather than hand over modules that were not paid for.

    The FIRST conversion for a payer establishes their anchor at "now", so the period
    starts here and the charge is a full one — matching what Stripe billed at conversion
    (280.00). A later entity joining an existing payer is prorated against the anchor
    already recorded (373.33). Both figures were verified against Stripe before cutover.

    ``defer_transient`` (the conversion): a failure of the PROCESSOR raises ``ChargeDeferred``
    and withdraws nothing, for the next pass to retry - which it can only do safely because
    ``kind="convert"`` makes every attempt findable again: before a new one is raised,
    ``_resolve_prior_conversions`` settles those since ``prior_since``.
    """
    from billing.services import changes
    from billing.services.billing import period_containing

    now = clock.now()
    # WHICH CARD. Resolved before anything is priced, because there is no fallback: a
    # company with no nomination is not billed on the account default, it is not billed at
    # all. Returning None here is the same answer as a declined card — the caller expires
    # the trial or tells the customer — which is right, because in both cases the money
    # was not collected and the modules must not be handed over.
    group = store.billing_group_for_entity(entity_id, payer_user_id)
    if group is None:
        logger.error(
            "billing: entity {} has no payment method nominated; refusing to charge "
            "payer {} on a card they did not choose for it",
            entity_id, payer_user_id,
        )
        return None

    anchor, _currency = store.billing_cycle_for_user(payer_user_id)
    # Whether this is the payer's FIRST charge, which is what decides below whether the
    # account's cycle may be written. The anchor answers it without a second read: it is
    # set once, here, and never moves again.
    first_charge = anchor is None
    if anchor is None:
        # Nothing has ever been billed for this payer: the cycle starts now, so this
        # period is charged in full rather than prorated against a period they were
        # never part of.
        plan = store.billing_plan_for_codes(current | codes)
        store.start_billing_cycle(payer_user_id, now, (plan.currency if plan else ""))
        anchor, _currency = store.billing_cycle_for_user(payer_user_id)
        if anchor is None:
            logger.error("trial: could not anchor payer {}", payer_user_id)
            return None

    period = period_containing(anchor, now)

    name = _entity_invoice_name(entity_id)

    if kind == "convert" and prior_since is not None:
        prior = _resolve_prior_conversions(
            entity_id, payer_user_id, customer_id, group, current | codes, prior_since, now
        )
        if prior == "declined":
            return None
        if prior is not None:
            # An earlier attempt was PAID - its answer lost, or collected since.
            _establish_card_cycle(group, first_charge, prior)
            return prior.end

    try:
        invoice = changes.issue_change(
            customer_id, entity_id, name, current, current | codes, period, now,
            group=group, kind=kind,
        )
    except Exception as exc:
        logger.exception(
            "trial: could not bill the conversion for {} {} in-house", entity_id, codes
        )
        from billing.services import billing_gateway

        if defer_transient and billing_gateway.retryable(exc):
            # The processor failed, not the card: nothing is withdrawn - whatever was
            # raised is settled by the next attempt, which finds it by its key.
            raise ChargeDeferred(f"conversion of {entity_id} deferred") from exc
        # THE path a declined card actually takes. ``Invoice.pay`` raises rather than
        # returning an unpaid invoice, and by then the document is finalized and OPEN —
        # so the branch below, which reads a returned status, never sees a decline at all.
        # Without this the customer keeps a bill for a module this function is in the
        # middle of refusing them. A bill found PAID instead is the lost reply: granted.
        if _void_unpaid_invoice(getattr(exc, "invoice_id", None), entity_id, "trial") == "paid":
            _establish_card_cycle(group, first_charge, period)
            return period.end
        return None

    # A None invoice means nothing was owed (already at this price), which is a success:
    # the modules are granted and the period stands.
    if invoice is not None and invoice.get("status") != "paid":
        logger.warning(
            "trial: conversion invoice {} for entity {} is {}; not granting modules",
            invoice.get("id"),
            entity_id,
            invoice.get("status"),
        )
        # Withdraw the bill as well as the modules. Refusing to grant and leaving the
        # invoice open charges the customer for something they were explicitly not given
        # — the row's own ``first_billed_at`` stays NULL, which is this code recording
        # that no real charge happened, beside an open document that says one did.
        #
        # It does not simply sit there, either. ``dunning.collect_due`` chases the
        # payer's OLDEST open invoice, so on any later episode this is the first thing
        # retried, for a module whose row has since gone ``expired`` — terminal, never
        # to be granted. A customer would pay for a trial that lapsed months earlier.
        #
        if _void_unpaid_invoice(invoice.get("id"), entity_id, "trial") == "paid":
            _establish_card_cycle(group, first_charge, period)
            return period.end
        return None

    # ESTABLISH the card's cycle; never ADVANCE one that already exists.
    #
    # This charge covered ONE entity, but ``paid_through`` is the CARD's marker for every
    # company on it, and ``renewals.due_renewals`` reads it to decide whether that card
    # owes anything at all. Moving it forward here announced that every OTHER company on
    # the same card was settled for the new period too. A trial converting exactly on a
    # period boundary therefore cancelled that day's renewal for its siblings, and they
    # went unbilled for the month — silently, because a card that is not due raises no
    # invoice to notice the absence of. Only a renewal covers every company on the card,
    # so only a renewal may move this.
    #
    # The first charge is the exception: the card has no cycle yet, and ``due_renewals``
    # skips a NULL ``paid_through`` outright, so leaving it unset would mean it never
    # renewed at all. Judged on the GROUP, not the payer: a payer who nominates a second
    # card is anchored already, but that card has collected nothing and starts here.
    _establish_card_cycle(group, first_charge, period)
    return period.end


def _finish_conversion(entity_id, billable) -> None:
    """Write the converted rows and align the entity's other active modules.

    Shared by both billers so the local state after a conversion is identical whichever
    one collected the money — the mirror should not be able to tell them apart.
    """
    now = clock.now()
    for row in billable:
        fields = {"phase": PHASE_ACTIVE, "app_access_until": None}
        # Stamped once, on the FIRST charge, and never moved: it is what marks this row
        # as a paid module rather than a trial for the rest of its life. Re-stamping on a
        # later change would still read as non-null, but the date would stop meaning
        # "when they started paying".
        if getattr(row, "first_billed_at", None) is None:
            fields["first_billed_at"] = now
        store.upsert_module_row(
            entity_id, row.function_code, row.payer_user_id, **fields
        )


def _bill_reinstatement_in_house(entity, user, code: str, row) -> None:
    """Collect the part of the period a reinstated module is not covered for.

    The extension paid up to ``app_access_until``; the renewal skipped this module for
    the rest of the period. So the customer owes from one to the other, at the MARGINAL
    price — putting Petty Cash back onto an entity already paying for Payment Request costs
    the 120 difference, not Petty Cash's 280.

    ``build_change`` does that arithmetic already: prorating from ``app_access_until``
    rather than from now bills exactly the uncovered window.

    Raises rather than returning on failure. Reinstating is a purchase, and a purchase
    that is not paid for must not be granted.
    """
    from billing.services import changes
    from billing.services.billing import period_containing

    payer_user_id = row.payer_user_id or getattr(user, "id", None)
    # Resolved, not read locally: "no customer" here reinstates the module WITHOUT
    # charging, so a missing mapping row is money quietly not collected.
    customer_id = _resolve_customer_id(payer_user_id)
    anchor, _currency = store.billing_cycle_for_user(payer_user_id)
    covered_to = row.app_access_until
    if not customer_id or anchor is None or covered_to is None:
        # Nothing to prorate against. Better to reinstate than to strand the customer
        # over a missing date, but say so — this is money that was not collected.
        logger.error(
            "reinstate: cannot price the uncovered period for {} {} (customer={} "
            "anchor={} covered_to={}); reinstating WITHOUT charging",
            entity.id, code, customer_id, anchor, covered_to,
        )
        return

    # The period we are IN, not the one containing covered_to -- those differ exactly
    # when the extension runs past the anchor, which is the case this guard is for.
    period = period_containing(anchor, clock.now())
    if covered_to >= period.end:
        # The 30 free days already cover the rest of this period. Charging anyway would
        # bill for days the extension paid for.
        return

    # WHICH CARD. The one this company is nominated onto, and nothing else. Passing no
    # ``group`` makes ``issue_change`` raise the invoice with no card named, which the
    # processor charges to the customer's account default: a card the payer never chose
    # for this company, on a document no billing account owns. Reinstating is a purchase,
    # so a company with no card is refused rather than billed somewhere else.
    group = store.billing_group_for_entity(entity.id, payer_user_id)
    if group is None:
        logger.error(
            "reinstate: entity {} has no payment method nominated for payer {}; refusing "
            "to charge a card they did not choose for it",
            entity.id, payer_user_id,
        )
        raise CheckoutError(
            "Choose a payment method for this company before restoring this module.",
            status=409,
        )

    before = _billed_codes_in_house(entity.id) - {code}
    name = getattr(entity, "name", None) or str(entity.id)
    try:
        # Each press is a new ATTEMPT under its own key (``changes._next_attempt``). The key
        # is otherwise stable - the company, the day its access ends, the modules - and a
        # declined attempt's voided invoice kept it claimed, so every retry was refused as
        # "check your payment method" without the card ever being tried.
        invoice = changes.issue_change(
            customer_id, entity.id, name, before, before | {code}, period, covered_to,
            group=group, attempts_of=payer_user_id,
        )
    except Exception as exc:
        logger.exception(
            "reinstate: could not bill the uncovered period for {} {}", entity.id, code
        )
        from billing.services import billing_gateway

        if getattr(exc, "claimed", False):
            # Another press of the same button got there first; it decides the outcome.
            raise CheckoutError(
                "This module is already being restored. Refresh the page in a moment.",
                status=409,
            ) from exc
        # A decline RAISES out of ``Invoice.pay``, with the document finalized and OPEN.
        # The module is not given back, so the bill must not survive either: left open,
        # dunning chases it and the portal offers it as *Retry payment* — a customer
        # paying to restore a module that stays cancelled. Found PAID instead (the lost
        # reply), the module is restored: it was paid for.
        if _void_unpaid_invoice(
            getattr(exc, "invoice_id", None), entity.id, "reinstate"
        ) == "paid":
            return
        if billing_gateway.retryable(exc):
            # The processor failed, not the card: "check your payment method" would send the
            # customer to fix something that is not broken.
            raise CheckoutError(
                "We couldn't reach the payment provider. Nothing was charged - please try "
                "again shortly.",
                status=503,
            ) from exc
        raise CheckoutError(
            "We couldn't take the payment to restore this module. Please check your "
            "payment method and try again.",
            status=402,
        ) from exc

    if invoice is not None and invoice.get("status") != "paid":
        logger.warning(
            "reinstate: invoice {} for {} {} is {}; not restoring the module",
            invoice.get("id"), entity.id, code, invoice.get("status"),
        )
        if _void_unpaid_invoice(invoice.get("id"), entity.id, "reinstate") == "paid":
            return
        raise CheckoutError(
            "We couldn't take the payment to restore this module. Please check your "
            "payment method and try again.",
            status=402,
        )


def _grant_purchased_modules(entity_id, payer_user_id, codes) -> None:
    """Activate modules bought outright, once their charge has been collected.

    The buy-path counterpart of ``_finish_conversion``. That one starts from existing
    trial ROWS; a purchase may have no row at all for the module, so this works from
    codes and creates them.

    Ordered so a failure cannot hand over an unpaid module: the caller has already
    collected the money before this runs.
    """
    now = clock.now()
    for code in sorted({str(c).upper() for c in codes}):
        existing = store.module_row(entity_id, code)
        fields = {
            "phase": PHASE_ACTIVE,
            "app_access_until": None,
        }
        if getattr(existing, "first_billed_at", None) is None:
            fields["first_billed_at"] = now
        store.upsert_module_row(entity_id, code, payer_user_id, **fields)
        _set_module_access(entity_id, code, True)


def _expire_due_trial(row) -> None:
    """End a trial that had nothing to bill: mark it expired and revoke access."""
    store.upsert_module_row(
        row.entity_id,
        row.function_code,
        row.payer_user_id,
        phase=PHASE_EXPIRED,
        app_access_until=None,
    )
    _set_module_access(row.entity_id, row.function_code, False)


def terminate_lapsed_module(row) -> bool:
    """End a wound-down module whose access has now actually run out. Returns whether
    it moved.

    ``scheduled_cancel`` and ``past_due`` are both TEMPORARY states with a date on them:
    one is riding out the days its extension bought, the other the grace its debt is
    allowed. Nothing fired when that date passed, so the phase stayed as it was — a
    module nobody can use still reading as "cancelling" or "in arrears" indefinitely,
    with the panel offering Renew or Pay now for a subscription that is over.

    ``cancelled`` is the terminal phase for both. It grants no access (``access_end``
    refuses terminal phases outright, without consulting a date) and, unlike
    ``scheduled_cancel``, it does not stand in the way of buying the module again —
    ``is_subscribed`` excludes it precisely because nothing is held any more. That is the
    difference the customer feels: cancelled-but-running means Renew, and terminated
    means Subscribe.

    Deliberately NOT applied to a trial. ``convert_or_expire_due_trials`` owns that
    boundary because it decides convert-or-expire, which this cannot; a trial that ends
    lands on ``expired`` instead — terminal too, and equally re-purchasable.

    The extension is left alone. ``pending_extensions_for_payer`` selects on
    ``extension_state``, not phase, so money recorded at cancellation is still collected
    by the next renewal run after the module has been terminated. Terminating ends the
    subscription, not the debt.
    """
    phase = getattr(row, "phase", None)
    if phase not in (PHASE_SCHEDULED_CANCEL, PHASE_PAST_DUE):
        return False

    ended_at = getattr(row, "app_access_until", None)
    store.upsert_module_row(
        row.entity_id,
        row.function_code,
        row.payer_user_id,
        phase=PHASE_CANCELLED,
        # Cleared for the same reason ``_expire_due_trial`` clears it: the window is
        # spent, and a leftover date on a terminal row is a trap for any future reader
        # that checks dates before phases.
        app_access_until=None,
    )
    store.record_action(
        entity_id=row.entity_id,
        function_code=row.function_code,
        payer_user_id=row.payer_user_id,
        actor_user_id=None,          # nobody clicked; a date passed
        action=AUDIT_TERMINATE,
        outcome=OUTCOME_SUCCEEDED,
        phase_before=phase,
        phase_after=PHASE_CANCELLED,
        # Kept here because the row's copy is being cleared — this is the log of WHEN
        # the access being terminated actually ran out.
        app_access_until=ended_at,
        note=(
            "access ran out after cancellation; subscription terminated"
            if phase == PHASE_SCHEDULED_CANCEL
            else "past-due grace ran out; subscription terminated"
        ),
    )
    return True


def _leaving_codes(rows, paid_through, now) -> set[str]:
    """The modules on their way out that were PAID for the period they are leaving.

    Not ``is_billing_forward`` (they bill no further, by definition) and not
    ``is_covered_this_period`` (that counts the ones staying too): this is the middle
    set, and it is what an extension is priced against — see ``_leaving_marginal``.

    A cancelled TRIAL is excluded by ``first_billed_at``: nothing was paid for it, so it
    has no extension and does not change what the others are worth.

    So is a module whose access has ALREADY RUN OUT. ``scheduled_cancel`` is a temporary
    phase with a date on it, and ``terminate_lapsed_module`` only moves it to ``cancelled``
    when the daily sweep next runs — which lags, and is skipped outright when a money job
    failed (``daily.SWEEP_BLOCKERS``). Until then the row still reads as winding down while
    holding nothing. ``_segmented_extension`` already refuses to let such a row share a
    slice (its ``live`` sets are rebuilt from the access ends), so no money was wrong — but
    it stayed in this set, which is what names the cancellation: a lone Petty Cash
    cancellation was labelled "Super Minty" because a module that lapsed last cycle was
    still counted as leaving with it.
    """
    if paid_through is None or now is None or paid_through <= now:
        return set()
    return {
        r.function_code.upper()
        for r in rows
        if r.phase == PHASE_SCHEDULED_CANCEL
        and getattr(r, "first_billed_at", None) is not None
        and not (
            getattr(r, "app_access_until", None) is not None
            and r.app_access_until <= now
        )
    }


def _leaving_marginal(leaving: set[str], code: str) -> int:
    """What ``code`` is worth WITHIN the set that is leaving, per month.

    The rule for the extra days a cancellation buys, in one line: the set leaving
    together is worth the plan that covers it, and that total is allocated SEQUENTIALLY
    in sorted code order — the first code takes its standalone price, each later one
    takes what it ADDS to the codes before it.

        {PETTY_CASH}          PETTY_CASH  280 - 0   = 280
        {BILL}                BILL        280 - 0   = 280
        {BILL, PETTY_CASH}    BILL        280 - 0   = 280
                              PETTY_CASH  400 - 280 = 120   -> 400 together

    The shares telescope, so they always sum to the plan price of the whole set. That is
    the property this exists for: a pair leaving together held the BUNDLE until they
    went, so the days they bought are worth the bundle. Charging each one what it adds to
    the OTHER LEAVER gave 120 + 120 = 240 — less than either module has ever cost, for
    days on which the customer had both.

    A module leaving alone is still worth its list price: nothing is leaving with it to
    share the line, so its prefix is empty and it pays the whole 280. That half of the
    rule is unchanged, and deliberately so — it is not the survivors that price this.

    Sorted order, not click order. Two customers cancelling the same pair in opposite
    orders must be charged the same, and ``sorted`` is the same canonicalisation
    ``billing.plan_code`` uses to key the catalog. The visible consequence is that the
    code sorting FIRST carries the standalone price: cancelling Payment Request beside an
    already-pending Petty Cash re-prices Petty Cash DOWN to the 120 step and takes the 280
    itself (see ``_reprice_pending_extensions``). The total is right either way.
    """
    from billing.services.billing import marginal_amount

    ordered = sorted(leaving)
    if code not in ordered:
        # Not leaving, so it is worth nothing here. Matches what the previous
        # difference-of-plans form happened to return, and keeps ``index`` below honest.
        return 0
    prefix = set(ordered[: ordered.index(code)])

    plan_with = store.billing_plan_for_codes(prefix | {code})
    # Never asked for the empty set: ``plan_code`` raises on it and
    # ``billing_plan_for_codes`` turns that into None, which would be indistinguishable
    # from a genuine catalog gap two lines below.
    plan_prefix = store.billing_plan_for_codes(prefix) if prefix else None
    if plan_with is None or (prefix and plan_prefix is None):
        # Charge nothing rather than guess a price — the posture ``build_change`` and
        # ``build_renewal`` already take. The old form fell back to "no survivor" here
        # and charged the WHOLE line price, which overcharges on a catalog gap.
        logger.error(
            "billing: no plan prices the cancellation step {} -> {}; charging no "
            "extension for {} rather than guessing",
            ",".join(sorted(prefix)) or "(nothing)",
            ",".join(sorted(prefix | {code})),
            code,
        )
        return 0

    # ``marginal_amount`` treats a falsy remainder as "nothing to offset" and returns the
    # whole line price, which is exactly the empty-prefix case.
    return marginal_amount(
        plan_with.amount, plan_prefix.amount if plan_prefix else None
    )


def _segmented_extension(code: str, ends: dict, period, paid_through) -> int:
    """What ``code`` owes for its extra days, priced piece by piece as others fall away.

    Modules cancelled on different days stop on different days, so the set sharing the
    line CHANGES during the window. Petty Cash cancelled on the 5th runs to 4 Sep and
    Payment Request cancelled on the 6th to 5 Sep: up to 4 Sep they are a pair, worth the
    400 bundle between them — Payment Request takes its 280 and Petty Cash the 120 step —
    but on the 5th Payment Request is alone, and the pieces after that are worth only its
    own 280. One rate for the whole window would charge that last day at a bundle price
    nobody is in.

    So the window is cut at every date another leaver ends, and each piece is priced with
    ``_leaving_marginal`` against whoever is still there. Pieces are taken as differences
    of the cumulative charge, so this bills exactly what ``extension_charge`` would for a
    single-rate window and cannot drift from it.

    One asymmetry falls out of the sorted allocation: the code sorting FIRST has an empty
    prefix in every slice, so its rate never changes and segmentation is a no-op for it.
    Only the later codes feel the cuts.

    ``ends`` is {code: access_end} for everything leaving, this module included.
    """
    pieces = _extension_pieces(code, ends, period, paid_through)
    return max(0, sum(charge for _start, _stop, _rate, charge in pieces))


def _extension_pieces(code: str, ends: dict, period, paid_through) -> list:
    """``(start, stop, rate, charge)`` for each piece of ``code``'s extra days — the
    arithmetic ``_segmented_extension`` sums, kept apart so the invoice line can also say
    what RATE the days were charged at (``pending_extension_terms``). Empty when access
    ends at or before ``paid_through``: there are no extra days."""
    from billing.services.billing import Period, extension_charge

    end = ends.get(code)
    if end is None or end <= paid_through:
        return []

    window = Period(period.start, paid_through)
    cuts = sorted(
        {paid_through, end}
        | {e for c, e in ends.items() if c != code and e and paid_through < e < end}
    )
    pieces = []
    for start, stop in zip(cuts, cuts[1:]):
        # Who still holds their module during this piece: everyone whose access outlasts
        # its start. The cuts are the only points where that can change.
        live = {c for c, e in ends.items() if e and e > start}
        base = _leaving_marginal(live, code)
        pieces.append((
            start, stop, base,
            extension_charge(base, window, stop) - extension_charge(base, window, start),
        ))
    return pieces


def pending_extension_terms(row) -> tuple:
    """``(start, end, rate)`` of the access extension ``row`` has pending — what its invoice
    line records (``renewals._pending_extension_lines``).

    The days are the overhang ``extension_charge`` priced: from what the company is paid
    through to the module's access end. The rate is re-derived the way the charge was — the
    window cut wherever another leaver stops, each piece priced against whoever is still
    there — and returned only when that REPRODUCES the amount the row holds and every piece
    shares one rate. Otherwise None, never a blend: a window whose rate stepped part-way had
    no single rate, and a re-derivation that does not reproduce the figure being billed is
    not evidence of one. The days are recorded either way.

    NEVER RAISES. This labels a charge, it does not price one, and a renewal must not fail
    over a label (the posture ``renewals._extension_product`` takes) — an error is logged
    and answered with whatever is known.
    """
    from billing.services.billing import period_containing

    end = getattr(row, "app_access_until", None)
    if end is None:
        return None, None, None
    start = None
    try:
        start = store.paid_through_for_entity(row.entity_id)
        anchor, _currency = store.billing_cycle_for_user(row.payer_user_id)
        if start is None or anchor is None or end <= start:
            return start, end, None
        code = (row.function_code or "").upper()
        # Every module on this company still leaving past the paid-through date, from the
        # rows as they stand — the set ``_reprice_pending_extensions`` last priced against.
        # One the sweep has already closed (its access ran out while this waited) still
        # counts: it shared the pieces before it went.
        ends = {
            (r.function_code or "").upper(): r.app_access_until
            for r in store.module_rows_for_entity(row.entity_id)
            if r.phase in (PHASE_SCHEDULED_CANCEL, PHASE_CANCELLED)
            and getattr(r, "first_billed_at", None) is not None
            and getattr(r, "app_access_until", None) is not None
            and r.app_access_until > start
        }
        ends[code] = end
        period = period_containing(anchor, start - timedelta(seconds=1))
        pieces = _extension_pieces(code, ends, period, start)
        rates = {rate for _s, _e, rate, _c in pieces}
        charged = max(0, sum(charge for _s, _e, _r, charge in pieces))
        if len(rates) == 1 and charged == int(getattr(row, "extension_amount", 0) or 0):
            return start, end, rates.pop()
        return start, end, None
    except Exception:
        logger.exception(
            "renewal: could not work out the terms of the pending extension {} {}; "
            "recording its line without them",
            getattr(row, "entity_id", None),
            getattr(row, "function_code", None),
        )
        return start, end, None


def _reprice_pending_extensions(
    entity, payer_user_id, *, also_leaving=(), no_longer_leaving=()
) -> None:
    """Re-price every not-yet-billed extension on this entity, in place.

    The price of a cancellation depends on what else is leaving with it, and that is not
    settled when the customer clicks: cancelling a SECOND module makes the first one part
    of a pair, and reinstating one leaves the other alone again. Whichever way it moves,
    the figure has to be the one the renewal will actually collect, so it is rewritten
    here rather than left as whatever the first dialog said.

    Under the sorted allocation (``_leaving_marginal``) the direction depends on where the
    newcomer sorts. Cancelling Payment Request beside an already-pending Petty Cash makes
    Payment Request the first code: it takes the standalone 280 and Petty Cash is re-priced
    DOWN to the 120 step. The pair still totals the 400 bundle, which is what the dialog
    quotes (``leaving_total``) and what the renewal collects.

    Only ``pending`` rows are touched. Once an extension is INVOICED the money has moved,
    and a bill already sent is not re-priced by a later click — reinstating then charges
    the uncovered remainder instead (``_bill_reinstatement_in_house``).

    ``also_leaving`` ({code: access_end}) / ``no_longer_leaving`` name the module whose
    phase this very call is a consequence of, rather than re-reading it back off the row. The write has happened,
    but "re-read what I just wrote" is a property of the session's identity map, and the
    price two customers are charged should not rest on that.
    """
    from billing.services.billing import period_containing

    now = clock.now()
    rows = list(store.module_rows_for_entity(entity.id))
    anchor, _currency = store.billing_cycle_for_user(payer_user_id)
    # The anchor is the payer's — one cycle — but what has been PAID FOR belongs to the
    # card this company is on.
    paid_through = store.paid_through_for_entity(entity.id)
    if anchor is None or paid_through is None:
        return

    also = {str(c).upper(): end for c, end in dict(also_leaving or {}).items()}
    gone = {str(c).upper() for c in no_longer_leaving}
    leaving = (_leaving_codes(rows, paid_through, now) | set(also)) - gone
    if not leaving:
        return

    period = period_containing(anchor, paid_through - timedelta(seconds=1))
    # Every leaver's access end, because each one's price depends on when the OTHERS stop
    # — see _segmented_extension. The module this call is about supplies its own, for the
    # same reason it supplies its own code: what was just written is not re-read.
    ends = {
        (r.function_code or "").upper(): getattr(r, "app_access_until", None)
        for r in rows
        if (r.function_code or "").upper() in leaving
    }
    for c, end in also.items():
        if c in leaving and not ends.get(c):
            ends[c] = end
    for r in rows:
        code = (r.function_code or "").upper()
        if code not in leaving or r.extension_state != EXT_PENDING:
            continue
        access_end = ends.get(code)
        if access_end is None:
            continue
        amount = _segmented_extension(code, ends, period, paid_through)
        if amount == (getattr(r, "extension_amount", None) or 0):
            continue
        store.upsert_module_row(
            entity.id,
            code,
            r.payer_user_id or payer_user_id,
            extension_amount=amount if amount > 0 else None,
            extension_state=EXT_PENDING if amount > 0 else None,
        )
        logger.info(
            "billing: re-priced the pending extension for {} {} to {} "
            "(leaving: {})",
            entity.id,
            code,
            amount,
            ",".join(sorted(leaving)),
        )


def _paid_cancel_terms(entity, user, code: str, row, also_cancelling=()) -> dict:
    """What cancelling this PAID module would cost and when access would end.

    PURE — reads only. ``_cancel_module_in_house`` calls this and then writes exactly
    what it returns, and ``preview_cancel_module`` calls it to fill the confirmation
    dialog. That shared call is the point: the figure the user is shown before
    confirming and the figure recorded on the row cannot be computed two ways.

    Returns the payer id, the access end, the prorated extension charge, and the modules
    that would survive.
    """
    from billing.services.billing import (
        cancel_access_end,
        period_containing,
    )

    payer_user_id = (getattr(row, "payer_user_id", None)) or getattr(user, "id", None)
    now = clock.now()
    rows = list(store.module_rows_for_entity(entity.id))

    # Only a module still billing forward can be cancelled. This is the guard against
    # cancelling the same module twice — which would record a SECOND extension on the
    # row — and it is deliberately not the set the price is taken from below.
    live = {
        r.function_code.upper() for r in rows if access.is_billing_forward(phase=r.phase)
    }
    if code not in live:
        raise CheckoutError(f"Module {code} isn't currently subscribed.", status=409)

    anchor, currency = store.billing_cycle_for_user(payer_user_id)
    # Priced against what THIS company is paid through — the card it is billed on — while
    # the period boundaries still come from the payer's one anchor.
    paid_through = store.paid_through_for_entity(entity.id)
    if anchor is None or paid_through is None:
        raise CheckoutError("This entity has no billing account yet.", status=409)

    access_end = cancel_access_end(
        paid_through, now, extension_days=policy.current().paid_cancel_access_days
    )

    # The extension is priced against the modules that are LEAVING — this one plus any
    # already winding down — not against the line the entity is keeping.
    #
    # A module on its own is worth its list price: nothing else is leaving with it, so
    # nothing offsets it. Two leaving together are worth the BUNDLE they still were,
    # allocated in sorted code order — 280 to the first, the 120 step to the second.
    # ``also_cancelling`` is the rest of ONE decision — the other modules the customer is
    # dropping in the same click. Without it each preview prices its module as if it were
    # leaving alone (280 each) while the cancellations, run in sequence, re-price them to
    # 280 + 120: the dialog would quote 560 where the invoice collects 400.
    leaving = (
        _leaving_codes(rows, paid_through, now)
        | {code}
        | {str(c).upper() for c in (also_cancelling or ())}
    )

    # What still BILLS after this, which is a different question and the one the
    # confirmation dialog asks ("your other modules are unaffected" / "this is the last
    # one"). A module already winding down is not an answer to it.
    remaining = live - {code}
    # Returned for the dialog's "your subscription then continues at ..." line, so it
    # follows what still BILLS.
    plan_after = store.billing_plan_for_codes(remaining) if remaining else None
    period = period_containing(anchor, paid_through - timedelta(seconds=1))

    # Every leaver's access end, this one included. A module going in the SAME click has
    # none recorded yet — it gets the one being computed here, because the window is the
    # same rule applied at the same moment; already-cancelled ones carry their own, which
    # is how a module cancelled a day earlier stops a day earlier.
    also_upper = {str(c).upper() for c in (also_cancelling or ())}
    ends = {code: access_end}
    for r in rows:
        r_code = (r.function_code or "").upper()
        if r_code == code or r_code not in leaving:
            continue
        ends[r_code] = getattr(r, "app_access_until", None) or (
            access_end if r_code in also_upper else None
        )

    amount = _segmented_extension(code, ends, period, paid_through)

    # What the INVOICE will hold, which is not this module's share when something else is
    # leaving with it: confirming this cancellation also RE-PRICES the modules already
    # winding down (a lone 280 becomes one share of a 400 pair), so quoting only this row
    # would name a figure that is never billed on its own and understate what is due.
    # The plan covering the leaving set names it — "Super Minty" for the pair.
    plan_leaving = store.billing_plan_for_codes(leaving)
    leaving_total = sum(
        _segmented_extension(c, ends, period, paid_through) for c in ends
    )

    return {
        "payer_user_id": payer_user_id,
        "access_end": access_end,
        "paid_through": paid_through,
        "amount": amount,
        "currency": currency,
        "remaining": sorted(remaining),
        "plan_after": plan_after,
        # The cancellation as the invoice will state it: everything leaving together,
        # under the name of the plan that covers it.
        "leaving": sorted(leaving),
        "leaving_label": (plan_leaving.display_name if plan_leaving else None),
        "leaving_total": leaving_total,
    }


def _cancel_module_in_house(entity, user, code: str, row, reason=None) -> dict:
    """Cancel a PAID module when Minty does the billing.

    Same promise as the Stripe path — access until ``max(paid_through, now + 30 days)``,
    and the days beyond what was paid for are charged — but nothing is collected here.
    The amount is RECORDED on the row and picked up by the next renewal run
    (``renewals._pending_extension_lines``).

    That deferral is the point, not an optimisation. Charging at cancellation would make
    leaving depend on a card clearing, so an expired card could trap somebody in a
    subscription. Recording it cannot fail.

    No Stripe object is touched: there is no line to swap down and no £0 grace price to
    park on, because the subscription those existed to keep alive is not what bills this
    payer any more.
    """
    # Read NOW, not at the audit call below: ``upsert_module_row`` writes through the
    # same identity-mapped object, so by then ``row.phase`` is already the new phase and
    # the log would record scheduled_cancel -> scheduled_cancel.
    phase_before = getattr(row, "phase", None)

    terms = _paid_cancel_terms(entity, user, code, row)
    payer_user_id = terms["payer_user_id"]
    access_end = terms["access_end"]
    amount = terms["amount"]

    fields = {
        "phase": PHASE_SCHEDULED_CANCEL,
        "app_access_until": access_end,
    }
    if amount > 0:
        fields["extension_amount"] = amount
        fields["extension_state"] = EXT_PENDING
    store.upsert_module_row(entity.id, code, payer_user_id, **fields)

    # This module joining the ones already leaving changes what THEY are worth: the pair
    # is now worth the 400 bundle between them, and the shares fall out of sorted code
    # order, so an already-pending row can move either way. Re-priced now, so the rows
    # carry what the renewal will collect rather than what the first dialog happened to say.
    _reprice_pending_extensions(
        entity, payer_user_id, also_leaving={code: access_end}
    )

    store.record_action(
        entity_id=entity.id,
        function_code=code,
        payer_user_id=payer_user_id,
        actor_user_id=getattr(user, "id", None),
        action=AUDIT_CANCEL,
        outcome=OUTCOME_SUCCEEDED,
        # Captured before the upsert above. Omitting it logged
        # "NULL -> scheduled_cancel" for every PAID cancellation while the trial path
        # logged "trial -> scheduled_cancel" — so the state was missing on exactly the
        # cancellations where money moved, in the table meant to answer "why was I
        # charged?".
        phase_before=phase_before,
        phase_after=PHASE_SCHEDULED_CANCEL,
        app_access_until=access_end,
        extension_amount=amount or None,
        extension_state=EXT_PENDING if amount > 0 else None,
        cancel_reason=reason,
        note="cancelled in-house; extension recorded for the next invoice",
    )
    return {
        "access_end": access_end,
        "extension_state": EXT_PENDING if amount > 0 else None,
    }


def _clean_cancel_reason(reason) -> str | None:
    """Normalise the free-text reason from the cancellation dialog.

    Trimmed to the audit column's width rather than rejected: the reason is a courtesy
    the customer typed on their way out, and refusing a cancellation because it was too
    long would be absurd. Blank (or whitespace) becomes None — "didn't say" is the
    honest record, and an empty string in the column reads as if they had.
    """
    text = (reason or "").strip()
    return text[:500] or None


def cancel_module(entity, user, function_code: str, reason=None) -> dict:
    """Cancel ONE module under the prorated access-extension rule.

    ``reason`` is the customer's own words from the cancellation dialog, optional on
    every path. It is recorded in ``subscription_audit_log`` — history, not state, so
    every cancellation keeps its own — and never affects what happens here; nobody has
    to justify leaving to be allowed to.

    Cancelling one of two modules drops the entity to the survivor's price (400 -> 280);
    cancelling the last one ends the entity's billing altogether.

    * Access runs until ``max(paid_through, now + PAID_CANCEL_ACCESS_DAYS)`` — the
      window only ever EXTENDS access, never shortens what was already paid for. It is
      recorded on the row as ``app_access_until``, the single access-end authority.
    * The extra days fall AFTER the billing anchor, so they are owed. They are priced
      against what is LEAVING (see ``_leaving_marginal``): a module going on its own bills
      its list price, and a pair going together bills the bundle between them, allocated
      in sorted code order. Prorated, recorded on the row as ``extension_amount`` and
      collected by the next renewal run. Nothing is charged up-front, so cancelling never
      depends on a card clearing and never aborts.

    An app-level trial has nothing to prorate, but it follows the same shape:
    cancelling SCHEDULES the cancellation rather than ending it. See below.

    Returns ``{access_end, extension_state}``.
    """
    code = (function_code or "").strip().upper()
    if not code:
        raise CheckoutError("A module is required to cancel.")
    user_id = getattr(user, "id", None)
    why = _clean_cancel_reason(reason)

    row = store.module_row(entity.id, code)
    # App-level trial: no Stripe object at all, and no charge either way.
    #
    # Cancelling must NOT end the trial. It only means "don't convert me to paid" —
    # the user keeps the free days they were given, exactly as a cancelled PAID module
    # keeps the days it was charged for. So this schedules the cancellation: access runs
    # to trial_end (``app_access_until``), the trial-end job then expires instead of
    # converting it (see ``_convert_due_trials``), and the user can change their mind any
    # time before that date (``renew_module``).
    if row is not None and row.phase == PHASE_TRIAL:
        now = clock.now()
        access_end = row.trial_end
        if access_end is None or access_end <= now:
            # Nothing left to keep — an overdue trial the end-job hasn't swept yet.
            # Expiring it now is the same outcome, just sooner.
            _expire_due_trial(row)
            store.record_action(
                entity_id=entity.id,
                function_code=code,
                payer_user_id=row.payer_user_id,
                actor_user_id=user_id,
                action=AUDIT_CANCEL,
                outcome=OUTCOME_SUCCEEDED,
                phase_before=PHASE_TRIAL,
                phase_after=PHASE_EXPIRED,
                cancel_reason=why,
                note="app-level trial cancelled after its end date; expired immediately",
            )
            return {"access_end": None, "extension_state": None}

        # Access deliberately left ENABLED — the trial keeps running.
        store.upsert_module_row(
            entity.id,
            code,
            row.payer_user_id,
            phase=PHASE_SCHEDULED_CANCEL,
            app_access_until=access_end,
        )
        store.record_action(
            entity_id=entity.id,
            function_code=code,
            payer_user_id=row.payer_user_id,
            actor_user_id=user_id,
            action=AUDIT_CANCEL,
            outcome=OUTCOME_SUCCEEDED,
            phase_before=PHASE_TRIAL,
            phase_after=PHASE_SCHEDULED_CANCEL,
            app_access_until=access_end,
            cancel_reason=why,
            note="app-level trial cancelled; access runs to trial end, will not convert",
        )
        return {"access_end": access_end, "extension_state": None}

    # A PAID module. Access continues to app_access_until and the extension is
    # recorded for the next renewal to collect, so cancelling never depends on a
    # card clearing.
    return _cancel_module_in_house(entity, user, code, row, reason=why)


def preview_subscribe_modules(entity, user, codes) -> dict:
    """What subscribing ``codes`` would charge TODAY, and what it costs from then on.

    The dialog before a purchase, and the counterpart of ``preview_cancel_module``: reads
    only, and quotes the figure the charge itself will use rather than a price list.

    "Charged today" is NOT the plan price whenever the payer already has a cycle running.
    An entity joining mid-period pays for the days left in it, and one adding a second
    module pays the difference to the bundle — both of which come out of
    ``changes.build_change``, the same calculation ``_bill_module_change_in_house``
    bills. A payer with no anchor yet starts their cycle here, so the first period is
    charged in full; that is the branch that quotes the plain plan price.

    Returns the plan being bought, what it covers, today's charge, the monthly rate that
    follows, and the saving against buying the modules separately.
    """
    from billing.services import changes
    from billing.services.billing import period_containing

    wanted = {str(c).strip().upper() for c in (codes or []) if str(c).strip()}
    if not wanted:
        raise CheckoutError("No modules to subscribe to.")

    payer_user_id = store.payer_for_entity(entity.id) or getattr(user, "id", None)
    current = _billed_codes_in_house(entity.id)
    target = current | wanted
    plan_target = store.billing_plan_for_codes(target)
    if plan_target is None:
        raise CheckoutError("No available subscription plan for those modules.")

    currency = getattr(plan_target, "currency", None) or ""
    now = clock.now()
    anchor, cycle_currency = store.billing_cycle_for_user(payer_user_id)
    currency = cycle_currency or currency

    if anchor is None:
        # Nothing has ever been billed for this payer: this purchase starts the cycle, so
        # the period is charged in full rather than prorated against one they were never
        # part of. Mirrors _bill_module_change_in_house's own no-anchor branch.
        charged_today = int(plan_target.amount)
    else:
        period = period_containing(anchor, now)
        invoice = changes.build_change(
            entity.id, getattr(entity, "name", None) or str(entity.id),
            current, target, period, now,
        )
        charged_today = int(getattr(invoice, "total", 0) or 0) if invoice else 0

    # What the same modules would cost bought one by one — the bundle IS the discount, so
    # this is the only place the saving can come from.
    separate = 0
    names = []
    for code in sorted(target):
        one = store.billing_plan_for_codes({code})
        if one is not None:
            separate += int(one.amount)
            names.append(one.display_name)

    monthly = int(plan_target.amount)
    saving = max(0, separate - monthly)
    return {
        "codes": sorted(target),
        "label": plan_target.display_name,
        "modules": names,
        "currency": currency,
        "charged_today": charged_today,
        "charged_today_formatted": (
            money.format_minor(charged_today, currency) if charged_today else None
        ),
        "monthly": monthly,
        "monthly_formatted": money.format_minor(monthly, currency),
        "saving": saving,
        "saving_formatted": money.format_minor(saving, currency) if saving else None,
        # Whether confirming also has to record this company's consent — the payer's card
        # is shared, so the first charge on a company is authorised explicitly. The card
        # is named in that case for the same reason ``_billing_confirmation`` names it:
        # authorising a charge without saying which card it lands on is half a disclosure.
        "needs_consent": not store.has_billing_consent(entity.id, payer_user_id),
        "card": _preview_card_display(payer_user_id, entity.id),
    }


def _preview_card_display(payer_user_id, entity_id=None) -> str | None:
    """"Visa •••• 4242" for the card THIS company will be charged on, or None.

    The company's nomination, falling back to the account's main card only when there is
    no company in the question at all. That fallback is a DISPLAY convenience and not a
    billing one: nothing charges an unnominated company, so a dialog naming the account
    default for one would be describing a charge that will not happen. Which is why the
    caller passes the entity.

    Never raises: this only decorates a dialog, and a Stripe hiccup must not stop the
    customer subscribing.
    """
    try:
        method = store.card_for_entity(entity_id) if entity_id else None
        if method is None and entity_id is None:
            customer_id = (
                store.customer_id_for_user(payer_user_id) if payer_user_id else None
            )
            method = customer_default_payment_method(customer_id) if customer_id else None
        card = payment_method_display(method) if method else None
    except Exception:
        logger.exception(
            "billing: could not read the saved card for payer {}", payer_user_id
        )
        return None
    if not card:
        return None
    # THE SAME WORDS AS EVERY CARD ROW IN THE PRODUCT. It used to read "visa ending 5556"
    # — a lowercase brand and a fourth way of saying the same thing, on the one dialog
    # that takes money the moment it is confirmed. Built from the same helper the picker
    # rows use so the two cannot drift again.
    from billing.services.payment_methods import _brand_label

    last4 = card.get("last4")
    if not last4:
        return _brand_label(card.get("brand"))
    return f"{_brand_label(card.get('brand'))} •••• {last4}"


def preview_reinstate_modules(entity, user, codes) -> dict:
    """What resuming ``codes`` would charge NOW. Reads only.

    Resuming is the one action in this flow that takes money on the spot: the extension
    paid up to ``app_access_until`` and the renewal skipped the module for the rest of the
    period, so the customer owes from one to the other — collected up front, because a
    purchase that waits until month end is one they can walk away from having already had
    the module. Until this preview existed, that charge happened with no dialog at all.

    Mirrors ``_bill_reinstatement_in_house`` step for step, including its two "charge
    nothing" branches: no anchor or no covered-to (nothing to prorate against) and an
    extension already covering the rest of the period. A module whose extension was never
    invoiced owes nothing either — deleting a pending number is free.

    Returns the plan being resumed, the charge, and the window it covers.
    """
    from billing.services import changes
    from billing.services.billing import period_containing

    wanted = {str(c).strip().upper() for c in (codes or []) if str(c).strip()}
    if not wanted:
        raise CheckoutError("No modules to resume.")

    rows = {
        (r.function_code or "").upper(): r
        for r in store.module_rows_for_entity(entity.id)
    }
    payer_user_id = next(
        (rows[c].payer_user_id for c in wanted if c in rows and rows[c].payer_user_id),
        None,
    ) or getattr(user, "id", None)

    plan_target = store.billing_plan_for_codes(
        _billed_codes_in_house(entity.id) | wanted
    )
    label_plan = store.billing_plan_for_codes(wanted)
    anchor, currency = store.billing_cycle_for_user(payer_user_id)
    currency = currency or getattr(plan_target, "currency", None) or ""

    # A cancelled TRIAL is winding down like a paid module and resumes through the same
    # tick, but nothing about the money is the same: nothing was ever charged, so there
    # are no paid days covering anything, and billing starts when the TRIAL ends rather
    # than at the period end. Saying otherwise is the one thing this dialog must not do.
    trials = [
        c for c in wanted
        if c in rows and getattr(rows[c], "first_billed_at", None) is None
    ]
    trial_ends = [
        getattr(rows[c], "trial_end", None) for c in trials
        if getattr(rows[c], "trial_end", None)
    ]
    kind = (
        "trial" if len(trials) == len(wanted)
        else ("paid" if not trials else "mixed")
    )

    charged = 0
    covers_from = None
    covers_to = None
    if anchor is not None:
        period = period_containing(anchor, clock.now())
        covers_to = period.end
        # ``before`` is the line WITHOUT this module, exactly as
        # _bill_reinstatement_in_house builds it — the covered set minus the one being
        # put back.
        #
        # It IS a running accumulation, because the commit is a sequence: the dialog
        # posts one list, and the JS then calls /renew once per module. The first call
        # sees the line as it stands; by the second, ``_reactivate_module_in_house`` has
        # already flipped the first module to ACTIVE, so it is genuinely on the line.
        # Seeding this with ``| wanted`` instead priced every module as though the others
        # were already back — two modules resumed after a renewal quoted the 120 step
        # twice while the two /renew calls collected 280 then 120.
        covered = _billed_codes_in_house(entity.id)
        for code in sorted(wanted):
            row = rows.get(code)
            covered_to = getattr(row, "app_access_until", None) if row else None
            invoiced = (
                getattr(row, "extension_state", None) == EXT_INVOICED if row else False
            )
            # Added whatever happens below, including the "charge nothing" branches: the
            # commit path writes ``phase=ACTIVE`` whether or not it billed, so the next
            # module in the sequence sees this one on the line either way.
            before = covered - {code}
            covered.add(code)
            if not invoiced or covered_to is None or covered_to >= period.end:
                continue
            invoice = changes.build_change(
                entity.id, getattr(entity, "name", None) or str(entity.id),
                before, before | {code}, period, covered_to,
            )
            charged += int(getattr(invoice, "total", 0) or 0) if invoice else 0
            covers_from = min(covers_from or covered_to, covered_to)

    # WHAT IT COSTS FROM NEXT PERIOD, which is a different set from what is charged today.
    #
    # ``plan_target`` above is built from ``_billed_codes_in_house`` — modules PAID FOR
    # this period — and that is right for the charge now: a module winding down was paid
    # for, so resuming beside it is an upgrade to the bundle they still are until it goes.
    # Only until the renewal that drops it, mind: past that the predicate stops counting
    # it and resuming is the fresh join it actually is.
    #
    # It is wrong for the ongoing price, because a winding-down module will not be there.
    # On an entity with both modules cancelling, resuming one quoted "HKD 400.00/mo" — the
    # bundle — when the other was about to lapse and the real ongoing cost is 280. The
    # customer is told they will pay 120/month more than they will.
    #
    # So the recurring figure is built from what will still be BILLING FORWARD once this
    # resume lands: whatever is active or past due, plus what is being resumed.
    from billing.services import access

    ongoing_codes = {
        code for code, row in rows.items()
        if access.is_billing_forward(phase=getattr(row, "phase", ""))
    } | wanted
    ongoing_plan = store.billing_plan_for_codes(ongoing_codes)
    monthly = int(ongoing_plan.amount) if ongoing_plan else 0
    return {
        "codes": sorted(wanted),
        "label": (label_plan.display_name if label_plan else None),
        # trial | paid | mixed — which sentences the dialog is allowed to say.
        "kind": kind,
        # When a resumed trial starts costing money. The earliest, because that is the
        # first date something is charged.
        "trial_end": min(trial_ends) if trial_ends else None,
        "currency": currency,
        "charged_today": charged,
        "charged_today_formatted": (
            money.format_minor(charged, currency) if charged else None
        ),
        # The window the charge covers — from where the cancellation's access ran out to
        # the end of the period the entity is in.
        "covers_from": covers_from,
        "covers_to": covers_to,
        "monthly": monthly,
        "monthly_formatted": money.format_minor(monthly, currency) if monthly else None,
    }


def preview_cancel_module(entity, user, function_code: str, also_cancelling=()) -> dict:
    """What cancelling would do, WITHOUT doing it — for the confirmation dialog.

    Answers the two questions the dialog exists to answer: when does access end, and is
    there anything left to pay. Reads only; safe to call on render or on hover.

    Deliberately mirrors ``cancel_module`` branch for branch, and shares
    ``_paid_cancel_terms`` with the paid path, so the dialog cannot quote a date or an
    amount that the cancellation then contradicts. Anything that changes the rule has to
    change it for both.

    Returns::

        {kind, access_end, amount, amount_formatted, currency, charged_now,
         remaining, remaining_amount, error}

    ``kind`` is one of ``trial`` (free days kept, nothing owed), ``trial_expired``
    (nothing left to keep — cancelling ends it now), ``paid``, or ``none`` when the
    module isn't cancellable at all. ``charged_now`` is always False: nothing is
    collected at cancellation, by design.
    """
    from billing.services import money

    code = (function_code or "").strip().upper()
    if not code:
        raise CheckoutError("A module is required to cancel.")

    row = store.module_row(entity.id, code)
    if row is None:
        return {"kind": "none", "error": "This module isn't subscribed."}

    base = {
        "code": code,
        "amount": 0,
        "amount_formatted": None,
        "currency": None,
        # The guaranteed-access window, so the dialog can name the number instead of
        # hardcoding "30 days" in the template. It is a tunable policy lever, and copy
        # that states it from memory is copy that goes wrong the day it is tuned.
        "access_days": policy.current().paid_cancel_access_days,
        # Nothing is charged at cancellation on any path — the extension is recorded
        # and collected by the next renewal run. The dialog says so rather than
        # implying a card is about to be hit.
        "charged_now": False,
        "remaining": [],
        "remaining_amount": None,
        "error": None,
    }

    if row.phase == PHASE_TRIAL:
        now = clock.now()
        access_end = row.trial_end
        if access_end is None or access_end <= now:
            return {**base, "kind": "trial_expired", "access_end": None}
        # The free days are kept: cancelling a trial only means "don't convert me".
        return {**base, "kind": "trial", "access_end": access_end}

    terms = _paid_cancel_terms(entity, user, code, row, also_cancelling)
    currency = terms["currency"]
    amount = terms["amount"]
    plan_after = terms["plan_after"]

    return {
        **base,
        "kind": "paid",
        "access_end": terms["access_end"],
        "paid_through": terms["paid_through"],
        "amount": amount,
        "amount_formatted": money.format_minor(amount, currency) if amount else None,
        "currency": currency,
        "remaining": terms["remaining"],
        # What the entity drops to once this module goes — the survivor's price, not
        # the bundle's. Shown so "you'll keep paying" is a number, not an implication.
        "remaining_amount": (
            money.format_minor(plan_after.amount, currency) if plan_after else None
        ),
        # The whole cancellation as it will appear on the invoice: every module leaving
        # together, under the name of the plan covering them. When something else is
        # already winding down, confirming this re-prices it too, so THIS is the figure
        # the customer is agreeing to — ``amount`` alone is one share of it.
        "leaving_label": terms["leaving_label"],
        "leaving_total": terms["leaving_total"],
        "leaving_total_formatted": (
            money.format_minor(terms["leaving_total"], currency)
            if terms["leaving_total"]
            else None
        ),
        "leaving_count": len(terms["leaving"]),
    }


def _reactivate_trial(entity, user, code: str, row) -> None:
    """Un-cancel an app-level trial: put it back in the trial phase so it converts again.

    Only valid while the trial still has time left. Once ``trial_end`` has passed the
    free days are gone and there is nothing to resume — the user has to subscribe, which
    is a purchase and must not be reachable by clicking Renew. (Cancelling and
    un-cancelling never moved money, so unlike the paid path there's no extension to
    reverse and no charge to credit.)
    """
    now = clock.now()
    if row.trial_end is None or row.trial_end <= now:
        raise CheckoutError(
            f"The free trial for {code} has already ended — subscribe to keep it.",
            status=409,
        )

    store.upsert_module_row(
        entity.id,
        code,
        row.payer_user_id,
        phase=PHASE_TRIAL,
        app_access_until=row.trial_end,
    )
    # Access was never revoked by the cancel, but re-assert it: if the trial lapsed and
    # was re-cancelled, or a sweep ran against a stale row, this is what puts it back.
    _set_module_access(entity.id, code, True)
    store.record_action(
        entity_id=entity.id,
        function_code=code,
        payer_user_id=row.payer_user_id,
        actor_user_id=getattr(user, "id", None),
        action=AUDIT_UNCANCEL,
        outcome=OUTCOME_SUCCEEDED,
        phase_before=PHASE_SCHEDULED_CANCEL,
        phase_after=PHASE_TRIAL,
        app_access_until=row.trial_end,
        note="app-level trial un-cancelled; will convert at trial end again",
    )


def _reactivate_module_in_house(entity, user, code: str, row) -> None:
    """Undo a scheduled cancellation when Minty does the billing.

    Much simpler than the Stripe path, because the extension was never charged. It was
    RECORDED on the row for the next renewal run to collect, so undoing it is deleting a
    number nobody has been billed for — no invoice item to remove, no credit note, no
    ``cancel_at`` to clear, and no money moved in either direction.

    Once a run has COLLECTED the extension, undoing is not free. The extension paid for
    access up to ``app_access_until`` and the renewal SKIPPED this module for the rest of
    the period (a cancelling module is not billing-forward), so the window from there to
    the period end is covered by nobody. Reinstating without charging it hands the
    customer the rest of the month.

    That gap is charged NOW rather than queued onto the next invoice. Cancelling defers
    deliberately — leaving must never depend on a card clearing — but reinstating is a
    PURCHASE, and a purchase that waits until month end is one the customer can walk away
    from having already had the module. So it is collected up front, and a decline leaves
    the module cancelled rather than granting it unpaid.

    The extension itself is NOT reversed: those days were used, at the marginal rate they
    would have cost anyway. Crediting them and re-charging the same window would move
    money twice to reach the same place, and put two lines on the invoice that only make
    sense read together.
    """
    if row.extension_state == EXT_INVOICED:
        _bill_reinstatement_in_house(entity, user, code, row)

    fields = {
        "phase": PHASE_ACTIVE,
        "app_access_until": None,
    }
    if row.extension_state != EXT_INVOICED:
        # Still pending: nobody was billed, so this is deleting a number.
        fields["extension_amount"] = None
        fields["extension_state"] = None

    store.upsert_module_row(entity.id, code, row.payer_user_id, **fields)
    _set_module_access(entity.id, code, True)
    # This module leaving the leaving-set changes what the rest are worth: whatever stays
    # cancelled is now alone, and alone costs its list price rather than a pair's margin.
    _reprice_pending_extensions(entity, row.payer_user_id, no_longer_leaving={code})
    store.record_action(
        entity_id=entity.id,
        function_code=code,
        payer_user_id=row.payer_user_id,
        actor_user_id=getattr(user, "id", None),
        action=AUDIT_UNCANCEL,
        outcome=OUTCOME_SUCCEEDED,
        phase_before=PHASE_SCHEDULED_CANCEL,
        phase_after=PHASE_ACTIVE,
        note=(
            "reactivated in-house; the uncovered remainder of the period was charged"
            if row.extension_state == EXT_INVOICED
            else "reactivated in-house; pending extension discarded before it was billed"
        ),
    )


def reactivate_module(entity, user, function_code: str) -> None:
    """Undo a scheduled cancellation for ONE module.

    Puts the module back on the entity's line (swapping 280 -> 400 as it rejoins the
    other module), clears any pending whole-subscription cancellation, and reverses the
    extension charge — deleted if it never billed, credited if it already did (see
    ``_reverse_extension``).

    A cancelled app-level TRIAL is handled separately below: it has no Stripe object, no
    extension and no charge to reverse, so un-cancelling is just putting the row back in
    the trial phase.
    """
    code = (function_code or "").strip().upper()
    row = store.module_row(entity.id, code)
    # A past-due module has nothing to un-cancel: it is live and in arrears, and what it
    # needs is a card the retry can use. The card no longer offers Renew for it, but a
    # page left open before that fix — or a stale tab — still can, so answer with the
    # action that actually helps rather than "isn't scheduled to cancel", which is true
    # and tells the customer nothing.
    if row is not None and row.phase == PHASE_PAST_DUE:
        raise CheckoutError(
            f"Module {code} is past due, not cancelled. Update your payment method to "
            "settle it.",
            status=409,
        )
    if row is None or row.phase != PHASE_SCHEDULED_CANCEL:
        raise CheckoutError(f"Module {code} isn't scheduled to cancel.", status=409)

    # A cancelled app-level trial: nothing in Stripe exists for it (no line, no
    # extension, no cancel_at), so none of the Stripe reversal below applies — running
    # it would fail on the missing billing account for an entity that has never paid.
    #
    # Asked of ``first_billed_at``, NOT of ``stripe_subscription_item_id``: only the
    # Stripe biller sets an item id, so in-house every paid module took this branch and
    # un-cancelling one was refused with "your free trial has already ended". The old
    # ``trial_end is not None`` half is gone because it is true of every row — a paid
    # module that began as a trial keeps its trial_end forever.
    if row.first_billed_at is None:
        _reactivate_trial(entity, user, code, row)
        return

    _reactivate_module_in_house(entity, user, code, row)


def start_trials_for_enabled_modules(entity, user) -> list:
    """Start app-level trials for every enabled module the entity hasn't used one on
    (called at the end of onboarding).

    Needs no customer and no card — the wizard lets a user skip the card, and that only
    matters when the trial ends (convert vs expire). Modules that already have a trial
    or a subscription are skipped rather than raising, since this runs over whatever the
    wizard enabled.
    """
    # Lazy import: the entity module service pulls in the full model graph.
    from billing.services.entity_modules import get_enabled_modules_for_entities

    enabled = get_enabled_modules_for_entities([entity.id]).get(entity.id, set())
    if not enabled:
        return []

    created = []
    for code in sorted(enabled):
        plan = catalog.plan_for_module(code)
        if plan is None:
            continue
        row = start_module_trial(entity, user, plan)
        if row is not None:
            created.append(row)
    return created
