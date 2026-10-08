"""The module settings page: one page model and eleven actions per company.

Company-scoped (``EntityBearerAuth``): the caller must hold a role on the entity, resolved
from the token's ``entity_id`` or the ``X-Entity-Id`` header (the page reached from the
portal carries an unscoped token and sends the header), and the company in the PATH must be
that same company. Reading the page needs ``MODULE_VIEW`` (cashier and up); every action
needs ``MODULE_MANAGE`` (admin and up) AND ``store.may_manage_subscription`` - the
``@require_subscription_payer`` port: only the payer, or any admin of a company that has no
payer yet, may act, because every one of these buttons spends ONE person's money. There is
no exception any more.

NO ACTION HANDS THE BROWSER TO STRIPE. Flask's ``checkout``, ``confirm-billing``,
``checkout-complete``, ``payment-method`` and ``manage-billing`` (setup-mode Checkout and the
Billing Portal) and the four ``payment-methods*`` card routes were DELETED on 2026-10-01: a
card is only ever added through a billing account, in-app (``/api/me/billing/...``), and a
company is put on one of its cards before it is charged.

Flask served this page from Jinja (``templates/entity/partials/module_*.html``) with the
actions as ``POST /entity/settings/module/<org_id>/<action>`` in ``entity/routes/settings.py``
(lines 1448-2460). minty-web renders it now, from ``GET /api/entities/{id}/modules``: the cards
(Flask's card dicts, ``cards.get_module_cards``), the summary and the panel (opaque to the
client until the screens that read them are built), the next payment, ``can_manage_modules``,
the payer when it is somebody else, the viewer, and the consent-takeover prompt. The action
bodies and answers are Flask's, except that dates render ISO 8601 (``IsoJSONEncoder``)
because the client computes on them, and ``restart-billing`` never answers ``{url}``.
"""

from __future__ import annotations

from datetime import datetime

from ninja import Router

from billing.api._json import IsoJSONEncoder, body, error, respond
from billing.services._log import logger
from billing.services.store import _by_pk
from core.auth import EntityBearerAuth
from core.exceptions import NotFoundError, PermissionDeniedError
from core.policy import Permission, has_permission
from shared_models.models import Entity, User

modules_router = Router(auth=EntityBearerAuth())

#: The eleven action names, Flask's spelling. ``POST
#: /api/entities/{id}/modules/{action}`` with any other word is a 404. ``restart-quote``
#: also answers GET, as Flask served it (``?codes=A,B``). ``activate-subscription`` is the
#: one Flask never had: it establishes the company's SUBSCRIBER, because a trial no longer
#: does.
ACTIONS = (
    "authorize-billing",
    "activate-subscription",
    "restart-quote",
    "restart-billing",
    "start-trial",
    "resume-preview",
    "subscribe-preview",
    "cancel-preview",
    "retry-payment",
    "cancel",
    "renew",
)
#: Stripe's return leg: the one action without the payer rule (see the module docstring).
# Flask's guard sentences, kept: the page shows them.
NO_VIEW = "You do not have permission to view module settings for this entity."
NO_MANAGE = "You do not have permission to manage subscriptions for this entity."
NOT_THE_PAYER = "Only the person who pays for this company can change its subscription."
WRONG_COMPANY = "That token is for a different company."
NO_SUCH_COMPANY = "That company doesn't exist."


def _respond(payload, status: int = 200):
    return respond(payload, status, encoder=IsoJSONEncoder)


# --- The gate -----------------------------------------------------------------------------


def _company(request, entity_id: str) -> Entity:
    """The company the path names - which must be the one the token was checked against.

    ``EntityBearerAuth`` proved the caller's role on ``request.entity_id`` (header, else the
    token's claim). A path naming another company would ride on that proof, so the two must
    agree; minty-web sends ``X-Entity-Id`` for the page it shows, and they always do."""
    if str(getattr(request, "entity_id", "") or "") != str(entity_id):
        raise PermissionDeniedError(WRONG_COMPANY)
    entity = _by_pk(Entity, entity_id)
    if entity is None:
        raise NotFoundError(NO_SUCH_COMPANY)
    return entity


def _gate(request, entity_id: str, *, permission: Permission, payer: bool):
    """Flask's decorator stack for one request: entity access, the permission, and - for the
    money routes - the payer rule. Returns ``(entity, user)``; refusals raise into
    ``core.exceptions``' 403 / 404 handlers."""
    entity = _company(request, entity_id)
    user = request.auth_user
    if not has_permission(user, permission, str(entity.id)):
        raise PermissionDeniedError(NO_MANAGE if permission is Permission.MODULE_MANAGE else NO_VIEW)
    if payer:
        from billing.services import store as sub_store

        if not sub_store.may_manage_subscription(str(entity.id), str(user.id)):
            raise PermissionDeniedError(NOT_THE_PAYER)
    return entity, user


# --- The page model -------------------------------------------------------------------------


def _person(user, user_id) -> dict:
    return {
        "user_id": str(getattr(user, "id", None) or user_id or ""),
        "name": " ".join(
            p for p in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if p
        ),
        "email": getattr(user, "email", None) or getattr(user, "username", None) or "",
    }


def _viewer(user) -> dict:
    """The person looking, for the page header: their name and two initials."""
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = " ".join(p for p in (first, last) if p) or (getattr(user, "email", None) or "")
    initials = "".join(p[0] for p in (first, last) if p).upper() or (name[:1].upper() if name else "")
    return {"name": name, "initials": initials}


def _iso_day(text):
    """Flask's card formats ``access_end_date`` for a Jinja template (``"August 19, 2026"``,
    ``cards.py``); the API's client counts days from it, so it travels as ``YYYY-MM-DD``.
    Parsed back from the exact ``%B %d, %Y`` the card writes (English month names - nothing
    in this process sets a locale); anything else passes through untouched."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()
    except (TypeError, ValueError):
        return text


def wire_card(card: dict) -> dict:
    """Flask's card dict as the page model lists it: every key as the service built it (the
    encoder renders ``period_end`` ISO and the Decimals as strings), ``access_end_date`` as
    an ISO date."""
    wire = dict(card)
    wire["access_end_date"] = _iso_day(card.get("access_end_date"))
    return wire


@modules_router.get("/{entity_id}/modules", summary="The module settings page model")
def module_page(request, entity_id: str):
    """What Flask's ``entity_settings_module`` handed its template, as JSON: the cards, the
    summary and panel, the date on the card at the top (READ OFF THE PANEL rather than
    computed beside it - the two were once two answers to one question), whether the
    viewer may act (admin AND payer, or no payer yet), who pays when it is somebody else,
    and the lapsed-trial restart screen when there is one.

    Plus ``has_subscriber``: whether the company has a payer AT ALL. A trial establishes
    none, so a running trial on false offers Activate Subscription where a confirmed one
    offers Manage Subscription - and ``payer`` cannot answer it, being null both when nobody
    pays and when the viewer is the one who does."""
    from billing.services import entity_modules
    from billing.services import store as sub_store

    entity, user = _gate(request, entity_id, permission=Permission.MODULE_VIEW, payer=False)
    eid = str(entity.id)

    module_cards = entity_modules.get_module_cards(eid)
    subscription_summary = entity_modules.get_subscription_summary(eid)
    # Never shown. The anchor only answers "has this payer ever been billed", which is what
    # puts the panel in its paid rather than its trial mode.
    billing_anchor = entity_modules.get_billing_anchor(eid)
    subscription_panel = entity_modules.build_subscription_panel(
        module_cards, subscription_summary, billing_anchor
    )
    next_payment_date = (
        entity_modules.next_payment_from_panel(subscription_panel)
        or entity_modules.get_next_payment_date(eid)
    )

    # Permission says who may administer the entity; the payer is whose card every one of
    # these buttons spends. Hidden for anyone else - and refused by ``_gate``, because hiding
    # a button is not a permission.
    can_manage_modules = bool(
        has_permission(user, Permission.MODULE_MANAGE, eid)
        and sub_store.may_manage_subscription(eid, str(user.id))
    )

    # Who to name when the actions are hidden. None while the entity has no payer, which is
    # the case any admin is allowed to act on.
    payer_id = sub_store.payer_for_entity(eid)
    payer = None
    if payer_id and str(payer_id) != str(user.id):
        payer = _person(_by_pk(User, str(payer_id)), payer_id)

    # The lapsed-trial restart screen. None on an ordinary page. Computed AFTER
    # ``can_manage_modules``, because a co-admin gets the naming fields and none of the
    # Stripe reads behind them.
    consent_takeover = entity_modules.build_consent_takeover(
        eid, str(user.id), can_manage=can_manage_modules
    )

    return _respond(
        {
            "entity_id": eid,
            "entity_name": entity.name or "",
            "cards": [wire_card(c) for c in module_cards],
            "summary": subscription_summary,
            "panel": subscription_panel,
            "next_payment_date": next_payment_date,
            "can_manage_modules": can_manage_modules,
            # Whether the company has a SUBSCRIBER at all. ``payer`` cannot answer it: it
            # is deliberately null both when nobody pays and when the viewer is the one
            # who does. The cards need the difference, because a running trial with no
            # subscriber offers "Activate Subscription" where a confirmed one offers
            # "Manage Subscription", and ``needs_card`` conflates it with "the payer has
            # no card".
            "has_subscriber": payer_id is not None,
            "payer": payer,
            "viewer": _viewer(user),
            "consent_takeover": consent_takeover,
        }
    )


# --- The actions ----------------------------------------------------------------------------
#
# One handler per action, ``(request, entity, user, payload) -> HttpResponse``, dispatched by
# name below. Each keeps its Flask twin's body keys, answers and status codes; ``CheckoutError``
# and ``PaymentMethodError`` carry their own status and a sentence written for the toast.


def _codes(payload: dict, key: str = "codes") -> list[str]:
    return [str(c).strip().upper() for c in (payload.get(key) or []) if str(c).strip()]


def _authorize_billing(request, entity, user, payload):
    """Authorise billing for this entity WITHOUT subscribing or charging - the trial banner's
    action, so the outcome is "convert at term end". Idempotent: consent is once per entity.
    Body: ``{"payment_method"?}``; when present it is nominated for THIS company BEFORE
    consent is recorded (authorising a charge while the company points at a different card
    authorises one the payer was never shown). A company on NO billing account, with none
    named, answers 402 "Choose a billing account for this company." and records nothing - the
    page opens the Billing Accounts picker on it."""
    from billing.services import payment_methods
    from billing.services.checkout import CheckoutError, authorize_entity_billing

    pm_id = str(payload.get("payment_method") or "").strip()
    try:
        if pm_id:
            payment_methods.set_for_entity(str(user.id), str(entity.id), pm_id)
    except payment_methods.PaymentMethodError as exc:
        return error(exc.message, exc.status)
    try:
        authorize_entity_billing(entity, user)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("module billing authorization failed for {}", entity.id)
        return error("Could not confirm billing. Please try again.", 500)
    return _respond({"ok": True})


def _activate_subscription(request, entity, user, payload):
    """Confirm a started trial: THE COMPANY GETS A SUBSCRIBER. Body:
    ``{"account"?, "payment_method"?, "codes"?}``.

    A trial is free and commits nobody, so starting one establishes no payer
    (``checkout.start_module_trial``). This is the act that does, and it is the only one
    besides onboarding's ``/billing/authorize``: the company is put on a billing account,
    consent is recorded, and every module row is stamped with the caller
    (``store.establish_entity_payer``).

    TWO OUTCOMES, chosen from the company's own state and never from the body - the client
    cannot ask to be charged:

    * a trial still RUNNING -> payer established, account recorded, consent written, and
      NOTHING CHARGED. It has paid days left and converts at term end, which is what
      confirming a running trial means;
    * a trial already OVER, and ``codes`` naming which modules to buy back -> the same, and
      then those modules are BOUGHT BACK, because free days nobody paid for cannot be
      resumed. Priced server-side from the codes ``consent.codes_for_restart`` resolves,
      through the same ``_restart_state_and_codes`` both restart routes share, so the quote
      the payer was shown and the charge they get resolve the SET identically.

    NO ``codes`` MEANS NO CHARGE, whatever the company's state. A company can have one
    module still trialling and another already lapsed, and the button on the running
    trial's card confirms that trial - it must not quietly buy back the other one. So the
    charge needs the modules named, and the server still decides whether they may be
    restarted at all (422 if not). The client can ask WHICH, never WHETHER.

    ``account`` places the company on one of the caller's billing accounts first - the
    picker's own move, made in THIS request, so a chosen account and the payer it was
    chosen for can never be left half-written. ``payment_method`` is the card-keyed twin,
    kept for parity with ``authorize-billing``. With neither, on a company that is on no
    account: 402 "Choose a billing account for this company." and nothing is recorded.

    402 choose an account / choose a card · 409 nothing to activate, or somebody else got
    there first · 422 codes that cannot be restarted · 500 otherwise.
    """
    from billing.services import billing_accounts, consent, payment_methods
    from billing.services.checkout import (
        CheckoutError,
        activate_entity_billing,
        confirm_modules_checkout,
    )

    account_id = str(payload.get("account") or "").strip()
    pm_id = str(payload.get("payment_method") or "").strip()
    try:
        # establish_payer, because a card-free trial has no subscriber yet and this
        # request is what gives it one. Both halves land in this request.
        if account_id:
            billing_accounts.move_company(str(user.id), str(entity.id), account_id)
        elif pm_id:
            payment_methods.set_for_entity(
                str(user.id), str(entity.id), pm_id, establish_payer=True
            )
    except payment_methods.PaymentMethodError as exc:
        return error(exc.message, exc.status)
    except NotFoundError as exc:
        return error(str(exc) or "That billing account couldn't be found.", 404)

    # Running trial or lapsed one? Asked of the COMPANY, before anything is written, so
    # activation cannot change its own answer. The codes gate it: with none named this is
    # a confirm and only a confirm (see the docstring).
    requested = _codes(payload)
    lapsed = bool(requested) and bool(
        consent.lapsed_trial_for_entity(str(entity.id), str(user.id)).get("mode")
    )

    try:
        activate_entity_billing(entity, user)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("activate subscription failed for {}", entity.id)
        return error("Could not activate the subscription. Please try again.", 500)

    if not lapsed:
        return _respond({"ok": True, "charged": False})

    # The lapsed half, through the SAME front end the quote uses.
    _state, codes, refused = _restart_state_and_codes(entity, user, requested)
    if refused:
        return refused
    if not _entity_has_card(entity.id):
        return error("Choose a card before restarting billing.", 402)
    try:
        bought = confirm_modules_checkout(entity, user, requested_codes=codes)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("activate subscription: restart billing failed for {}", entity.id)
        return error("Could not restart billing. Please try again.", 500)
    return _respond({"ok": True, "charged": True, "restarted": bought.get("created", codes)})


def _restart_state_and_codes(entity, user, requested):
    """``(state, codes, refusal)`` for the two restart routes - their shared front half. The
    quote and the charge MUST resolve the submitted codes identically, or the payer is shown
    one number and billed against another set: 409 when nothing lapsed, 422 when the set
    cannot be honoured (refused whole, never filtered down)."""
    from billing.services import consent

    state = consent.lapsed_trial_for_entity(str(entity.id), str(user.id))
    if not state.get("mode"):
        return state, [], error("There is nothing to restart for this company.", 409)
    codes = consent.codes_for_restart(state, requested)
    if not codes:
        return state, [], error("Choose at least one module to restart.", 422)
    return state, codes, None


def _restart_quote(request, entity, user, payload):
    """What restarting the chosen modules would cost. READS ONLY. ``codes`` in the body, or
    ``?codes=A,B`` on a GET as Flask took it. Priced through ``preview_subscribe_modules`` -
    the same figure the charge uses."""
    from billing.services.checkout import CheckoutError, preview_subscribe_modules

    requested = payload.get("codes")
    if requested is None:
        requested = [p for p in (request.GET.get("codes") or "").split(",") if p.strip()]
    _state, codes, refused = _restart_state_and_codes(entity, user, requested)
    if refused:
        return refused
    try:
        return _respond(preview_subscribe_modules(entity, user, codes))
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("restart quote failed for {}", entity.id)
        return error("Could not price that. Please try again.", 500)


def _entity_has_card(entity_id) -> bool:
    """Whether THIS company has a card nominated for it - read AFTER any nomination, and
    asked about the company because that is what will be charged: there is deliberately no
    account default to fall back on."""
    from billing.services import store as sub_store

    return bool(sub_store.card_for_entity(str(entity_id)))


def _restart_billing(request, entity, user, payload):
    """Buy back a lapsed trial's modules. THIS CHARGES. Body: ``{codes, payment_method?}``.
    In order, each with its own answer: a genuine lapse (409), codes that lapsed (422), a
    card nominated for THIS company before the charge (402 when there is none), then the
    subscribe priced server-side from the resolved codes."""
    from billing.services import payment_methods
    from billing.services.checkout import CheckoutError, confirm_modules_checkout

    _state, codes, refused = _restart_state_and_codes(entity, user, payload.get("codes"))
    if refused:
        return refused
    pm_id = str(payload.get("payment_method") or "").strip()
    try:
        if pm_id:
            payment_methods.set_for_entity(str(user.id), str(entity.id), pm_id)
        if not _entity_has_card(entity.id):
            return error("Choose a card before restarting billing.", 402)
    except payment_methods.PaymentMethodError as exc:
        return error(exc.message, exc.status)
    try:
        result = confirm_modules_checkout(entity, user, requested_codes=codes)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("restart billing failed for {}", entity.id)
        return error("Could not restart billing. Please try again.", 500)
    return _respond({"ok": True, "restarted": result.get("created", codes)})


def _start_trial(request, entity, user, payload):
    """Start a card-free trial for one or more never-subscribed modules. Body: ``{codes}``.
    Access is granted at once through the map writer (``actor="subscription"``); answers the
    resulting ``{"modules": {code: bool}}`` so the client can re-render."""
    from billing.services import entity_modules
    from billing.services.checkout import CheckoutError, start_module_trials

    codes = payload.get("codes") or []
    try:
        started = start_module_trials(entity, user, codes)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    data, status = {"modules": {}}, 200
    for code in started:
        data, status = entity_modules.set_entity_module(
            str(entity.id), code, True, actor="subscription", user_id=str(user.id)
        )
        if status != 200:
            return _respond(data, status)
    return _respond(data, status)


def _fmt_day(moment):
    return moment.strftime("%d %b %Y") if moment else None


def _resume_preview(request, entity, user, payload):
    """What resuming cancelled modules would charge - for the dialog. Writes nothing. Body:
    ``{codes}``. Resuming COLLECTS money up front when the cancellation's extension was
    already invoiced, so this is the disclosure that action needs."""
    from billing.services.checkout import CheckoutError, preview_reinstate_modules

    codes = _codes(payload)
    if not codes:
        return error("codes are required", 400)
    try:
        p = preview_reinstate_modules(entity, user, codes)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("module resume preview failed for {}", entity.id)
        return error("Could not price that.", 500)
    return _respond(
        {
            **{k: v for k, v in p.items() if k not in ("covers_from", "covers_to", "trial_end")},
            "trial_end": _fmt_day(p.get("trial_end")),
            "covers_from": _fmt_day(p.get("covers_from")),
            "covers_to": _fmt_day(p.get("covers_to")),
            "covers_days": (
                round((p["covers_to"] - p["covers_from"]).total_seconds() / 86400)
                if p.get("covers_from") and p.get("covers_to")
                else None
            ),
        }
    )


def _subscribe_preview(request, entity, user, payload):
    """What subscribing would charge - for the confirmation dialog. Writes nothing. Body:
    ``{codes}``. The figure is the one the purchase itself will bill, not a price list."""
    from billing.services.checkout import CheckoutError, preview_subscribe_modules

    codes = _codes(payload)
    if not codes:
        return error("codes are required", 400)
    try:
        return _respond(preview_subscribe_modules(entity, user, codes))
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    except Exception:
        logger.exception("module subscribe preview failed for {}", entity.id)
        return error("Could not price that subscription.", 500)


def _cancel_preview(request, entity, user, payload):
    """What cancelling this module would do - for the confirmation dialog. Writes nothing.
    Body: ``{code, also?}`` - ``also`` names the OTHER modules going in the same click, since
    an extension is priced against everything leaving together."""
    from billing.services.checkout import CheckoutError, preview_cancel_module

    code = str(payload.get("code") or "").strip()
    if not code:
        return error("code is required", 400)
    also = _codes(payload, "also")
    try:
        preview = preview_cancel_module(entity, user, code, also)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    access_end = preview.get("access_end")
    return _respond(
        {
            "kind": preview.get("kind"),
            "access_until": access_end.strftime("%B %d, %Y") if access_end else None,
            "access_days": preview.get("access_days"),
            "amount_formatted": preview.get("amount_formatted"),
            "currency": preview.get("currency"),
            "charged_now": preview.get("charged_now", False),
            "remaining": preview.get("remaining") or [],
            "remaining_amount": preview.get("remaining_amount"),
            "leaving_label": preview.get("leaving_label"),
            "leaving_total_formatted": preview.get("leaving_total_formatted"),
            "leaving_count": preview.get("leaving_count") or 0,
            "error": preview.get("error"),
        }
    )


def _retry_payment(request, entity, user, payload):
    """Collect a past-due payer's outstanding invoice right now. No body. Answers
    ``{"ok", "status", "message"}`` (the words are ``api._retry``'s, shared with the payer
    portal's invoice row). Charges against the same budget and deadline as the scheduled run;
    the debt settled is the one on the card THIS company is billed to."""
    from billing.api._retry import retry_answer
    from billing.services import store as sub_store
    from billing.services.dunning import retry_now

    payer_id = sub_store.payer_for_entity(str(entity.id))
    if not payer_id:
        return error("This entity has no billing account.", 409)
    try:
        result = retry_now(payer_id, str(entity.id))
    except Exception:
        logger.exception("retry-payment: collection failed for entity {}", entity.id)
        return error("We couldn't reach the card processor. Try again shortly.", 502)
    return _respond(retry_answer(result))


def _cancel(request, entity, user, payload):
    """Cancel ONE module. Body: ``{code, reason?}``. A paid module cancels under the prorated
    rule (access until max(period end, now + 30 days), the extra days billed on the next
    invoice); an app-level trial simply stops. Answers ``{"ok": true, "access_until"}``."""
    from billing.services.checkout import CheckoutError, cancel_module

    code = str(payload.get("code") or "").strip()
    if not code:
        return error("code is required", 400)
    try:
        result = cancel_module(entity, user, code, reason=payload.get("reason"))
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    access_end = result.get("access_end")
    return _respond({
        "ok": True,
        "access_until": access_end.strftime("%B %d, %Y") if access_end else None,
    })


def _renew(request, entity, user, payload):
    """Reactivate a module scheduled to cancel - in-app, no portal. Body: ``{code}``."""
    from billing.services.checkout import CheckoutError, reactivate_module

    code = str(payload.get("code") or "").strip()
    if not code:
        return error("code is required", 400)
    try:
        reactivate_module(entity, user, code)
    except CheckoutError as exc:
        return error(exc.message, exc.status)
    return _respond({"ok": True})


HANDLERS = {
    "authorize-billing": _authorize_billing,
    "activate-subscription": _activate_subscription,
    "restart-quote": _restart_quote,
    "restart-billing": _restart_billing,
    "start-trial": _start_trial,
    "resume-preview": _resume_preview,
    "subscribe-preview": _subscribe_preview,
    "cancel-preview": _cancel_preview,
    "retry-payment": _retry_payment,
    "cancel": _cancel,
    "renew": _renew,
}
assert set(HANDLERS) == set(ACTIONS)


def _run(request, entity_id: str, action: str):
    handler = HANDLERS.get(action)
    if handler is None:
        return error("Unknown action.", 404)
    entity, user = _gate(request, entity_id, permission=Permission.MODULE_MANAGE, payer=True)
    return handler(request, entity, user, body(request))


# The one Flask served as GET keeps answering GET. Declared BEFORE the catch-all, with both
# methods on one path: Django resolves URL patterns in order, and a pattern that matched the
# path but not the method answers 405 rather than trying the next. The catch-all keeps
# ``{path:action}`` so a slashed name (the deleted ``payment-methods/...``) still answers
# the JSON 404 "Unknown action." rather than Django's own page.


@modules_router.api_operation(
    ["GET", "POST"], "/{entity_id}/modules/restart-quote", summary="Price a restart (?codes= on GET)"
)
def module_restart_quote(request, entity_id: str):
    return _run(request, entity_id, "restart-quote")


@modules_router.post("/{entity_id}/modules/{path:action}", summary="One of ACTIONS")
def module_action(request, entity_id: str, action: str):
    return _run(request, entity_id, action)
