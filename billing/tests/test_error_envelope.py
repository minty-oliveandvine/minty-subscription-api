"""The one error envelope every Minty API answers with (minty_shared.django.errors, user
2026-10-22): ``{"error": <sentence>}`` - never ``detail`` - 401 ``Unauthorized``, 500 the house
sentence with the detail kept out of the response."""

from __future__ import annotations

import pytest

from billing.tests.conftest import make_token

pytestmark = pytest.mark.django_db

HOUSE = "Something went wrong on my end. Mind trying again?"


def test_no_token_is_401_unauthorized(client):
    res = client.get("/api/me/subscriptions")
    assert res.status_code == 401
    assert res.json() == {"error": "Unauthorized"}


def _handled(exc):
    """What the API answers for ``exc``, through the handler it registered for that type."""
    import json

    from django.test import RequestFactory

    from config.urls import api

    request = RequestFactory().get("/api/me/subscriptions")
    response = api._lookup_exception_handler(exc)(request, exc)
    return response.status_code, json.loads(response.content)


def test_an_unhandled_failure_is_the_house_sentence_and_never_its_detail():
    status, body = _handled(RuntimeError("database fell over: secret-detail"))
    assert (status, body) == (500, {"error": HOUSE})


def test_each_domain_refusal_keeps_its_status():
    from core.exceptions import BillingError, NotFoundError, PermissionDeniedError, UpstreamError

    assert _handled(BillingError("Your card was declined. Use another card.", 402)) == (
        402,
        {"error": "Your card was declined. Use another card."},
    )
    assert _handled(PermissionDeniedError("Only admins can.")) == (403, {"error": "Only admins can."})
    assert _handled(NotFoundError("No such invoice.")) == (404, {"error": "No such invoice."})
    assert _handled(UpstreamError("Flask said no.", 409)) == (409, {"error": "Flask said no."})


def test_a_rule_refusal_keeps_its_status_and_the_error_key(client, user):
    # Retrying an invoice that is not the caller's is a refusal with a status of its own.
    res = client.post(
        "/api/me/invoices/00000000-0000-0000-0000-000000000000/retry",
        HTTP_AUTHORIZATION=f"Bearer {make_token(user.id)}",
    )
    assert 400 <= res.status_code < 500
    body = res.json()
    assert isinstance(body.get("error"), str) and body["error"]
    assert "detail" not in body
