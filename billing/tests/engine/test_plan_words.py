"""The catalogue answers in either plan word while ``billing_plan.code`` is renamed.

Schema item 20 kept the word BILL on ``billing_plan.code`` after the module became
PAYMENT_REQUEST. The rows are being renamed to PAYMENT_REQUEST / PAYMENT_REQUEST+PETTY_CASH,
and until November development and production read the SAME rows, so a running service
must price correctly on whichever spelling it finds. Both catalogues below must give the
same lookups, the same plan views and the same cancellation shares (280 / 280 / 280 + 120).
Drop the BILL half once every database is renamed.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

OLD_WORDS = {"PETTY_CASH": "PETTY_CASH", "BILL": "BILL", "BILL+PETTY_CASH": "BILL+PETTY_CASH"}
NEW_WORDS = {
    "PETTY_CASH": "PETTY_CASH", "BILL": "PAYMENT_REQUEST",
    "BILL+PETTY_CASH": "PAYMENT_REQUEST+PETTY_CASH",
}


def _seed(words):
    from billing.tests.engine.conftest import seed_plan

    seed_plan(words["PETTY_CASH"], 28000, display_name="Petty Cash")
    seed_plan(words["BILL"], 28000, display_name="Payment Request")
    seed_plan(words["BILL+PETTY_CASH"], 40000, display_name="Super Minty")


def test_plan_code_is_the_module_word():
    from billing.services.billing import plan_code

    assert plan_code({"PETTY_CASH", "PAYMENT_REQUEST"}) == "PAYMENT_REQUEST+PETTY_CASH"
    assert plan_code({"payment_request"}) == "PAYMENT_REQUEST"


@pytest.mark.parametrize("words", [OLD_WORDS, NEW_WORDS], ids=["bill-rows", "payment-request-rows"])
def test_lookup_resolves_either_spelling(app, words):
    from billing.services import store

    _seed(words)
    assert store.billing_plan_for_codes({"PAYMENT_REQUEST"}).display_name == "Payment Request"
    assert store.billing_plan_for_codes({"PETTY_CASH"}).display_name == "Petty Cash"
    both = store.billing_plan_for_codes({"PETTY_CASH", "PAYMENT_REQUEST"})
    assert (both.display_name, both.amount) == ("Super Minty", 40000)


def test_the_new_spelling_wins_when_both_rows_exist(app):
    from billing.services import store
    from billing.tests.engine.conftest import seed_plan

    seed_plan("BILL", 99900, display_name="Old row")
    seed_plan("PAYMENT_REQUEST", 28000, display_name="Payment Request")
    assert store.billing_plan_for_codes({"PAYMENT_REQUEST"}).display_name == "Payment Request"


@pytest.mark.parametrize("words", [OLD_WORDS, NEW_WORDS], ids=["bill-rows", "payment-request-rows"])
def test_catalog_views_speak_module_codes(app, words):
    from billing.services import catalog

    _seed(words)
    assert {p.function_code for p in catalog.available_plans()} == {"PAYMENT_REQUEST", "PETTY_CASH"}
    assert catalog.bundle_plan().function_codes == ("PAYMENT_REQUEST", "PETTY_CASH")


@pytest.mark.parametrize("words", [OLD_WORDS, NEW_WORDS], ids=["bill-rows", "payment-request-rows"])
def test_cancellation_shares_are_unchanged(app, words):
    from billing.services.checkout import _leaving_marginal

    _seed(words)
    assert _leaving_marginal({"PETTY_CASH"}, "PETTY_CASH") == 28000
    assert _leaving_marginal({"PAYMENT_REQUEST"}, "PAYMENT_REQUEST") == 28000
    both = {"PAYMENT_REQUEST", "PETTY_CASH"}
    assert _leaving_marginal(both, "PAYMENT_REQUEST") == 28000
    assert _leaving_marginal(both, "PETTY_CASH") == 12000
