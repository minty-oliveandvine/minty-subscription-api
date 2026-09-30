"""A trial's conversion the PROCESSOR failed is retried, never charged twice
(``checkout._resolve_prior_conversions``), and the trial keeps its access meanwhile
(``access_sweep._conversion_pending``).

The user's rule (2026-09-30): when Stripe itself fails at trial end, the trial keeps running
and the next pass tries again. Each attempt takes a fresh ``convert-`` key (a fixed one would
replay the processor's error for a day, and move the anchor and the proration), so before a
new attempt the earlier ones are found by that key and settled by what the processor says:

* PAID (a lost answer, or collected since) is ADOPTED - never charged again;
* OPEN and never tried is charged now, on the company's current card;
* OPEN and refused is withdrawn, and the trial expires as a decline does;
* a DRAFT is withdrawn, and a fresh attempt goes on;
* the processor unreadable defers again - nothing is charged on a guess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import stripe as stripe_lib

from billing.tests.engine.test_invoice_refresh import CUSTOMER, DEAD_CARD, END, START, _world

pytestmark = pytest.mark.django_db

AT = datetime(2027, 3, 8, 12, tzinfo=UTC)            # when the attempt was made
SINCE = AT - timedelta(hours=1)                      # the trial ended just before
AFTER = {"PAYMENT_REQUEST"}


def _conversion(monkeypatch):
    from billing.services import store

    stripe, payer, entity, account = _world(monkeypatch)
    store.upsert_customer_mapping(payer.id, CUSTOMER)
    return stripe, payer, entity, account


def _attempt(payer, entity, account, *, after=AFTER):
    """Raise one conversion attempt through the real gateway. Returns its key; a failure is
    left exactly as the processor left it (the conversion withdraws nothing on an outage)."""
    from billing.services import billing_gateway, changes
    from billing.services.billing import Invoice, Line, Period

    key = changes.change_key(entity.id, AT, after, kind="convert")
    invoice = Invoice(currency="hkd", period=Period(START, END), lines=(
        Line(str(entity.id), "Refresh Co", "Payment Request", 28000,
             period_start=START, period_end=END, unit_amount=28000),))
    try:
        billing_gateway.issue_invoice(
            CUSTOMER, invoice, idempotency_key=key,
            metadata={"change_key": key, "billing_group": str(account.id)},
            payer_user_id=payer.id, payment_method=account.stripe_payment_method_id,
            billing_group_id=account.id,
        )
    except billing_gateway.BillingError:
        pass
    return key


def _resolve(payer, entity, account, *, after=AFTER, since=SINCE, now=None):
    from billing.services import checkout

    return checkout._resolve_prior_conversions(
        entity.id, payer.id, CUSTOMER, account, after, since, now
    )


def _invoice_of(key):
    from billing.services import store

    return store.invoice_for_key(key)


def test_an_attempt_nobody_charged_is_charged_on_the_current_card_and_adopted(monkeypatch):
    """The outage came between finalizing and charging: open, never tried."""
    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.outage["pay"] = "before"
    key = _attempt(payer, entity, account)
    stripe.declining.discard(DEAD_CARD)              # the processor is back; the card is good

    period = _resolve(payer, entity, account)

    assert (period.start, period.end) == (START, END)
    assert stripe.invoices[_invoice_of(key).external_id]["status"] == "paid"


def test_an_attempt_paid_after_all_is_adopted_without_a_charge(monkeypatch):
    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.outage["pay"] = "before"
    key = _attempt(payer, entity, account)
    stripe.invoices[_invoice_of(key).external_id].update(
        status="paid", status_transitions={"paid_at": 1800000200})
    stripe.calls.clear()

    assert _resolve(payer, entity, account).end == END
    assert [c for c in stripe.calls if c[0] in ("create", "pay")] == []


def test_an_attempt_the_card_refused_expires_the_trial(monkeypatch):
    """Tried and declined, then the answer was lost: it IS a decline, whatever reached us."""
    stripe, payer, entity, account = _conversion(monkeypatch)
    key = _attempt(payer, entity, account)           # the DEAD card declines: open, refused

    assert _resolve(payer, entity, account) == "declined"
    assert stripe.invoices[_invoice_of(key).external_id]["status"] == "void"


def test_a_draft_attempt_is_withdrawn_for_a_fresh_one(monkeypatch):
    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.outage["finalize"] = "before"
    key = _attempt(payer, entity, account)
    draft = _invoice_of(key).external_id

    assert _resolve(payer, entity, account) is None
    assert draft not in stripe.invoices               # deleted at the processor


def test_the_processor_unreadable_defers_again(monkeypatch):
    from billing.services import checkout

    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.outage["pay"] = "before"
    _attempt(payer, entity, account)

    def _down(*a, **k):
        raise stripe_lib.APIConnectionError("no route to Stripe")

    monkeypatch.setattr(stripe.Invoice, "retrieve", _down)
    with pytest.raises(checkout.ChargeDeferred):
        _resolve(payer, entity, account)


def test_an_attempt_paid_for_other_modules_waits_for_a_person(monkeypatch, caplog):
    """Two trials of one company ending inside one outage. Guessing either way charges twice
    or gives modules away."""
    from billing.services import checkout

    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.outage["pay"] = "before"
    key = _attempt(payer, entity, account, after={"PETTY_CASH"})
    stripe.invoices[_invoice_of(key).external_id].update(
        status="paid", status_transitions={"paid_at": 1800000200})

    with pytest.raises(checkout.ChargeDeferred):
        _resolve(payer, entity, account, after=AFTER)
    assert any(r.levelname == "ERROR" and "resolve it by hand" in r.getMessage()
               for r in caplog.records)


def test_an_older_conversion_is_not_this_trials(monkeypatch):
    """Another module converted months ago on the same company: not an attempt at this one."""
    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.declining.discard(DEAD_CARD)
    _attempt(payer, entity, account)                  # paid - but before this trial ended

    assert _resolve(payer, entity, account, since=AT + timedelta(days=1)) is None


# --- the trial keeps working while its conversion is pending --------------------------------------


def _trial(ended, phase="trial"):
    return SimpleNamespace(phase=phase, trial_end=ended)


def test_a_trial_whose_conversion_is_pending_keeps_its_access():
    from billing.services import access_sweep

    now = datetime(2027, 3, 10, tzinfo=UTC)

    assert access_sweep._conversion_pending(_trial(now - timedelta(days=2)), now, 15)
    assert not access_sweep._conversion_pending(_trial(now - timedelta(days=16)), now, 15)
    assert not access_sweep._conversion_pending(
        _trial(now - timedelta(days=2), phase="expired"), now, 15)


def test_a_paid_attempt_for_a_period_that_has_ended_is_not_adopted(monkeypatch):
    """It paid for the period it was raised in. Adopted after the card moved into the next one,
    the company went in marked as billed there and its renewal skipped it - a free month."""
    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.declining.discard(DEAD_CARD)
    _attempt(payer, entity, account)                          # paid, for START..END

    assert _resolve(payer, entity, account, now=END + timedelta(hours=1)) is None
    assert _resolve(payer, entity, account, now=END - timedelta(days=1)).end == END


def test_an_attempt_in_the_second_the_trial_ended_is_this_trials(monkeypatch):
    """Keys carry whole seconds; ``trial_end`` carries microseconds."""
    stripe, payer, entity, account = _conversion(monkeypatch)
    stripe.declining.discard(DEAD_CARD)
    _attempt(payer, entity, account)

    assert _resolve(payer, entity, account, since=AT + timedelta(microseconds=500)).end == END
