"""Card capture for onboarding Step 2.

Adding a card is OPTIONAL — it decides how the trial ends (converts to paid vs
lapses), not whether it can start. When the user does add one, it goes through a
billing account on the in-app card form (``/billing/payment-methods/*``, covered in
``test_billing_payment_methods``); the hosted setup-mode Checkout this file used to pin
was deleted on 2026-10-01. What is left here: the "has a card?" flag the wizard reads,
the payer's identity on their Stripe customer, and the settings-page nudge.

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


# --- the payer's identity on their Stripe customer -------------------------

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
    """``User.email`` is nullable. Sending email=None would blank the customer's address
    at Stripe rather than leave whatever it already holds — so it's omitted."""
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
    """A missing user must not block the customer create — the user_id stamp still has
    to go on, since being resolvable matters more than having a display name."""
    import shared_models.models as models_db
    from billing.services import checkout

    monkeypatch.setattr(models_db, "User", fake_model([]))

    assert checkout._payer_identity("u_gone") == {}


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


