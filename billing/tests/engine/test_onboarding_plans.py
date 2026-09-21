"""The onboarding plan catalog behind Step 2's subscription summary.

The wizard previews subtotal / bulk discount / total client-side, so the figures it
is handed must be the same ones the server bills from. These tests pin the catalog's
amounts and discount unit to the plan views the catalog reads out of ``billing_plan``,
and pin the discount RULE (2+ modules, same currency) to the same arithmetic
``get_subscription_summary`` uses.

NOTE: imports are done INSIDE each test (as in the other subscription tests) so the
lazy re-imports stay mutually consistent.
"""
from __future__ import annotations

import uuid

import pytest

# The price catalog is patched BY DOTTED PATH, not via an imported reference (Minty's
# conftest re-imports project modules mid-session; kept as the module was written).
_CATALOG = "billing.services.catalog"


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


def _seed_catalog(db):
    """The module catalog rows the names in the plan list come from."""
    from shared_models.models import EntityFunction

    for code, name in (("PETTY_CASH", "Petty Cash"), ("PAYMENT_REQUEST", "Payment Request")):
        EntityFunction.objects.create(
                id=str(uuid.uuid4()),
                function_code=code,
                function_name=name,
                description=f"{name} module",
                is_active=True,
            )
    pass  # commit: autocommit under Django


def _plan(code, fn_id, *, amount=28000, currency="HKD"):
    from billing.services import catalog

    return catalog.PlanView(
        entity_function_id=fn_id,
        function_code=code,
        display_name=code.title().replace("_", " "),
        amount=amount,
        currency_code=currency,
        billing_interval="month",
        billing_interval_count=1,
        is_available_for_subscription=True,
    )


def _bundle():
    from billing.services import catalog

    return catalog.BundlePlanView(
        function_codes=("PETTY_CASH", "PAYMENT_REQUEST"),
        display_name="Super Minty",
        amount=40000,
        currency_code="HKD",
        billing_interval="month",
        billing_interval_count=1,
    )


def _wire_catalog(monkeypatch, *, plans=None, bundle=True):
    """Point the plan catalog at fixed plan views."""
    if plans is None:
        plans = [_plan("PETTY_CASH", "fn_pc"), _plan("PAYMENT_REQUEST", "fn_bill")]
    monkeypatch.setattr(f"{_CATALOG}.available_plans", lambda: plans)
    monkeypatch.setattr(f"{_CATALOG}.bundle_plan", lambda: (_bundle() if bundle else None))


def test_catalog_normalizes_amounts_and_bundle(db_session, monkeypatch):
    """Smallest-currency-unit amounts come back as display decimals."""
    pytest.importorskip("billing.services.cards")  # slice D
    from billing.services import entity_modules as modules

    _seed_catalog(db_session)
    _wire_catalog(monkeypatch)

    catalog = modules.get_module_plan_catalog()

    by_code = {p["code"]: p for p in catalog["plans"]}
    assert set(by_code) == {"PETTY_CASH", "PAYMENT_REQUEST"}
    # 28000 (cents) -> 280.00, not 28000.
    assert by_code["PETTY_CASH"]["amount"] == 280.0
    assert by_code["PETTY_CASH"]["formatted_amount"] == "280.00"
    assert by_code["PETTY_CASH"]["currency_code"] == "HKD"
    assert by_code["PETTY_CASH"]["billing_interval"] == "month"
    # The label shown on the summary line comes from the catalog, not the code.
    assert by_code["PETTY_CASH"]["name"] == "Petty Cash"
    assert by_code["PAYMENT_REQUEST"]["name"] == "Payment Request"
    # The bundle IS the discount: 40000 -> 400.00 for both modules together, and the
    # wizard is told which modules it covers so it can price the cart the same way.
    assert catalog["bundle_amount"] == 400.0
    assert catalog["bundle_codes"] == ["PAYMENT_REQUEST", "PETTY_CASH"]
    assert catalog["bundle_currency"] == "HKD"
    # The wizard needs the trial length to label "Due today / free for N days".
    assert catalog["trial_period_days"] == 30


def test_single_module_pays_full_price(db_session, monkeypatch):
    """One module picked: not the bundle set, so it pays its own price."""
    pytest.importorskip("billing.services.cards")  # slice D
    from billing.services import entity_modules as modules

    _seed_catalog(db_session)
    _wire_catalog(monkeypatch)

    catalog = modules.get_module_plan_catalog()

    lines = [p for p in catalog["plans"] if p["code"] == "PAYMENT_REQUEST"]
    subtotal = sum(p["amount"] for p in lines)
    picked = sorted(p["code"] for p in lines)
    total = catalog["bundle_amount"] if picked == catalog["bundle_codes"] else subtotal
    assert total == 280.0  # standalone price, no bundle
    assert subtotal - total == 0


def test_module_without_a_priced_plan_is_omitted(db_session, monkeypatch):
    """A module with no priced plan is left out rather than shown as free."""
    pytest.importorskip("billing.services.cards")  # slice D
    from billing.services import entity_modules as modules

    _seed_catalog(db_session)
    _wire_catalog(monkeypatch, plans=[_plan("PETTY_CASH", "fn_pc")])

    catalog = modules.get_module_plan_catalog()

    assert [p["code"] for p in catalog["plans"]] == ["PETTY_CASH"]


def test_catalog_without_a_bundle_product(db_session, monkeypatch):
    """No bundle row in the price catalog → the wizard previews no bundle price."""
    pytest.importorskip("billing.services.cards")  # slice D
    from billing.services import entity_modules as modules

    _seed_catalog(db_session)
    _wire_catalog(monkeypatch, bundle=False)

    catalog = modules.get_module_plan_catalog()

    assert catalog["bundle_amount"] == 0
    assert catalog["bundle_codes"] == []
    assert catalog["bundle_currency"] is None
