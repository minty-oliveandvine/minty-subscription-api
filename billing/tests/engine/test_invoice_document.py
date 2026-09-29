"""The invoice PDF's content, Figma 09-A (``invoice_document.build_invoice_document``).

What is pinned (the user's decisions, 2026-09-29):

* **Bill to is the invoice's BILLING ACCOUNT**, as 08-B prints it — the account's name, its
  billing email, its charged card's address — never the Stripe customer. An invoice raised
  before accounts existed belongs to the payer's OLDEST account, as the list and dunning read it.
* **The lines are 09-A's plan lines with the companies under each**, every company row ONE
  Stripe item in Stripe's own words minus the plan its heading names — proven against what the
  gateway actually sent.
* **It never disagrees with the charge.** Lines that do not add up to the invoice's total are
  refused, loudly; an invoice that is no document (never sent, a draft, void) is refused.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from billing.tests.engine.conftest import seed_country, seed_currency
from billing.tests.engine.test_billing_accounts import _entity
from billing.tests.engine.test_invoice_breakdown import END, START, _invoice, _lines, _payer

pytestmark = pytest.mark.django_db

ADDRESS = {"line1": "Unit 10, 1/F", "line2": "ABC Building", "city": "Quarry Bay",
           "state": None, "postal_code": None, "country": "HK"}
OTHER_ADDRESS = {"line1": "1 Other Road", "line2": None, "city": "Central",
                 "state": None, "postal_code": None, "country": "HK"}
UPGRADED = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _hkd():
    """HKD printing as the frame does ("HK$"), seeded before ``_payer`` seeds it as "$"."""
    seed_currency("HKD", symbol="HK$")
    seed_country()


def _account(payer, *, company="Company A Limited", email="billing@companyalimited.com",
             card="pm_charged"):
    from billing.services import store

    return store.create_billing_account(
        payer.id, card, billing_email=email, billing_company=company
    )


def _wallet(monkeypatch, *cards, fails=False):
    """What ``payment_methods.list_for_user`` answers: ``(card id, address)`` pairs."""
    from billing.services import payment_methods

    reads: list = []

    def _list(user_id):
        reads.append(user_id)
        if fails:
            raise RuntimeError("the processor is down")
        return {
            "has_account": True, "default_id": None, "total": len(cards),
            "methods": [{"id": card, "address": address} for card, address in cards],
        }

    monkeypatch.setattr(payment_methods, "list_for_user", _list)
    return reads


def _document(payer, invoice):
    from billing.services.invoice_document import build_invoice_document

    return build_invoice_document(payer.id, invoice.id)


def _lines_of(doc):
    return [(group.label, group.amount, [(row.text, row.amount) for row in group.rows])
            for group in doc.groups]


# --- the lines ---------------------------------------------------------------------------


def test_a_renewal_prints_09a_plan_lines_with_their_companies_under_each(app, monkeypatch):
    _hkd()
    payer = _payer()
    account = _account(payer)
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    e, f, g = (_entity(None, name) for name in
               ("Company E Limited", "Company F Limited", "Company G Limited"))
    aetheria, nexora = _entity(None, "Aetheria Capital Limited"), _entity(None, "Nexora Health Limited")
    invoice = _invoice(payer, total=133032, billing_group_id=account.id)
    _lines(
        invoice,
        (nexora, "Super Minty", 40000, "full", None),
        (aetheria, "Payment Request", 28000, "full", None),
        (g, "Petty Cash", 28000, "full", None),
        (e, "Petty Cash", 28000, "full", None),
        (f, "Petty Cash (access after cancellation)", 9032, "full", None),
    )

    doc = _document(payer, invoice)

    # 09-A's order and words; each plan's figure is its companies' sum.
    assert _lines_of(doc) == [
        ("Petty cash module only", "HK$650.32", [
            ("Company E Limited", "HK$280"),
            ("Company F Limited (access after cancellation)", "HK$90.32"),
            ("Company G Limited", "HK$280"),
        ]),
        ("Payment request module only", "HK$280", [("Aetheria Capital Limited", "HK$280")]),
        ("SuperMinty", "HK$400", [("Nexora Health Limited", "HK$400")]),
    ]
    assert (doc.total, doc.total_minor) == ("HK$1,330.32", 133032)
    assert doc.amount_header == "Amount (HK$)"
    assert (doc.reference, doc.filename) == ("in_breakdown_1", "Inv-in_breakdown_1.pdf")


def test_a_credit_sits_under_its_own_plan_with_the_sign_in_front(app, monkeypatch):
    """An upgrade: the old plan's unused days credited, the new one charged. Each stays under
    its own plan, and a credit reads "-HK$180.65", never "HK$-180.65"."""
    _hkd()
    payer = _payer()
    account = _account(payer)
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    e = _entity(None, "Company E Limited")
    invoice = _invoice(payer, total=-18065 + 25806, billing_group_id=account.id)
    _lines(
        invoice,
        (e, "Petty Cash", -18065, "unused", UPGRADED),
        (e, "Super Minty", 25806, "remaining", UPGRADED),
    )

    doc = _document(payer, invoice)

    assert _lines_of(doc) == [
        ("Petty cash module only", "-HK$180.65",
         [("Company E Limited (unused time)", "-HK$180.65")]),
        ("SuperMinty", "HK$258.06", [("Company E Limited", "HK$258.06")]),
    ]
    assert doc.total == "HK$77.41"


def test_a_zero_line_is_on_no_invoice_anyone_saw(app, monkeypatch):
    _hkd()
    payer = _payer()
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=28000, billing_group_id=_account(payer).id)
    _lines(
        invoice,
        (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None),
        (_entity(None, "Zero Ltd"), "Payment Request", 0, "full", None),
    )

    assert _lines_of(_document(payer, invoice)) == [
        ("Petty cash module only", "HK$280", [("Acme Ltd", "HK$280")]),
    ]


def test_a_plan_09a_has_no_words_for_prints_its_own_name_last(app, monkeypatch, caplog):
    _hkd()
    payer = _payer()
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=56000, billing_group_id=_account(payer).id)
    _lines(
        invoice,
        (_entity(None, "Acme Ltd"), "Mega Minty", 28000, "full", None),
        (_entity(None, "Beta Ltd"), "Petty Cash", 28000, "full", None),
    )

    with caplog.at_level(logging.WARNING, logger="billing-api"):
        doc = _document(payer, invoice)

    assert [group.label for group in doc.groups] == ["Petty cash module only", "Mega Minty"]
    assert any("Mega Minty" in record.getMessage() for record in caplog.records)


def test_every_company_row_is_one_stripe_item_in_stripe_s_own_words(app, monkeypatch):
    """Issued through the real gateway against a Stripe that keeps what it was sent: each
    row's full text and amount is exactly one item's description and amount."""
    from billing.services import billing_gateway
    from billing.services.billing import Invoice, Line, Period
    from shared_models.models import SubscriptionInvoice, UserStripeCustomer

    _hkd()
    payer = _payer()
    account = _account(payer)
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    stripe = _RecordingStripe()
    monkeypatch.setattr(billing_gateway, "get_stripe", lambda: stripe)
    nexora, e, f = (_entity(None, name) for name in
                    ("Nexora Health Limited", "Company E Limited", "Company F Limited"))
    customer = UserStripeCustomer.objects.get(user_id=str(payer.id)).stripe_customer_id

    billing_gateway.issue_invoice(
        customer,
        Invoice(currency="hkd", period=Period(START, END), lines=(
            Line(str(nexora.id), nexora.name, "Super Minty", 40000),
            Line(str(e.id), e.name, "Petty Cash", -15355, kind="unused", at=UPGRADED),
            Line(str(e.id), e.name, "Super Minty", 21935, kind="remaining", at=UPGRADED),
            Line(str(f.id), f.name, "Petty Cash (access after cancellation)", 9032),
        )),
        idempotency_key="doc-roundtrip",
        payer_user_id=str(payer.id),
        payment_method="pm_charged",
        billing_group_id=str(account.id),
    )
    invoice = SubscriptionInvoice.objects.get(external_id="in_doc_1")

    doc = _document(payer, invoice)

    printed = sorted((row.stripe_text, row.amount_minor)
                     for group in doc.groups for row in group.rows)
    sent = sorted((item["description"], item["amount"]) for item in stripe.items)
    assert printed == sent
    assert len(sent) == 4


# --- Bill to -----------------------------------------------------------------------------


def test_bill_to_is_the_invoices_billing_account_as_08b_prints_it(app, monkeypatch):
    _hkd()
    payer = _payer()
    _account(payer, company="Oldest Co Ltd", email="ap@oldest.test", card="pm_old")
    account = _account(payer)
    _wallet(monkeypatch, ("pm_old", OTHER_ADDRESS), ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=28000, billing_group_id=account.id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    doc = _document(payer, invoice)

    assert doc.bill_to_name == "Company A Limited"
    # The account's CHARGED card's address, laid out as 08-B's addressLines does.
    assert doc.bill_to_address == ("Unit 10, 1/F", "ABC Building", "Quarry Bay, Hong Kong")
    assert doc.bill_to_email == "billing@companyalimited.com"


def test_an_invoice_from_before_accounts_belongs_to_the_oldest_account(app, monkeypatch):
    """The same attribution as the invoice list's account filter and dunning's - the PDF
    and the page it is downloaded from agree on whose invoice it is."""
    from billing.services import portal
    from shared_models.models import PayerBillingGroup

    _hkd()
    payer = _payer()
    oldest = _account(payer, company="First Co Ltd", email="ap@first.test", card="pm_first")
    newer = _account(payer, company="Second Co Ltd", email="ap@second.test", card="pm_second")
    # Ages pinned: a Postgres test transaction freezes now(), so both would share one
    # created_at and "oldest" would fall to the id tiebreak - a coin toss.
    for account, opened in ((oldest, datetime(2026, 1, 5, tzinfo=UTC)),
                            (newer, datetime(2026, 6, 5, tzinfo=UTC))):
        PayerBillingGroup.objects.filter(id=account.id).update(created_at=opened)
    _wallet(monkeypatch, ("pm_first", ADDRESS), ("pm_second", OTHER_ADDRESS))
    invoice = _invoice(payer, total=28000)  # raised before accounts: no billing group
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    doc = _document(payer, invoice)

    assert (doc.bill_to_name, doc.bill_to_email) == ("First Co Ltd", "ap@first.test")
    listed = portal.build_payer_invoices(payer.id, account_id=str(oldest.id))
    assert [row["id"] for row in listed["invoices"]] == [str(invoice.id)]


def test_an_unnamed_account_reads_as_the_payer_and_so_does_its_email(app, monkeypatch):
    _hkd()
    payer = _payer("unnamed@payer.test")
    account = _account(payer, company=None, email=None)
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=28000, billing_group_id=account.id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    doc = _document(payer, invoice)

    assert (doc.bill_to_name, doc.bill_to_email) == ("Pat Payer", "unnamed@payer.test")


def test_a_payer_with_no_account_is_billed_by_their_own_name_without_asking_stripe(
    app, monkeypatch
):
    _hkd()
    payer = _payer("solo@payer.test")
    reads = _wallet(monkeypatch, fails=True)
    invoice = _invoice(payer, total=28000)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    doc = _document(payer, invoice)

    assert (doc.bill_to_name, doc.bill_to_address, doc.bill_to_email) == (
        "Pat Payer", (), "solo@payer.test",
    )
    assert reads == []


def test_a_card_stripe_no_longer_holds_leaves_no_address_as_on_08b(app, monkeypatch):
    _hkd()
    payer = _payer()
    account = _account(payer)
    _wallet(monkeypatch)  # the charged card is gone from the wallet
    invoice = _invoice(payer, total=28000, billing_group_id=account.id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    assert _document(payer, invoice).bill_to_address == ()


def test_an_address_that_cannot_be_read_is_refused_never_printed_blank(app, monkeypatch):
    from billing.services.invoice_document import BillToUnavailable

    _hkd()
    payer = _payer()
    account = _account(payer)
    _wallet(monkeypatch, fails=True)
    invoice = _invoice(payer, total=28000, billing_group_id=account.id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    with pytest.raises(BillToUnavailable):
        _document(payer, invoice)


def test_the_bill_date_is_the_date_the_invoice_list_shows(app, monkeypatch):
    from billing.services import portal
    from shared_models.models import SubscriptionInvoice

    _hkd()
    payer = _payer()
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=28000, billing_group_id=_account(payer).id)
    SubscriptionInvoice.objects.filter(id=invoice.id).update(
        issued_at=datetime(2026, 9, 14, 3, tzinfo=UTC)
    )
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    doc = _document(payer, invoice)

    listed = portal.build_payer_invoices(payer.id)["invoices"][0]
    assert doc.bill_date == listed["date"] == "14 Sep 2026"


# --- never a document that disagrees -----------------------------------------------------


@pytest.mark.parametrize(
    "status, external_id",
    [("void", "in_void"), ("draft", "in_draft"), ("paid", None)],
    ids=["voided", "a draft", "never sent"],
)
def test_an_invoice_that_is_no_document_is_refused(app, monkeypatch, status, external_id):
    from billing.services import portal
    from billing.services.invoice_document import NoDocument

    _hkd()
    payer = _payer()
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=28000, status=status, external_id=external_id,
                       billing_group_id=_account(payer).id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    with pytest.raises(NoDocument):
        _document(payer, invoice)
    # ...and the list offers no PDF for it: one rule answers both.
    assert portal.build_payer_invoices(payer.id)["invoices"][0]["has_pdf"] is False


def test_lines_that_do_not_add_up_to_the_charge_are_refused_loudly(app, monkeypatch, caplog):
    from billing.services.invoice_document import TotalMismatch

    _hkd()
    payer = _payer()
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=40000, billing_group_id=_account(payer).id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    with caplog.at_level(logging.ERROR, logger="billing-api"), pytest.raises(TotalMismatch):
        _document(payer, invoice)
    assert any(record.levelno == logging.ERROR and str(invoice.id) in record.getMessage()
               for record in caplog.records)


def test_someone_elses_invoice_is_none(app, monkeypatch):
    _hkd()
    payer, stranger = _payer(), _payer("stranger@payer.test")
    _wallet(monkeypatch, ("pm_charged", ADDRESS))
    invoice = _invoice(payer, total=28000, billing_group_id=_account(payer).id)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    assert _document(stranger, invoice) is None


class _RecordingStripe:
    """Just enough Stripe for ``issue_invoice`` to raise, finalize and pay one invoice — and it
    keeps every item it was sent."""

    def __init__(self):
        self.items: list[dict] = []
        outer = self

        class _Invoices:
            @staticmethod
            def create(**_kw):
                return {"id": "in_doc_1", "status": "draft", "created": 1800000000}

            @staticmethod
            def finalize_invoice(invoice_id, **_kw):
                return outer._state(invoice_id, "open")

            @staticmethod
            def pay(invoice_id, **_kw):
                return outer._state(invoice_id, "paid")

            @staticmethod
            def retrieve(invoice_id, **_kw):
                return outer._state(invoice_id, "paid")

        class _Items:
            @staticmethod
            def create(**kw):
                outer.items.append(kw)
                return {"id": f"ii_{len(outer.items)}"}

        self.Invoice, self.InvoiceItem = _Invoices, _Items

    def _state(self, invoice_id, status):
        transitions = {"finalized_at": 1800000100}
        if status == "paid":
            transitions["paid_at"] = 1800000200
        return {"id": invoice_id, "status": status, "created": 1800000000,
                "total": sum(item["amount"] for item in self.items),
                "status_transitions": transitions}
