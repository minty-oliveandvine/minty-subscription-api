"""08-B's "Billing Breakdown": one invoice, company by company (``portal.build_invoice_breakdown``).

What is pinned:

* **Each kind of line reads back its own days and its rate.** A renewal's rate is its charge;
  a mid-period start or upgrade, and the credit for the plan it replaced, cover ``at`` to the
  period's end; a cancellation extension covers the invoice's start to the module's access end.
* **A row adds up.** The rate is recovered from the charge over the period it was priced
  against, so a module that left a bundle shows the MARGINAL rate it was charged at rather
  than the catalogue's price - and a module resumed since (its access end cleared) shows no
  end day rather than a guessed one.
* **It is the payer's own.** Someone else's invoice, no invoice, or no id at all is None.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from billing.tests.engine.conftest import seed_currency
from billing.tests.engine.test_billing_accounts import _entity, _user

pytestmark = pytest.mark.django_db

ANCHOR = datetime(2026, 7, 18, 12, tzinfo=UTC)
START = datetime(2026, 8, 18, 12, tzinfo=UTC)
END = datetime(2026, 9, 18, 12, tzinfo=UTC)  # 31 days; the period before it is 31 too


def _payer(email="breakdown@payer.test"):
    """A payer with a cycle: extensions are priced against the period before the invoice."""
    from shared_models.models import UserStripeCustomer

    seed_currency("HKD")  # a foreign key on Postgres (fk_usc_currency); SQLite never checks it
    payer = _user(None, email)
    UserStripeCustomer(
        id=str(uuid.uuid4()),
        user_id=str(payer.id),
        stripe_customer_id=f"cus_{uuid.uuid4().hex[:8]}",
        anchor_at=ANCHOR,
        currency="HKD",
    ).save(force_insert=True)
    return payer


def _invoice(payer, *, total=0, status="paid", external_id="in_breakdown_1",
             billing_group_id=None):
    from shared_models.models import SubscriptionInvoice

    seed_currency("HKD")  # fk_si_currency, likewise
    invoice = SubscriptionInvoice(
        id=str(uuid.uuid4()),
        payer_user_id=str(payer.id),
        external_id=external_id,
        period_start=START,
        period_end=END,
        currency="HKD",
        total=total,
        status=status,
        billing_group_id=billing_group_id,
    )
    invoice.save(force_insert=True)
    return invoice


def _lines(invoice, *lines):
    """``(entity, product, amount, kind, at[, (period_start, period_end, unit_amount)])`` in the
    order they were written - spaced a second apart, because a test transaction freezes
    ``now()`` and ties would reorder them. Without the sixth item a line is one issued before
    lines recorded what they paid for (schema item 23)."""
    from shared_models.models import SubscriptionInvoiceLine

    for i, (entity, product, amount, kind, at, *recorded) in enumerate(lines):
        period_start, period_end, unit_amount = recorded[0] if recorded else (None, None, None)
        SubscriptionInvoiceLine(
            id=str(uuid.uuid4()),
            invoice_id=invoice.id,
            entity_id=entity.id,
            entity_name=entity.name,
            product_name=product,
            amount=amount,
            kind=kind,
            at=at,
            period_start=period_start,
            period_end=period_end,
            unit_amount=unit_amount,
            created_at=START + timedelta(seconds=i),
        ).save(force_insert=True)


def _read(payer, invoice):
    from billing.services import portal

    return portal.build_invoice_breakdown(payer.id, invoice.id)


def _row(result, entity_name, subscription):
    return next(
        r for r in result["rows"]
        if r["entity_name"] == entity_name and r["subscription"] == subscription
    )


def test_each_kind_of_line_reads_back_its_own_days_and_rate(app):
    from billing.tests.engine.conftest import seed_plans

    seed_plans()
    payer = _payer()
    renewing, joining, zero = (_entity(None, n) for n in ("Nexora Ltd", "Aetheria Ltd", "Zero Ltd"))
    upgraded = datetime(2026, 9, 1, 12, tzinfo=UTC)  # 17 of the period's 31 days left
    invoice = _invoice(payer, total=40000 + 15355 + 21935 - 15355)
    _lines(
        invoice,
        (renewing, "Super Minty", 40000, "full", None),
        (joining, "Payment Request", -15355, "unused", upgraded),
        (joining, "Super Minty", 21935, "remaining", upgraded),
        (zero, "Petty Cash", 0, "full", None),
    )

    result = _read(payer, invoice)

    assert result["invoice"]["reference"] == "in_breakdown_1"
    assert result["invoice"]["currency"] == "HKD"
    # The order written, and the zero line - never sent to anyone - left out.
    assert [(r["entity_name"], r["subscription"], r["kind"]) for r in result["rows"]] == [
        ("Nexora Ltd", "Super Minty", "full"),
        ("Aetheria Ltd", "Payment Request", "unused"),
        ("Aetheria Ltd", "Super Minty", "remaining"),
    ]
    # A renewal: the whole period, and its rate IS its charge.
    renewal = _row(result, "Nexora Ltd", "Super Minty")
    assert renewal["monthly_minor"] == 40000
    assert renewal["charged_minor"] == 40000
    assert renewal["period_start"].startswith("2026-08-18")
    assert renewal["period_end"].startswith("2026-09-18")
    # The upgrade: from the day it happened to the period's end, at the bundle's rate...
    upgrade = _row(result, "Aetheria Ltd", "Super Minty")
    assert upgrade["period_start"].startswith("2026-09-01")
    assert upgrade["period_end"].startswith("2026-09-18")
    assert upgrade["monthly_minor"] == 40000
    # ...and the credit for the plan it replaced: negative, at that plan's rate.
    credit = _row(result, "Aetheria Ltd", "Payment Request")
    assert (credit["charged_minor"], credit["monthly_minor"]) == (-15355, 28000)


def test_an_extension_shows_the_rate_it_was_charged_at_and_its_access_end(app):
    """Petty Cash leaving a Super Minty bundle is charged at the MARGINAL rate (400 - 280):
    8129 for 21 of the previous period's 31 days. Quoting the catalogue's 280 beside it would
    make a row that does not add up. A module resumed since has no access end any more - its
    day is left out, and the rate falls back to the catalogue's."""
    from billing.services import store
    from billing.tests.engine.conftest import seed_plans

    seed_plans()
    payer = _payer()
    leaving, resumed = _entity(None, "Leaving Ltd"), _entity(None, "Resumed Ltd")
    until = START + timedelta(days=21)
    store.upsert_module_row(leaving.id, "PETTY_CASH", payer.id, phase="cancelled",
                            app_access_until=until)
    store.upsert_module_row(resumed.id, "PAYMENT_REQUEST", payer.id, phase="active")
    invoice = _invoice(payer, total=8129 + 903)
    _lines(
        invoice,
        (leaving, "Petty Cash (access after cancellation)", 8129, "full", None),
        (resumed, "Payment Request (access after cancellation)", 903, "full", None),
    )

    result = _read(payer, invoice)

    marginal = _row(result, "Leaving Ltd", "Petty Cash")
    assert marginal["kind"] == "extension"
    assert marginal["monthly_minor"] == 12000
    assert marginal["period_start"].startswith("2026-08-18")
    assert marginal["period_end"] == until.isoformat()
    gone = _row(result, "Resumed Ltd", "Payment Request")
    assert gone["period_end"] is None
    assert gone["monthly_minor"] == 28000


def test_what_a_line_recorded_is_read_as_recorded_even_after_a_resume(app):
    """The gap item 23 closes. Payment Request's extension was billed at the 120 step to 5
    Sep; the module has since been resumed, so its row no longer says when access ended - and
    derivation would print a blank end and the catalogue's 280. The line itself says."""
    from billing.services import store
    from billing.tests.engine.conftest import seed_plans

    seed_plans()
    payer = _payer()
    resumed, joining = _entity(None, "Resumed Ltd"), _entity(None, "Joining Ltd")
    store.upsert_module_row(resumed.id, "PAYMENT_REQUEST", payer.id, phase="active")
    until = START + timedelta(days=18)
    joined = START + timedelta(days=10)
    invoice = _invoice(payer, total=6968 + 18968)  # 120 x 18/31 days; 280 x 21/31 days
    _lines(
        invoice,
        (resumed, "Payment Request (access after cancellation)", 6968, "full", None,
         (START, until, 12000)),
        (joining, "Petty Cash", 18968, "remaining", joined, (joined, END, 28000)),
    )

    result = _read(payer, invoice)

    extension = _row(result, "Resumed Ltd", "Payment Request")
    assert extension["kind"] == "extension"
    assert (extension["period_start"], extension["period_end"]) == (START.isoformat(),
                                                                    until.isoformat())
    assert extension["monthly_minor"] == 12000
    started = _row(result, "Joining Ltd", "Petty Cash")
    assert (started["period_start"], started["monthly_minor"]) == (joined.isoformat(), 28000)


def test_a_recorded_extension_with_no_single_rate_shows_the_rate_its_days_add_up_to(app):
    """Priced in pieces at two rates, it recorded its days and no rate. The row shows the one
    rate that makes it add up over those days, against the period they were priced on (31
    days here): 8129 for 21 days is 12000 a month."""
    from billing.tests.engine.conftest import seed_plans

    seed_plans()
    payer = _payer()
    leaving = _entity(None, "Leaving Ltd")
    until = START + timedelta(days=21)
    invoice = _invoice(payer, total=8129)
    _lines(invoice, (leaving, "Petty Cash (access after cancellation)", 8129, "full", None,
                     (START, until, None)))

    row = _row(_read(payer, invoice), "Leaving Ltd", "Petty Cash")

    assert row["period_end"] == until.isoformat()
    assert row["monthly_minor"] == 12000


def test_the_breakdown_is_the_payers_own(app):
    payer, stranger = _payer(), _payer("stranger@payer.test")
    invoice = _invoice(payer)
    _lines(invoice, (_entity(None, "Acme Ltd"), "Petty Cash", 28000, "full", None))

    from billing.services import portal

    assert _read(payer, invoice) is not None
    assert _read(stranger, invoice) is None
    assert portal.build_invoice_breakdown(payer.id, str(uuid.uuid4())) is None
    assert portal.build_invoice_breakdown(payer.id, "not-an-id") is None
