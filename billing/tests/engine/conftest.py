"""Fixtures for the ported engine suite (Part 2 step 2).

The tests under this folder are Minty's ``tests/test_subscription_*`` family, ported with as
few edits as possible so they stay the regression pins they were. Three shims keep the edits
small:

* ``app`` - Flask's tests wrap service calls in ``with app.app_context():`` /
  ``app.test_request_context()`` to get a fresh ``g``. Here both open a fresh
  ``billing.services._context`` scope, which is what ``g`` became. Nothing else of Flask's
  ``app`` is offered; a test that reaches for its client or config belongs to step 3.
* ``factories`` - the Django versions of ``tests/char_factories.py``'s ``seed_currency``,
  ``seed_country``, ``seed_module``, ``make_user``, ``make_entity``, plus ``seed_plan`` and
  ``seed_policy`` (Flask inserted those inline). They always create the parent user/entity
  first: SQLite checks foreign keys at the END of the test, Postgres at insert, and a fixture
  that is FK-clean on both is the only kind that means the same thing on both.
* ``_no_stripe`` (autouse) - ``stripe_client.get_stripe`` raises unless a test overrides it,
  the belt to ``settings_test``'s empty key. Flask's suite had no key in its env, so an
  unstubbed call raised there too.

DB tests use pytest-django's ``db`` (a transaction per test) where Flask's used a
``db_session`` fixture that DELETEd every table afterwards; the store's savepoints
(``reserve_invoice``) are what let an IntegrityError be caught inside that transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from billing.services import _context

# The parked files belong to later slices (their modules are not ported yet).
collect_ignore_glob = ["pending_slices/*"]


def _now():
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class _AppShim:
    """Just enough of Flask's ``app`` for ``with app.app_context():`` to mean "a fresh scope"."""

    def app_context(self):
        return _context.scope()

    def test_request_context(self, *args, **kwargs):
        return _context.scope()


@pytest.fixture
def app():
    return _AppShim()


@pytest.fixture(autouse=True)
def _no_stripe(monkeypatch):
    """No test reaches Stripe. ``stripe_client`` arrives in slice B; until then the guard is
    settings_test's empty key, and afterwards this makes the unstubbed case loud."""
    try:
        # ``billing_gateway`` too, imported HERE before anything is patched: it binds
        # ``get_stripe`` by name when first imported, so a first import inside a test that had
        # swapped in a fake kept THAT fake for the rest of the run - monkeypatch never saw
        # the binding, so never undid it, and later tests met another file's Stripe (an
        # invoice "paid" in ``test_char_subscription`` answered a void three files later).
        from billing.services import billing_gateway, stripe_client  # noqa: F401
    except ImportError:
        return

    def _refuse():
        raise AssertionError("a test reached stripe_client.get_stripe(); stub it")

    monkeypatch.setattr(stripe_client, "get_stripe", _refuse)


# ---- reference data (Django versions of char_factories) --------------------------------


def snapshot(row, *fields):
    """Plain values copied off a row, as Flask's tests kept them (ids and names, never live
    rows). Kept so ported tests read identically."""
    return SimpleNamespace(**{f: getattr(row, f) for f in fields})


def seed_currency(code="HKD", *, symbol="$", decimal_places=2):
    from shared_models.models import CurrencyInfo

    row = CurrencyInfo.objects.filter(currency_code=code).first()
    if row is None:
        row = CurrencyInfo.objects.create(
            id=new_id(), currency_code=code, currency_name=code, symbol=symbol,
            decimal_places=decimal_places, is_active=True,
        )
    return snapshot(row, "id", "currency_code")


def seed_country(currency=None, code="HK"):
    from shared_models.models import CountryInfo

    row, _ = CountryInfo.objects.get_or_create(
        country_code=code,
        defaults={
            "alpha3_code": "HKG", "country_name_en": "Hong Kong",
            "currency_id": currency.id if currency is not None else None,
            "is_active": True, "display_order": 1,
        },
    )
    return snapshot(row, "country_code", "currency_id")


def seed_module(code="PETTY_CASH", name="Petty Cash"):
    from shared_models.models import EntityFunction

    row = EntityFunction.objects.filter(function_code=code).first()
    if row is None:
        row = EntityFunction.objects.create(
            id=new_id(), function_code=code, function_name=name, is_active=True, description=name,
        )
    return row


def seed_modules():
    """Both canonical modules."""
    return {
        "PETTY_CASH": seed_module("PETTY_CASH", "Petty Cash"),
        "PAYMENT_REQUEST": seed_module("PAYMENT_REQUEST", "Payment Request"),
    }


def seed_plan(code="PETTY_CASH", amount=28000, currency="HKD", display_name=None, *, interval_months=1):
    from shared_models.models import BillingPlan

    seed_currency(currency)
    row = BillingPlan.objects.filter(code=code).first()
    if row is None:
        row = BillingPlan.objects.create(
            id=new_id(), code=code, display_name=display_name or code.replace("_", " ").title(),
            amount=amount, currency=currency, interval_months=interval_months, is_active=True,
        )
    return row


def seed_plans():
    """The real catalogue's shape: two singles at 28000 and the bundle at 40000 (HKD)."""
    return {
        "PETTY_CASH": seed_plan("PETTY_CASH", 28000, display_name="Petty Cash"),
        "BILL": seed_plan("BILL", 28000, display_name="Payment Request"),
        "BILL+PETTY_CASH": seed_plan("BILL+PETTY_CASH", 40000, display_name="Super Minty"),
    }


def seed_policy(**overrides):
    from shared_models.models import BillingPolicy

    values = {
        "trial_days": 30, "paid_cancel_access_days": 30, "past_due_window_days": 15,
        "retry_offsets_days": "1,2,3,4,5,6,7,8,9,10,11,12,13",
    }
    values.update(overrides)
    row, created = BillingPolicy.objects.get_or_create(id=1, defaults=values)
    if not created:
        for key, value in values.items():
            setattr(row, key, value)
        row.save()
    return row


# ---- people and companies -----------------------------------------------------------


def make_user(email="user@test.com", *, system_role=None, first_name="Test", last_name="User"):
    from shared_models.models import User

    row = User.objects.create(
        id=new_id(),
        email=email,
        username=email,
        first_name=first_name,
        last_name=last_name,
        password="not-checked-here",
        system_role=system_role or "normal",
        approved=True,
    )
    return snapshot(row, "id", "email", "username", "first_name", "last_name")


def make_entity(owner, *, name="Acme Shop", role="admin", currency=None, country=None,
                modules=("PETTY_CASH",), status="disconnected"):
    """An entity the owner belongs to, with the given modules switched on (the map rows
    written as the app writes them: ``created_by`` the owner, explicit stamps)."""
    from shared_models.models import Entity, EntityFunctionMap, UserEntity

    if country is None:
        country = seed_country(currency)
    entity = Entity.objects.create(
        id=new_id(),
        name=name,
        status=status,
        currency_id=currency.id if currency is not None else None,
        country_code=country.country_code,
    )
    UserEntity.objects.create(user_id=owner.id, entity_id=entity.id, role=role, approved=True)
    for code in modules:
        fn = seed_module(code, code.replace("_", " ").title())
        now = _now()
        EntityFunctionMap.objects.create(
            entity_id=entity.id, entity_function_id=fn.id,
            is_enabled=True, created_by=owner.id, enabled_at=now,
            created_at=now, updated_at=now,
        )
    return snapshot(entity, "id", "name", "status")


@pytest.fixture
def factories(db):
    """The factory module as one object, on a database-backed test."""
    return SimpleNamespace(
        new_id=new_id, snapshot=snapshot, seed_currency=seed_currency, seed_country=seed_country,
        seed_module=seed_module, seed_modules=seed_modules, seed_plan=seed_plan, seed_plans=seed_plans,
        seed_policy=seed_policy, make_user=make_user, make_entity=make_entity,
    )
