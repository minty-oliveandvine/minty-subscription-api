"""``POST /api/me/invoices/{invoice_id}/retry`` - the HTTP half of
``billing/tests/engine/test_invoice_retry.py``: the token's own failed invoice only, the engine's
answer in the words the module page's retry uses (``api._retry``), and a failure that still
reaches the browser (CORS on the 502)."""

from __future__ import annotations

import pytest

from billing.api._retry import RETRY_MESSAGES
from billing.tests.api.conftest import ORIGIN, bearer, post_json
from billing.tests.engine.test_invoice_retry import _invoice, _key, _payer

pytestmark = pytest.mark.django_db


def _path(invoice_id) -> str:
    return f"/api/me/invoices/{invoice_id}/retry"


def _engine(monkeypatch, answer):
    from billing.services import dunning

    asked: list[dict] = []

    def _retry_now(user_id, entity_id=None, *, group_id=None, expect_invoice=None):
        asked.append({"group": group_id, "invoice": expect_invoice})
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dunning, "retry_now", _retry_now)
    return asked


def test_it_needs_a_token(client):
    assert post_json(client, _path("00000000-0000-0000-0000-000000000000"), {}).status_code == 401


def test_the_invoice_is_retried_on_its_account_and_answers_in_the_shared_words(
    client, monkeypatch
):
    payer, account = _payer(monkeypatch)
    invoice = _invoice(payer, account, external="in_current", key=_key(payer, account))
    asked = _engine(monkeypatch, {"status": "paid", "attempts": 1, "invoice": "in_current",
                                  "reason": None})

    response = post_json(client, _path(invoice.id), {}, **bearer(payer))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "paid", "message": RETRY_MESSAGES["paid"]}
    assert asked == [{"group": str(account.id), "invoice": "in_current"}]


def test_a_decline_carries_the_processors_words(client, monkeypatch):
    payer, account = _payer(monkeypatch)
    invoice = _invoice(payer, account, external="in_current", key=_key(payer, account))
    _engine(monkeypatch, {"status": "failed", "attempts": 2, "invoice": "in_current",
                          "reason": "Your card has insufficient funds."})

    body = post_json(client, _path(invoice.id), {}, **bearer(payer)).json()

    assert body == {"ok": False, "status": "failed",
                    "message": "That card was declined: Your card has insufficient funds."}


def test_someone_elses_or_a_paid_invoice_is_refused_before_the_engine(client, monkeypatch):
    payer, account = _payer(monkeypatch)
    stranger, _theirs = _payer(monkeypatch, "stranger@payer.test")
    failed = _invoice(payer, account, external="in_current", key=_key(payer, account))
    paid = _invoice(payer, account, external="in_paid", key="change-e1-20270101-PETTY_CASH",
                    status="paid")
    asked = _engine(monkeypatch, {"status": "paid", "attempts": 1, "invoice": None,
                                  "reason": None})

    theirs = post_json(client, _path(failed.id), {}, **bearer(stranger))
    assert theirs.status_code == 404
    assert theirs.json() == {"error": "That invoice couldn't be found."}
    assert post_json(client, _path("not-an-id"), {}, **bearer(payer)).status_code == 404
    settled = post_json(client, _path(paid.id), {}, **bearer(payer))
    assert settled.status_code == 409
    assert settled.json() == {"error": "That invoice isn't waiting for a payment."}
    assert asked == []


def test_a_processor_failure_is_a_sentence_and_still_reaches_the_browser(client, monkeypatch):
    payer, account = _payer(monkeypatch)
    invoice = _invoice(payer, account, external="in_current", key=_key(payer, account))
    _engine(monkeypatch, RuntimeError("stripe is down"))

    response = post_json(client, _path(invoice.id), {}, HTTP_ORIGIN=ORIGIN, **bearer(payer))

    assert response.status_code == 502
    assert response.json() == {"error": "We couldn't reach the card processor. Try again shortly."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        # Stripe's generic decline adds nothing to our sentence: never "declined: ...declined".
        ("Your card was declined.", "That card was declined. Try a different payment method."),
        # What it adds AFTER that phrase is kept.
        ("Your card was declined. Your request was in live mode, but used a known test card.",
         "That card was declined. Your request was in live mode, but used a known test card."),
        # A reason that says something else is quoted as it is.
        ("Your card has insufficient funds.",
         "That card was declined: Your card has insufficient funds."),
        (None, "That card was declined. Try a different payment method."),
    ],
)
def test_a_decline_never_repeats_itself(reason, message):
    from billing.api._retry import retry_answer

    answer = retry_answer({"status": "failed", "reason": reason})
    assert answer == {"ok": False, "status": "failed", "message": message}


def test_an_invoice_that_cannot_be_reissued_is_not_called_a_decline():
    """``not_collectable``: the processor will no longer collect it and it could not be re-issued
    automatically. Nothing was charged, so the decline's words would be false."""
    from billing.api._retry import retry_answer

    answer = retry_answer({"status": "not_collectable", "reason": None})
    assert answer == {"ok": False, "status": "not_collectable",
                      "message": "Payment could not be completed."}
