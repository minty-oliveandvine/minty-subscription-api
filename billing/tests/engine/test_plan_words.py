"""``billing_plan.code`` is the module codes themselves (schema item 20, 2026-11-03).

Until then the rows said BILL / BILL+PETTY_CASH while the module said PAYMENT_REQUEST, and
the code mapped between them; a renamed row the old build could not find priced a
cancellation's extension at 0. These pin the one spelling: the key, the lookup, the plan
views and the cancellation shares (280 / 280 / 280 + 120).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def test_plan_code_is_the_module_codes():
    from billing.services.billing import plan_code, plan_modules

    assert plan_code({"PETTY_CASH", "PAYMENT_REQUEST"}) == "PAYMENT_REQUEST+PETTY_CASH"
    assert plan_code({"payment_request"}) == "PAYMENT_REQUEST"
    assert plan_modules("PAYMENT_REQUEST+PETTY_CASH") == ("PAYMENT_REQUEST", "PETTY_CASH")


def test_lookup_and_views(app):
    from billing.services import catalog, store
    from billing.tests.engine.conftest import seed_plans

    seed_plans()
    assert store.billing_plan_for_codes({"PAYMENT_REQUEST"}).display_name == "Payment Request"
    both = store.billing_plan_for_codes({"PETTY_CASH", "PAYMENT_REQUEST"})
    assert (both.display_name, both.amount) == ("Super Minty", 40000)
    assert {p.function_code for p in catalog.available_plans()} == {"PAYMENT_REQUEST", "PETTY_CASH"}
    assert catalog.bundle_plan().function_codes == ("PAYMENT_REQUEST", "PETTY_CASH")


def test_an_old_word_row_is_not_found(app):
    """No fallback: a database still holding BILL rows must be renamed (seed_catalogue.sql)."""
    from billing.services import store
    from billing.tests.engine.conftest import seed_plan

    seed_plan("BILL", 28000, display_name="Payment Request")
    assert store.billing_plan_for_codes({"PAYMENT_REQUEST"}) is None


def test_cancellation_shares(app):
    from billing.services.checkout import _leaving_marginal
    from billing.tests.engine.conftest import seed_plans

    seed_plans()
    assert _leaving_marginal({"PETTY_CASH"}, "PETTY_CASH") == 28000
    assert _leaving_marginal({"PAYMENT_REQUEST"}, "PAYMENT_REQUEST") == 28000
    both = {"PAYMENT_REQUEST", "PETTY_CASH"}
    assert _leaving_marginal(both, "PAYMENT_REQUEST") == 28000
    assert _leaving_marginal(both, "PETTY_CASH") == 12000
