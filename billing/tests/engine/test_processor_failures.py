"""A processor that fails is not a card that declines, and a charge whose answer was lost is
still a charge (``billing_gateway``, ``stripe_client``, ``store``, ``dunning``, ``notify``).

The user's rule (2026-09-30): when STRIPE itself fails - an outage, a timeout, a rate limit,
our own key - nothing is known about the customer's card, so it is retried on the next pass
rather than read as a decline. These are the shared pieces every caller builds on.

What is pinned:

* CLASSIFIED. A processor failure comes out ``retryable``; a refusal of the card, and anything
  unrecognised, does not (today's handling, a decline).
* PAID IS PAID. A pay that raised but went through is recorded and returned as paid - by the
  first charge, by a dunning retry, and by a void that meets it.
* ASKED, NOT GUESSED. ``recheck`` raises when it cannot read; ``declined`` answers from the
  processor's own record of the attempt.
* THE SILENT GRACE. Past due without a dunning stamp - access kept, nobody told - and released
  once the card is paid up; an attempt the processor failed to make is given back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import stripe as stripe_lib

from billing.tests.engine.test_invoice_refresh import (
    CUSTOMER,
    DEAD_CARD,
    END,
    GOOD_CARD,
    START,
    _renewal,
    _row,
    _world,
)

pytestmark = pytest.mark.django_db

KEY = "renewal-failures-20270306-g"


def _invoice(entity):
    from billing.services.billing import Invoice, Line, Period

    return Invoice(currency="hkd", period=Period(START, END), lines=(
        Line(str(entity.id), "Refresh Co", "Petty Cash", 28000,
             period_start=START, period_end=END, unit_amount=28000),))


def _issue(payer, entity, account, *, card, key=KEY, collect=True):
    from billing.services import billing_gateway

    return billing_gateway.issue_invoice(
        CUSTOMER, _invoice(entity), idempotency_key=key, collect=collect,
        metadata={"renewal_key": key, "billing_group": str(account.id)},
        payer_user_id=payer.id, payment_method=card, billing_group_id=account.id,
    )


# --- classified ---------------------------------------------------------------------------------


@pytest.mark.parametrize("error", [
    stripe_lib.APIConnectionError("no route"),
    stripe_lib.APIError("500 from Stripe"),
    stripe_lib.RateLimitError("slow down"),
    stripe_lib.AuthenticationError("bad key"),
    stripe_lib.PermissionError("key lacks the right"),
    stripe_lib.IdempotencyError("key reused"),
])
def test_a_processor_failure_is_transient(error):
    from billing.services import billing_gateway, stripe_client

    assert stripe_client.is_transient(error)
    assert billing_gateway.retryable(error)


@pytest.mark.parametrize("error", [
    stripe_lib.CardError("Your card was declined.", None, "card_declined"),
    stripe_lib.InvalidRequestError("No such payment_method", "payment_method"),
    ValueError("our own bug"),
])
def test_a_refusal_or_anything_unrecognised_is_not(error):
    from billing.services import billing_gateway, stripe_client

    assert not stripe_client.is_transient(error)
    assert not billing_gateway.retryable(error)


def test_an_outage_comes_out_retryable_and_a_decline_does_not(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    stripe.outage["create"] = "before"
    with pytest.raises(billing_gateway.BillingError) as outage:
        _issue(payer, entity, account, card=GOOD_CARD)
    assert outage.value.retryable

    with pytest.raises(billing_gateway.BillingError) as refused:
        _issue(payer, entity, account, card=DEAD_CARD, key=f"{KEY}-2")
    assert not refused.value.retryable
    assert _row(refused.value.invoice_id).status == "open"


def test_a_missing_key_is_retryable_not_a_bare_error(monkeypatch):
    """No key means nothing can be charged - our failure. It used to escape ``issue_invoice``
    as a RuntimeError, which every caller read as a decline."""
    from billing.services import billing_gateway, stripe_client

    stripe, payer, entity, account = _world(monkeypatch)

    def _unconfigured():
        raise stripe_client.StripeNotConfigured("STRIPE_SECRET_KEY is not configured")

    monkeypatch.setattr(billing_gateway, "get_stripe", _unconfigured)
    with pytest.raises(billing_gateway.BillingError) as raised:
        _issue(payer, entity, account, card=GOOD_CARD)
    assert raised.value.retryable


def test_a_claimed_key_is_retryable_and_says_so(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    _issue(payer, entity, account, card=GOOD_CARD)
    stripe.calls.clear()

    with pytest.raises(billing_gateway.BillingError) as raised:
        _issue(payer, entity, account, card=GOOD_CARD)
    assert raised.value.claimed and raised.value.retryable
    assert stripe.calls == []                       # never reached the processor


# --- paid is paid ---------------------------------------------------------------------------------


def test_a_charge_whose_answer_was_lost_is_paid(monkeypatch):
    """The charge went through and the answer died on the way back. Read as a decline, a
    purchase is refused and charged again on the next attempt."""
    stripe, payer, entity, account = _world(monkeypatch)
    stripe.fail["pay"] = "after"

    result = _issue(payer, entity, account, card=GOOD_CARD)

    assert result["status"] == "paid"
    assert _row(result["id"]).status == "paid"
    assert [c for c in stripe.calls if c[0] == "pay"] == [("pay", result["id"])]


def test_a_retry_whose_answer_was_lost_is_paid(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    owed = _renewal(stripe, payer, entity, account)
    stripe.fail["pay"] = "after"

    assert billing_gateway.retry_invoice(owed, GOOD_CARD) == (True, None)
    assert _row(owed).status == "paid"


def test_a_retry_that_meets_an_outage_is_unavailable_not_a_decline(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    owed = _renewal(stripe, payer, entity, account)
    stripe.outage["pay"] = "before"

    assert billing_gateway.retry_invoice(owed, GOOD_CARD) == (False, billing_gateway.UNAVAILABLE)
    assert _row(owed).status == "open"


def test_a_void_meeting_a_paid_invoice_records_it_paid(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    paid = _issue(payer, entity, account, card=GOOD_CARD)["id"]
    stripe.calls.clear()

    assert billing_gateway.void_invoice(paid) == "paid"
    assert _row(paid).status == "paid"
    assert stripe.calls == []


def test_a_void_refused_because_it_was_paid_meanwhile_is_paid(monkeypatch):
    """Read open, paid before the void landed (a Pay now, or a lost reply's charge)."""
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    owed = _renewal(stripe, payer, entity, account)
    stale = stripe.Invoice.retrieve(owed)
    reads = iter([stale])
    fresh = stripe.Invoice.retrieve

    def _retrieve(invoice_id, expand=None, **kw):
        return next(reads, None) or fresh(invoice_id, expand)

    monkeypatch.setattr(stripe.Invoice, "retrieve", _retrieve)
    stripe.invoices[owed].update(status="paid", status_transitions={"paid_at": 1800000200})

    assert billing_gateway.void_invoice(owed) == "paid"
    assert _row(owed).status == "paid"


def test_a_void_withdraws_an_open_invoice_and_deletes_a_draft(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    owed = _renewal(stripe, payer, entity, account)
    held = _issue(payer, entity, account, card=GOOD_CARD, key=f"{KEY}-draft", collect=False)["id"]

    assert billing_gateway.void_invoice(owed) == "voided"
    assert billing_gateway.void_invoice(held) == "deleted"
    assert (_row(owed).status, _row(held).status) == ("void", "void")
    assert held not in stripe.invoices


# --- asked, not guessed -------------------------------------------------------------------------------


def test_recheck_records_and_returns_what_the_processor_says(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    owed = _renewal(stripe, payer, entity, account)
    stripe.invoices[owed].update(status="paid", status_transitions={"paid_at": 1800000200})

    found = billing_gateway.recheck(_row(owed))

    assert found["status"] == "paid"
    assert isinstance(found["payment_intent"], dict)        # expanded, for ``declined``
    assert _row(owed).status == "paid"


def test_a_recheck_that_cannot_read_raises_retryable(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    owed = _renewal(stripe, payer, entity, account)

    def _down(*a, **k):
        raise stripe_lib.APIConnectionError("no route")

    monkeypatch.setattr(stripe.Invoice, "retrieve", _down)
    with pytest.raises(billing_gateway.BillingError) as raised:
        billing_gateway.recheck(_row(owed))
    assert raised.value.retryable


def test_declined_reads_the_processors_record_of_the_attempt(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    refused = _renewal(stripe, payer, entity, account)
    stripe.fail["pay"] = "before"           # finalized, then nothing ever tried
    with pytest.raises(billing_gateway.BillingError) as raised:
        _issue(payer, entity, account, card=GOOD_CARD, key=f"{KEY}-untried")
    untried = raised.value.invoice_id

    assert billing_gateway.declined(billing_gateway.recheck(_row(refused))) is True
    assert billing_gateway.declined(billing_gateway.recheck(_row(untried))) is False


# --- the silent grace ------------------------------------------------------------------------------


def _card_with_company(paid_through):
    from billing.services import store
    from billing.tests.engine.conftest import make_entity, make_user, seed_currency

    payer = make_user(f"grace-{uuid.uuid4().hex[:6]}@payer.test")
    entity = make_entity(payer, name="Grace Co", currency=seed_currency("HKD"))
    group = store.create_billing_account(payer.id, "pm_grace")
    store.nominate_group_for_entity(entity.id, payer.id, group.id)
    store.upsert_module_row(entity.id, "PETTY_CASH", payer.id, phase="active")
    if paid_through is not None:
        store.set_group_paid_through(group.id, paid_through)
    return store.billing_group(group.id), entity


def _phase(entity):
    from billing.services import store

    return store.module_row(entity.id, "PETTY_CASH").phase


def test_a_silent_grace_is_past_due_without_a_dunning_stamp():
    from billing.services import store

    now = datetime(2027, 3, 7, tzinfo=UTC)
    group, entity = _card_with_company(now - timedelta(days=1))

    assert store.hold_group_grace(group.id, now) == 1
    assert _phase(entity) == "past_due"
    assert store.billing_group(group.id).dunning_started_at is None


def test_a_card_still_paid_up_needs_no_grace():
    from billing.services import store

    now = datetime(2027, 3, 7, tzinfo=UTC)
    group, entity = _card_with_company(now + timedelta(days=3))

    assert store.hold_group_grace(group.id, now) == 0
    assert _phase(entity) == "active"


def test_the_grace_is_released_only_once_the_card_is_paid_up():
    from billing.services import store

    now = datetime(2027, 3, 7, tzinfo=UTC)
    group, entity = _card_with_company(now - timedelta(days=1))
    store.hold_group_grace(group.id, now)

    assert store.release_group_grace(group.id, now) == 0         # still behind
    store.set_group_paid_through(group.id, now + timedelta(days=30))
    assert store.release_group_grace(group.id, now) == 1
    assert _phase(entity) == "active"


def test_a_running_dunning_is_not_released_as_a_silent_grace():
    from billing.services import store

    now = datetime(2027, 3, 7, tzinfo=UTC)
    group, entity = _card_with_company(now - timedelta(days=1))
    store.begin_group_dunning(group.id, now)
    store.set_group_paid_through(group.id, now + timedelta(days=30))

    assert store.release_group_grace(group.id, now) == 0
    assert _phase(entity) == "past_due"      # dunning ends its own episode


def test_an_attempt_the_processor_failed_to_make_is_given_back():
    from billing.services import store

    now = datetime(2027, 3, 7, tzinfo=UTC)
    group, _entity = _card_with_company(now - timedelta(days=1))
    store.begin_group_dunning(group.id, now)
    store.record_group_dunning_attempt(group.id)

    assert store.refund_group_dunning_attempt(group.id) == 0
    assert store.refund_group_dunning_attempt(group.id) == 0     # never below zero


def test_attempts_are_listed_by_key_prefix_with_wildcards_escaped():
    """``_`` is in every PAYMENT_REQUEST key; as a LIKE wildcard it would match any character."""
    from billing.services import store
    from billing.services.billing import Line, Period
    from billing.tests.engine.conftest import make_entity, make_user, seed_currency

    payer = make_user(f"prefix-{uuid.uuid4().hex[:6]}@payer.test")
    entity = make_entity(payer, name="Prefix Co", currency=seed_currency("HKD"))
    line = Line(str(entity.id), "Prefix Co", "Petty Cash", 28000)

    def _reserve(key):
        return store.reserve_invoice(
            payer_user_id=payer.id, stripe_customer_id=CUSTOMER, period=Period(START, END),
            currency="hkd", lines=[line], memo=None, idempotency_key=key,
        )

    first = _reserve("convert-e-PAYMENT_REQUEST-1")
    second = _reserve("convert-e-PAYMENT_REQUEST-2")
    _reserve("convert-e-PAYMENTXREQUEST-1")

    found = store.invoices_with_key_prefix(payer.id, "convert-e-PAYMENT_REQUEST")
    # As a set: rows made in one test transaction share ``created_at``.
    assert {row.id for row in found} == {first.id, second.id}


# --- collection over, and what went out ----------------------------------------------------------------


def _group(paid_through=None, started=None):
    return SimpleNamespace(paid_through=paid_through, dunning_started_at=started)


def test_collection_is_over_past_the_access_window_stamp_or_not():
    from billing.services import dunning

    now = datetime(2027, 3, 20, tzinfo=UTC)
    assert not dunning.collection_over(_group(now - timedelta(days=14)), now, 15)
    assert dunning.collection_over(_group(now - timedelta(days=15)), now, 15)
    # A card that never collected has no access window to run out.
    assert not dunning.collection_over(_group(None), now, 15)


def test_collection_is_over_past_a_running_episodes_deadline():
    from billing.services import dunning

    now = datetime(2027, 3, 20, tzinfo=UTC)
    assert dunning.collection_over(_group(None, started=now - timedelta(days=15)), now, 15)
    assert not dunning.collection_over(_group(None, started=now - timedelta(days=2)), now, 15)


def test_already_sent_means_delivered():
    from billing.services import notify
    from billing.tests.engine.conftest import make_user
    from shared_models.models import SubscriptionEmailLog

    payer = make_user(f"sent-{uuid.uuid4().hex[:6]}@payer.test")
    for key, status in (("k-sent", notify.STATUS_SENT), ("k-failed", notify.STATUS_FAILED)):
        SubscriptionEmailLog.objects.create(
            id=str(uuid.uuid4()), user_id=payer.id, event=notify.RENEWAL_FAILED,
            dedupe_key=key, status=status,
        )

    assert notify.already_sent(notify.RENEWAL_FAILED, "k-sent")
    assert not notify.already_sent(notify.RENEWAL_FAILED, "k-failed")     # retried, not sent
    assert not notify.already_sent(notify.RENEWAL_FAILED, "k-never")



def test_the_subscriptions_list_does_not_call_a_silent_grace_past_due():
    from billing.services import portal

    now = datetime(2027, 3, 10, tzinfo=UTC)
    row = SimpleNamespace(phase="past_due", trial_end=None, app_access_until=None,
                          first_billed_at=now - timedelta(days=60), extension_state=None,
                          extension_amount=None)
    behind = now - timedelta(days=2)

    held = portal._module_state(row, now=now, paid_through=behind, grace_days=15, told=False)
    told = portal._module_state(row, now=now, paid_through=behind, grace_days=15, told=True)

    assert held["status"] == portal.STATUS_ACTIVE
    assert told["status"] == portal.STATUS_PAST_DUE


def test_told_of_failure_reads_the_companys_card(monkeypatch):
    from billing.services import dunning, policy, store

    now = datetime(2027, 3, 10, tzinfo=UTC)
    monkeypatch.setattr(policy, "current", lambda: policy.DEFAULTS)
    cards = {"silent": _group(now - timedelta(days=2)),
             "told": _group(now - timedelta(days=2), started=now - timedelta(days=1)),
             "none": None}
    monkeypatch.setattr(store, "billing_group_for_entity", lambda eid, uid=None: cards[eid])

    assert dunning.told_of_failure("silent", now) is False
    assert dunning.told_of_failure("told", now) is True
    assert dunning.told_of_failure("none", now) is True            # unknown keeps the banner
