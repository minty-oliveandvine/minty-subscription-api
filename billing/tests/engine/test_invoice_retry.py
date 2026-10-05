"""08-B's *Retry payment* on a failed invoice's row: which row it is offered on
(``portal.retryable_invoice_ids`` -> each invoice's ``retryable``) and what pressing it asks
the engine (``billing_accounts.retry_invoice`` -> ``dunning.retry_now``).

What is pinned:

* **The button sits only on the invoice a retry would charge**, by the engine's own rule
  (``dunning._manual_target``): the current period's renewal first, else an open mid-period
  charge - never an abandoned give-up bill, and nothing on a card past its give-up deadline.
  A failed invoice without the button is still shown failed.
* **Pressing it names the card and the invoice.** The card is the invoice's account (the
  payer's oldest for one raised before accounts); the invoice is Stripe's id of that row, so
  the engine refuses rather than paying a different bill.
* **Only a failed invoice of the caller's.** Someone else's is None (the route's 404); a paid
  one is refused before anything reaches the engine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from billing.tests.engine.conftest import seed_currency
from billing.tests.engine.test_billing_accounts import _user

pytestmark = pytest.mark.django_db

ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
PAID_THROUGH = datetime(2027, 2, 8, 13, tzinfo=UTC)  # the current period starts here
NOW = PAID_THROUGH + timedelta(days=2)
FAILED = "open"


def _payer(monkeypatch, email="retry@payer.test"):
    """A payer with a cycle and one account (card ``pm_a``) paid to ``PAID_THROUGH``, and the
    clock two days past it - inside the give-up window."""
    from billing.services import clock, store
    from shared_models.models import UserStripeCustomer

    monkeypatch.setattr(clock, "now", lambda: NOW)
    seed_currency("HKD")
    payer = _user(None, email)
    UserStripeCustomer(
        id=str(uuid.uuid4()),
        user_id=str(payer.id),
        stripe_customer_id=f"cus_{uuid.uuid4().hex[:8]}",
        anchor_at=ANCHOR,
        currency="HKD",
    ).save(force_insert=True)
    account = store.create_billing_account(payer.id, "pm_a", billing_company="Acme Ltd")
    store.set_group_paid_through(account.id, PAID_THROUGH)
    return payer, account


def _key(payer, account, start=PAID_THROUGH) -> str:
    from billing.services import renewals
    from billing.services.billing import Period

    return renewals.period_key(payer.id, Period(start, start + timedelta(days=31)), account.id)


def _invoice(payer, account, *, external, key, status=FAILED, age_days=0):
    """A local invoice row as the gateway leaves it: ``idempotency_key`` is the renewal's
    period key (``renewals.period_key``), or a change's own key."""
    from shared_models.models import SubscriptionInvoice

    invoice = SubscriptionInvoice(
        id=str(uuid.uuid4()),
        payer_user_id=str(payer.id),
        billing_group_id=str(account.id) if account is not None else None,
        external_id=external,
        idempotency_key=key,
        period_start=PAID_THROUGH,
        period_end=PAID_THROUGH + timedelta(days=28),
        currency="HKD",
        total=28000,
        status=status,
        created_at=NOW - timedelta(days=age_days),
    )
    invoice.save(force_insert=True)
    return invoice


def _retryable(payer) -> dict[str, bool]:
    from billing.services import portal

    rows = portal.build_payer_invoices(payer.id, per_page=100)["invoices"]
    return {row["reference"]: row["retryable"] for row in rows}


# --- where the button is offered -----------------------------------------------------


def test_the_current_periods_renewal_is_the_one_offered(app, monkeypatch):
    """Paying it gives the service back. An open change beside it, and an abandoned renewal
    for a period long gone, are shown failed without the button."""
    payer, account = _payer(monkeypatch)
    _invoice(payer, account, external="in_old", key=_key(payer, account, ANCHOR - timedelta(days=62)),
             age_days=60)
    _invoice(payer, account, external="in_change", key="change-e1-20270205-PETTY_CASH", age_days=5)
    _invoice(payer, account, external="in_current", key=_key(payer, account), age_days=2)

    assert _retryable(payer) == {"in_old": False, "in_change": False, "in_current": True}


def test_with_no_current_renewal_an_open_charge_is_offered_never_a_give_up_bill(
    app, monkeypatch
):
    """A mid-period charge is real debt the customer may settle; the abandoned renewal is a
    bill this business chose not to pursue (2026-08-11) - no button, only the red row."""
    payer, account = _payer(monkeypatch)
    _invoice(payer, account, external="in_old", key=_key(payer, account, ANCHOR - timedelta(days=62)),
             age_days=60)
    _invoice(payer, account, external="in_change", key="change-e1-20270205-PETTY_CASH", age_days=5)

    assert _retryable(payer) == {"in_old": False, "in_change": True}

    payer2, account2 = _payer(monkeypatch, "retry2@payer.test")
    _invoice(payer2, account2, external="in_abandoned",
             key=_key(payer2, account2, ANCHOR - timedelta(days=62)), age_days=60)
    assert _retryable(payer2) == {"in_abandoned": False}


def test_nothing_is_offered_past_the_give_up_deadline_or_on_a_paid_invoice(app, monkeypatch):
    from billing.services import store

    payer, account = _payer(monkeypatch)
    _invoice(payer, account, external="in_paid", key=_key(payer, account, ANCHOR), status="paid",
             age_days=30)
    _invoice(payer, account, external="in_current", key=_key(payer, account), age_days=2)
    assert _retryable(payer) == {"in_paid": False, "in_current": True}

    # Collection started long enough ago that the deadline has passed.
    store.begin_group_dunning(account.id, NOW - timedelta(days=60))
    assert _retryable(payer) == {"in_paid": False, "in_current": False}


def test_a_replay_scoped_renewal_is_this_periods_and_offered(app, monkeypatch):
    """The dev database's data was lived by the replay harness, which scopes every key it
    issues (``<key>-<suffix>``). The same period - so the same button."""
    payer, account = _payer(monkeypatch)
    _invoice(payer, account, external="in_scoped", key=f"{_key(payer, account)}-ENwIm8kHRqpJ",
             age_days=2)

    assert _retryable(payer) == {"in_scoped": True}


def test_the_period_key_resolves_to_a_replays_scoped_claim(app, monkeypatch):
    """Dunning's "this period's invoice" (``_current_period_key``) names the row the renewal
    run reads (``renewals.claimed_period_key``): the replay's scoped one. A voided duplicate
    whose key was retired beside it (``store.retired_key``) is not the period's claim."""
    from billing.services import dunning, renewals, store

    payer, account = _payer(monkeypatch)
    mapping = store.customer_mapping_for_user(payer.id)
    scoped = f"{_key(payer, account)}-{renewals.replay_scope(mapping.stripe_customer_id)}"
    _invoice(payer, account, external="in_scoped", key=scoped, age_days=2)
    _invoice(payer, account, external="in_dup", status="void",
             key=store.retired_key(_key(payer, account), "in_dup"))

    group = store.billing_group(account.id)
    assert dunning._current_period_key(mapping, group) == scoped
    assert store.invoice_for_key(scoped).external_id == "in_scoped"


def test_a_card_whose_access_ran_out_offers_nothing_even_without_a_dunning_stamp(
    app, monkeypatch
):
    """Giving up CLOSES the episode (no stamp) and leaves ``paid_through`` where it stopped -
    so the abandoned renewal is, by key, "the current period". Access ran out weeks ago: the
    business does not chase that bill (2026-08-11), so the row is red with no button."""
    from billing.services import store

    payer, account = _payer(monkeypatch)
    store.set_group_paid_through(account.id, ANCHOR)  # paid to 8 Jan; access ended on the 23rd
    _invoice(payer, account, external="in_abandoned", key=_key(payer, account, ANCHOR),
             age_days=33)

    assert _retryable(payer) == {"in_abandoned": False}


def test_an_invoice_from_before_accounts_belongs_to_the_oldest(app, monkeypatch):
    payer, account = _payer(monkeypatch)
    _invoice(payer, None, external="in_legacy", key="change-e1-20270201-PETTY_CASH", age_days=3)

    assert _retryable(payer) == {"in_legacy": True}


# --- what pressing it asks -----------------------------------------------------------


def _engine(monkeypatch, answer=None):
    """``dunning.retry_now`` stubbed: records what it was asked, answers ``answer``."""
    from billing.services import dunning

    asked: list[dict] = []

    def _retry_now(user_id, entity_id=None, *, group_id=None, expect_invoice=None):
        asked.append({"user": str(user_id), "entity": entity_id, "group": group_id,
                      "invoice": expect_invoice})
        return answer or {"status": "paid", "attempts": 1, "invoice": expect_invoice,
                          "reason": None}

    monkeypatch.setattr(dunning, "retry_now", _retry_now)
    return asked


def test_pressing_it_names_the_invoices_card_and_the_invoice(app, monkeypatch):
    from billing.services import billing_accounts

    payer, account = _payer(monkeypatch)
    invoice = _invoice(payer, account, external="in_current", key=_key(payer, account))
    asked = _engine(monkeypatch)

    result = billing_accounts.retry_invoice(payer.id, invoice.id)

    assert result["status"] == "paid"
    assert asked == [{"user": str(payer.id), "entity": None, "group": str(account.id),
                      "invoice": "in_current"}]


def test_a_legacy_invoice_is_charged_to_the_oldest_account(app, monkeypatch):
    from billing.services import billing_accounts

    payer, account = _payer(monkeypatch)
    invoice = _invoice(payer, None, external="in_legacy", key="change-e1-20270201-PETTY_CASH")
    asked = _engine(monkeypatch)

    billing_accounts.retry_invoice(payer.id, invoice.id)

    assert asked[0]["group"] == str(account.id)


def test_a_bill_the_page_would_not_offer_is_refused_before_the_engine(app, monkeypatch):
    """A page read before something moved, or a direct call, cannot reach an abandoned bill."""
    from billing.services import billing_accounts, store

    payer, account = _payer(monkeypatch)
    store.set_group_paid_through(account.id, ANCHOR)
    abandoned = _invoice(payer, account, external="in_abandoned", key=_key(payer, account, ANCHOR))
    asked = _engine(monkeypatch)

    result = billing_accounts.retry_invoice(payer.id, abandoned.id)

    assert result["status"] == "not_this_invoice"
    assert asked == [], "nothing reached the engine"


def test_only_a_failed_invoice_of_the_callers(app, monkeypatch):
    from billing.services import billing_accounts
    from billing.services.payment_methods import PaymentMethodError

    payer, account = _payer(monkeypatch)
    stranger, _theirs = _payer(monkeypatch, "stranger@payer.test")
    failed = _invoice(payer, account, external="in_current", key=_key(payer, account))
    paid = _invoice(payer, account, external="in_paid", key=_key(payer, account, ANCHOR),
                    status="paid")
    asked = _engine(monkeypatch)

    assert billing_accounts.retry_invoice(stranger.id, failed.id) is None
    assert billing_accounts.retry_invoice(payer.id, "not-an-id") is None
    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.retry_invoice(payer.id, paid.id)
    assert caught.value.status == 409
    assert asked == [], "nothing reached the engine"


# --- after a re-issue (``billing_gateway.refresh_invoice``) ----------------------------------
#
# Stripe cancels an invoice's payment once it has been confirmed too many times; the engine
# re-issues it and moves the period key onto the replacement (``store.supersede_invoice``). The
# button follows the key, because the key is how a row says which period it bills.


def _refreshed(payer, account, *, key):
    """The rows a finished refresh leaves: the original void under a retired key, the
    replacement open and holding the period key."""
    from billing.services import store

    dead = _invoice(payer, account, external="in_dead", key=key, age_days=12)
    replacement = _invoice(payer, account, external="in_new", key=store.refresh_key("in_dead"))
    store.supersede_invoice(dead.id, replacement.id)
    return dead, replacement


def test_after_a_refresh_the_button_moves_to_the_replacement(app, monkeypatch):
    payer, account = _payer(monkeypatch)
    _refreshed(payer, account, key=_key(payer, account))

    assert _retryable(payer) == {"in_dead": False, "in_new": True}


def test_while_a_refresh_is_unfinished_the_button_stays_on_the_original(app, monkeypatch):
    """Replacement raised, key not yet moved: the rules still pick the original - it holds the
    key and is the older - so that is where the button is, and pressing it finishes the job."""
    from billing.services import store

    payer, account = _payer(monkeypatch)
    _invoice(payer, account, external="in_dead", key=_key(payer, account), age_days=12)
    _invoice(payer, account, external="in_new", key=store.refresh_key("in_dead"))

    assert _retryable(payer) == {"in_dead": True, "in_new": False}


def test_a_stale_page_retrying_a_replaced_invoice_is_told_to_look_again(app, monkeypatch):
    """Drawn before the re-issue, pressed after: the debt is still there, on another row - so
    "refresh the page", not "that invoice isn't waiting for a payment"."""
    from billing.services import billing_accounts

    payer, account = _payer(monkeypatch)
    dead, _replacement = _refreshed(payer, account, key=_key(payer, account))
    asked = _engine(monkeypatch)

    result = billing_accounts.retry_invoice(payer.id, dead.id)

    assert result["status"] == "not_this_invoice"
    assert asked == [], "nothing reached the engine"
