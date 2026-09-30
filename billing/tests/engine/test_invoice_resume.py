"""An invoice learned about after the fact, and a draft nothing finished (``billing_gateway``).

``issue_invoice`` creates the invoice (``auto_advance=False``), records its id as a DRAFT,
adds the items and finalizes it - and only then asks for payment. An error or a crash in
between leaves a draft that Stripe will never finalize by itself, that dunning never sees (it
chases open invoices) and that nothing else ever touched: the period was simply not billed.

What is pinned:

* LOUD. The error that leaves a draft names it and the key it was raised under.
* RECORDED WHOLE. An invoice found after the fact becomes the row's status, total, times and
  link (``record_found_invoice``), and a row that can still move is re-read, never raising
  (``refresh_record``).
* FINISHED, NEVER TWICE (``resume_invoice``). A row still reading "draft" was never charged
  by us, so whatever the processor now holds is finished: the missing items and only those,
  a finalize only if it is still a draft, the charge on the account's CURRENT card. Anything
  that is not exactly what was reserved is refused, loudly, with nothing finalized.
"""

from __future__ import annotations

import pytest

from billing.tests.engine.test_invoice_refresh import (
    CUSTOMER,
    DEAD_CARD,
    END,
    GOOD_CARD,
    START,
    _row,
    _world,
)

pytestmark = pytest.mark.django_db

KEY = "renewal-resume-20270306-g"


def _issue(stripe, payer, entity, account, *, fail):
    """Raise a renewal through the real gateway and stop it at ``fail`` = (step, when).
    Returns the Stripe invoice it left behind."""
    from datetime import UTC, datetime

    from billing.services import billing_gateway
    from billing.services.billing import Invoice, Line, Period

    invoice = Invoice(
        currency="hkd",
        period=Period(START, END),
        lines=(
            Line(str(entity.id), "Refresh Co", "Petty Cash", 28000,
                 period_start=START, period_end=END, unit_amount=28000),
            Line(str(entity.id), "Refresh Co", "Payment Request (access after cancellation)",
                 8129, period_start=START,
                 period_end=datetime(2027, 3, 27, 12, tzinfo=UTC)),
        ),
    )
    stripe.fail[fail[0]] = fail[1]
    with pytest.raises(billing_gateway.BillingError):
        billing_gateway.issue_invoice(
            CUSTOMER, invoice, memo="Renewal", idempotency_key=KEY,
            metadata={"renewal_key": KEY, "billing_group": str(account.id)},
            payer_user_id=payer.id, payment_method=DEAD_CARD, billing_group_id=account.id,
        )
    return next(i for i, inv in stripe.invoices.items() if inv["metadata"]["renewal_key"] == KEY)


def _stranded(monkeypatch, *, fail=("finalize", "before")):
    stripe, payer, entity, account = _world(monkeypatch)
    left = _issue(stripe, payer, entity, account, fail=fail)
    stripe.calls.clear()
    return stripe, left


def _errors(caplog, needle):
    return [r.getMessage() for r in caplog.records
            if r.levelname == "ERROR" and needle in r.getMessage()]


# --- loud -------------------------------------------------------------------------------------


def test_an_error_before_finalizing_names_the_draft_and_its_key(monkeypatch, caplog):
    stripe, left = _stranded(monkeypatch)

    assert _row(left).status == "draft"
    named = _errors(caplog, "was left a DRAFT")
    assert len(named) == 1
    assert left in named[0] and KEY in named[0]


def test_a_decline_after_finalizing_is_not_called_a_draft(monkeypatch, caplog):
    """A decline raises from ``pay`` with the invoice finalized and open - owed, and dunning's
    to chase. Calling that a draft would send someone looking for the wrong problem."""
    from billing.services import billing_gateway, store
    from billing.services.billing import Invoice, Line, Period

    stripe, payer, entity, account = _world(monkeypatch)
    invoice = Invoice(currency="hkd", period=Period(START, END), lines=(
        Line(str(entity.id), "Refresh Co", "Petty Cash", 28000,
             period_start=START, period_end=END, unit_amount=28000),))
    with pytest.raises(billing_gateway.BillingError):
        billing_gateway.issue_invoice(
            CUSTOMER, invoice, idempotency_key=KEY, metadata={"renewal_key": KEY},
            payer_user_id=payer.id, payment_method=DEAD_CARD, billing_group_id=account.id,
        )

    assert store.invoice_for_key(KEY).status == "open"
    assert _errors(caplog, "DRAFT") == []


# --- recorded whole ---------------------------------------------------------------------------


def test_a_row_is_brought_up_to_what_the_processor_says(monkeypatch):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)
    stripe.invoices[left].update(status="paid", total=36129, hosted_invoice_url="https://i/x",
                                 status_transitions={"finalized_at": 1800000100,
                                                     "paid_at": 1800000200})

    assert billing_gateway.refresh_record(_row(left)) == "paid"
    row = _row(left)
    assert (row.status, row.total, row.hosted_invoice_url) == ("paid", 36129, "https://i/x")
    assert row.paid_at is not None and row.issued_at is not None


def test_an_unreachable_processor_answers_the_stored_status(monkeypatch, caplog):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)

    def _down(*a, **k):
        raise ConnectionError("processor down")

    monkeypatch.setattr(stripe.Invoice, "retrieve", _down)

    assert billing_gateway.refresh_record(_row(left)) == "draft"
    assert _errors(caplog, f"could not re-read invoice {left}")


# --- finished, never twice ---------------------------------------------------------------------


def test_a_draft_an_error_left_is_finished_on_the_current_card(monkeypatch):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)

    result = billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD)

    assert result["status"] == "paid"
    # The draft still names the card that was current when it was made - and that one
    # declines. Paid means the account's card as it is NOW was charged.
    assert stripe.invoices[left]["default_payment_method"] == DEAD_CARD
    assert stripe.calls == [("finalize", left), ("pay", left)]
    assert sorted(i["amount"] for i in stripe.items[left]) == [8129, 28000]
    row = _row(left)
    assert (row.status, row.total) == ("paid", 36129)
    assert row.paid_at is not None


def test_an_interrupted_item_copy_gets_only_its_missing_items(monkeypatch):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch, fail=("item", "after"))
    assert len(stripe.items[left]) == 1          # the first one made it, the second did not

    result = billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD)

    assert result["status"] == "paid"
    assert sorted(i["amount"] for i in stripe.items[left]) == [8129, 28000]
    assert [c for c in stripe.calls if c[0] == "item"] == [("item", left)]


def test_a_draft_the_processor_finalized_after_all_is_charged_not_finalized_again(monkeypatch):
    """The finalize went through and its answer was lost: open at Stripe, "draft" here. Our
    row never saw it finalized, so it was never charged either."""
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch, fail=("finalize", "after"))
    assert stripe.invoices[left]["status"] == "open"

    result = billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD)

    assert result["status"] == "paid"
    assert stripe.calls == [("pay", left)]


def test_a_draft_settled_some_other_way_is_recorded_not_charged(monkeypatch):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)
    stripe.invoices[left].update(status="paid", total=36129,
                                 status_transitions={"paid_at": 1800000200})

    result = billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD)

    assert result["status"] == "paid"
    assert stripe.calls == []
    assert _row(left).status == "paid"


def test_a_decline_on_the_finish_raises_naming_the_invoice(monkeypatch):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)

    with pytest.raises(billing_gateway.BillingError) as raised:
        billing_gateway.resume_invoice(_row(left), payment_method=DEAD_CARD)

    assert raised.value.invoice_id == left
    # Finalized - and recorded as owed - before the charge, like any first attempt.
    assert _row(left).status == "open"


# --- refused, loudly ---------------------------------------------------------------------------


def test_an_item_nobody_reserved_is_refused(monkeypatch, caplog):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)
    stripe.items[left].append({"id": "ii_extra", "invoice": left, "amount": 5000,
                               "currency": "hkd", "description": "Added by hand",
                               "metadata": {}, "period": {}})

    assert billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD) is None
    assert stripe.calls == []                    # nothing finalized, nothing charged
    assert _row(left).status == "draft"
    refused = _errors(caplog, f"STRANDED DRAFT {left}")
    assert len(refused) == 1 and billing_gateway.MISMATCHED in refused[0]


def test_a_total_that_disagrees_with_the_reservation_is_refused(monkeypatch, caplog):
    from billing.services import billing_gateway
    from shared_models.models import SubscriptionInvoice

    stripe, left = _stranded(monkeypatch)
    SubscriptionInvoice.objects.filter(external_id=left).update(total=1)

    assert billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD) is None
    assert stripe.calls == []
    assert _errors(caplog, f"STRANDED DRAFT {left}")


def test_a_reworded_line_is_still_the_same_item(monkeypatch):
    """Matched by amount, company and days - never by the words, which a resume rebuilds from
    names cut to 255 characters, and which a deploy may have changed since. Matching on them
    would take the item already there for a missing one and charge it twice."""
    from billing.services import billing_gateway
    from shared_models.models import SubscriptionInvoiceLine

    stripe, left = _stranded(monkeypatch)
    SubscriptionInvoiceLine.objects.filter(
        invoice_id=_row(left).id, product_name="Petty Cash"
    ).update(product_name="Petty Cash (renamed since)")

    result = billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD)

    assert result["status"] == "paid"
    assert len(stripe.items[left]) == 2


def test_a_draft_deleted_at_stripe_is_reported_not_recreated(monkeypatch, caplog):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)
    del stripe.invoices[left]

    assert billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD) is None
    assert [c for c in stripe.calls if c[0] == "create"] == []
    gone = _errors(caplog, f"STRANDED DRAFT {left}")
    assert len(gone) == 1 and "no longer exists" in gone[0]


def test_a_draft_written_off_by_hand_is_not_billed(monkeypatch):
    from billing.services import billing_gateway

    stripe, left = _stranded(monkeypatch)
    stripe.invoices[left]["status"] = "uncollectible"

    assert billing_gateway.resume_invoice(_row(left), payment_method=GOOD_CARD) is None
    assert stripe.calls == []
    assert _row(left).status == "uncollectible"
