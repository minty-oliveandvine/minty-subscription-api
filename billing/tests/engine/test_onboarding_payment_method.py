"""Card capture for onboarding Step 2.

Adding a card is OPTIONAL — it decides how the trial ends (converts to paid vs
lapses), not whether it can start. When the user does add one, the capture is a
setup-mode Stripe Checkout that saves a card and does NOTHING else; the trials
themselves are still created at finalize. These tests pin that separation (a
card-only setup must not create subscriptions), the "has a card?" flag the wizard
reads, and the endpoints' auth contract.

NOTE: imports are done INSIDE each test (see the note in the other subscription
tests) so lazy re-imports stay mutually consistent.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from .fakes import fake_model

# The price catalog is patched BY DOTTED PATH, not via an imported reference (Minty's
# conftest re-imports project modules mid-session; kept as the module was written).
_CATALOG = "billing.services.catalog"

# user.id is a uuid (C1); SQLite refuses a non-hex literal outright. Named so the tests
# still read as "user 1 / user 2 / a stranger".
U1 = "0a7d0e6e-0000-4000-8000-000000000001"
E1 = "0a7d0e6e-0000-4000-8000-00000000e001"  # entity ids are uuid columns
NOT_MINE = "0a7d0e6e-0000-4000-8000-00000000e002"  # an entity the user is no member of
U2 = "0a7d0e6e-0000-4000-8000-000000000002"
U9 = "0a7d0e6e-0000-4000-8000-000000000009"



@pytest.fixture
def db_session(db):
    """Flask's per-test database (rows DELETEd afterwards) is pytest-django's ``db``
    (a transaction rolled back afterwards). The helpers below take it for parity."""
    return _DbShim()


class _DbShim:
    """What the ported helpers still reach for: ``db.session.refresh(row)``."""

    class session:  # noqa: N801
        @staticmethod
        def refresh(row):
            row.refresh_from_db()

        @staticmethod
        def commit():
            pass


class _FakeEntity:
    id = E1
    name = "Acme"


class _FakeUser:
    id = U1
    email = "u1@example.com"


def _plan(code="PAYMENT_REQUEST", fn_id="fn_bill"):
    from billing.services import catalog

    return catalog.PlanView(
        entity_function_id=fn_id,
        function_code=code,
        display_name=code.title().replace("_", " "),
        amount=28000,
        currency_code="HKD",
        billing_interval="month",
        billing_interval_count=1,
        is_available_for_subscription=True,
    )


# --- the card gate ---------------------------------------------------------

def test_entity_has_payment_method_reads_stripe(monkeypatch):
    from billing.services import checkout

    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: "cus_1")
    monkeypatch.setattr(checkout, "customer_default_payment_method", lambda cid: "pm_1")
    assert checkout.entity_has_payment_method(_FakeEntity()) is True

    monkeypatch.setattr(checkout, "customer_default_payment_method", lambda cid: None)
    assert checkout.entity_has_payment_method(_FakeEntity()) is False


def test_entity_without_a_stripe_customer_has_no_card(monkeypatch):
    """A brand-new entity has no Stripe customer at all — that's "no card", not a crash."""
    from billing.services import checkout

    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: None)
    assert checkout.entity_has_payment_method(_FakeEntity()) is False


def test_trial_payment_method_is_none_without_a_card(monkeypatch):
    """The card is optional, so this reports its absence rather than raising — a
    card-free trial is a supported outcome, not an error."""
    from billing.services import checkout

    monkeypatch.setattr(checkout, "customer_default_payment_method", lambda cid: None)
    assert checkout.trial_payment_method("cus_1") is None

    monkeypatch.setattr(checkout, "customer_default_payment_method", lambda cid: "pm_1")
    assert checkout.trial_payment_method("cus_1") == "pm_1"


# --- capturing the card ----------------------------------------------------

def test_setup_session_is_card_only(monkeypatch):
    """The session must NOT carry modules_to_subscribe — if it did, the paid
    completion handler would create subscriptions off a card-only capture."""
    from billing.services import checkout, store

    captured = {}

    def fake_session(customer_id, success_url, cancel_url, currency, metadata=None):
        captured.update(
            customer_id=customer_id, success_url=success_url,
            cancel_url=cancel_url, currency=currency, metadata=metadata or {},
        )
        return {"url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(f"{_CATALOG}.available_plans", lambda: [_plan()])
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_1")
    monkeypatch.setattr(checkout, "create_setup_checkout_session", fake_session)

    result = checkout.start_payment_method_setup(
        _FakeEntity(), _FakeUser(), "https://app/ok", "https://app/no"
    )

    assert result == {"url": "https://checkout.stripe.com/pay/cs_1"}
    assert captured["currency"] == "HKD"  # from the live plan, not hardcoded
    assert captured["metadata"]["purpose"] == "payment_method"
    assert "modules_to_subscribe" not in captured["metadata"]
    assert captured["customer_id"] == "cus_1"  # existing customer reused, not duplicated


def test_setup_session_creates_no_customer_for_a_new_payer(monkeypatch):
    """The invariant: opening the card form must not create a Stripe customer.

    A payer with no customer yet gets a session with customer_id=None, which makes
    create_setup_checkout_session use customer_creation="always" — so Stripe creates
    the customer at confirmation and an abandoned form leaves nothing behind.
    """
    from billing.services import checkout, store, stripe_client

    captured = {}

    def fake_session(customer_id, success_url, cancel_url, currency, metadata=None):
        captured.update(customer_id=customer_id, metadata=metadata or {})
        return {"url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(f"{_CATALOG}.available_plans", lambda: [_plan()])
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)  # no mapping row
    monkeypatch.setattr(checkout, "create_setup_checkout_session", fake_session)

    # ...and Stripe has never heard of them either. Resolution asks BOTH — a missing
    # mapping row alone doesn't mean "no customer" — so this is what makes the payer
    # genuinely new rather than merely unmapped.
    searched: list = []
    monkeypatch.setattr(
        checkout, "find_customer_by_user", lambda uid: searched.append(uid) or None
    )

    # Beyond that lookup nothing may reach the Stripe SDK. The session builder and the
    # search are both faked, so any surviving call would be an unintended write — a
    # re-added customer create being the one we actually care about.
    def _no_stripe():
        raise AssertionError("opening the card form must not call Stripe directly")

    monkeypatch.setattr(stripe_client, "get_stripe", _no_stripe)

    checkout.start_payment_method_setup(
        _FakeEntity(), _FakeUser(), "https://app/ok", "https://app/no"
    )

    assert searched == [U1]
    assert captured["customer_id"] is None
    # The payer still has to be recoverable at completion.
    assert captured["metadata"]["user_id"] == U1
    assert captured["metadata"]["entity_id"] == E1


def test_complete_payment_method_setup_saves_card_and_creates_no_subscription(monkeypatch):
    """The whole point of the card-only path: a card is saved as the default and NOT
    a single subscription is created (those come at finalize)."""
    from billing.services import changes, checkout, store

    defaults: list = []
    subs: list = []

    stamped: list = []
    mapped: list = []
    consents: list = []
    nominations: list = []

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)
    # no mapping row -> _resolve_customer_id searches Stripe for the payer; nothing there
    monkeypatch.setattr(checkout, "find_customer_by_user", lambda uid: None)
    monkeypatch.setattr(
        store, "record_billing_consent",
        lambda eid, uid, source: consents.append((eid, uid, source)),
    )
    # Capturing a card in a Checkout opened FOR this company also puts the company on
    # that card. Two records, one step: consent says the payer may be billed for it, the
    # nomination says on what.
    monkeypatch.setattr(
        store, "nominate_card_for_entity",
        lambda eid, uid, pm, source="chosen": nominations.append((eid, uid, pm, source)),
    )
    monkeypatch.setattr(
        checkout, "retrieve_checkout_session",
        lambda sid: {
            # Stripe created this at confirmation — it did not exist when the
            # session was opened.
            "customer": "cus_1",
            "setup_intent": {"payment_method": "pm_new"},
            "metadata": {
                "purpose": "payment_method", "entity_id": E1, "user_id": U1,
            },
        },
    )
    monkeypatch.setattr(
        checkout, "set_customer_identity", lambda cid, **kw: stamped.append((cid, kw))
    )
    monkeypatch.setattr(
        checkout, "_payer_identity",
        lambda uid: {"name": "Pat Payer", "description": "@patpayer",
                     "email": "pat@example.com"},
    )
    monkeypatch.setattr(
        checkout, "_seed_user_customer_mapping",
        lambda uid, cid: mapped.append((uid, cid)),
    )
    monkeypatch.setattr(
        checkout, "set_customer_default_payment_method",
        lambda cid, pm: defaults.append((cid, pm)),
    )
    # Billing is the only way money could move on this path; the card capture must not
    # reach it. (A Stripe SUBSCRIPTION can no longer be created at all — checkout
    # imports nothing that would make one — so there is nothing left to stub for that.)
    monkeypatch.setattr(
        changes, "issue_change",
        lambda *a, **kw: subs.append(kw) or {"id": "in_1", "status": "paid"},
    )

    assert checkout.complete_payment_method_setup(_FakeEntity(), "cs_1") is True
    assert defaults == [("cus_1", "pm_new")]
    assert subs == []  # nothing billed, nothing subscribed
    # The Stripe-made customer is adopted: stamped so find_customer_by_user can
    # recover it, named after the PAYER (not the entity), and mapped locally.
    assert stamped == [(
        "cus_1",
        {"metadata": {"user_id": U1}, "name": "Pat Payer",
         "description": "@patpayer", "email": "pat@example.com"},
    )]
    assert mapped == [(U1, "cus_1")]
    # Entering a card in THIS entity's Checkout is consent to bill it — otherwise the
    # payer would be asked to confirm again straight after typing their card.
    assert consents == [(E1, U1, "card")]
    # ...and it is the choice of card for it. Two records, one step. Without the second,
    # the company would be authorised to be billed and billed to nothing — its renewal
    # skipped and its trial expiring at term end having been told it would convert.
    assert nominations == [(E1, U1, "pm_new", "capture")]


def test_complete_adopts_the_payers_existing_customer_over_a_duplicate(monkeypatch):
    """Two setup sessions confirming (two tabs, a back-button replay) must not leave
    the payer billing a second customer: the pre-existing one wins and the card is
    moved onto it."""
    from billing.services import checkout, store

    defaults: list = []
    attached: list = []
    stamped: list = []

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_FIRST")
    monkeypatch.setattr(store, "record_billing_consent", lambda eid, uid, source: None)
    monkeypatch.setattr(
        store, "nominate_card_for_entity", lambda eid, uid, pm, source="chosen": None
    )
    monkeypatch.setattr(
        checkout, "retrieve_checkout_session",
        lambda sid: {
            "customer": "cus_DUPLICATE",
            "setup_intent": {"payment_method": "pm_new"},
            "metadata": {"entity_id": E1, "user_id": U1},
        },
    )
    monkeypatch.setattr(
        checkout, "attach_payment_method", lambda pm, cid: attached.append((pm, cid))
    )
    monkeypatch.setattr(
        checkout, "set_customer_identity", lambda cid, **kw: stamped.append((cid, kw))
    )
    monkeypatch.setattr(
        checkout, "set_customer_default_payment_method",
        lambda cid, pm: defaults.append((cid, pm)),
    )

    assert checkout.complete_payment_method_setup(_FakeEntity(), "cs_1") is True
    assert attached == [("pm_new", "cus_FIRST")]
    assert defaults == [("cus_FIRST", "pm_new")]
    # The duplicate must NOT be stamped — two customers carrying the same user_id
    # would make the find_customer_by_user fallback ambiguous.
    assert stamped == []


def test_payer_identity_names_the_customer_after_the_user_not_the_entity(monkeypatch):
    """One customer can pay for several entities, so an entity name would be wrong the
    moment a second entity is added. The username rides in ``description`` because
    first/last names are not unique."""
    import shared_models.models as models_db
    from billing.services import checkout

    class _Payer:
        first_name = "Pat"
        last_name = "Payer"
        username = "patpayer"
        email = "pat@example.com"

    monkeypatch.setattr(models_db, "User", fake_model([_Payer()]))

    assert checkout._payer_identity(U1) == {
        "name": "Pat Payer",
        "description": "@patpayer",
        "email": "pat@example.com",
    }


def test_payer_identity_omits_a_missing_email_rather_than_blanking_it(monkeypatch):
    """``User.email`` is nullable. Sending email=None would wipe the address Stripe
    collected at Checkout, which is the only one we'd have — so it's omitted."""
    import shared_models.models as models_db
    from billing.services import checkout

    class _Payer:
        first_name = "Pat"
        last_name = "Payer"
        username = "patpayer"
        email = None

    monkeypatch.setattr(models_db, "User", fake_model([_Payer()]))

    assert "email" not in checkout._payer_identity(U1)


def test_payer_identity_is_empty_when_the_user_is_gone(monkeypatch):
    """A missing user must not block the adoption — the user_id stamp still has to go
    on, since being resolvable matters more than having a display name."""
    import shared_models.models as models_db
    from billing.services import checkout

    monkeypatch.setattr(models_db, "User", fake_model([]))

    assert checkout._payer_identity("u_gone") == {}


def test_complete_rejects_another_entitys_session(monkeypatch):
    """A crafted session id from another entity must not save a card here.

    The binding is the session's ``metadata.entity_id`` (stamped when we opened it),
    not the customer — on a payer's first card there is no customer to compare against.
    """
    from billing.services import checkout

    monkeypatch.setattr(
        checkout, "retrieve_checkout_session",
        lambda sid: {
            "customer": "cus_OTHER",
            "setup_intent": {"payment_method": "pm_x"},
            "metadata": {"entity_id": "e_SOMEONE_ELSE", "user_id": U9},
        },
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.complete_payment_method_setup(_FakeEntity(), "cs_1")
    assert exc.value.status == 404


# --- the settings-page nudge -----------------------------------------------

def _seed_catalog(db):
    import uuid

    from shared_models.models import EntityFunction

    rows = {}
    for code, name in (("PETTY_CASH", "Petty Cash"), ("PAYMENT_REQUEST", "Payment Request")):
        row = EntityFunction(
            id=str(uuid.uuid4()), function_code=code, function_name=name,
            description=f"{name} module", is_active=True,
        )
        row.save(force_insert=True)
        rows[code] = row
    pass  # commit: autocommit under Django
    return rows


@pytest.mark.parametrize(
    "card,consent,expected_nudge,expected_consent_only",
    [
        (None, False, True, False),
        ("pm_1", False, True, True),
        ("pm_1", True, False, False),
    ],
    ids=[
        "no card -> nudge to add one",
        "card but entity not authorised -> nudge to confirm billing",
        "card + consent -> converts, no nudge",
    ],
)
def test_trialing_module_nudges_unless_the_trial_will_actually_convert(
    db_session, monkeypatch, card, consent, expected_nudge, expected_consent_only
):
    """A trial that won't convert lapses when it ends, so its settings card must warn.

    Two distinct reasons it won't convert, and the fix differs: no card at all, or a
    card the payer has never authorised for THIS entity (their card is shared across
    every entity they pay for, so it doesn't authorise this one on its own).
    """
    pytest.importorskip("billing.services.cards")  # slice D
    from billing.services import clock, store
    from billing.services import entity_modules as modules

    _seed_catalog(db_session)
    now = clock.now()

    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: consent)

    # A running app-level trial, as a module ROW. The card used to read this from a
    # live Stripe subscription view; a trial has no Stripe object at all, which is why
    # the two sources had to be merged and could disagree.
    class _Row:
        entity_id = E1
        function_code = "PAYMENT_REQUEST"
        payer_user_id = U1
        phase = "trial"
        trial_end = now + timedelta(days=20)
        app_access_until = now + timedelta(days=20)
        first_billed_at = None

    monkeypatch.setattr(modules, "_entity_customer_id", lambda eid: "cus_1")
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: [_Row()])
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: None)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: None)
    monkeypatch.setattr(f"{_CATALOG}.available_plans", lambda: [_plan()])
    # get_module_cards imports this lazily from stripe_client — patch it at the source.
    monkeypatch.setattr(
        "billing.services.stripe_client.customer_default_payment_method",
        lambda cid: card,
    )

    cards = {c["code"]: c for c in modules.get_module_cards(E1)}

    assert cards["PAYMENT_REQUEST"]["subscription_status"] == "trialing"
    assert cards["PAYMENT_REQUEST"]["needs_card"] is expected_nudge
    # Which banner to show: "add a payment method" vs "confirm billing for this company".
    assert cards["PAYMENT_REQUEST"]["needs_consent_only"] is expected_consent_only
    # A module with no subscription at all is never nudged — there's no trial to save.
    assert cards["PETTY_CASH"]["needs_card"] is False


# --- the endpoints the wizard calls ----------------------------------------


def _entity_with_member(db, user_id=U1):
    """A real entity the bearer is a member of, so the routes' membership check passes."""
    import uuid as _uuid

    from shared_models.models import Entity, User, UserEntity

    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        username=f"{user_id}@example.com",
        first_name="Pay",
        last_name="Er",
        password="x",
        system_role="normal",
        approved=True,
    )
    entity = Entity(id=str(_uuid.uuid4()), name="Acme")
    db.session.add_all([user, entity])
    pass  # flush: nothing to do under Django
    UserEntity.objects.create(user_id=user.id, entity_id=entity.id, role="admin")
    pass  # commit: autocommit under Django
    # The ID, not the instance: the request the test then makes closes the session out
    # from under it, and reading .id afterwards raises DetachedInstanceError.
    return entity.id


