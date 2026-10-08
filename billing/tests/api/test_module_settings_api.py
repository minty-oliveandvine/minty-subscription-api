"""The module settings page's routes: the page model and the ten actions.

Flask checked these routes STRUCTURALLY - ``tests/test_subscription_payer_permission.py`` and
``tests/test_restart_billing_guard.py`` read the decorator stack and the executable body of
``entity/routes/settings.py`` with regexes, because a guard there was a decorator a route could
simply forget. Here the guard is one function every action runs through, so the same rules are
pinned BEHAVIOURALLY, by calling every action as the wrong person:

* **Only the payer may change what a company is billed for.** ``MODULE_MANAGE`` says who may
  administer the entity; the payer is whose card every button spends. A co-admin is refused
  on every action (403, the sentence the page shows) and the service behind it is never
  called. An entity with no payer yet is open to any admin - starting the first trial is what
  makes the payer.
* **Being the payer is necessary, not sufficient.** A cashier who happens to hold the card is
  still refused: the permission is checked too.
* **The restart route's refusals, in order** (from ``test_restart_billing_guard``): a genuine
  lapse (409), codes that lapsed (422, refused whole), a card nominated for THIS company
  before the charge (402), then the subscribe priced from the resolved codes - and it never
  answers ``{url}``.
* **No action hands the browser to Stripe.** The nine actions behind setup-mode Checkout,
  the Billing Portal and the old card routes were deleted on 2026-10-01 and answer 404.

And what is new to the API: the page model's wire shape (Flask's card dict with ISO dates, the
payer when it is somebody else, the viewer).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from billing.tests.api.conftest import ORIGIN, bearer, post_json
from shared_models.models import UserEntity

pytestmark = pytest.mark.django_db

# Every action - each spends or commits money, or prices what would. Flask's MONEY_ROUTES plus
# the two previews, which carried the same guard. There is no unguarded action any more.
MONEY_ACTIONS = (
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

# Deleted 2026-10-01 (a card is only ever added through a billing account, in-app).
REMOVED_ACTIONS = (
    "checkout",
    "confirm-billing",
    "checkout-complete",
    "payment-method",
    "manage-billing",
    "payment-methods",
    "payment-methods/setup-intent",
    "payment-methods/confirm",
    "payment-methods/default",
)


def _page(entity):
    return f"/api/entities/{entity.id}/modules"


def _scoped(user, entity):
    """A token for ``user`` scoped to ``entity``, with the header minty-web always sends."""
    return {**bearer(user, entity_id=entity.id), "HTTP_X_ENTITY_ID": str(entity.id)}


def _act(client, user, entity, action, payload=None):
    return post_json(client, f"{_page(entity)}/{action}", payload or {}, HTTP_ORIGIN=ORIGIN, **_scoped(user, entity))


@pytest.fixture
def cashier(other_user, entity):
    UserEntity.objects.create(user_id=other_user.id, entity_id=entity.id, role="cashier", approved=True)
    return other_user


@pytest.fixture
def co_admin(other_user, entity):
    UserEntity.objects.create(user_id=other_user.id, entity_id=entity.id, role="admin", approved=True)
    return other_user


@pytest.fixture
def payer_is(monkeypatch):
    """Who the store says pays for any company (None: nobody yet)."""
    from billing.services import store as sub_store

    def _set(user_id):
        monkeypatch.setattr(sub_store, "payer_for_entity", lambda eid: str(user_id) if user_id else None)

    return _set


@pytest.fixture
def services(monkeypatch):
    """Every service an action calls, replaced by a recorder. ``calls`` lists ``(name, args,
    kwargs)`` in order; ``answers`` sets what a name returns (a callable is called, an
    exception instance is raised)."""
    from billing.services import (
        billing_accounts,
        checkout,
        consent,
        dunning,
        entity_modules,
        payment_methods,
    )
    from billing.services import store as sub_store

    calls: list = []
    answers: dict = {}

    def _record(name, default=None):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            answer = answers.get(name, default)
            if isinstance(answer, BaseException):
                raise answer
            return answer(*args, **kwargs) if callable(answer) else answer
        return _fn

    targets = {
        checkout: {
            "start_modules_checkout": {"created": ["PETTY_CASH"]},
            "confirm_modules_checkout": {"created": ["PETTY_CASH"]},
            "authorize_entity_billing": {"ok": True},
            "activate_entity_billing": {"ok": True, "payer_user_id": "u1"},
            "start_module_trials": ["PETTY_CASH"],
            "preview_subscribe_modules": {"total_formatted": "HK$68.00"},
            "preview_reinstate_modules": {"amount_formatted": "0.00"},
            "preview_cancel_module": {"kind": "trial"},
            "cancel_module": {"access_end": None},
            "reactivate_module": None,
        },
        payment_methods: {
            "set_for_entity": {"nominated_id": "pm_1"},
            # No module action may reach these any more; recorded so a regression shows.
            "start_setup": {"client_secret": "seti_secret"},
            "confirm_setup": {"methods": []},
        },
        billing_accounts: {
            # The picker's own move, made in the activation request.
            "move_company": {"moved": {"from_account": None}},
        },
        consent: {
            "lapsed_trial_for_entity": {"mode": None, "lapsed": []},
        },
        dunning: {
            "retry_now": {"status": "paid"},
        },
        entity_modules: {
            "set_entity_module": ({"modules": {"PETTY_CASH": True}}, 200),
        },
        sub_store: {
            "card_for_entity": "pm_1",
        },
    }
    for module, names in targets.items():
        for name, default in names.items():
            monkeypatch.setattr(module, name, _record(name, default))

    return {"calls": calls, "answers": answers}


def _names(services):
    return [name for name, _a, _k in services["calls"]]


# --- The gate --------------------------------------------------------------------------------


@pytest.mark.parametrize("action", MONEY_ACTIONS)
def test_every_money_action_requires_the_payer(client, user, entity, co_admin, payer_is, services, action):
    """Hiding the buttons is presentation. This is the permission: a second admin on someone
    else's billing relationship is refused, and nothing behind the route runs."""
    payer_is(user.id)

    response = _act(client, co_admin, entity, action, {"codes": ["PETTY_CASH"], "code": "PETTY_CASH"})

    assert response.status_code == 403, action
    assert response.json() == {
        "error": "Only the person who pays for this company can change its subscription."
    }
    assert services["calls"] == [], action
    assert response["Access-Control-Allow-Origin"] == ORIGIN


@pytest.mark.parametrize("action", MONEY_ACTIONS)
def test_every_action_requires_the_permission_even_for_the_payer(
    client, entity, cashier, payer_is, services, action
):
    """Being the payer is necessary, not sufficient - an entity admin who stops being an
    admin must not keep the buttons because they happen to hold the card."""
    payer_is(cashier.id)

    response = _act(client, cashier, entity, action, {"codes": ["PETTY_CASH"], "code": "PETTY_CASH"})

    assert response.status_code == 403, action
    assert response.json() == {
        "error": "You do not have permission to manage subscriptions for this entity."
    }
    assert services["calls"] == [], action


def test_an_entity_with_no_payer_is_open_to_any_admin(client, entity, co_admin, payer_is, services):
    """Nobody is being billed yet, so any admin may start a free trial - it commits nobody.
    Refusing here would leave an entity that no one could ever subscribe."""
    payer_is(None)

    response = _act(client, co_admin, entity, "start-trial", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 200
    assert response.json() == {"modules": {"PETTY_CASH": True}}


def test_any_admin_may_activate_a_company_nobody_pays_for(client, entity, co_admin, payer_is, services):
    """ACTIVATION is the act that makes the payer (the user, 2026-10-08), so it has to be open
    to the same people starting a trial is. The company is placed on the account named, and
    then billing is confirmed - in this one request, so the two cannot be left disagreeing."""
    payer_is(None)

    response = _act(client, co_admin, entity, "activate-subscription", {"account": "acc_1"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "charged": False}
    assert "activate_entity_billing" in _names(services)


def test_activating_without_codes_never_charges(client, entity, co_admin, payer_is, services):
    """A company can have one module trialling and another lapsed. Confirming the first must
    not quietly buy back the second, so the charge needs the modules NAMED - and the server
    still decides whether they may be restarted at all."""
    payer_is(None)
    services["answers"]["lapsed_trial_for_entity"] = {"mode": "takeover", "lapsed": []}

    response = _act(client, co_admin, entity, "activate-subscription", {"account": "acc_1"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "charged": False}
    assert "confirm_modules_checkout" not in _names(services)


def test_activating_a_company_somebody_else_pays_for_is_refused(
    client, user, entity, co_admin, payer_is, services
):
    """Once it HAS a subscriber the ordinary rule carries it: only they may act."""
    payer_is(user.id)

    response = _act(client, co_admin, entity, "activate-subscription", {"account": "acc_1"})

    assert response.status_code == 403
    assert services["calls"] == []


def test_the_page_needs_only_module_view(client, entity, cashier, payer_is):
    """A cashier reads the page - and sees no buttons."""
    payer_is(None)

    response = client.get(_page(entity), **_scoped(cashier, entity))

    assert response.status_code == 200
    assert response.json()["can_manage_modules"] is False


def test_a_path_for_another_company_is_refused(client, user, entity, countries, services):
    """The token (and header) name one company; the path names another. The role was proved
    for the first, so the second gets nothing from it."""
    from shared_models.models import Entity

    other = Entity.objects.create(id=str(uuid.uuid4()), name="Somebody Else Ltd", country_code="HK", status="connected")

    response = client.get(_page(other), **_scoped(user, entity))

    assert response.status_code == 403
    assert response.json() == {"error": "That token is for a different company."}
    assert services["calls"] == []


def test_an_unknown_action_is_a_404(client, user, entity, payer_is):
    payer_is(user.id)
    response = _act(client, user, entity, "delete-everything", {})
    assert response.status_code == 404
    assert response.json() == {"error": "Unknown action."}


@pytest.mark.parametrize("action", REMOVED_ACTIONS)
def test_the_hosted_stripe_actions_are_gone(client, user, entity, payer_is, services, action):
    """Setup-mode Checkout, the Billing Portal and the old card routes: each is now just an
    unknown word - 404, and nothing behind any of them runs."""
    payer_is(user.id)

    response = _act(client, user, entity, action, {"codes": ["PETTY_CASH"], "session_id": "cs_1"})

    assert response.status_code == 404, action
    assert response.json() == {"error": "Unknown action."}
    assert services["calls"] == [], action


def test_the_card_list_no_longer_answers_get(client, user, entity, payer_is, services):
    """Flask served ``payment-methods`` on GET too; the GET route went with it."""
    payer_is(user.id)
    response = client.get(f"{_page(entity)}/payment-methods", **_scoped(user, entity))
    assert response.status_code in (404, 405)
    assert services["calls"] == []


def test_a_company_that_does_not_exist_is_a_404(client, user):
    ghost = str(uuid.uuid4())
    response = client.get(
        f"/api/entities/{ghost}/modules",
        **{**bearer(user, entity_id=ghost), "HTTP_X_ENTITY_ID": ghost},
    )
    # EntityBearerAuth refuses first: no role on a company that does not exist.
    assert response.status_code in (401, 404)


# --- The page model --------------------------------------------------------------------------


def _card(**over):
    """Flask's card dict, as ``cards.get_module_cards`` builds it (the keys the client types)."""
    card = {
        "code": "PETTY_CASH", "name": "Petty Cash", "description": "Track sales.", "image": "",
        "learn_more": "#", "is_subscribed": True, "trial_eligible": False, "trial_closing": False,
        "trial_expired": False, "lapsed_long": None, "has_access": True, "subscription_id": None,
        "subscription_status": "active", "can_cancel": True, "amount": Decimal("68.00"),
        "formatted_amount": "HK$68.00", "currency_code": "HKD", "billing_interval": "month",
        "cancel_at_period_end": False, "pending_cancel": False, "trial_cancelled": False,
        "formatted_period_end": "August 19, 2026", "period_end_short": "19 Aug",
        "period_end_long": "19 Aug 2026", "period_end": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
        "conversion_charge": Decimal(0), "extension_amount": Decimal(0), "extension_formatted": None,
        "access_end_date": None, "access_end_long": None, "needs_card": False, "needs_consent_only": False,
    }
    card.update(over)
    return card


@pytest.fixture
def page_services(monkeypatch):
    """The page's reads, stubbed where they need Stripe or a seeded catalog; the rest run for
    real over the empty database."""
    from billing.services import entity_modules

    state = {"cards": [_card()]}
    monkeypatch.setattr(entity_modules, "get_module_cards", lambda eid: list(state["cards"]))
    monkeypatch.setattr(entity_modules, "build_subscription_panel", lambda cards, summary, anchor: None)
    monkeypatch.setattr(entity_modules, "build_consent_takeover", lambda eid, uid, *, can_manage, access_state=None: None)
    return state


def test_the_page_model_carries_flasks_card_with_iso_dates(client, user, entity, payer_is, page_services):
    """The card is Flask's dict key for key - what minty-web's ``ModuleCard`` types - and the
    two dates the client COUNTS from travel ISO: ``period_end`` as the encoder writes a
    datetime, ``access_end_date`` re-shaped from the words Flask's template printed."""
    payer_is(user.id)
    page_services["cards"] = [_card(
        pending_cancel=True, cancel_at_period_end=True, can_cancel=False,
        access_end_date="August 19, 2026", access_end_long="19 Aug 2026",
    )]

    response = client.get(_page(entity), HTTP_ORIGIN=ORIGIN, **_scoped(user, entity))

    assert response.status_code == 200
    page = response.json()
    assert page["entity_id"] == str(entity.id)
    assert page["entity_name"] == "Payer Trading Co"
    (card,) = page["cards"]
    assert card["code"] == "PETTY_CASH"
    assert card["period_end"] == "2026-08-19T00:00:00+00:00"
    assert card["access_end_date"] == "2026-08-19"
    assert card["period_end_long"] == "19 Aug 2026"
    assert card["formatted_amount"] == "HK$68.00"
    assert card["amount"] == "68.00"
    assert card["pending_cancel"] is True
    assert page["can_manage_modules"] is True
    assert page["payer"] is None
    assert page["viewer"] == {"name": "Pay Er", "initials": "PE"}
    assert page["consent_takeover"] is None
    assert page["panel"] is None
    assert "summary" in page and "next_payment_date" in page
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_the_payer_is_named_when_it_is_somebody_else(client, entity, co_admin, user, payer_is, page_services):
    """A co-admin sees the cards, no buttons, and who to ask."""
    payer_is(user.id)

    response = client.get(_page(entity), **_scoped(co_admin, entity))

    assert response.status_code == 200
    page = response.json()
    assert page["can_manage_modules"] is False
    assert page["payer"] == {"user_id": str(user.id), "name": "Pay Er", "email": "payer@example.com"}


def test_the_payer_sees_no_payer_block_for_themselves(client, user, entity, payer_is, page_services):
    payer_is(user.id)
    page = client.get(_page(entity), **_scoped(user, entity)).json()
    assert page["payer"] is None and page["can_manage_modules"] is True


def test_the_page_works_from_the_portal_with_an_unscoped_token_and_the_header(client, user, entity, payer_is, page_services):
    """Reached from the portal: an unscoped token, the company only in ``X-Entity-Id``."""
    payer_is(None)
    response = client.get(_page(entity), HTTP_X_ENTITY_ID=str(entity.id), **bearer(user))
    assert response.status_code == 200
    assert response.json()["entity_id"] == str(entity.id)


def test_the_page_over_an_empty_database_is_still_a_page(client, user, entity):
    """No stubs: the real reads over a company with nothing. Whatever the catalog holds, the
    answer is a 200 the client can render, never a 500."""
    response = client.get(_page(entity), **_scoped(user, entity))
    assert response.status_code == 200
    page = response.json()
    assert isinstance(page["cards"], list)
    assert page["can_manage_modules"] is True  # admin, and no payer yet


# --- The money routes nominate through the ownership proof -----------------------------------


@pytest.mark.parametrize("action", ["authorize-billing", "restart-billing"])
def test_nominating_goes_through_the_ownership_proof(client, user, entity, payer_is, services, action):
    """Another payer's ``pm_...`` answers "not found" rather than being nominated onto
    anything - and nothing is charged after the refusal."""
    from billing.services.payment_methods import PaymentMethodError

    payer_is(user.id)
    services["answers"]["set_for_entity"] = PaymentMethodError("That payment method was not found.", 404)
    services["answers"]["lapsed_trial_for_entity"] = {
        "mode": "takeover", "lapsed": [{"code": "PETTY_CASH", "name": "Petty Cash", "lapsed_on": None}],
    }

    response = _act(client, user, entity, action, {"codes": ["PETTY_CASH"], "payment_method": "pm_theirs"})

    assert response.status_code == 404
    assert response.json() == {"error": "That payment method was not found."}
    assert not {"start_modules_checkout", "confirm_modules_checkout", "authorize_entity_billing"} & set(_names(services))


# --- The restart route: four refusals, in order ----------------------------------------------


def _lapsed(mode="takeover", codes=("PETTY_CASH",)):
    return {
        "mode": mode,
        "lapsed": [{"code": c, "name": c.title(), "lapsed_on": None} for c in codes],
        "payer_user_id": "u1",
        "has_card": True,
        "has_consent": False,
    }


def test_an_entity_with_no_lapse_is_refused(client, user, entity, payer_is, services):
    """The check that stops this URL charging a company that is running fine."""
    payer_is(user.id)

    response = _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 409
    assert response.json() == {"error": "There is nothing to restart for this company."}
    assert "confirm_modules_checkout" not in _names(services)


@pytest.mark.parametrize("mode", ["takeover", "panel"])
def test_both_modes_may_restart(client, user, entity, payer_is, services, mode):
    """The panel's button posts to the same route - a part-lapsed entity has just as much
    right to buy its module back as a fully lapsed one."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed(mode)

    response = _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "restarted": ["PETTY_CASH"]}


@pytest.mark.parametrize("requested", [
    [],                                  # nothing ticked
    ["PAYMENT_REQUEST"],                 # did not lapse
    ["PETTY_CASH", "PAYMENT_REQUEST"],   # one good, one not - refused whole
    ["NOT_A_MODULE"],                    # not a module at all
])
def test_a_set_that_cannot_be_honoured_is_refused(client, user, entity, payer_is, services, requested):
    """Refused OUTRIGHT rather than filtered down: silently dropping the bad code would charge
    for a different set than the payer submitted."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed(codes=("PETTY_CASH",))

    response = _act(client, user, entity, "restart-billing", {"codes": requested, "payment_method": "pm_1"})

    assert response.status_code == 422
    assert response.json() == {"error": "Choose at least one module to restart."}
    # The codes are resolved BEFORE the card is touched: no nomination, no charge.
    assert _names(services) == ["lapsed_trial_for_entity"]


def test_picking_one_of_two_lapsed_modules_is_allowed(client, user, entity, payer_is, services):
    """Forcing the bundle would sell a module the customer may have let go on purpose."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed(codes=("PETTY_CASH", "PAYMENT_REQUEST"))

    response = _act(client, user, entity, "restart-billing", {"codes": ["payment_request"]})

    assert response.status_code == 200
    assert response.json()["restarted"] == ["PETTY_CASH"]  # the recorder's default answer
    _name, _args, kwargs = services["calls"][-1]
    assert kwargs["requested_codes"] == ["PAYMENT_REQUEST"], "priced from the RESOLVED codes"


def test_the_card_is_nominated_before_anything_is_charged(client, user, entity, payer_is, services):
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed()

    response = _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"], "payment_method": "pm_9"})

    assert response.status_code == 200
    assert _names(services) == [
        "lapsed_trial_for_entity", "set_for_entity", "card_for_entity", "confirm_modules_checkout",
    ]


def test_a_payer_with_no_card_is_refused_before_the_charge(client, user, entity, payer_is, services):
    """402, and asked about the COMPANY: a payer with three cards saved and none of them put
    on this company cannot be billed for it."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed()
    services["answers"]["card_for_entity"] = None

    response = _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 402
    assert response.json() == {"error": "Choose a card before restarting billing."}
    assert "confirm_modules_checkout" not in _names(services)


def test_a_restart_is_charged_with_no_return_urls(client, user, entity, payer_is, services):
    """The engine takes the company and the codes - nothing that could send the browser to a
    Stripe page."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed()

    _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"]})

    name, _args, kwargs = services["calls"][-1]
    assert name == "confirm_modules_checkout"
    assert kwargs == {"requested_codes": ["PETTY_CASH"]}


def test_a_restart_never_answers_a_url(client, user, entity, payer_is, services):
    """Even a service answer carrying ``url`` is not handed to the browser: the answer is
    always ``{ok, restarted}`` or an error."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed()
    services["answers"]["confirm_modules_checkout"] = {"url": "https://stripe.test/setup"}

    response = _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 200
    assert "url" not in response.json()
    assert response.json() == {"ok": True, "restarted": ["PETTY_CASH"]}


def test_a_restart_the_engine_finds_cardless_is_its_402(client, user, entity, payer_is, services):
    """The card can vanish between the route's check and the charge: the engine's own 402
    comes back as it is, never as a hosted fallback."""
    from billing.services.checkout import CheckoutError

    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed()
    services["answers"]["confirm_modules_checkout"] = CheckoutError(
        "Choose a card before subscribing.", 402
    )

    response = _act(client, user, entity, "restart-billing", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 402
    assert response.json() == {"error": "Choose a card before subscribing."}


def test_the_quote_route_resolves_codes_the_same_way(client, user, entity, payer_is, services):
    """The quote and the charge MUST name the same set, or the payer is shown one number and
    billed against another. Both the GET Flask served (``?codes=``) and a POST body."""
    payer_is(user.id)
    services["answers"]["lapsed_trial_for_entity"] = _lapsed(codes=("PETTY_CASH", "PAYMENT_REQUEST"))

    by_get = client.get(f"{_page(entity)}/restart-quote?codes=payment_request", **_scoped(user, entity))
    by_post = _act(client, user, entity, "restart-quote", {"codes": ["payment_request"]})

    assert by_get.status_code == by_post.status_code == 200
    assert by_get.json() == by_post.json() == {"total_formatted": "HK$68.00"}
    priced = [args for name, args, _k in services["calls"] if name == "preview_subscribe_modules"]
    assert [a[2] for a in priced] == [["PAYMENT_REQUEST"], ["PAYMENT_REQUEST"]]

    refused = _act(client, user, entity, "restart-quote", {"codes": ["NOT_A_MODULE"]})
    assert refused.status_code == 422


# --- The other actions, answer for answer -----------------------------------------------------


def test_start_trial_grants_access_through_the_map_writer(client, user, entity, payer_is, services):
    payer_is(None)

    response = _act(client, user, entity, "start-trial", {"codes": ["PETTY_CASH"]})

    assert response.status_code == 200
    assert response.json() == {"modules": {"PETTY_CASH": True}}
    written = [(args, kwargs) for name, args, kwargs in services["calls"] if name == "set_entity_module"]
    assert written == [((str(entity.id), "PETTY_CASH", True), {"actor": "subscription", "user_id": str(user.id)})]


def test_start_trial_with_nothing_started_answers_an_empty_map(client, user, entity, payer_is, services):
    payer_is(None)
    services["answers"]["start_module_trials"] = []
    response = _act(client, user, entity, "start-trial", {"codes": []})
    assert response.status_code == 200 and response.json() == {"modules": {}}


def test_authorize_billing_nominates_then_consents(client, user, entity, payer_is, services):
    payer_is(user.id)

    response = _act(client, user, entity, "authorize-billing", {"payment_method": "pm_2"})

    assert response.status_code == 200 and response.json() == {"ok": True}
    assert _names(services) == ["set_for_entity", "authorize_entity_billing"]

    services["calls"].clear()
    assert _act(client, user, entity, "authorize-billing", {}).status_code == 200
    assert _names(services) == ["authorize_entity_billing"]


def test_the_previews_need_their_codes(client, user, entity, payer_is, services):
    payer_is(user.id)
    for action, key in (("resume-preview", "codes are required"), ("subscribe-preview", "codes are required"),
                        ("cancel-preview", "code is required"), ("cancel", "code is required"),
                        ("renew", "code is required")):
        response = _act(client, user, entity, action, {})
        assert response.status_code == 400, action
        assert response.json() == {"error": key}, action
    assert services["calls"] == []


def test_resume_preview_formats_its_dates_and_counts_the_days(client, user, entity, payer_is, services):
    payer_is(user.id)
    services["answers"]["preview_reinstate_modules"] = {
        "amount_formatted": "45.00", "charged_now": True,
        "covers_from": datetime(2026, 8, 19, tzinfo=UTC), "covers_to": datetime(2026, 9, 18, tzinfo=UTC),
        "trial_end": None,
    }

    response = _act(client, user, entity, "resume-preview", {"codes": ["petty_cash"]})

    assert response.status_code == 200
    assert response.json() == {
        "amount_formatted": "45.00", "charged_now": True,
        "covers_from": "19 Aug 2026", "covers_to": "18 Sep 2026", "trial_end": None, "covers_days": 30,
    }
    _name, args, _kwargs = services["calls"][-1]
    assert args[2] == ["PETTY_CASH"]


def test_cancel_preview_formats_the_date_and_carries_the_companions(client, user, entity, payer_is, services):
    payer_is(user.id)
    services["answers"]["preview_cancel_module"] = {
        "kind": "paid", "access_end": datetime(2027, 2, 4, tzinfo=UTC), "access_days": 30,
        "amount_formatted": "120.00", "currency": "HKD", "charged_now": False,
        "remaining": ["PAYMENT_REQUEST"], "remaining_amount": "280.00",
        "leaving_label": "Petty Cash", "leaving_total_formatted": "120.00", "leaving_count": 1,
    }

    response = _act(client, user, entity, "cancel-preview", {"code": "PETTY_CASH", "also": ["payment_request"]})

    assert response.status_code == 200
    assert response.json() == {
        "kind": "paid", "access_until": "February 04, 2027", "access_days": 30,
        "amount_formatted": "120.00", "currency": "HKD", "charged_now": False,
        "remaining": ["PAYMENT_REQUEST"], "remaining_amount": "280.00",
        "leaving_label": "Petty Cash", "leaving_total_formatted": "120.00", "leaving_count": 1,
        "error": None,
    }
    _name, args, _kwargs = services["calls"][-1]
    assert args[2:] == ("PETTY_CASH", ["PAYMENT_REQUEST"])


def test_cancel_reports_when_access_ends(client, user, entity, payer_is, services):
    payer_is(user.id)
    services["answers"]["cancel_module"] = {"access_end": datetime(2027, 2, 4, tzinfo=UTC)}

    response = _act(client, user, entity, "cancel", {"code": "PETTY_CASH", "reason": "too expensive"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "access_until": "February 04, 2027"}
    _name, args, kwargs = services["calls"][-1]
    assert args[2] == "PETTY_CASH" and kwargs == {"reason": "too expensive"}


def test_retry_payment_speaks_for_the_processor(client, user, entity, payer_is, services):
    payer_is(user.id)

    paid = _act(client, user, entity, "retry-payment", {})
    assert paid.json() == {"ok": True, "status": "paid", "message": "Payment received — your subscription is active again."}
    _name, args, _kwargs = services["calls"][-1]
    assert args == (str(user.id), str(entity.id)), "the debt settled is the one on THIS company's card"

    services["answers"]["retry_now"] = {"status": "failed", "reason": "insufficient funds"}
    declined = _act(client, user, entity, "retry-payment", {})
    assert declined.json() == {"ok": False, "status": "failed", "message": "That card was declined: insufficient funds"}

    services["answers"]["retry_now"] = RuntimeError("stripe down")
    down = _act(client, user, entity, "retry-payment", {})
    assert down.status_code == 502
    assert down.json() == {"error": "We couldn't reach the card processor. Try again shortly."}


def test_retry_payment_without_a_billing_account_is_a_409(client, entity, co_admin, payer_is, services):
    payer_is(None)
    response = _act(client, co_admin, entity, "retry-payment", {})
    assert response.status_code == 409
    assert response.json() == {"error": "This entity has no billing account."}


def test_renew(client, user, entity, payer_is, services):
    payer_is(user.id)
    assert _act(client, user, entity, "renew", {"code": "PETTY_CASH"}).json() == {"ok": True}
    assert _names(services) == ["reactivate_module"]


