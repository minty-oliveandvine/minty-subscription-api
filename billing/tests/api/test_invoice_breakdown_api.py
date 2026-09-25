"""``GET /api/me/invoices/{invoice_id}/breakdown`` - the HTTP half of
``billing/tests/engine/test_invoice_breakdown.py``: the token's own invoice only, a sentence for
one that is not, and a failure that still reaches the browser (CORS on the 500)."""

from __future__ import annotations

import pytest

from billing.tests.api.conftest import ORIGIN, bearer
from billing.tests.engine.test_billing_accounts import _entity
from billing.tests.engine.test_invoice_breakdown import _invoice, _lines

pytestmark = pytest.mark.django_db


def _path(invoice_id) -> str:
    return f"/api/me/invoices/{invoice_id}/breakdown"


def test_it_needs_a_token(client):
    assert client.get(_path("00000000-0000-0000-0000-000000000000")).status_code == 401


def test_the_invoice_is_the_tokens_own(client, user, other_user):
    invoice = _invoice(user, total=28000)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    mine = client.get(_path(invoice.id), **bearer(user))
    assert mine.status_code == 200
    body = mine.json()
    assert body["invoice"]["id"] == str(invoice.id)
    assert [(r["entity_name"], r["subscription"], r["charged_minor"]) for r in body["rows"]] == [
        ("Acme Ltd", "Petty Cash", 28000),
    ]

    theirs = client.get(_path(invoice.id), **bearer(other_user))
    assert theirs.status_code == 404
    assert theirs.json() == {"error": "That invoice couldn't be found."}
    assert client.get(_path("not-an-id"), **bearer(user)).status_code == 404


def test_a_failure_is_a_sentence_and_still_reaches_the_browser(client, user, monkeypatch):
    from billing.services import portal

    def boom(*_args, **_kwargs):
        raise RuntimeError("the lines table is gone")

    monkeypatch.setattr(portal, "build_invoice_breakdown", boom)
    response = client.get(
        _path("00000000-0000-0000-0000-000000000000"), HTTP_ORIGIN=ORIGIN, **bearer(user)
    )
    assert response.status_code == 500
    assert response.json() == {"error": "Could not load that invoice's breakdown."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN
