"""Fixtures for the billing API suite.

Runs against SQLite with ``--no-migrations`` (see pytest.ini), tables built from the
models because ``SHARED_MODELS_MANAGED_FOR_TESTING`` is set - or against the Postgres build
of ``01_schema_rebased.sql`` when ``MINTY_TEST_PG_URI`` is set (root ``conftest.py``).

WHAT THESE TESTS CAN AND CANNOT PROVE

On SQLite they prove the service's own logic: auth decisions, response
shapes, status codes. They cannot prove the model mirrors match the real schema - SQLite
builds tables FROM the models. That gap is closed by the Postgres mode and by Minty's
``audit_models.py`` (``tests/test_zz_schema_audit.py`` there). Both are needed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from django.conf import settings
from django.test import Client

from shared_models.models import CountryInfo, Entity, User, UserEntity


def make_token(user_id, *, entity_id="", role="admin", system_role="normal", minutes=30,
               key=None, **extra):
    """A JWT shaped exactly like the one Flask mints (``_generate_module_token``):
    ``user_id``, ``entity_id`` (empty for the unscoped portal token), ``role``,
    ``system_role``, ``module``, ``sid``, the two module claims, ``exp`` and ``iat``.

    Kept as a helper rather than a fixture because half the auth tests need to vary one
    claim, and the point of those tests is that varying it changes the answer.
    """
    now = datetime.now(UTC)
    claims = {
        "user_id": str(user_id),
        "entity_id": str(entity_id or ""),
        "xero_org_id": "",
        "role": role,
        "system_role": system_role,
        "module": "billing",
        "sid": "test",
        "billing_enabled": True,
        "petty_cash_enabled": True,
        "exp": now + timedelta(minutes=minutes),
        "iat": now,
    }
    claims.update(extra)
    return jwt.encode(claims, key or settings.SECRET_KEY, algorithm="HS256")


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    return User.objects.create(
        id=str(uuid.uuid4()),
        email="payer@example.com",
        password="not-checked-here",
        first_name="Pay",
        last_name="Er",
        username="payer@example.com",
        system_role="normal",
        approved=True,
    )


@pytest.fixture
def other_user(db):
    """Somebody with no role on the entity under test - the 401 case on company routes."""
    return User.objects.create(
        id=str(uuid.uuid4()),
        email="stranger@example.com",
        password="not-checked-here",
        first_name="No",
        last_name="Access",
        username="stranger@example.com",
    )


@pytest.fixture
def countries(db):
    """HK, because ``entities.country_code`` is a real FK on Postgres."""
    CountryInfo.objects.get_or_create(
        country_code="HK",
        defaults={"alpha3_code": "HKG", "country_name_en": "Hong Kong",
                  "is_active": True, "display_order": 1},
    )


@pytest.fixture
def entity(db, user, countries):
    """A live company the ``user`` fixture administers."""
    e = Entity.objects.create(
        id=str(uuid.uuid4()), name="Payer Trading Co", country_code="HK", status="connected",
    )
    UserEntity.objects.create(user_id=user.id, entity_id=e.id, role="admin", approved=True)
    return e


@pytest.fixture
def auth(user):
    """Headers for an UNSCOPED request as ``user`` - how the portal is reached."""
    return {"HTTP_AUTHORIZATION": f"Bearer {make_token(user.id)}"}


@pytest.fixture
def auth_scoped(user, entity):
    """Headers for a request as ``user`` scoped to ``entity`` - the module page."""
    return {
        "HTTP_AUTHORIZATION": f"Bearer {make_token(user.id, entity_id=entity.id)}",
        "HTTP_X_ENTITY_ID": str(entity.id),
    }


# ---------------------------------------------------------------------------------------
# NO NETWORK. Every outbound call in this service goes through ``requests`` (the Flask
# forward in core/flask_client.py) or the Stripe SDK (billing/services/stripe_client.py,
# step 2), and none of it may reach a real host from a test. A test whose result depends on
# what is listening on localhost:5001, or on a live Stripe account, is not a test. Tests
# that need a response stub ``requests.request`` (or the Stripe client) themselves.
# ---------------------------------------------------------------------------------------


class _NetworkBlocked(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import requests

    def _refuse(*args, **kwargs):
        raise _NetworkBlocked(
            "a test tried to make a real HTTP call; stub requests.request / .post / .get"
        )

    monkeypatch.setattr(requests, "request", _refuse)
    monkeypatch.setattr(requests, "post", _refuse)
    monkeypatch.setattr(requests, "get", _refuse)
    # The Stripe SDK uses its own requests.Session, which the module-level names do not cover.
    monkeypatch.setattr(requests.Session, "request", _refuse)
