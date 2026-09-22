"""The payer portal: the fifteen ``/api/me/*`` routes of Flask's ``routes/portal.py``.

Person-scoped (``SelfBearerAuth``): every row is found by the caller's ``user_id``, never
by the company in the token or the ``X-Entity-Id`` header. Paths, methods, JSON and status
codes are Flask's byte for byte, so minty-web's ``features/subscription/api/payerPortal.ts``
is billing-frontend's ``lib/payerPortal.ts`` with a new base URL.

WHAT FLASK'S VIEWS DID THAT IS NOT HERE, because the framework does it: the CORS headers on
every answer (``corsheaders`` middleware), the OPTIONS preflight (same), the 404-while-dark
(``SubscriptionsDarkMiddleware``), the bearer check and the ``no_user_claim`` 403 (the auth
class loads the user or refuses). What IS here is the contract each view kept:

* ``400 {"error": "<field> is required"}`` for a missing routing id;
* ``404 {"error": "That company isn't on your billing account."}`` when the read model answers
  None - "not the payer" and "no such company" are the same answer to someone who should not
  be asking;
* ``422 {"error": <the service's sentence>}`` for a stated refusal (the handover routes, the
  invitation) - 422 rather than 403 because the client shows the server's words only when
  they read as prose;
* ``500`` with each route's own copy for a surprise, never a stack trace;
* the payment-method routes go through ``payment_methods.run`` (200 / 409 / 422 / 500), the
  same shell the onboarding twins use.
"""

from __future__ import annotations

from ninja import Router

from billing.api._json import body, error, int_arg, respond, user_id
from billing.services._log import logger
from core import flask_client
from core.auth import SelfBearerAuth

me_router = Router(auth=SelfBearerAuth())

#: (method, path) - the contract, also walked by billing/tests/test_dark.py and e2e/.
ROUTES = (
    ("GET", "/subscriptions"),
    ("GET", "/subscriptions/subscriber-options"),
    ("POST", "/subscriptions/invite-admin"),  # forwards to Flask (core/flask_client.py)
    ("POST", "/subscriptions/transfer"),
    ("POST", "/subscriptions/transfer/respond"),
    ("POST", "/subscriptions/transfer/cancel"),
    ("GET", "/subscriptions/transfers"),
    ("GET", "/invoices"),
    ("GET", "/billing/payment-methods"),
    ("POST", "/billing/payment-methods/setup-intent"),
    ("POST", "/billing/payment-methods/confirm"),
    ("POST", "/billing/payment-methods/default"),
    ("GET", "/billing/entity-payment-method"),
    ("POST", "/billing/entity-payment-method"),
    ("POST", "/billing/payment-methods/update"),
    ("POST", "/billing/payment-methods/remove"),
)

#: Flask forwards the invitation here: its bearer-authenticated onboarding invite endpoint
#: (``entity/routes/create.py::onboarding_invite``), which enforces USER_INVITE and the role
#: rank for the token's user and answers ``{"error": ...}`` / ``{"status": "success",
#: "email_sent": ...}``. The invitation row, its token and its email stay Flask's until Part 3.
INVITE_PATH = "/api/onboarding/invite"


# --- The subscriptions tab -----------------------------------------------------------


@me_router.get("/subscriptions", summary="Every company the caller pays for")
def my_subscriptions(request):
    """Query params, all optional: ``q``, ``sort`` (one of ``portal.SORT_FIELDS``, default
    ``entity``), ``direction`` (``asc``/``desc``), ``page`` (1-based), ``per_page`` (clamped to
    ``portal.MAX_PER_PAGE``). An unknown sort field falls back to entity name rather than
    erroring: this drives a column header, and an unrecognised one means the client is newer
    than the server, which should degrade to a sensible order and not a broken table."""
    from billing.services import portal

    uid = user_id(request)
    try:
        payload = portal.build_payer_subscriptions(
            uid,
            query=request.GET.get("q", ""),
            sort=request.GET.get("sort", "entity"),
            direction=request.GET.get("direction", "asc"),
            page=int_arg(request, "page", 1),
            per_page=int_arg(request, "per_page", portal.DEFAULT_PER_PAGE),
        )
    except Exception:
        # Unlike the notice endpoint there is no useful empty answer here - an empty table
        # reads as "you pay for nothing", which is a worse lie than an error the page can
        # retry from.
        logger.exception("payer subscriptions API failed for user {}", uid)
        return error("Could not load your subscriptions.", 500)
    return respond(payload)


@me_router.get("/subscriptions/subscriber-options", summary="Who one company's bill could go to")
def my_subscriber_options(request):
    """``entity`` is REQUIRED, and it is the first id the payer portal accepts that is not a
    filter over an already-payer-scoped set. So it is checked rather than trusted: the read
    model answers None unless the caller is the payer for that entity, and this returns 404
    for it."""
    from billing.services import portal

    uid = user_id(request)
    entity_id = (request.GET.get("entity") or "").strip()
    if not entity_id:
        return error("entity is required", 400)
    try:
        payload = portal.build_subscriber_options(uid, entity_id)
    except Exception:
        logger.exception("subscriber options API failed for user {} entity {}", uid, entity_id)
        return error("Could not load the subscribers.", 500)
    if payload is None:
        return error("That company isn't on your billing account.", 404)
    return respond(payload)


@me_router.post("/subscriptions/invite-admin", summary="Invite someone into a company as an admin")
def my_invite_admin(request):
    """Body: ``{"entity": id, "email": ...}``. The ONLY write the payer portal performs against
    Minty's own tables, and it is deliberately the smallest one on this screen: it adds a
    MEMBER, it does not move a payer. The gates (payer of the company, USER_INVITE) are the
    service's; the invitation itself is Flask's, reached through ``core.flask_client``."""
    from billing.services import portal

    uid = user_id(request)
    payload = body(request)
    entity_id = str(payload.get("entity") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not entity_id:
        return error("entity is required", 400)
    try:
        ok, message = portal.invite_admin_to_entity(
            uid, entity_id, email,
            # send(body) -> (payload, status): Flask, as the caller, with the caller's token.
            send=lambda invite: flask_client.forward(request, INVITE_PATH, json=invite),
        )
    except Exception:
        logger.exception("invite-admin failed for user {} entity {}", uid, entity_id)
        return error("Could not send that invitation.", 500)
    if not ok:
        # 422: the request was understood and refused for a stated reason the form shows
        # against the field - already a member, already invited, not an address.
        return error(message, 422)
    return respond({"ok": True, "message": message})


# --- Handovers ------------------------------------------------------------------------


class _MissingField(Exception):
    """A required routing id was absent - a 400, distinct from a stated refusal."""


def _required(payload: dict, field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise _MissingField(f"{field} is required")
    return value


def _transfer_call(request, handler, *, description: str):
    """The shared shell for the four handover routes: 400 only for a missing routing id,
    **422 with a full sentence** for any business refusal, 500 for a surprise."""
    uid = user_id(request)
    payload = body(request)
    try:
        result = handler(uid, payload)
    except _MissingField as exc:
        # App-authored text from _required(), not exception detail: it names the field on
        # purpose so the client can say which one is missing.
        logger.warning("{} missing field: {}", description, exc)
        return error(str(exc), 400)
    except Exception:
        logger.exception("{} failed for user {}", description, uid)
        return error("Something got stuck on my end!", 500)

    ok, message, data = result
    if not ok:
        return error(message, 422)
    answer = {"ok": True, "message": message}
    if data is not None:
        answer["transfer"] = data
    return respond(answer)


@me_router.post("/subscriptions/transfer", summary="Offer a company's subscription to another admin")
def my_transfer_initiate(request):
    """Body: ``{entity, to_user}``. Authorised inside the service: ``transfer_blockers`` refuses
    unless the caller is that entity's payer."""
    from billing.services import transfers

    return _transfer_call(
        request,
        lambda uid, payload: transfers.offer_transfer(
            uid, _required(payload, "entity"), _required(payload, "to_user")
        ),
        description="transfer initiate",
    )


@me_router.post("/subscriptions/transfer/respond", summary="Accept or decline a handover offered to you")
def my_transfer_respond(request):
    """Body: ``{transfer, accept}``. THE ONE ROUTE HERE THAT MOVES MONEY, and re-entrant: called
    twice it adopts the invoice already paid under the offer's key rather than raising a
    second one, so a double-click or a retry after a timeout costs nothing."""
    from billing.services import transfers

    return _transfer_call(
        request,
        lambda uid, payload: transfers.respond_to_transfer(
            uid, _required(payload, "transfer"), accept=bool(payload.get("accept"))
        ),
        description="transfer respond",
    )


@me_router.post("/subscriptions/transfer/cancel", summary="Withdraw a handover you offered")
def my_transfer_cancel(request):
    """Body: ``{transfer}``. The initiator's escape hatch."""
    from billing.services import transfers

    return _transfer_call(
        request,
        lambda uid, payload: (*transfers.cancel_transfer(uid, _required(payload, "transfer")), None),
        description="transfer cancel",
    )


@me_router.get("/subscriptions/transfers", summary="Handovers offered to the caller")
def my_transfers(request):
    """Scoped on ``to_user_id`` from the token - deliberately companies the caller does NOT
    pay for yet. Still nothing in the request can be swapped for someone else's offers."""
    from billing.services import transfers

    uid = user_id(request)
    try:
        payload = transfers.incoming_transfers_payload(uid)
    except Exception:
        logger.exception("incoming transfers failed for user {}", uid)
        return error("Could not load those requests.", 500)
    return respond({"transfers": payload})


# --- The invoices tab -----------------------------------------------------------------


@me_router.get("/invoices", summary="The caller's invoices, newest first")
def my_invoices(request):
    """Query params: ``entity`` (narrow to one company), ``page``, ``per_page``. ``entity`` is a
    filter, not a permission: the set is already fixed to ``payer_user_id`` from the token."""
    from billing.services import portal

    uid = user_id(request)
    try:
        payload = portal.build_payer_invoices(
            uid,
            entity_id=(request.GET.get("entity") or "").strip() or None,
            page=int_arg(request, "page", 1),
            per_page=int_arg(request, "per_page", 10),
        )
    except Exception:
        logger.exception("payer invoices API failed for user {}", uid)
        return error("Could not load your invoices.", 500)
    return respond(payload)


# --- Saved payment methods --------------------------------------------------------------
#
# The in-app wallet behind the billing account page. Every one of these carries ids and
# display fields only - the number is typed into Stripe Elements and confirmed straight
# against a SetupIntent, so no PAN reaches this process (see ``services.payment_methods``).
# Four of them take a ``pm_...`` id FROM THE REQUEST; ``payment_methods._owned`` compares the
# method's customer against the customer resolved from the TOKEN's user, and somebody else's
# id answers "not found" rather than being acted on. All POST, including the ones that read
# as DELETE or PATCH.


def _payment_methods_call(request, handler):
    """Run one payment-method action for the bearer's own account; answer the fresh list.
    The three shared failure modes live in ``payment_methods.run``."""
    from billing.services import payment_methods

    payload, status = payment_methods.run(handler, user_id(request))
    return respond(payload, status)


def _pm_id(request) -> str:
    return str(body(request).get("payment_method") or "").strip()


@me_router.get("/billing/payment-methods", summary="Every saved payment method, default first")
def my_payment_methods(request):
    from billing.services import payment_methods

    return _payment_methods_call(request, payment_methods.list_for_user)


@me_router.post("/billing/payment-methods/setup-intent", summary="Open a SetupIntent for the card form")
def my_payment_method_setup_intent(request):
    """Answers ``{client_secret, publishable_key, setup_intent}``. Takes nothing from the
    request; no customer is created here - that happens in ``confirm`` once Stripe says a
    card exists, so an abandoned form leaves nothing behind."""
    from billing.services import payment_methods

    return _payment_methods_call(request, payment_methods.start_setup)


@me_router.post("/billing/payment-methods/confirm", summary="Adopt the card the browser confirmed")
def my_payment_method_confirm(request):
    """Body: ``{setup_intent, make_default?}``. The intent is re-read from Stripe and refused
    unless it carries this caller's own ``metadata.user_id`` stamp. Idempotent."""
    from billing.services import payment_methods

    payload = body(request)
    setup_intent = str(payload.get("setup_intent") or "").strip()
    make_default = bool(payload.get("make_default"))
    return _payment_methods_call(
        request, lambda uid: payment_methods.confirm_setup(uid, setup_intent, make_default=make_default)
    )


@me_router.post("/billing/payment-methods/default", summary="Make one saved method the account's main card")
def my_payment_method_default(request):
    """Body: ``{payment_method}``. NOMINATES NOTHING - each company is billed on the card it was
    put on; this decides which card the pickers offer first."""
    from billing.services import payment_methods

    payment_method = _pm_id(request)
    return _payment_methods_call(request, lambda uid: payment_methods.set_default(uid, payment_method))


@me_router.get("/billing/entity-payment-method", summary="The card one company is billed on")
def my_entity_payment_method(request):
    """``?entity=<id>`` -> the saved methods, plus ``nominated_id``."""
    from billing.services import payment_methods

    entity_id = str(request.GET.get("entity") or "").strip()
    return _payment_methods_call(request, lambda uid: payment_methods.for_entity(uid, entity_id))


@me_router.post("/billing/entity-payment-method", summary="Put one company on a card")
def my_entity_payment_method_set(request):
    """Body: ``{entity, payment_method}``. THE ONE WRITE ON THIS SURFACE WITH BILLING
    CONSEQUENCES, and they stop at the company named. Two proofs inside the service: the
    method is the caller's (``_owned``) and the caller is the company's payer (``_payer_of``)."""
    from billing.services import payment_methods

    payload = body(request)
    entity_id = str(payload.get("entity") or "").strip()
    payment_method = str(payload.get("payment_method") or "").strip()
    return _payment_methods_call(
        request, lambda uid: payment_methods.set_for_entity(uid, entity_id, payment_method)
    )


@me_router.post("/billing/payment-methods/update", summary="Edit a saved method's expiry or billing details")
def my_payment_method_update(request):
    """Body: ``{payment_method, exp_month?, exp_year?, name?, address?}`` - only what Stripe
    permits to change on an existing method."""
    from billing.services import payment_methods

    payload = body(request)
    payment_method = _pm_id(request)
    address = payload.get("address")
    return _payment_methods_call(
        request,
        lambda uid: payment_methods.update(
            uid,
            payment_method,
            exp_month=payload.get("exp_month"),
            exp_year=payload.get("exp_year"),
            name=payload.get("name"),
            address=address if isinstance(address, dict) else None,
        ),
    )


@me_router.post("/billing/payment-methods/remove", summary="Detach a saved method")
def my_payment_method_remove(request):
    """Body: ``{payment_method}``. Two 409s the page shows verbatim: the default cannot go
    while another method could take its place, and the last method cannot go at all while
    something is still billing forward."""
    from billing.services import payment_methods

    payment_method = _pm_id(request)
    return _payment_methods_call(request, lambda uid: payment_methods.remove(uid, payment_method))
