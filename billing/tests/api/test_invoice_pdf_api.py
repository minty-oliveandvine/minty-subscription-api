"""``GET /api/me/invoices/{invoice_id}/pdf`` - the HTTP half of ``test_invoice_document`` and
``test_invoice_pdf``: the token's own invoice as ``application/pdf``, a sentence for everything
else, every answer reaching the browser (CORS), and the list's ``has_pdf`` agreeing with what
the route will serve."""

from __future__ import annotations

import pytest

from billing.tests.api.conftest import ORIGIN, bearer
from billing.tests.engine.test_billing_accounts import _entity
from billing.tests.engine.test_invoice_breakdown import _invoice, _lines
from billing.tests.engine.test_invoice_document import ADDRESS, _account, _hkd, _wallet

pytestmark = pytest.mark.django_db


def _path(invoice_id) -> str:
    return f"/api/me/invoices/{invoice_id}/pdf"


def _billed(user, monkeypatch, *, status="paid", external_id="in_breakdown_1", total=28000):
    """One invoice on the user's account, one company line of 28000."""
    _hkd()
    account = _account(user)
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(user, total=total, status=status, external_id=external_id,
                       billing_group_id=account.id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))
    return invoice


def test_it_needs_a_token(client):
    assert client.get(_path("00000000-0000-0000-0000-000000000000")).status_code == 401


def test_the_pdf_is_the_tokens_own_invoice(client, user, other_user, monkeypatch):
    invoice = _billed(user, monkeypatch)

    mine = client.get(_path(invoice.id), HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert mine.status_code == 200
    assert mine["Content-Type"] == "application/pdf"
    assert mine.content.startswith(b"%PDF-")
    assert mine["Content-Disposition"] == 'attachment; filename="Inv-in_breakdown_1.pdf"'
    assert mine["Cache-Control"] == "private, no-store"
    assert mine["Access-Control-Allow-Origin"] == ORIGIN

    theirs = client.get(_path(invoice.id), **bearer(other_user))
    assert theirs.status_code == 404
    assert theirs.json() == {"error": "That invoice couldn't be found."}
    assert client.get(_path("not-an-id"), **bearer(user)).status_code == 404


@pytest.mark.parametrize(
    "status, external_id, served",
    [
        ("paid", "in_paid", True),
        ("open", "in_open", True),
        ("uncollectible", "in_given_up", True),
        ("void", "in_void", False),
        ("draft", "in_draft", False),
        ("paid", None, False),
    ],
    ids=["paid", "open", "uncollectible", "voided", "a draft", "never sent"],
)
def test_has_pdf_is_exactly_what_the_route_serves(
    client, user, monkeypatch, status, external_id, served
):
    invoice = _billed(user, monkeypatch, status=status, external_id=external_id)

    listed = client.get("/api/me/invoices", **bearer(user)).json()["invoices"]
    response = client.get(_path(invoice.id), HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert [row["has_pdf"] for row in listed] == [served]
    if served:
        assert response.status_code == 200
    else:
        assert response.status_code == 409
        assert response.json() == {"error": "There's no PDF for that invoice."}
        assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_an_address_that_cannot_be_read_is_502_never_a_blank_bill_to(client, user, monkeypatch):
    invoice = _billed(user, monkeypatch)
    _wallet(monkeypatch, fails=True)

    response = client.get(_path(invoice.id), HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert response.status_code == 502
    assert response.json() == {
        "error": "We couldn't reach the payment processor for the billing address. "
                 "Please try again in a moment."
    }
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_lines_that_disagree_with_the_charge_are_never_served(client, user, monkeypatch):
    invoice = _billed(user, monkeypatch, total=40000)  # one 28000 line on a 40000 invoice

    response = client.get(_path(invoice.id), HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert response.status_code == 500
    assert response.json() == {"error": "Could not prepare that invoice's PDF."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_a_failure_is_a_sentence_and_still_reaches_the_browser(client, user, monkeypatch):
    from billing.services import invoice_pdf

    invoice = _billed(user, monkeypatch)

    def boom(_doc):
        raise RuntimeError("the fonts are gone")

    monkeypatch.setattr(invoice_pdf, "render_invoice_pdf", boom)
    response = client.get(_path(invoice.id), HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert response.status_code == 500
    assert response.json() == {"error": "Could not prepare that invoice's PDF."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN
