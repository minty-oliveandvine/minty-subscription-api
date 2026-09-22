"""The in-app wallet's routes: the HTTP half of Minty's ``tests/test_billing_payment_methods.py``.

The wallet's rules live next door (``billing/tests/engine/test_billing_payment_methods.py``,
whose ``wallet`` stub these tests share). What is worth pinning HERE is that the routes carry
those rules to the page intact:

* **The id in the request is checked, not trusted.** Four of the endpoints take a ``pm_…``
  from the browser - the first thing in the payer portal that can be pointed somewhere - so a
  method belonging to another customer answers "not found" from the endpoint too, rather than
  being promoted.

* **The two removal refusals reach the page as sentences.** The 409s are written for the
  customer and shown verbatim - a bare "failed" would leave them clicking Remove forever -
  and the one that costs real money names the companies, because the fix is per company.
"""

from __future__ import annotations

import pytest

from billing.tests.api.conftest import ORIGIN, bearer, post_json, preflight
from billing.tests.engine.test_billing_payment_methods import _card, wallet  # noqa: F401

pytestmark = pytest.mark.django_db

WALLET = "/api/me/billing/payment-methods"


def test_the_wallet_needs_a_token(client):
    assert client.get(WALLET).status_code == 401
    assert post_json(client, f"{WALLET}/remove", {}).status_code == 401


def test_preflight_answers_without_a_token(client):
    response = preflight(client, f"{WALLET}/setup-intent")
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_the_list_comes_back_for_the_tokens_own_account(client, user, wallet):  # noqa: F811
    wallet["methods"] = [_card("pm_1")]
    wallet["default"] = "pm_1"

    response = client.get(WALLET, **bearer(user))

    assert response.status_code == 200
    body = response.json()
    assert body["default_id"] == "pm_1"
    assert body["methods"][0]["label"] == "Visa •••• 4242"


def test_a_refusal_reaches_the_page_with_its_reason(client, user, wallet):  # noqa: F811
    """The 409s are written for the customer and shown verbatim - a bare "failed" here
    would leave them clicking Remove forever."""
    wallet["methods"] = [_card("pm_1"), _card("pm_2", last4="1111")]
    wallet["default"] = "pm_1"

    response = post_json(client, f"{WALLET}/remove", {"payment_method": "pm_1"}, **bearer(user))

    assert response.status_code == 409
    assert "default" in response.json()["error"]


def test_a_card_companies_are_billed_to_cannot_be_removed(client, user, wallet):  # noqa: F811
    """The refusal that costs real money if it is missing.

    Detaching a card companies are nominated onto leaves them pointing at a ``pm_...``
    Stripe no longer holds, and every one of their renewals fails into dunning on a date
    nobody is watching. It outranks the default rule - nothing is billed to the default -
    so it is checked first, and it NAMES the companies, because the fix is per company.
    """
    wallet["methods"] = [_card("pm_1"), _card("pm_2", last4="1111")]
    wallet["default"] = "pm_2"
    wallet["billing_on"] = {"pm_1": ["Acme Ltd", "Beta Co"]}

    response = post_json(client, f"{WALLET}/remove", {"payment_method": "pm_1"}, **bearer(user))

    assert response.status_code == 409
    message = response.json()["error"]
    assert "Acme Ltd" in message and "Beta Co" in message
    assert wallet["detached"] == []


def test_a_method_that_is_not_yours_is_a_404_from_the_endpoint_too(client, user, wallet):  # noqa: F811
    wallet["methods"] = [_card("pm_theirs", customer="cus_someone_else")]

    response = post_json(
        client, f"{WALLET}/default", {"payment_method": "pm_theirs"}, **bearer(user)
    )

    assert response.status_code == 404
    assert wallet["default"] is None


def test_a_surprise_is_the_shells_500_with_cors(client, user, wallet, monkeypatch):  # noqa: F811
    """``payment_methods.run`` is the one shell every transport shares; a Stripe outage
    reaches the page as its sentence, not as a stack trace, and with CORS so the browser
    reads the error rather than a blocked response."""
    pm = wallet["module"]

    def _boom(_customer):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(pm, "list_payment_methods", _boom)

    response = client.get(WALLET, HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert response.status_code == 500
    assert "down" not in response.json()["error"]
    assert response["Access-Control-Allow-Origin"] == ORIGIN
