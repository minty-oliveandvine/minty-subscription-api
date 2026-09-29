"""The payer portal: the ``/api/me/*`` routes — Flask's ``routes/portal.py`` plus what this
service added.

Fifteen paths (sixteen operations) are Flask's, byte for byte: paths, methods, JSON and
status codes, so minty-web's ``features/subscription/api/payerPortal.ts`` is
billing-frontend's ``lib/payerPortal.ts`` with a new base URL. Eight are this service's own
(``billing/tests/test_contract.py::ADDED_PORTAL_PATHS``): ``subscriptions/transfer/seen``,
the four ``billing/accounts`` routes behind the portal's billing accounts (08-A/B/C), and
08-B's three per-invoice actions - ``invoices/{id}/breakdown``, ``/retry`` and ``/pdf``.

Person-scoped (``SelfBearerAuth``): every row is found by the caller's ``user_id``, never
by the company in the token or the ``X-Entity-Id`` header.

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
    ("POST", "/subscriptions/transfer/seen"),
    ("POST", "/subscriptions/transfer/cancel"),
    ("GET", "/subscriptions/transfers"),
    ("GET", "/invoices"),
    ("GET", "/invoices/{invoice_id}/breakdown"),  # 08-B's "Billing Breakdown" (Flask had none)
    ("GET", "/invoices/{invoice_id}/pdf"),  # 08-B's "Invoice PDF", Figma 09-A (Flask had none)
    ("POST", "/invoices/{invoice_id}/retry"),  # 08-B's "Retry payment" (Flask had none)
    ("GET", "/billing/payment-methods"),
    ("POST", "/billing/payment-methods/setup-intent"),
    ("POST", "/billing/payment-methods/confirm"),
    ("POST", "/billing/payment-methods/default"),
    ("GET", "/billing/entity-payment-method"),
    ("POST", "/billing/entity-payment-method"),
    ("POST", "/billing/payment-methods/update"),
    ("POST", "/billing/payment-methods/remove"),
    # The billing accounts (Flask never had them): read, rename, switch card, move a company.
    ("GET", "/billing/accounts"),
    ("POST", "/billing/accounts/update"),
    ("POST", "/billing/accounts/default-card"),
    ("POST", "/billing/accounts/move"),
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
    """Body: ``{transfer, accept, codes?}``. THE ONE ROUTE HERE THAT MOVES MONEY, and re-entrant:
    called twice it adopts the invoice already paid under the offer's key rather than raising a
    second one, so a double-click or a retry after a timeout costs nothing.

    ``codes`` is the modules being taken on (07-D "Choose Modules"); anything the company has and
    the list does not name is cancelled as part of accepting. OMITTED MEANS ALL OF THEM, which is
    what every caller written before the screen offered a choice sends — the field is additive
    and an older client keeps working unchanged."""
    from billing.services import transfers

    return _transfer_call(
        request,
        lambda uid, payload: transfers.respond_to_transfer(
            uid,
            _required(payload, "transfer"),
            accept=bool(payload.get("accept")),
            codes=payload.get("codes"),
        ),
        description="transfer respond",
    )


@me_router.post(
    "/subscriptions/transfer/seen", summary="Mark how one of your handovers ended as seen"
)
def my_transfer_seen(request):
    """Body: ``{transfer}``. The payer pressing Done on 07-I / A-07 / A-08.

    Stamped from their click rather than from the read that drew the modal: rendering is not
    evidence anybody saw it. Idempotent, so a double-click costs nothing, and it answers the
    same way for a transfer that does not exist and one that is not theirs."""
    from billing.services import transfers

    return _transfer_call(
        request,
        lambda uid, payload: (
            *transfers.mark_outcome_seen(uid, _required(payload, "transfer")),
            None,
        ),
        description="transfer seen",
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
    """Query params: ``entity`` (narrow to one company), ``account`` (to one billing
    account - 08-B), ``page``, ``per_page``. Both are filters, not permissions: the set is
    already fixed to ``payer_user_id`` from the token, and someone else's account matches
    nothing."""
    from billing.services import portal

    uid = user_id(request)
    try:
        payload = portal.build_payer_invoices(
            uid,
            entity_id=(request.GET.get("entity") or "").strip() or None,
            account_id=(request.GET.get("account") or "").strip() or None,
            page=int_arg(request, "page", 1),
            per_page=int_arg(request, "per_page", 10),
        )
    except Exception:
        logger.exception("payer invoices API failed for user {}", uid)
        return error("Could not load your invoices.", 500)
    return respond(payload)


@me_router.get(
    "/invoices/{invoice_id}/breakdown",
    summary="One invoice, company by company - the billing breakdown",
)
def my_invoice_breakdown(request, invoice_id: str):
    """08-B's "Billing Breakdown · Download csv": a row per company line - the subscription,
    its monthly rate, the days it paid for and what was charged - which the web writes as the
    CSV. An invoice that is not the caller's, or no invoice at all, is 404."""
    from billing.services import portal

    uid = user_id(request)
    try:
        payload = portal.build_invoice_breakdown(uid, invoice_id)
    except Exception:
        logger.exception("invoice breakdown API failed for user {} invoice {}", uid, invoice_id)
        return error("Could not load that invoice's breakdown.", 500)
    if payload is None:
        return error("That invoice couldn't be found.", 404)
    return respond(payload)


@me_router.get(
    "/invoices/{invoice_id}/pdf",
    summary="One invoice as a PDF - Figma 09-A",
    openapi_extra={
        "responses": {
            200: {
                "description": "The invoice, an A4 PDF.",
                "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
            },
        },
    },
)
def my_invoice_pdf(request, invoice_id: str):
    """08-B's "Invoice PDF": the invoice drawn as Figma 09-A, not Stripe's hosted page. Bill to
    is the invoice's BILLING ACCOUNT (read live, as 08-B shows it); each of 09-A's plan lines
    lists its companies, one row per Stripe item. 404 when it is not the caller's; 409 when it
    is no document - never sent, a draft, voided (the list's ``has_pdf`` is false for exactly
    those); 502 when the billing address cannot be read from the processor; 500 otherwise,
    including lines that do not add up to the invoice's total, which is never printed."""
    from django.http import HttpResponse

    from billing.services import invoice_document, invoice_pdf

    uid = user_id(request)
    try:
        doc = invoice_document.build_invoice_document(uid, invoice_id)
        if doc is None:
            return error("That invoice couldn't be found.", 404)
        data = invoice_pdf.render_invoice_pdf(doc)
    except invoice_document.NoDocument:
        return error("There's no PDF for that invoice.", 409)
    except invoice_document.BillToUnavailable:
        return error(
            "We couldn't reach the payment processor for the billing address. "
            "Please try again in a moment.",
            502,
        )
    except invoice_document.TotalMismatch:
        return error("Could not prepare that invoice's PDF.", 500)  # logged where it was found
    except Exception:
        logger.exception("invoice pdf API failed for user {} invoice {}", uid, invoice_id)
        return error("Could not prepare that invoice's PDF.", 500)
    response = HttpResponse(data, content_type="application/pdf")
    # The web names its own download (CORS exposes no headers); this is for a direct open.
    response["Content-Disposition"] = f'attachment; filename="{doc.filename}"'
    response["Cache-Control"] = "private, no-store"
    return response


@me_router.post("/invoices/{invoice_id}/retry", summary="Retry a failed invoice's payment now")
def my_invoice_retry(request, invoice_id: str):
    """08-B's *Retry payment*: collect this failed invoice now, on its account's card
    (``billing_accounts.retry_invoice``). Answers ``{"ok", "status", "message"}`` in the words
    the module page's retry uses (``api._retry``): ``paid``, ``failed`` (with the processor's
    reason), ``no_card``, ``gave_up``, ``nothing_owed``, ``older_debt_only``,
    ``not_this_invoice``, ``not_collectable``. An invoice the processor will no longer collect
    is re-issued and the replacement charged in the same press; one that cannot be re-issued
    automatically is ``not_collectable``. Someone else's invoice is 404; one not waiting for a
    payment, 409 (or ``not_this_invoice`` when it was re-issued since the page was drawn)."""
    from billing.api._retry import retry_answer
    from billing.services import billing_accounts
    from billing.services.payment_methods import PaymentMethodError

    uid = user_id(request)
    try:
        result = billing_accounts.retry_invoice(uid, invoice_id)
    except PaymentMethodError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("invoice retry failed for user {} invoice {}", uid, invoice_id)
        return error("We couldn't reach the card processor. Try again shortly.", 502)
    if result is None:
        return error("That invoice couldn't be found.", 404)
    return respond(retry_answer(result))


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
    """Body: ``{setup_intent, make_default?, billing_group_id?, billing_email?,
    billing_company?}``. The intent is re-read from Stripe and refused unless it carries this
    caller's own ``metadata.user_id`` stamp. Idempotent.

    The account fields are the onboarding twin's: ``billing_group_id`` puts the card on one
    of the caller's accounts (08-B's "Add payment method"); a company and an email OPEN one
    ("New billing account"), and both are required to. Both are checked HERE, before the
    service runs, because the service only reaches them after the card is attached at
    Stripe - a refusal there would leave a card saved against no account."""
    from billing.services import billing_accounts, payment_methods

    payload = body(request)
    setup_intent = str(payload.get("setup_intent") or "").strip()
    make_default = bool(payload.get("make_default"))
    billing_group_id = str(payload.get("billing_group_id") or "").strip() or None
    billing_email = payload.get("billing_email")
    billing_company = payload.get("billing_company")

    def _confirm(uid):
        email, company = billing_email, billing_company
        if billing_group_id:
            payment_methods.account_of(uid, billing_group_id)
        elif email is not None or company is not None:
            email, company = billing_accounts.validate_identity(
                email, company, require_both=True
            )
        return payment_methods.confirm_setup(
            uid, setup_intent, make_default=make_default,
            billing_group_id=billing_group_id,
            billing_email=email, billing_company=company,
        )

    return _payment_methods_call(request, _confirm)


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
    """Body: ``{payment_method, account?}``. Two 409s the page shows verbatim: the default
    cannot go while another method could take its place, and the last method cannot go at
    all while something is still billing forward. ``account`` is the billing account whose
    page asked (08-B): its own charged card is refused in its words, and the Stripe
    customer's default is handed to its card rather than refused."""
    from billing.services import payment_methods

    payment_method = _pm_id(request)
    account = str(body(request).get("account") or "").strip() or None
    return _payment_methods_call(
        request, lambda uid: payment_methods.remove(uid, payment_method, account_id=account)
    )


# --- Billing accounts (08-A / 08-B / 08-C) ----------------------------------------------
#
# A payer's named billing accounts: each one a name, an email, the cards on it, the card it
# charges and the companies it pays for (``services.billing_accounts``). Every account id
# arrives from the browser and is proven to be the caller's before anything reads or writes
# it (``payment_methods.account_of``); every write answers the fresh accounts. Through the
# wallet's shell, so the refusals reach the page as sentences (400 / 404 / 409 / 422).


@me_router.get("/billing/accounts", summary="The caller's billing accounts, oldest first")
def my_billing_accounts(request):
    """``?countries=1`` adds what 08-C's address form needs and no other screen does: the
    country registry (the countries it offers) and Stripe's publishable key (it IS Stripe's
    own form, the AddressElement)."""
    from billing.services import portal

    countries = (request.GET.get("countries") or "").strip() in ("1", "true")
    return _payment_methods_call(
        request, lambda uid: portal.build_billing_accounts(uid, countries=countries)
    )


@me_router.post("/billing/accounts/update", summary="Rename a billing account, or change its address")
def my_billing_account_update(request):
    """Body: ``{account, billing_company?, billing_email?, address?, cardholder?}``. The
    address - and the cardholder, the name Stripe's address form asks for with it - are the
    account's charged card's billing details at Stripe (the account holds none)."""
    from billing.services import billing_accounts

    payload = body(request)
    account = str(payload.get("account") or "").strip()
    return _payment_methods_call(
        request,
        lambda uid: billing_accounts.update(
            uid,
            account,
            billing_company=payload.get("billing_company"),
            billing_email=payload.get("billing_email"),
            address=payload.get("address"),
            cardholder=payload.get("cardholder"),
        ),
    )


@me_router.post("/billing/accounts/default-card", summary="Switch the card a billing account charges")
def my_billing_account_default_card(request):
    """Body: ``{account, payment_method}``. From its next bill every company on the account
    is charged to this card."""
    from billing.services import billing_accounts

    payload = body(request)
    account = str(payload.get("account") or "").strip()
    payment_method = str(payload.get("payment_method") or "").strip()
    return _payment_methods_call(
        request, lambda uid: billing_accounts.set_default_card(uid, account, payment_method)
    )


@me_router.post("/billing/accounts/move", summary="Move a company to another billing account")
def my_billing_account_move(request):
    """Body: ``{entity, account}``. Nothing is charged; the company's paid days travel with
    it. A company on no account yet (a card-free trial) is placed on this one, with no
    consent written. Answers the accounts plus ``moved`` (null when it was already there;
    ``from_account`` null for a first placement)."""
    from billing.services import billing_accounts

    payload = body(request)
    entity_id = str(payload.get("entity") or "").strip()
    account = str(payload.get("account") or "").strip()
    return _payment_methods_call(
        request, lambda uid: billing_accounts.move_company(uid, entity_id, account)
    )
