"""A charge the customer may retry, raised under its own key each time
(``changes.issue_change(attempts_of=...)``, ``changes._next_attempt``).

A reinstatement's key is stable - the company, the day its access ends, the modules. The
declined attempt's invoice is voided, its row keeps the key, and the unique index refused
every retry until the cancellation window ran out: "check your payment method", with the
card never even tried.

What is pinned, against the real gateway and store and a processor with a memory:

* A DECLINE CAN BE RETRIED - the next attempt takes the next key.
* NEVER TWICE. An earlier attempt found PAID is adopted, not charged again; one still open
  is withdrawn before the next is raised.
* NEVER AN OLD KEY. A reservation the processor never saw is discarded, and the retry moves
  on to the next number (Stripe replays a keyed error for 24 hours).
* NOTHING ON A GUESS. With the processor unreadable, nothing is raised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import stripe as stripe_lib

from billing.services.billing import Line, Period, plan_code
from billing.tests.engine.test_change_billing import PLANS
from billing.tests.engine.test_invoice_refresh import CUSTOMER, DEAD_CARD, END, START, _world

pytestmark = pytest.mark.django_db

PERIOD = Period(START, END)
# Where the cancelled module's paid extension ran out: what the reinstatement bills from.
COVERED_TO = datetime(2027, 3, 20, 12, tzinfo=UTC)
AFTER = {"BILL", "PETTY_CASH"}


def _world_for_changes(monkeypatch):
    from billing.services import store

    stripe, payer, entity, account = _world(monkeypatch)
    store.upsert_customer_mapping(payer.id, CUSTOMER)
    monkeypatch.setattr(store, "billing_plan_for_codes",
                        lambda codes: PLANS.get(plan_code(codes)))
    return stripe, payer, entity, account


def _attempt(payer, entity, account):
    from billing.services import changes

    return changes.issue_change(
        CUSTOMER, entity.id, "Refresh Co", {"BILL"}, AFTER, PERIOD, COVERED_TO,
        group=account, attempts_of=payer.id,
    )


def _declined(payer, entity, account):
    from billing.services.billing_gateway import BillingError

    with pytest.raises(BillingError) as raised:
        _attempt(payer, entity, account)
    return raised.value.invoice_id


def _base(entity):
    from billing.services import changes

    return changes.change_key(entity.id, COVERED_TO, AFTER)


def _keys(payer):
    from billing.services import store

    return sorted(row.idempotency_key for row in store.invoices_with_key_prefix(payer.id, "change-"))


def test_a_declined_attempt_is_retried_under_the_next_key(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    first = _declined(payer, entity, account)
    billing_gateway.void_invoice(first)          # what the reinstatement does with a decline
    stripe.declining.discard(DEAD_CARD)          # the customer fixes the card

    result = _attempt(payer, entity, account)

    assert result["status"] == "paid"
    assert _keys(payer) == [_base(entity), f"{_base(entity)}~2"]


def test_an_earlier_attempt_found_paid_is_adopted_not_charged_again(monkeypatch):
    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    first = _declined(payer, entity, account)
    # Paid after all - a reply lost on the way back, or a payment made elsewhere.
    stripe.invoices[first].update(status="paid", status_transitions={"paid_at": 1800000200})
    stripe.calls.clear()

    result = _attempt(payer, entity, account)

    assert (result["id"], result["status"]) == (first, "paid")
    assert [c for c in stripe.calls if c[0] in ("create", "pay")] == []
    assert _keys(payer) == [_base(entity)]


def test_an_earlier_attempt_still_open_is_withdrawn_before_the_next(monkeypatch):
    """Its void failed (logged), so it is still owed at the processor: charged again beside a
    new attempt, the customer would pay twice."""
    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    first = _declined(payer, entity, account)
    stripe.declining.discard(DEAD_CARD)

    result = _attempt(payer, entity, account)

    assert stripe.invoices[first]["status"] == "void"
    assert result["status"] == "paid" and result["id"] != first


def test_a_reservation_the_processor_never_saw_is_not_reused(monkeypatch):
    """Claimed, then nothing came back. Its key is never raised again - a keyed 5xx is
    replayed for a day - so the retry moves on to the next number."""
    from billing.services import store

    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    _reserved(payer, entity, age=timedelta(minutes=30))     # long enough to be abandoned
    stripe.declining.discard(DEAD_CARD)

    result = _attempt(payer, entity, account)

    assert result["status"] == "paid"
    assert store.invoice_for_key(_base(entity)) is None
    assert store.invoice_for_key(f"{_base(entity)}~2").external_id == result["id"]


def test_an_attempt_still_in_flight_is_refused_not_discarded(monkeypatch):
    """A second press while the first one's create has not answered: the processor cannot show
    it yet. Discarded and followed by the next attempt, the customer was charged twice the
    moment the first landed. It is refused as claimed - a 409, "already being restored"."""
    from billing.services import billing_gateway, store

    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    _reserved(payer, entity, age=timedelta(seconds=5))
    stripe.calls.clear()

    with pytest.raises(billing_gateway.BillingError) as raised:
        _attempt(payer, entity, account)

    assert raised.value.claimed
    assert store.invoice_for_key(_base(entity)) is not None        # still the first press's
    assert stripe.calls == []


def _reserved(payer, entity, *, age):
    """A reservation of the first attempt that the processor never confirmed, ``age`` old."""
    from billing.services import store
    from shared_models.models import SubscriptionInvoice

    row = store.reserve_invoice(
        payer_user_id=payer.id, stripe_customer_id=CUSTOMER, period=PERIOD, currency="hkd",
        lines=[Line(str(entity.id), "Refresh Co", "Petty Cash", 1000)], memo=None,
        idempotency_key=_base(entity),
    )
    SubscriptionInvoice.objects.filter(pk=row.id).update(created_at=datetime.now(UTC) - age)
    return row


def test_a_refresh_s_retired_key_is_not_an_attempt(monkeypatch):
    """A refresh retires a dead invoice's key to ``<key>~in_…`` (``store.retired_key``). Read as
    an attempt number, the count would skip or collide."""
    from billing.services import billing_gateway, store

    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    first = _declined(payer, entity, account)
    billing_gateway.void_invoice(first)
    store.reserve_invoice(
        payer_user_id=payer.id, stripe_customer_id=CUSTOMER, period=PERIOD, currency="hkd",
        lines=[Line(str(entity.id), "Refresh Co", "Petty Cash", 1000)], memo=None,
        idempotency_key=f"{_base(entity)}~in_old",
    )
    stripe.declining.discard(DEAD_CARD)

    assert _attempt(payer, entity, account)["status"] == "paid"
    assert f"{_base(entity)}~2" in _keys(payer)


def test_nothing_is_raised_while_the_last_attempt_cannot_be_read(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world_for_changes(monkeypatch)
    first = _declined(payer, entity, account)
    billing_gateway.void_invoice(first)

    def _down(*a, **k):
        raise stripe_lib.APIConnectionError("no route to Stripe")

    monkeypatch.setattr(stripe.Invoice, "retrieve", _down)
    stripe.calls.clear()
    with pytest.raises(billing_gateway.BillingError) as raised:
        _attempt(payer, entity, account)

    assert raised.value.retryable
    assert [c for c in stripe.calls if c[0] == "create"] == []
