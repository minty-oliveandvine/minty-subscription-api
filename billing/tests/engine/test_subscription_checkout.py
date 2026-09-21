"""Unit tests for paid checkout — ONE billing cycle per payer.

Activation sends the user to a hosted setup-mode Stripe Checkout to capture a card;
the modules are then billed server-side on return (``complete_setup_checkout``). Stripe
is the payment RAIL only: the charge is an invoice Minty raises itself, there is no
Stripe subscription behind it.

The pricing model has no coupon: an entity is priced by the modules it bills for — one
module at its own price, both at the bundle price. So adding a second module bills the
MARGINAL difference rather than its standalone price.

NOTE: imports are done INSIDE each test (see the note in the other subscription
tests) so lazy re-imports stay mutually consistent.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

# The price catalog is patched BY DOTTED PATH, not via an imported reference:
# conftest re-imports project modules mid-session, so a module object captured at
# import time is not the one the code under test ends up calling.
_CATALOG = "billing.services.catalog"

# The payer's anchor and a "now" inside that period — ``period_containing``
# extrapolates from the anchor, so both have to be pinned for the money to be stable.
_ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
_NOW = datetime(2027, 1, 20, 13, tzinfo=UTC)
# The end of the period _NOW sits in. The double-buy guard asks whether the module is
# still live, not just what its phase says, so these tests need a payer whose cycle is
# actually running — otherwise "already subscribed" would be false and the guards under
# test would never fire.
_PAID_THROUGH = datetime(2027, 2, 8, 13, tzinfo=UTC)


class _Policy:
    """Shipped defaults, so the guard's grace lookup needs no database."""

    trial_days = 30
    paid_cancel_access_days = 30
    past_due_window_days = 10
    retry_offsets_days = (1, 3, 5, 7)


class _FakeEntity:
    id = "e1"
    name = "Acme"


class _FakeUser:
    id = "u1"
    email = "u1@example.com"


def _plan(code, fn_id, currency="HKD"):
    from billing.services import catalog

    return catalog.PlanView(
        entity_function_id=fn_id,
        function_code=code,
        display_name=code.title().replace("_", " "),
        amount=28000,
        currency_code=currency,
        billing_interval="month",
        billing_interval_count=1,
        is_available_for_subscription=True,
    )


def _bundle():
    from billing.services import catalog

    return catalog.BundlePlanView(
        function_codes=("PETTY_CASH", "PAYMENT_REQUEST"),
        display_name="Super Minty",
        amount=40000,
        currency_code="HKD",
        billing_interval="month",
        billing_interval_count=1,
    )


_PLANS = {"PAYMENT_REQUEST": ("fn_bill",), "PETTY_CASH": ("fn_pc",)}


class _Plan:
    def __init__(self, amount):
        self.amount = amount
        self.display_name = "plan"
        self.currency = "HKD"


class _Group:
    """The card a company is billed on, and the cycle that card owns."""

    def __init__(self, id="g1", card="pm_1", paid_through=None):
        self.id = id
        self.payer_user_id = "u1"
        self.stripe_payment_method_id = card
        self.paid_through = paid_through


class _ModuleRow:
    """Stand-in for an entity_module_subscription row (the fields the guard reads).

    A trial row carries ``trial_end`` — ``start_module_trial`` writes it with the row and
    nothing clears it — so the default fills one in. Leaving it None described a trial
    with no term, which the access rules read as already over, and the double-buy guard
    then let a module mid-trial be bought.
    """

    def __init__(self, phase="active", *, trial_end=None):
        self.phase = phase
        self.function_code = "PAYMENT_REQUEST"
        self.payer_user_id = "u1"
        self.app_access_until = None
        self.first_billed_at = None
        self.trial_end = trial_end or (_PAID_THROUGH if phase == "trial" else None)


def _wire(monkeypatch, *, default_pm=None, module_rows=None, billed_codes=(),
          paid=True, now=_NOW, anchor=_ANCHOR, paid_through=_PAID_THROUGH):
    """Wire the catalog, store and in-house biller; return (checkout, calls)."""
    from billing.services import changes, checkout, policy, store
    from billing.services import clock as clock_mod

    monkeypatch.setattr(clock_mod, "now", lambda: now)
    # The double-buy guard reads both: a module only counts as held if its phase says so
    # AND access is still granted, because nothing rewrites the phase when a period
    # simply lapses. Both are stubbed so the guard needs no database.
    monkeypatch.setattr(policy, "current", lambda: _Policy())
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)

    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: "cus_1")
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_1")
    monkeypatch.setattr(
        f"{_CATALOG}.plan_for_module",
        lambda code: _plan(code.upper(), _PLANS[code.upper()][0])
        if code.upper() in _PLANS else None,
    )
    monkeypatch.setattr(f"{_CATALOG}.bundle_plan", lambda: _bundle())

    # The already-subscribed guard reads the module row, not live Stripe — Stripe
    # reported a failing card as ``past_due`` rather than ``active``, so a past-due
    # module could be bought a second time. Default to "this entity owns nothing yet".
    rows = module_rows or {}
    monkeypatch.setattr(
        store, "module_row", lambda eid, code: rows.get((str(eid), code.upper()))
    )
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: [])
    # What the entity ALREADY bills, read from Minty's own rows.
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: set(billed_codes))

    # Default to an entity the payer HAS authorised, so these tests keep testing
    # pricing mechanics. The consent gate is covered separately below.
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: True)
    monkeypatch.setattr(store, "record_billing_consent", lambda eid, uid, source: None)
    monkeypatch.setattr(
        store, "nominate_card_for_entity", lambda eid, uid, pm, source="chosen": None
    )

    calls = {"charged": [], "default_pm": [], "setup": [], "stamped": [],
             "mapped": [], "attached": [], "rows": [], "granted": [],
             "paid_through": []}

    monkeypatch.setattr(checkout, "customer_default_payment_method", lambda cid: default_pm)
    monkeypatch.setattr(
        store, "billing_cycle_for_user", lambda uid: (anchor, "HKD")
    )
    monkeypatch.setattr(store, "start_billing_cycle", lambda uid, at, cur: None)
    # The card this company is nominated onto. ``paid_through`` mirrors the account's, so
    # the cases below describe one card paying for everything — which is what an account
    # looks like until somebody nominates a second.
    group = _Group(paid_through=paid_through)
    monkeypatch.setattr(
        store, "billing_group_for_entity", lambda eid, uid=None: group
    )
    monkeypatch.setattr(
        store, "set_group_paid_through",
        lambda gid, until: calls["paid_through"].append(("u1", until)),
    )
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        lambda codes: _Plan(40000 if len(set(codes)) > 1 else 28000),
    )
    monkeypatch.setattr(
        changes, "issue_change",
        lambda cid, eid, name, before, after, period, at, **kw: calls["charged"].append(
            {"customer": cid, "before": set(before), "after": set(after),
             "start": period.start, "end": period.end,
             "card": getattr(kw.get("group"), "stripe_payment_method_id", None)}
        ) or {"id": "in_1", "status": "paid" if paid else "open"},
    )
    monkeypatch.setattr(
        store, "upsert_module_row",
        lambda e, code, payer, **f: calls["rows"].append((e, code, payer, f)),
    )
    monkeypatch.setattr(
        checkout, "_set_module_access",
        lambda eid, code, enabled: calls["granted"].append((eid, code, enabled)),
    )

    monkeypatch.setattr(
        checkout, "set_customer_default_payment_method",
        lambda cid, pm: calls["default_pm"].append((cid, pm)),
    )
    # Adopting the customer a setup session produced. The payer already resolves to
    # cus_1 here, so these are the no-duplicate path.
    monkeypatch.setattr(
        checkout, "set_customer_identity",
        lambda cid, **kw: calls["stamped"].append((cid, kw)),
    )
    monkeypatch.setattr(checkout, "_payer_identity", lambda uid: {})
    monkeypatch.setattr(
        checkout, "_seed_user_customer_mapping",
        lambda uid, cid: calls["mapped"].append((uid, cid)),
    )
    monkeypatch.setattr(
        checkout, "attach_payment_method",
        lambda pm, cid: calls["attached"].append((pm, cid)),
    )

    def fake_setup(customer_id, success_url, cancel_url, currency, metadata=None):
        calls["setup"].append({"customer_id": customer_id, "currency": currency,
                               "metadata": metadata})
        return {"url": "https://setup.example/session"}

    monkeypatch.setattr(checkout, "create_setup_checkout_session", fake_setup)
    return checkout, calls


# --- capturing a card ---------------------------------------------------------


def test_checkout_without_card_redirects_to_setup(monkeypatch):
    checkout, calls = _wire(monkeypatch, default_pm=None)

    result = checkout.start_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST", "PETTY_CASH"]
    )

    # No saved card → hosted setup-checkout redirect; nothing billed until return.
    assert result["url"] == "https://setup.example/session"
    assert calls["charged"] == []
    # Modules' currency + codes ride on the session metadata for the completion step.
    assert calls["setup"][0]["currency"] == "HKD"
    assert calls["setup"][0]["metadata"]["modules_to_subscribe"] == "PAYMENT_REQUEST,PETTY_CASH"


def test_module_checkout_creates_no_customer_for_a_new_payer(monkeypatch):
    """THE INVARIANT: a Stripe customer exists only once a card has been saved.

    A first-time payer clicking Subscribe must reach the setup checkout without any
    Customer being created — abandoning there has to leave nothing behind. Stripe makes
    the customer at session confirmation instead (customer_creation="always"), which is
    why customer_id goes out as None.
    """
    from billing.services import store, stripe_client

    checkout, calls = _wire(monkeypatch, default_pm=None)

    # Brand-new payer: nothing resolves to a customer anywhere.
    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: None)
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)

    # No direct Stripe traffic on this path — the session builder is faked, so anything
    # else reaching the SDK is an unintended write (a re-added customer create above all).
    def _no_stripe():
        raise AssertionError("starting checkout must not call Stripe directly")

    monkeypatch.setattr(stripe_client, "get_stripe", _no_stripe)

    result = checkout.start_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"]
    )

    assert result["url"] == "https://setup.example/session"
    assert calls["setup"][0]["customer_id"] is None
    assert calls["setup"][0]["metadata"]["user_id"] == "u1"


# --- consent before a shared card is used -------------------------------------


def test_a_saved_card_alone_does_not_authorise_a_second_entity(monkeypatch):
    """The payer's card lives on their ONE customer and is shared by every entity they
    pay for. Clicking Subscribe on an entity they've never agreed to pay for must ask
    first, not just bill the card sitting on entity #1."""
    from billing.services import store

    checkout, calls = _wire(monkeypatch, default_pm="pm_saved")
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: False)
    monkeypatch.setattr(
        checkout, "payment_method_display",
        lambda pm: {"brand": "visa", "last4": "4242"},
    )

    result = checkout.start_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"]
    )

    # NOTHING billed.
    assert calls["charged"] == []
    info = result["needs_confirmation"]
    assert info["entity_name"] == "Acme"
    assert info["amount"] == 28000
    assert info["currency"] == "HKD"
    assert info["card"] == {"brand": "visa", "last4": "4242"}


def test_confirming_records_consent_then_bills(monkeypatch):
    """The other half of the handshake: once the payer accepts, consent is recorded for
    THIS entity and the charge goes through."""
    from billing.services import store

    checkout, calls = _wire(monkeypatch, default_pm="pm_saved")
    consents: list = []
    consented = {"value": False}

    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: consented["value"])
    monkeypatch.setattr(
        store, "record_billing_consent",
        lambda eid, uid, source: (consents.append((eid, uid, source)),
                                  consented.update(value=True)),
    )

    created = checkout.confirm_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"]
    )

    assert consents == [("e1", "u1", "confirmed")]
    assert created["created"] == ["PAYMENT_REQUEST"]
    assert len(calls["charged"]) == 1  # billed, once


def test_confirmation_amount_is_the_bundle_price_not_the_sum(monkeypatch):
    """Two modules bill as ONE bundle price — the bundle IS the discount. Summing the
    per-module plans would quote the payer more than they'd actually be charged."""
    from billing.services import store

    checkout, calls = _wire(monkeypatch, default_pm="pm_saved")
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: False)
    monkeypatch.setattr(checkout, "payment_method_display", lambda pm: None)

    result = checkout.start_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST", "PETTY_CASH"]
    )

    # 40000 (bundle), NOT 28000 + 28000.
    assert result["needs_confirmation"]["amount"] == 40000
    assert sorted(result["needs_confirmation"]["codes"]) == ["PAYMENT_REQUEST", "PETTY_CASH"]


# --- buying, in-house ---------------------------------------------------------


def test_buying_is_collected_by_an_in_house_invoice(monkeypatch):
    """The purchase is collected by Minty's own invoice against the payer's period —
    no Stripe subscription, no subscription item.

    Buying must GRANT the module, not just collect for it. Under Stripe the row was
    written afterwards by the subscription webhook; there is no webhook now, so this
    path has to do it or the customer pays and gets nothing."""
    checkout, calls = _wire(monkeypatch, default_pm="pm_1")

    created = checkout.start_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"]
    )

    assert created == {"created": ["PAYMENT_REQUEST"]}
    assert len(calls["charged"]) == 1
    assert calls["charged"][0]["after"] == {"PAYMENT_REQUEST"}
    assert calls["charged"][0]["start"] == _ANCHOR

    # The row is active, stamped as billed, and the access gate is open.
    assert len(calls["rows"]) == 1
    _eid, code, _payer, fields = calls["rows"][0]
    assert code == "PAYMENT_REQUEST"
    assert fields["phase"] == "active"
    assert fields["first_billed_at"] is not None
    assert calls["granted"] == [("e1", "PAYMENT_REQUEST", True)]


def test_adding_a_second_module_is_priced_as_an_upgrade(monkeypatch):
    """The entity already bills BILL. Adding PETTY_CASH must be priced from what it
    already bills to the bundle — the MARGINAL 120 — not at PETTY_CASH's own 280."""
    checkout, calls = _wire(monkeypatch, default_pm="pm_saved", billed_codes={"PAYMENT_REQUEST"})

    result = checkout.start_modules_checkout(
        _FakeEntity(), _FakeUser(), "s", "c", ["PETTY_CASH"]
    )

    assert result == {"created": ["PETTY_CASH"]}
    assert len(calls["charged"]) == 1
    assert calls["charged"][0]["before"] == {"PAYMENT_REQUEST"}
    assert calls["charged"][0]["after"] == {"PAYMENT_REQUEST", "PETTY_CASH"}


def test_an_uncollected_in_house_purchase_tells_the_user(monkeypatch):
    """Interactive path: the customer is waiting, so a failure must surface a message
    rather than leave them silently unsubscribed."""
    checkout, calls = _wire(monkeypatch, default_pm="pm_1", paid=False)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.start_modules_checkout(_FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"])

    assert exc.value.status == 402
    assert "payment method" in str(exc.value.message)
    assert calls["granted"] == []  # nothing handed over


# --- the double-buy guards ----------------------------------------------------


def test_checkout_rejects_already_active_module(monkeypatch):
    """Double-buy guard: the entity already bills BILL, so buying it again is refused."""
    checkout, calls = _wire(
        monkeypatch, module_rows={("e1", "PAYMENT_REQUEST"): _ModuleRow("active")}
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.start_modules_checkout(_FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"])
    assert exc.value.status == 409
    assert calls["setup"] == []


def test_checkout_refuses_a_past_due_module_that_stripe_called_inactive(monkeypatch):
    """The gap that moving this guard off Stripe closed. Stripe reported a failing card
    as ``past_due``, not ``active``, so the old check let the module be bought AGAIN —
    a second charge for something the customer already has and has not paid for. The
    module row still says ``active``, because the phase tracks what is owned rather than
    whether the last payment cleared."""
    checkout, calls = _wire(
        monkeypatch, module_rows={("e1", "PAYMENT_REQUEST"): _ModuleRow("active")}
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.start_modules_checkout(_FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"])
    assert exc.value.status == 409


def test_checkout_refuses_a_module_that_is_already_on_a_free_trial(monkeypatch):
    """Selling a module mid-trial would take money for something the customer is
    currently getting free, and leave two claims on one module. The trial converts on
    its own at term end.

    The old Stripe-backed guard could not see this at all — an app-level trial creates
    no subscription, so a trialing module looked unsold and was purchasable."""
    checkout, calls = _wire(
        monkeypatch, module_rows={("e1", "PAYMENT_REQUEST"): _ModuleRow("trial")}
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.start_modules_checkout(_FakeEntity(), _FakeUser(), "s", "c", ["PAYMENT_REQUEST"])

    assert exc.value.status == 409
    # The message must not claim a paid subscription exists, and should make clear
    # nothing needs doing.
    assert "free trial" in str(exc.value.message)
    assert "automatically" in str(exc.value.message)
    assert calls["setup"] == []


# --- returning from the hosted card capture -----------------------------------


def test_complete_setup_sets_the_default_card_then_bills(monkeypatch):
    checkout, calls = _wire(monkeypatch)
    session = {
        "customer": "cus_1",
        "setup_intent": {"payment_method": "pm_new"},
        "metadata": {"modules_to_subscribe": "PAYMENT_REQUEST,PETTY_CASH",
                     "entity_id": "e1", "user_id": "u1"},
    }
    monkeypatch.setattr(checkout, "retrieve_checkout_session", lambda sid: session)

    created = checkout.complete_setup_checkout(_FakeEntity(), _FakeUser(), "cs_1")

    assert created == ["PAYMENT_REQUEST", "PETTY_CASH"]
    assert calls["default_pm"] == [("cus_1", "pm_new")]  # saved card set as default
    # Both modules on ONE charge, at the bundle price rather than two standalone ones.
    assert len(calls["charged"]) == 1
    assert calls["charged"][0]["after"] == {"PAYMENT_REQUEST", "PETTY_CASH"}


def test_complete_setup_surfaces_a_failure_to_collect(monkeypatch):
    """The card was captured but the charge didn't clear — the user is waiting on the
    return page, so this must say so rather than report success."""
    checkout, calls = _wire(monkeypatch, paid=False)
    session = {
        "customer": "cus_1",
        "setup_intent": {"payment_method": "pm_new"},
        "metadata": {"modules_to_subscribe": "PAYMENT_REQUEST",
                     "entity_id": "e1", "user_id": "u1"},
    }
    monkeypatch.setattr(checkout, "retrieve_checkout_session", lambda sid: session)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.complete_setup_checkout(_FakeEntity(), _FakeUser(), "cs_1")

    assert exc.value.status == 402
    assert calls["granted"] == []


def test_complete_setup_rejects_foreign_session(monkeypatch):
    checkout, calls = _wire(monkeypatch)
    monkeypatch.setattr(
        checkout, "retrieve_checkout_session",
        lambda sid: {"customer": "cus_OTHER", "setup_intent": {"payment_method": "pm_x"},
                     "metadata": {}},
    )

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.complete_setup_checkout(_FakeEntity(), _FakeUser(), "cs_1")

    assert exc.value.status == 404
    assert calls["charged"] == []
