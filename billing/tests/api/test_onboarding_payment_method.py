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
    "/api/onboarding/billing/authorize",
    "/api/onboarding/trials/start",
])
def test_the_company_routes_require_membership(client, user, path):
    """A token for a user who isn't a member of the entity gets 403, not the answer."""
    res = post_json(client, path, {"entity_id": NOT_MINE}, **bearer(user))
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

    monkeypatch.setattr(payment_methods, "account_of", lambda uid, gid: None)

    res = post_json(
        client, "/api/onboarding/billing/payment-methods/confirm",
        {"setup_intent": "seti_1", "make_default": True, "billing_group_id": "g1"}, **bearer(user),
    )

    assert res.status_code == 200
    assert seen == {
        "user_id": str(user.id), "setup_intent": "seti_1", "make_default": True,
        "billing_group_id": "g1", "billing_email": None, "billing_company": None,
    }


def test_confirm_naming_no_account_is_refused_before_stripe(client, user, monkeypatch):
    """THE RULE (2026-10-01): no card is saved unattached to a billing account. 422 in the
    sentence the web matches, before anything is attached or created at Stripe."""
    from billing.services import payment_methods

    def _stripe(*_a, **_k):
        raise AssertionError("a confirm naming no account must not reach Stripe")

    monkeypatch.setattr(payment_methods, "retrieve_setup_intent", _stripe)
    monkeypatch.setattr(payment_methods, "attach_payment_method", _stripe)
    monkeypatch.setattr(payment_methods, "create_customer_for_user", _stripe)

    res = post_json(
        client, "/api/onboarding/billing/payment-methods/confirm",
        {"setup_intent": "seti_1", "make_default": True}, **bearer(user),
    )

    assert res.status_code == 422
    assert res.json() == {"error": "Choose a billing account for this card."}


def test_confirm_with_someone_elses_account_is_refused_before_stripe(client, user, other_user, monkeypatch):
    """Checked HERE now, not after the attach: the onboarding twin used to reach the
    ownership check only once the card was on the customer."""
    from billing.services import payment_methods
    from billing.services import store as sub_store

    theirs = sub_store.create_billing_account(other_user.id, "pm_theirs")
    monkeypatch.setattr(
        payment_methods, "retrieve_setup_intent", lambda *_a: pytest.fail("reached Stripe")
    )

    res = post_json(
        client, "/api/onboarding/billing/payment-methods/confirm",
        {"setup_intent": "seti_1", "billing_group_id": theirs.id}, **bearer(user),
    )

    assert res.status_code == 404
    assert res.json() == {"error": "That billing account couldn't be found."}


NOT_ENGLISH = "Email can only contain English letters, numbers and symbols."


@pytest.mark.parametrize("email", ["김철수@vine.test", "ap@회사.한국"])
def test_confirm_refuses_a_non_english_email_before_stripe(client, user, monkeypatch, email):
    from billing.services import payment_methods

    monkeypatch.setattr(payment_methods, "confirm_setup", lambda *a, **k: pytest.fail("reached Stripe"))

    res = post_json(
        client, "/api/onboarding/billing/payment-methods/confirm",
        {"setup_intent": "seti_1", "billing_company": "Vine", "billing_email": email}, **bearer(user),
    )

    assert res.status_code == 422
    assert res.json() == {"error": NOT_ENGLISH}


def test_opening_an_account_refuses_a_non_english_email(client, user, monkeypatch):
    from billing.services import payment_methods
    from billing.services import store as sub_store

    monkeypatch.setattr(payment_methods, "_owned", lambda uid, pm_id: None)
    monkeypatch.setattr(sub_store, "create_billing_account", lambda *a, **k: pytest.fail("must not open"))

    res = post_json(
        client, "/api/onboarding/billing/accounts",
        {"payment_method": "pm_mine", "billing_email": "김철수@vine.test"}, **bearer(user),
    )

    assert res.status_code == 422
    assert res.json() == {"error": NOT_ENGLISH}


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


@pytest.fixture
def on_account(user, entity):
    """The company already on one of the payer's billing accounts - what the sheet's card
    choice leaves behind. Authorising billing for a company on none is refused."""
    from billing.services import store

    return store.nominate_card_for_entity(str(entity.id), str(user.id), "pm_on_file", "chosen")


def test_authorize_records_consent_for_this_entity_and_charges_nothing(client, user, other_user, entity, on_account, monkeypatch):
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
    from billing.services import payment_methods, store

    seen = {}

    def _set_for_entity(user_id, ent, pm_id, *, source="chosen", establish_payer=False):
        seen.update(user_id=user_id, entity_id=str(ent), payment_method=pm_id, establish_payer=establish_payer)
        store.nominate_card_for_entity(str(ent), user_id, pm_id, source)
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


def test_authorize_for_a_company_on_no_account_is_refused_and_records_nothing(client, user, entity):
    """No fallback to the Stripe customer's default card (the user's rule, 2026-10-01): with
    no billing account chosen the answer is 402, so the screen opens the account picker, and
    no consent is written - an agreement to be billed to nothing would let the trial lapse
    having been told it would convert."""
    from billing.services import store
    from billing.services.checkout import NO_ACCOUNT_FOR_COMPANY

    res = post_json(client, "/api/onboarding/billing/authorize", {"entity_id": str(entity.id)}, **bearer(user))

    assert res.status_code == 402
    assert res.json() == {"error": NO_ACCOUNT_FOR_COMPANY}
    assert store.has_billing_consent(str(entity.id), str(user.id)) is False
    assert store.billing_group_for_entity(str(entity.id), str(user.id)) is None


def test_authorize_is_idempotent(client, user, entity, on_account):
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


# --- The hosted card capture is gone ----------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/onboarding/payment-method/setup",
    "/api/onboarding/payment-method/complete",
])
def test_the_hosted_card_capture_routes_are_gone(client, user, entity, path):
    """Deleted 2026-10-01: a card is only ever added through a billing account, in-app."""
    res = post_json(client, path, {"entity_id": str(entity.id), "session_id": "cs_1"}, **bearer(user))
    assert res.status_code in (404, 405)


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
    assert res.json() == {"error": "This trial could not be started. Mind trying again?"}

    monkeypatch.setattr(
        checkout, "start_trials_for_enabled_modules",
        lambda ent, usr: (_ for _ in ()).throw(checkout.CheckoutError("Subscriptions are temporarily unavailable.", 503)),
    )
    refused = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)}, **bearer(user))
    assert refused.status_code == 503
    assert refused.json() == {"error": "Subscriptions are temporarily unavailable."}


def _onboarding_token(user, *, iat_offset_seconds=0):
    """The token Flask mints for the wizard (``create.py``'s onboarding token), which
    minty-onboarding-api forwards here verbatim: ``user_id``, ``scope``, ``exp``, ``iat`` -
    no ``entity_id``, no role claims."""
    from datetime import UTC, datetime, timedelta

    import jwt
    from django.conf import settings

    now = datetime.now(UTC) + timedelta(seconds=iat_offset_seconds)
    claims = {"user_id": str(user.id), "scope": "onboarding",
              "iat": now, "exp": now + timedelta(minutes=60)}
    return {"HTTP_AUTHORIZATION": f"Bearer {jwt.encode(claims, settings.SECRET_KEY, algorithm='HS256')}"}


def test_the_forwarded_onboarding_token_is_accepted(client, user, entity, catalogue):
    """minty-onboarding-api's finalize calls trials/start with the wizard's own token; it
    carries no entity, so the request takes the unscoped path and membership is the handler's."""
    _enable(entity, user)
    res = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)},
                    **_onboarding_token(user))
    assert res.status_code == 200, res.content


def test_an_onboarding_token_minted_a_few_seconds_ahead_is_accepted(client, user, entity, catalogue):
    """Flask's clock running ahead of this host must not 401 a token onboarding-api accepted
    (it allows 60 s of skew; so does this service)."""
    res = post_json(client, "/api/onboarding/trials/start", {"entity_id": str(entity.id)},
                    **_onboarding_token(user, iat_offset_seconds=30))
    assert res.status_code == 200, res.content
