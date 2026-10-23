"""The invoice number, ``INV-<ACCOUNT>-YYYYMM-NNNN`` (``billing/services/invoice_number.py``).

Given once, the first time the row is an issued document; never to a draft; one counter per
month for every account; frozen against a later rename of the account.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from billing.services import invoice_number, store
from billing.tests.engine.conftest import make_user, new_id, seed_currency

OCT = datetime(2026, 10, 23, 9, tzinfo=UTC)
NOV = datetime(2026, 11, 1, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Acme", "ACME"),
        ("Acme Trading Ltd", "ACMETRAD"),
        ("  o&v  café ", "OVCAFE"),
        ("美思有限公司", "ABCDEF"),
        ("", "ABCDEF"),
        (None, "ABCDEF"),
    ],
)
def test_the_account_code(company, expected):
    assert invoice_number.account_code(company, "abcdef12-0000") == expected


def test_the_counter_keeps_four_digits_and_grows_past_them():
    assert invoice_number.format_number("ACME", "202610", 42) == "INV-ACME-202610-0042"
    assert invoice_number.format_number("ACME", "202610", 12345) == "INV-ACME-202610-12345"


@pytest.fixture
def payer(db):
    seed_currency("HKD")
    return make_user("payer@test.com")


def _account(payer, company="Acme Ltd"):
    from shared_models.models import PayerBillingGroup

    return PayerBillingGroup.objects.create(
        id=new_id(), payer_user_id=payer.id, stripe_payment_method_id="pm_1",
        billing_company=company,
    )


def _invoice(payer, group=None, *, status="draft"):
    from shared_models.models import SubscriptionInvoice

    return SubscriptionInvoice.objects.create(
        id=new_id(), payer_user_id=payer.id, billing_group=group,
        period_start=OCT, period_end=NOV, currency="HKD", total=40000, status=status,
    )


def _number(invoice):
    invoice.refresh_from_db()
    return invoice.invoice_number


def test_a_draft_has_no_number_even_with_an_issue_date(payer):
    """A draft's settle carries Stripe's ``created`` as ``issued_at`` - that is not issuing."""
    invoice = _invoice(payer, _account(payer))

    store.settle_invoice(invoice.id, external_id="in_1", status="draft", issued_at=OCT)

    assert _number(invoice) is None


def test_finalizing_gives_the_number_and_settling_again_keeps_it(payer):
    invoice = _invoice(payer, _account(payer))

    store.settle_invoice(invoice.id, status="open", issued_at=OCT)
    store.settle_invoice(invoice.id, status="paid")
    store.settle_invoice(invoice.id, status="void")

    assert _number(invoice) == "INV-ACMELTD-202610-0001"


def test_one_counter_per_month_across_accounts(payer):
    acme, beta = _account(payer, "Acme"), _account(payer, "Beta")
    first, second, third = _invoice(payer, acme), _invoice(payer, beta), _invoice(payer, acme)

    store.settle_invoice(first.id, status="open", issued_at=OCT)
    store.settle_invoice(second.id, status="paid", issued_at=OCT)
    store.settle_invoice(third.id, status="open", issued_at=NOV)

    assert _number(first) == "INV-ACME-202610-0001"
    assert _number(second) == "INV-BETA-202610-0002"
    assert _number(third) == "INV-ACME-202611-0001"


def test_two_accounts_with_one_name_never_collide(payer):
    one, two = _invoice(payer, _account(payer, "Acme")), _invoice(payer, _account(payer, "Acme"))

    store.settle_invoice(one.id, status="open", issued_at=OCT)
    store.settle_invoice(two.id, status="open", issued_at=OCT)

    assert {_number(one), _number(two)} == {"INV-ACME-202610-0001", "INV-ACME-202610-0002"}


def test_renaming_the_account_changes_no_issued_number(payer):
    group = _account(payer, "Acme")
    invoice = _invoice(payer, group)
    store.settle_invoice(invoice.id, status="paid", issued_at=OCT)

    group.billing_company = "Zeta Holdings"
    group.save()
    store.settle_invoice(invoice.id, status="paid")

    assert _number(invoice) == "INV-ACME-202610-0001"


def test_an_invoice_without_an_account_uses_the_payer(payer):
    invoice = _invoice(payer)

    store.settle_invoice(invoice.id, status="paid", issued_at=OCT)

    code = invoice_number.account_code(None, payer.id)
    assert _number(invoice) == f"INV-{code}-202610-0001"


def test_the_list_prints_the_number_once_given(payer):
    from billing.services import portal

    invoice = _invoice(payer, _account(payer))
    store.settle_invoice(invoice.id, external_id="in_1", status="draft")
    invoice.refresh_from_db()
    assert portal._reference(invoice) == "in_1"

    store.settle_invoice(invoice.id, status="open", issued_at=OCT)
    invoice.refresh_from_db()
    assert portal._reference(invoice) == "INV-ACMELTD-202610-0001"


def test_numbering_failing_still_records_what_stripe_said(payer, monkeypatch):
    invoice = _invoice(payer, _account(payer))

    def boom(_id):
        raise RuntimeError("counter unavailable")

    monkeypatch.setattr(invoice_number, "assign", boom)
    store.settle_invoice(invoice.id, status="paid", issued_at=OCT)

    invoice.refresh_from_db()
    assert invoice.status == "paid"
    assert invoice.invoice_number is None


@pytest.mark.django_db(transaction=True)
def test_two_workers_at_once_take_different_numbers():
    """Each thread is its own connection; on Postgres the counter's row lock is what keeps
    them apart (SQLite serialises writes anyway, so it proves nothing there)."""
    import threading

    from django.db import connection, connections

    if connection.vendor != "postgresql":
        pytest.skip("row locks are a Postgres property")
    seed_currency("HKD")
    payer = make_user("payer@test.com")
    group = _account(payer, "Acme")
    invoices = [_invoice(payer, group, status="open") for _ in range(6)]
    start = threading.Barrier(len(invoices))
    errors = []

    def issue(invoice_id):
        try:
            start.wait()
            invoice_number.assign(invoice_id)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=issue, args=(i.id,)) for i in invoices]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    numbers = sorted(_number(i) for i in invoices)
    assert numbers == [f"INV-ACME-{datetime.now(UTC):%Y%m}-{n:04d}" for n in range(1, 7)]


def test_the_pdf_is_named_after_the_number_without_doubling_the_prefix():
    from billing.services.invoice_document import InvoiceDocument

    def named(reference):
        document = InvoiceDocument.__new__(InvoiceDocument)
        object.__setattr__(document, "reference", reference)
        return document.filename

    assert named("INV-ACME-202610-0042") == "INV-ACME-202610-0042.pdf"
    assert named("in_1UJ9BX") == "Inv-in_1UJ9BX.pdf"
    assert named("#3F2A9C1B") == "Inv-3F2A9C1B.pdf"
