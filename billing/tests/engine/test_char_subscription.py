"""Characterisation: one company's subscription life through the SERVICES and the scheduled
jobs, on the real database. The ``test_subscription_*`` files are unit-level with the store
and the biller faked; this walks the rows.

    trial started from the module card -> access on, a `trial` row
    trial ends with no card             -> `expired`, access off, the lapsed-trial restart state
    trial ends with a card + consent    -> converted: an in-house invoice with one line for the
                                            company, exact cents, the payer's cycle anchored,
                                            an audit row with outcome `succeeded`
    the payer cancels                   -> `scheduled_cancel` with access to the period end, and
                                            the audit row names module, phases and outcome
    the payer portal                    -> answers for a payer with nothing, and lists the
                                            converted company as active with a paid_through

Minty's copy (``tests/test_char_subscription.py``) drove the first step and the portal through
Flask's routes; here the same steps go through what those routes called
(``checkout.start_module_trials`` + ``entity_modules.set_entity_module``, and
``portal.build_payer_subscriptions``), which is what step 3's routers will call too. The gate
check on the report pages is Flask's and stays in Minty.

Stripe is faked at ``stripe_client.get_stripe`` (the SDK boundary); the plan comes from a real
``billing_plan`` row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from .conftest import make_entity, make_user, seed_country, seed_currency, seed_module, seed_plan

pytestmark = pytest.mark.django_db

MODULE = "PETTY_CASH"
PRICE_CENTS = 28000


@pytest.fixture
def shop():
    """An admin and a company with Petty Cash switched OFF and a sellable plan for it."""
    currency = seed_currency("HKD")
    country = seed_country(currency)
    owner = make_user("owner@test.com")
    entity = make_entity(owner, currency=currency, country=country, modules=())
    seed_module(MODULE, "Petty Cash")
    seed_plan(MODULE, PRICE_CENTS, display_name="Petty Cash")
    return owner, entity


# ---- Stripe, faked at the SDK boundary ------------------------------------------------------


class FakeStripe:
    """Just enough of the SDK for the in-house invoice: create -> items -> finalize -> pay."""

    def __init__(self, *, pays=True):
        self.pays = pays
        self.invoices: dict[str, dict] = {}
        self.items: list[dict] = []
        outer = self

        class Invoice:
            @staticmethod
            def create(**kw):
                inv = {"id": f"in_{len(outer.invoices) + 1}", "status": "draft", "lines": {"data": []},
                       "customer": kw.get("customer"), "metadata": kw.get("metadata") or {},
                       "amount_due": 0, "total": 0, "currency": "hkd", "hosted_invoice_url": None,
                       "payment_intent": None, "collection_method": kw.get("collection_method")}
                outer.invoices[inv["id"]] = inv
                return inv

            @staticmethod
            def retrieve(invoice_id, **kw):
                return outer.invoices[invoice_id]

            @staticmethod
            def finalize_invoice(invoice_id, **kw):
                inv = outer.invoices[invoice_id]
                total = sum(i["amount"] for i in outer.items if i["invoice"] == invoice_id)
                inv.update(status="open", total=total, amount_due=total, number=f"INV-{invoice_id}")
                return inv

            @staticmethod
            def pay(invoice_id, **kw):
                inv = outer.invoices[invoice_id]
                if outer.pays:
                    inv.update(status="paid", amount_paid=inv["total"], amount_due=0,
                               status_transitions={"paid_at": int(datetime.now(UTC).timestamp())})
                else:
                    inv.update(status="open", last_finalization_error={"message": "card_declined"})
                return inv

            @staticmethod
            def void_invoice(invoice_id, **kw):
                outer.invoices[invoice_id]["status"] = "void"
                return outer.invoices[invoice_id]

            @staticmethod
            def list(**kw):
                rows = [i for i in outer.invoices.values() if not kw.get("status") or i["status"] == kw["status"]]
                return SimpleNamespace(data=rows, auto_paging_iter=lambda: iter(rows))

        class InvoiceItem:
            @staticmethod
            def create(**kw):
                item = {"id": f"ii_{len(outer.items) + 1}", "invoice": kw.get("invoice"),
                        "amount": kw.get("amount", 0), "description": kw.get("description")}
                outer.items.append(item)
                return item

        class Customer:
            @staticmethod
            def retrieve(customer_id, **kw):
                return {"id": customer_id, "invoice_settings": {"default_payment_method": "pm_card"}}

            @staticmethod
            def search(**kw):
                return {"data": []}

        class PaymentMethod:
            @staticmethod
            def retrieve(pm_id, **kw):
                return {"id": pm_id, "type": "card", "card": {"brand": "visa", "last4": "4242",
                        "exp_month": 12, "exp_year": 2099}}

        self.Invoice, self.InvoiceItem, self.Customer, self.PaymentMethod = Invoice, InvoiceItem, Customer, PaymentMethod


def install_fake_stripe(monkeypatch, *, pays=True) -> FakeStripe:
    """At the SDK boundary, plus the modules that bound ``get_stripe`` at import time."""
    import sys

    from billing.services import stripe_client

    fake = FakeStripe(pays=pays)
    monkeypatch.setattr(stripe_client, "get_stripe", lambda: fake)
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("billing.services") and hasattr(module, "get_stripe"):
            monkeypatch.setattr(module, "get_stripe", lambda: fake)
    return fake


def set_clock(monkeypatch, moment):
    from billing.services import clock

    monkeypatch.setattr(clock, "now", lambda: moment)


# ---- readers (rows, through the store) ---------------------------------------------------------


def module_row(entity_id):
    from billing.services import store

    rows = store.module_rows_for_entity(entity_id)
    return next((r for r in rows if r.function_code == MODULE), None)


def module_on(entity_id) -> bool:
    from billing.services import entity_modules

    return bool(entity_modules._enabled_state(entity_id).get(MODULE))


def db_entity(entity_id):
    from shared_models.models import Entity

    return Entity.objects.get(pk=entity_id)


def db_user(user_id):
    from shared_models.models import User

    return User.objects.get(pk=user_id)


def start_trial(owner, entity):
    """What ``POST /entity/settings/module/<id>/start-trial`` did: start the trials, then
    switch each started module on as the subscription actor. Returns ``(payload, status)``
    in the route's shape."""
    from billing.services import entity_modules
    from billing.services.checkout import CheckoutError, start_module_trials

    try:
        started = start_module_trials(db_entity(entity.id), db_user(owner.id), [MODULE])
    except CheckoutError as exc:
        return {"error": exc.message}, exc.status
    data, status = {"modules": {}}, 200
    for code in started:
        data, status = entity_modules.set_entity_module(
            entity.id, code, True, actor="subscription", user_id=str(owner.id)
        )
        if status != 200:
            return data, status
    return data, status


# ---- the walk ---------------------------------------------------------------------------------


def test_the_module_card_starts_a_trial_that_switches_the_module_on(shop):
    owner, entity = shop
    assert module_on(entity.id) is False

    payload, status = start_trial(owner, entity)

    assert status == 200, payload
    assert payload["modules"][MODULE] is True
    assert module_on(entity.id) is True
    row = module_row(entity.id)
    assert row is not None and str(row.phase) == "trial"
    # NO SUBSCRIBER. Starting a trial is free and commits nobody, so the company gets a payer
    # only when someone confirms billing on a billing account (the user, 2026-10-08).
    assert row.payer_user_id is None
    assert row.trial_end is not None and row.trial_end > datetime.now(UTC)
    # once per module: a second trial is refused, the card offers paid checkout instead
    again, status = start_trial(owner, entity)
    assert status in (400, 409), again


def test_a_trial_that_ends_without_a_card_expires_and_lapses(shop, monkeypatch):
    owner, entity = shop
    start_trial(owner, entity)
    # No mapping row, so the close-out ASKS Stripe whether a customer exists before it
    # expires the trial (``checkout._resolve_customer_id``); the fake answers "none".
    # (Minty's copy did not stub this: its test process had the .env test key and made a
    # real Customer.search - see docs/features/subscriptions-api.md.)
    install_fake_stripe(monkeypatch)
    ended = module_row(entity.id).trial_end + timedelta(days=1)
    set_clock(monkeypatch, ended)
    from billing.services import checkout, consent

    outcome = checkout.convert_or_expire_due_trials()
    assert outcome["expired"] == [{"entity_id": entity.id, "code": MODULE}]
    assert outcome["converted"] == []
    assert module_on(entity.id) is False
    assert str(module_row(entity.id).phase) == "expired"

    # the dashboard knows it as a lapsed trial the payer can restart
    lapse = consent.lapsed_trial_for_entity(entity.id, owner.id)
    assert lapse["mode"] == "takeover", lapse  # every module the company had has lapsed
    assert [row["code"] for row in lapse["lapsed"]] == [MODULE]
    assert lapse["has_card"] is False


def _payer_with_a_card(owner, entity):
    """The payer's Stripe customer, a billing account on a card with this company on it,
    the company's billing consent, AND the payer established on its module rows - what a
    conversion needs.

    The last one is the act "Activate Subscription" makes (``checkout.activate_entity_billing``
    → ``store.establish_entity_payer``): a trial establishes no subscriber, so without it the
    conversion has nobody to charge and the trial expires instead. That is the behaviour the
    `..._expires_...` tests above pin; this is its other half.
    """
    from billing.services import store

    store.upsert_customer_mapping(owner.id, "cus_test")
    store.nominate_card_for_entity(entity.id, owner.id, "pm_card", source="chosen")
    store.record_billing_consent(entity.id, owner.id, source="module_card")
    store.establish_entity_payer(entity.id, owner.id)


def test_a_trial_that_ends_with_a_card_converts_to_an_exact_in_house_invoice(shop, monkeypatch):
    owner, entity = shop
    start_trial(owner, entity)
    _payer_with_a_card(owner, entity)
    stripe = install_fake_stripe(monkeypatch, pays=True)
    ended = module_row(entity.id).trial_end + timedelta(minutes=1)
    set_clock(monkeypatch, ended)
    from billing.services import checkout, store
    from shared_models.models import (
        SubscriptionAuditLog,
        SubscriptionInvoice,
        SubscriptionInvoiceLine,
    )

    outcome = checkout.convert_or_expire_due_trials()
    assert outcome["converted"] == [{"entity_id": entity.id, "code": MODULE}], outcome
    assert outcome["expired"] == []

    row = module_row(entity.id)
    assert str(row.phase) == "active"
    assert module_on(entity.id) is True

    invoices = list(SubscriptionInvoice.objects.filter(payer_user_id=owner.id))
    assert len(invoices) == 1
    invoice = invoices[0]
    lines = list(SubscriptionInvoiceLine.objects.filter(invoice_id=invoice.id))
    assert [str(line.entity_id) for line in lines] == [entity.id]
    # cents are integers: exact, and the invoice total is the line
    assert Decimal(invoice.total) == Decimal(sum(line.amount for line in lines))
    assert 0 < invoice.total <= PRICE_CENTS
    assert invoice.status in ("paid", "open")
    # Stripe was asked for exactly that
    assert len(stripe.invoices) == 1 and stripe.items and stripe.items[0]["amount"] == lines[0].amount

    anchor, currency = store.billing_cycle_for_user(owner.id)
    assert anchor is not None and currency.upper() == "HKD"

    # a cancellation is what the audit log records (conversions are the invoice's story):
    # the module is scheduled to cancel, keeps access to the period end, and the row
    # names the module, both phases and the outcome
    cancelled = checkout.cancel_module(db_entity(entity.id), db_user(owner.id), MODULE, reason="moving on")
    assert cancelled, cancelled
    row = module_row(entity.id)
    assert str(row.phase) == "scheduled_cancel"
    assert row.app_access_until is not None and row.app_access_until > ended
    audit = list(SubscriptionAuditLog.objects.filter(entity_id=entity.id))
    assert len(audit) == 1
    (entry,) = audit
    assert str(entry.function_code) == MODULE
    assert str(entry.payer_user_id) == owner.id and str(entry.actor_user_id) == owner.id
    assert entry.action == "cancel" and str(entry.outcome) == "succeeded"
    assert (str(entry.phase_before), str(entry.phase_after)) == ("active", "scheduled_cancel")
    assert entry.cancel_reason == "moving on"


def test_the_payer_portal_answers_for_a_payer_with_nothing_yet(shop):
    """F5 (fixed in C7): a payer with no companies billed answered 500 - the summary read a
    per-company figure from a loop that never ran."""
    owner, _entity = shop
    from billing.services import portal

    body = portal.build_payer_subscriptions(owner.id)
    assert body["entities"] == [] and body["total"] == 0
    assert body["billing"]["paid_through"] is None


def test_the_payer_portal_lists_the_converted_company(shop, monkeypatch):
    owner, entity = shop
    start_trial(owner, entity)
    _payer_with_a_card(owner, entity)
    install_fake_stripe(monkeypatch, pays=True)
    set_clock(monkeypatch, module_row(entity.id).trial_end + timedelta(minutes=1))
    from billing.services import checkout, portal

    assert checkout.convert_or_expire_due_trials()["converted"]

    body = portal.build_payer_subscriptions(owner.id)
    assert [e["entity_id"] for e in body["entities"]] == [entity.id]
    module = next(m for m in body["entities"][0]["modules"] if m["code"] == MODULE)
    assert module["status"] == "active"
    assert body["billing"]["paid_through"] is not None
