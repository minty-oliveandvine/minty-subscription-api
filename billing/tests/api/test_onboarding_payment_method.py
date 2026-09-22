"""The wizard's money routes: the HTTP half of Minty's ``tests/test_onboarding_payment_method.py``,
plus the one route that is new here, ``POST /api/onboarding/trials/start``.

What is worth pinning: the routes that name a company check the caller's MEMBERSHIP of it
(403 for a stranger, before anything runs); the billing-sheet card routes act on the payer and
pass the service's own sentence and status through; ``/billing/authorize`` nominates the card it
was given BEFORE it records consent (and ``establish_payer=True``, because during the wizard no
company has a payer yet), charges nothing, and is idempotent; the status read keeps the card and
the consent as two separate facts, and an unreachable Stripe may say "no card" but never "no
consent". And the new route: finalize's trial start, which fails LOUDLY (a failure fails
finalize), and reads ``trial_end`` back from the rows so a revisit answers the same date.

Flask's ``test_buy_now_card_routes_answer_the_onboarding_origin`` is not here: the wizard's
browser never calls this API - onboarding-backend proxies these routes server-side - so the
origin question does not arise.
"""

from __future__ import annotations

import pytest

from billing.tests.api.conftest import bearer, post_json
from billing.tests.engine.conftest import seed_modules, seed_plans, seed_policy
from shared_models.models import EntityBillingConsent, EntityModuleSubscription

pytestmark = pytest.mark.django_db

NOT_MINE = "0a7d0e6e-0000-4000-8000-00000000e002"  # an entity the user is no member of


@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/onboarding/payment-method?entity_id=e1"),
    ("post", "/api/onboarding/payment-method/setup"),
    ("post", "/api/onboarding/payment-method/complete"),
    ("get", "/api/onboarding/billing/payment-methods"),
    ("post", "/api/onboarding/billing/payment-methods/setup-intent"),
    ("post", "/api/onboarding/billing/payment-methods/confirm"),
    ("post", "/api/onboarding/billing/payment-methods/default"),
    ("get", "/api/onboarding/billing/accounts"),
    ("post", "/api/onboarding/billing/accounts"),
    ("post", "/api/onboarding/billing/authorize"),
    ("post", "/api/onboarding/trials/start"),
])
def test_every_onboarding_route_requires_a_token(client, method, path):
    if method == "get":
        assert client.get(path).status_code == 401
    else:
        assert post_json(client, path, {}).status_code == 401


@pytest.mark.parametrize("path", [
    "/api/onboarding/payment-method/setup",
    "/api/onboarding/payment-method/complete",
    "/api/onboarding/billing/authorize",
    "/api/onboarding/trials/start",
])
def test_the_company_routes_require_membership(client, user, path):
    """A token for a user who isn't a member of the entity gets 403, not the answer."""
    res = post_json(client, path, {"entity_id": NOT_MINE, "session_id": "cs_1"}, **bearer(user))
    assert res.status_code == 403
    assert res.json() == {"error": "You don't have access to this entity"}


def test_payment_method_status_endpoint_requires_membership(client, user):
    res = client.get(f"/api/onboarding/payment-method?entity_id={NOT_MINE}", **bearer(user))
    assert res.status_code == 403


def test_the_company_routes_need_an_entity_id(client, user):
    res = post_json(client, "/api/onboarding/billing/authorize", {}, **bearer(user))
    assert res.status_code == 400
    assert res.json() == {"error": "entity_id is required"}


# --- The billing sheet's card routes --------------------------------------------------------


def test_buy_now_card_route_reports_the_service_error_verbatim(client, user, monkeypatch):
    """``PaymentMethodError`` messages are written for the payer, so they're passed through
    with their own status rather than flattened to a generic 500."""
    from billing.services import payment_methods

    def _boom(user_id):
        raise payment_methods.PaymentMethodError(
            "Card payments aren't configured on this environment.", status=503
        )

    monkeypatch.setattr(payment_methods, "start_setup", _boom)

    res = post_json(client, "/api/onboarding/billing/payment-methods/setup-intent", {}, **bearer(user))

    assert res.status_code == 503
    assert "aren't configured" in res.json()["error"]


def test_the_list_is_the_payers_own(client, user, monkeypatch):
    from billing.services import payment_methods

    seen = []
    monkeypatch.setattr(
        payment_methods, "list_for_user",
        lambda uid: seen.append(uid) or {"has_account": True, "default_id": "pm_1", "methods": [], "total": 0},
    )

    res = client.get("/api/onboarding/billing/payment-methods", **bearer(user))

    assert res.status_code == 200
    assert res.json()["default_id"] == "pm_1"
    assert seen == [str(user.id)]


def test_confirm_makes_the_new_card_the_default_when_asked(client, user, monkeypatch):
    """A card added inside Buy now is the one the payer was shown, so it must be the one
    that gets charged - the flag reaches the service rather than being dropped."""
    from billing.services import payment_methods

    seen = {}

    def _confirm(user_id, setup_intent, *, make_default=False, billing_group_id=None,
                 billing_email=None, billing_company=None):
        seen.update(
            user_id=user_id, setup_intent=setup_intent, make_default=make_default,
            billing_group_id=billing_group_id, billing_email=billing_email,
            billing_company=billing_company,
        )
        return {"has_account": True, "default_id": "pm_new", "methods": [], "total": 1}

    monkeypatch.setattr(payment_methods, "confirm_setup", _confirm)

    res = post_json(
        client, "/api/onboarding/billing/payment-methods/confirm",
        {"setup_intent": "seti_1", "make_default": True}, **bearer(user),
    )

    assert res.status_code == 200
    # The billing-account fields are pinned as ABSENT too: a body that names no account
    # must not open one.
    assert seen == {
        "user_id": str(user.id), "setup_intent": "seti_1", "make_default": True,
        "billing_group_id": None, "billing_email": None, "billing_company": None,
    }


def test_opening_an_account_needs_a_saved_card(client, user):
    res = post_json(client, "/api/onboarding/billing/accounts", {}, **bearer(user))
    assert res.status_code == 400
    assert res.json() == {"error": "A saved card is required to open a billing account."}


def test_opening_an_account_proves_ownership_first(client, user, monkeypatch):
    from billing.services import payment_methods
    from billing.services import store as sub_store

    def _not_mine(uid, pm_id):
        raise payment_methods.PaymentMethodError("That payment method was not found.", 404)

    monkeypatch.setattr(payment_methods, "_owned", _not_mine)
    monkeypatch.setattr(sub_store, "create_billing_account", lambda *a, **k: pytest.fail("must not open"))

    res = post_json(
        client, "/api/onboarding/billing/accounts", {"payment_method": "pm_theirs"}, **bearer(user),
    )

    assert res.status_code == 404


# --- Consent: /billing/authorize --------------------------------------------------------------


def test_authorize_records_consent_for_this_entity_and_charges_nothing(client, user, other_user, entity, monkeypatch):
    """Buy now's actual effect: a consent row, and no Stripe call of any kind (the autouse
    ``_no_stripe`` would fail one loudly)."""
    from billing.services import checkout, store

    def _never(*a, **k):
        raise AssertionError("Buy now must not subscribe or charge anything")

    monkeypatch.setattr(checkout, "start_modules_checkout", _never)

    res = post_json(client, "/api/onboarding/billing/authorize", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 200
    assert res.json() == {"has_billing_consent": True}
    assert store.has_billing_consent(str(entity.id), str(user.id)) is True
    # Per (entity, PAYER) - someone else's agreement is not this payer's.
    assert store.has_billing_consent(str(entity.id), str(other_user.id)) is False


def test_authorize_nominates_the_card_it_was_given(client, user, entity, monkeypatch):
    """Sending the id says which card THIS company goes on and moves nothing else -
    ``establish_payer`` because during the wizard the company has no module rows, so no
    payer, and without the flag the real ``set_for_entity`` refuses."""
    from billing.services import payment_methods

    seen = {}

    def _set_for_entity(user_id, ent, pm_id, *, source="chosen", establish_payer=False):
        seen.update(user_id=user_id, entity_id=str(ent), payment_method=pm_id, establish_payer=establish_payer)
        return {"methods": [], "nominated_id": pm_id}

    monkeypatch.setattr(payment_methods, "set_for_entity", _set_for_entity)

    res = post_json(
        client, "/api/onboarding/billing/authorize",
        {"entity_id": str(entity.id), "payment_method": "pm_chosen"}, **bearer(user),
    )

    assert res.status_code == 200
    assert seen == {
        "user_id": str(user.id), "entity_id": str(entity.id),
        "payment_method": "pm_chosen", "establish_payer": True,
    }


def test_authorize_nominates_before_it_records_consent(client, user, entity, monkeypatch):
    """Order, not just presence: a nomination that fails must take the consent down with it."""
    from billing.services import checkout, payment_methods, store

    def _refuse(*a, **k):
        raise payment_methods.PaymentMethodError("That payment method couldn't be found.", status=404)

    monkeypatch.setattr(payment_methods, "set_for_entity", _refuse)
    monkeypatch.setattr(
        checkout, "authorize_entity_billing",
        lambda *a, **k: pytest.fail("consent must not be recorded when the card is refused"),
    )

    res = post_json(
        client, "/api/onboarding/billing/authorize",
        {"entity_id": str(entity.id), "payment_method": "pm_someone_elses"}, **bearer(user),
    )

    assert res.status_code == 404
    assert store.has_billing_consent(str(entity.id), str(user.id)) is False


def test_authorize_without_a_card_still_works(client, user, entity):
    from billing.services import store

    res = post_json(client, "/api/onboarding/billing/authorize", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 200
    assert store.has_billing_consent(str(entity.id), str(user.id)) is True


def test_authorize_is_idempotent(client, user, entity):
    """A double-click, or a sheet re-opened before the status read caught up."""
    for _ in range(2):
        res = post_json(client, "/api/onboarding/billing/authorize", {"entity_id": str(entity.id)}, **bearer(user))
        assert res.status_code == 200

    assert EntityBillingConsent.objects.filter(entity_id=str(entity.id), user_id=str(user.id)).count() == 1


def test_authorize_reports_a_surprise_in_its_own_words(client, user, entity, monkeypatch):
    from billing.services import checkout

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(checkout, "authorize_entity_billing", _boom)

    res = post_json(client, "/api/onboarding/billing/authorize", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 500
    assert res.json() == {"error": "Could not confirm billing. Please try again."}


# --- Step 2's status read ---------------------------------------------------------------------


def test_status_reports_card_and_consent_separately(client, user, entity, monkeypatch):
    """The card belongs to the PAYER and is shared by every entity they pay for; only
    consent is about this entity. ``card`` is THIS ENTITY'S nominated card - null here
    because nothing has been nominated, not a fallback to the account default."""
    from billing.services import checkout, store

    monkeypatch.setattr(checkout, "entity_has_payment_method", lambda e: True)
    url = f"/api/onboarding/payment-method?entity_id={entity.id}"

    before = client.get(url, **bearer(user)).json()
    assert before == {"has_payment_method": True, "has_billing_consent": False, "card": None}

    store.record_billing_consent(str(entity.id), str(user.id), "confirmed")

    after = client.get(url, **bearer(user)).json()
    assert after == {"has_payment_method": True, "has_billing_consent": True, "card": None}


def test_status_still_reports_consent_when_stripe_is_down(client, user, entity, monkeypatch):
    """The card read may fail closed (no card); the consent read is local and must not be
    dragged down with it, or a payer who has already bought is offered Buy now again."""
    from billing.services import checkout, store

    store.record_billing_consent(str(entity.id), str(user.id), "confirmed")

    def _down(_entity):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(checkout, "entity_has_payment_method", _down)

    body = client.get(f"/api/onboarding/payment-method?entity_id={entity.id}", **bearer(user)).json()

    assert body == {"has_payment_method": False, "has_billing_consent": True, "card": None}


def test_status_describes_the_nominated_card_from_the_wallet(client, user, entity, monkeypatch):
    from billing.services import checkout, payment_methods
    from billing.services import store as sub_store

    monkeypatch.setattr(checkout, "entity_has_payment_method", lambda e: True)
    monkeypatch.setattr(sub_store, "card_for_entity", lambda eid, uid=None: "pm_2")
    monkeypatch.setattr(
        payment_methods, "list_for_user",
        lambda uid: {"methods": [{"id": "pm_1", "label": "Visa •••• 4242"}, {"id": "pm_2", "label": "Visa •••• 1111"}]},
    )

    body = client.get(f"/api/onboarding/payment-method?entity_id={entity.id}", **bearer(user)).json()

    assert body["card"] == {"id": "pm_2", "label": "Visa •••• 1111"}


# --- The hosted card capture ------------------------------------------------------------------


def test_setup_returns_the_browser_to_the_wizard(client, user, entity, monkeypatch, settings):
    from billing.services import checkout

    seen = {}

    def _setup(ent, usr, success_url, cancel_url):
        seen.update(entity=str(ent.id), user=str(usr.id), success_url=success_url, cancel_url=cancel_url)
        return {"url": "https://stripe.test/setup"}

    monkeypatch.setattr(checkout, "start_payment_method_setup", _setup)

    res = post_json(client, "/api/onboarding/payment-method/setup", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 200 and res.json() == {"url": "https://stripe.test/setup"}
    assert seen == {
        "entity": str(entity.id), "user": str(user.id),
        "success_url": f"{settings.ONBOARDING_WEB_URL}/?pm_session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.ONBOARDING_WEB_URL}/?pm_cancelled=1",
    }


def test_setup_reports_stripe_being_down_as_a_503(client, user, entity, monkeypatch):
    from billing.services import checkout

    def _down(*a, **k):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(checkout, "start_payment_method_setup", _down)

    res = post_json(client, "/api/onboarding/payment-method/setup", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 503
    assert res.json() == {"error": "Could not open the payment form. Please try again."}


def test_complete_needs_a_session_and_saves_the_card(client, user, entity, monkeypatch):
    from billing.services import checkout

    seen = []
    monkeypatch.setattr(checkout, "complete_payment_method_setup", lambda ent, sid: seen.append((str(ent.id), sid)) or True)

    missing = post_json(client, "/api/onboarding/payment-method/complete", {"entity_id": str(entity.id)}, **bearer(user))
    assert missing.status_code == 400 and missing.json() == {"error": "session_id is required"}

    res = post_json(
        client, "/api/onboarding/payment-method/complete",
        {"entity_id": str(entity.id), "session_id": "cs_1"}, **bearer(user),
    )
    assert res.status_code == 200 and res.json() == {"has_payment_method": True}
    assert seen == [(str(entity.id), "cs_1")]


# --- Finalize's trial start ------------------------------------------------------------------


@pytest.fixture
def catalogue(db):
    """Both modules, their plans and the policy - what a real trial start reads."""
    seed_modules()
    seed_plans()
    seed_policy()


def _enable(entity, user, code="PETTY_CASH"):
    from billing.services import entity_modules

    data, status = entity_modules.set_entity_module(str(entity.id), code, True, actor="settings_ui", user_id=str(user.id))
    assert status == 200, data


def test_trials_start_opens_the_trial_and_states_its_end(client, user, entity, catalogue):
    """The real services, no Stripe: a card-free trial needs none. The date is the row's."""
    _enable(entity, user)

    res = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 200, res.content
    row = EntityModuleSubscription.objects.get(entity_id=str(entity.id), function_code="PETTY_CASH")
    assert row.phase == "trial"
    assert str(row.payer_user_id) == str(user.id)
    assert res.json() == {"trial_end": row.trial_end.isoformat()}


def test_trials_start_is_idempotent(client, user, entity, catalogue):
    """A revisit of the All Set screen: the same date back, still one trial."""
    _enable(entity, user)

    first = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user)).json()
    second = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user)).json()

    assert first == second
    assert EntityModuleSubscription.objects.filter(entity_id=str(entity.id)).count() == 1


def test_trials_start_with_nothing_enabled_states_no_trial(client, user, entity, catalogue):
    """Null is a legitimate answer: the screen omits the date rather than inventing one."""
    res = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user))
    assert res.status_code == 200
    assert res.json() == {"trial_end": None}
    assert not EntityModuleSubscription.objects.filter(entity_id=str(entity.id)).exists()


def test_trials_start_fails_loudly(client, user, entity, monkeypatch):
    """Flask's finalize started trials best-effort and swallowed the failure; here a failure
    FAILS, so onboarding-backend's finalize fails and the All Set screen offers Try again."""
    from billing.services import checkout

    def _boom(ent, usr):
        raise RuntimeError("the catalog is empty")

    monkeypatch.setattr(checkout, "start_trials_for_enabled_modules", _boom)

    res = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 502
    assert res.json() == {"error": "The trial could not be started. Please try again."}

    monkeypatch.setattr(
        checkout, "start_trials_for_enabled_modules",
        lambda ent, usr: (_ for _ in ()).throw(checkout.CheckoutError("Subscriptions are temporarily unavailable.", 503)),
    )
    refused = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user))
    assert refused.status_code == 503
    assert refused.json() == {"error": "Subscriptions are temporarily unavailable."}
