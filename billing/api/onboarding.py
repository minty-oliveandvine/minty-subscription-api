"""The wizard's money routes, and the trial start.

Seven of the nine ``/api/onboarding/{payment-method*,billing/*}`` routes of Flask's
``entity/routes/create.py`` (lines 757-1247), which onboarding-backend proxies here from
Part 2 step 5 (``core/billing_client.py`` there, forwarding the caller's bearer). Plus one
new route, ``POST /trials/start {entity_id} -> {trial_end}``: onboarding-backend's native
``finalize`` flips the company live and then calls this; **it must not fail silently** - a
failure here fails finalize, the All Set screen offers Try again, and both halves are
idempotent (a trial already started is returned, not duplicated).

Plain ``BearerAuth`` (the API default): the wizard's token names a person and no company, and
each route reads the company from the query or the body and checks the caller's MEMBERSHIP of
it (``_entity_for_member``: 400 without an id, 403 for a stranger, 404 for no such company) -
Flask's rule, kept. The four billing-sheet card routes act on the PAYER, not on a company (a
card belongs to the person), so they check nothing about an entity. Bodies, answers and
status codes are Flask's.

Flask's other two, ``POST /payment-method/setup`` and ``/payment-method/complete`` (a
Stripe-hosted setup-mode Checkout), were DELETED on 2026-10-01: a card is only ever added
through a billing account, in-app (``/billing/payment-methods/setup-intent`` + ``confirm``,
which refuses a confirm naming no account).
"""

from __future__ import annotations

from ninja import Router

from billing.api._json import body, error, respond, user_id
from billing.services._log import logger
from billing.services.store import _by_pk
from shared_models.models import Entity, UserEntity

onboarding_router = Router()

#: (method, path) - the contract, pinned by billing/tests/test_contract.py.
ROUTES = (
    ("GET", "/payment-method"),
    ("GET", "/billing/payment-methods"),
    ("POST", "/billing/payment-methods/setup-intent"),
    ("POST", "/billing/payment-methods/confirm"),
    ("POST", "/billing/payment-methods/default"),
    ("GET", "/billing/accounts"),
    ("POST", "/billing/accounts"),
    ("POST", "/billing/authorize"),
    ("POST", "/trials/start"),  # new in Part 2
)


class _Refused(Exception):
    """A membership check failed; carries the answer."""

    def __init__(self, response):
        super().__init__()
        self.response = response


def _entity_for_member(request, entity_id: str) -> Entity:
    """The entity, if the caller is a member of it - Flask's ``_entity_for_member``. Raises
    ``_Refused`` with the answer otherwise: 400 without an id, 403 for a stranger, 404 for a
    company that does not exist. Membership is a ``user_entity`` row, as Flask read it."""
    entity_id = str(entity_id or "").strip()
    if not entity_id:
        raise _Refused(error("entity_id is required", 400))
    try:
        member = UserEntity.objects.filter(user_id=user_id(request), entity_id=entity_id).exists()
    except Exception:  # noqa: BLE001 - a garbage id is "not a member", as in the store
        member = False
    if not member:
        raise _Refused(error("You don't have access to this entity", 403))
    entity = _by_pk(Entity, entity_id)
    if entity is None:
        raise _Refused(error("Entity not found", 404))
    return entity


# --- Step 2's billing status -----------------------------------------------------------------


@onboarding_router.get("/payment-method", summary="Whether this company can be billed")
def payment_method_status(request):
    """``?entity_id=`` -> ``{"has_payment_method", "has_billing_consent", "card"}``.

    Two separate questions, and the second is the one that matters: ``has_payment_method``
    is read live from Stripe and belongs to the PAYER, shared across every entity they pay
    for; consent is per (entity, payer) and decides whether the 30-day trial converts or
    lapses. ``card`` IS THIS ENTITY'S NOMINATED CARD, not the payer's default - null when
    nothing is nominated, and null is the honest answer. Neither gates the wizard. Stripe
    unreachable degrades to "no card" rather than 500; the consent read is local and
    deliberately outside that guard."""
    from billing.services import payment_methods
    from billing.services import store as sub_store
    from billing.services.checkout import entity_has_payment_method

    try:
        entity = _entity_for_member(request, request.GET.get("entity_id"))
    except _Refused as refused:
        return refused.response
    uid = user_id(request)

    try:
        has_pm = entity_has_payment_method(entity)
    except Exception:
        logger.exception("onboarding payment-method: status check failed for {}", entity.id)
        has_pm = False

    card = None
    try:
        nominated = sub_store.card_for_entity(str(entity.id), uid)
        if nominated:
            wallet = payment_methods.list_for_user(uid)
            card = next((m for m in wallet.get("methods", []) if m.get("id") == nominated), None)
    except Exception:
        logger.exception("onboarding payment-method: could not describe the card for {}", entity.id)

    return respond(
        {
            "has_payment_method": has_pm,
            "has_billing_consent": sub_store.has_billing_consent(str(entity.id), uid),
            "card": card,
        }
    )


# --- The billing sheet: the payer's cards and accounts ---------------------------------------
#
# Deliberate MIRRORS of the payer portal's ``/api/me/billing/payment-methods*`` (same service
# functions, the onboarding twins Flask kept apart for its per-origin CORS). They act on the
# PAYER, not on an entity - a card belongs to the person - so there is no membership to check.


def _billing_call(request, handler):
    """``payment_methods.run`` for the bearer's own account: 200 / 409 / 422 / 500."""
    from billing.services import payment_methods

    payload, status = payment_methods.run(handler, user_id(request))
    return respond(payload, status)


@onboarding_router.get("/billing/payment-methods", summary="Every card on the payer's account")
def billing_payment_methods(request):
    from billing.services import payment_methods

    return _billing_call(request, payment_methods.list_for_user)


@onboarding_router.post("/billing/payment-methods/setup-intent", summary="Open a SetupIntent for the card form")
def billing_setup_intent(request):
    """``{client_secret, publishable_key, setup_intent}``; needs no customer yet."""
    from billing.services import payment_methods

    return _billing_call(request, payment_methods.start_setup)


@onboarding_router.post("/billing/payment-methods/confirm", summary="Adopt the confirmed card")
def billing_confirm(request):
    """Body ``{setup_intent, make_default?, billing_group_id?, billing_email?,
    billing_company?}``. Saving a card AUTHORISES NOTHING - that is ``/billing/authorize``.
    A BILLING ACCOUNT IS REQUIRED: a group id puts the card on an account the payer already
    has (checked to be theirs), a company AND an email open a new one; naming neither is 422
    "Choose a billing account for this card." The payer portal's route and this one share
    ``payment_methods.confirm_into_account``, which refuses before anything is attached at
    Stripe."""
    from billing.services import payment_methods

    payload = body(request)
    return _billing_call(
        request,
        lambda uid: payment_methods.confirm_into_account(
            uid,
            str(payload.get("setup_intent") or "").strip(),
            make_default=bool(payload.get("make_default")),
            billing_group_id=payload.get("billing_group_id"),
            billing_email=payload.get("billing_email"),
            billing_company=payload.get("billing_company"),
        ),
    )


@onboarding_router.post("/billing/payment-methods/default", summary="Make one card the account's main one")
def billing_set_default(request):
    """Body ``{payment_method}``. Nominates nothing by itself; decides which card is offered
    first - including to ``/billing/authorize``."""
    from billing.services import payment_methods

    payment_method = str(body(request).get("payment_method") or "").strip()
    return _billing_call(request, lambda uid: payment_methods.set_default(uid, payment_method))


@onboarding_router.api_operation(["GET", "POST"], "/billing/accounts", summary="The payer's billing accounts; open one")
def billing_accounts(request):
    """GET -> ``{accounts: [{id, billing_email, billing_company, default_id, cards[]}], …}``.
    POST ``{payment_method, billing_email?, billing_company?}`` opens an account on a card the
    payer already holds (ownership proven first); it authorises nothing."""
    from billing.services import payment_methods

    if request.method == "GET":
        return _billing_call(request, payment_methods.accounts_for_user)

    payload = body(request)
    payment_method = str(payload.get("payment_method") or "").strip()
    if not payment_method:
        return error("A saved card is required to open a billing account.", 400)
    billing_email = payload.get("billing_email")
    billing_company = payload.get("billing_company")

    def _open(uid):
        from billing.services import store as sub_store
        from billing.services.billing_accounts import validate_identity

        # OWNERSHIP FIRST: a ``pm_...`` copied from anywhere else cannot open an account.
        payment_methods._owned(uid, payment_method)
        # The email rule before the write (printable ASCII; 422 in the form's words).
        email, _ = validate_identity(billing_email, None, require_both=False)
        account = sub_store.create_billing_account(
            uid, payment_method, billing_email=email, billing_company=billing_company
        )
        return {
            "id": account.id,
            "billing_email": account.billing_email,
            "billing_company": account.billing_company,
            "default_id": account.stripe_payment_method_id,
        }

    return _billing_call(request, _open)


@onboarding_router.post("/billing/authorize", summary="Record consent to bill this company")
def billing_authorize(request):
    """Body ``{entity_id, payment_method?}`` -> ``{"has_billing_consent": true}``. Charges
    nothing: the trial runs its term, and this is the difference between "converts" and
    "expires" at the end of it. ``payment_method`` is nominated BEFORE consent is recorded
    (``establish_payer=True`` - the one caller that does, because during the wizard no
    company has a payer yet and this request is what establishes one). A company on no billing
    account with no ``payment_method`` is refused 402 "Choose a billing account for this
    company." and nothing is recorded - there is no fallback to the customer's default card.
    Idempotent."""
    from billing.services import payment_methods
    from billing.services.checkout import CheckoutError, authorize_entity_billing

    payload = body(request)
    try:
        entity = _entity_for_member(request, payload.get("entity_id"))
    except _Refused as refused:
        return refused.response
    uid = user_id(request)

    pm_id = str(payload.get("payment_method") or "").strip()
    try:
        if pm_id:
            payment_methods.set_for_entity(uid, str(entity.id), pm_id, establish_payer=True)
    except payment_methods.PaymentMethodError as exc:
        return error(exc.message, exc.status)
    try:
        authorize_entity_billing(entity, request.auth_user)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("onboarding billing: could not authorize billing for {}", entity.id)
        return error("Could not confirm billing. Please try again.", 500)
    return respond({"has_billing_consent": True})


# --- Finalize's trial start ------------------------------------------------------------------


@onboarding_router.post("/trials/start", summary="Start the card-free trials of a finished wizard")
def trials_start(request):
    """Body ``{entity_id}`` -> ``{"trial_end": iso8601 | null}``.

    What Flask's ``onboarding_finalize`` did inline (``start_trials_for_enabled_modules``,
    best-effort, then the earliest ``trial_end`` read back) - except that here a failure
    FAILS: onboarding-backend's finalize has already flipped the company live when it calls
    this, and a trial that silently did not start is a company with modules on and nothing
    behind them. A ``CheckoutError`` answers with its own status, anything else 502; the All
    Set screen offers Try again. Idempotent: modules that already hold a trial or a
    subscription are skipped by the starter, and ``trial_end`` is READ BACK from the rows -
    the answer on a revisit as much as on the first call. Null is a legitimate answer (an
    entity with no enabled module has no trial to state)."""
    from billing.services import store as sub_store
    from billing.services.checkout import CheckoutError, start_trials_for_enabled_modules

    payload = body(request)
    try:
        entity = _entity_for_member(request, payload.get("entity_id"))
    except _Refused as refused:
        return refused.response

    try:
        start_trials_for_enabled_modules(entity, request.auth_user)
    except CheckoutError as exc:
        logger.exception("trials/start: refused for entity {}", entity.id)
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("trials/start: failed to start trials for entity {}", entity.id)
        return error("The trial could not be started. Please try again.", 502)

    ends = [
        row.trial_end
        for row in sub_store.module_rows_for_entity(str(entity.id))
        if getattr(row, "trial_end", None) is not None
    ]
    return respond({"trial_end": min(ends).isoformat() if ends else None})
