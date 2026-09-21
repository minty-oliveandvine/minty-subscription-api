"""Unit tests for the Stripe Customer Portal seams.

ONE rule holds this file together: **cancelling is in-app only, never on Stripe's side.**

Stripe's own cancel button skips everything the in-app flow guarantees — it queues no
access extension, stamps no ``app_access_until``, writes no audit row — and the webhook it
fires then revokes access on the spot, taking the 30 days the customer already paid for.
The restricted portal configuration turns it off; a session WITHOUT a configuration
silently uses the account default, where it's on. So every path here must pass the
restricted config, and must fail closed rather than fall back.

NOTE: imports are done INSIDE each test — the conftest ``app`` fixture clears and
re-imports project modules mid-session, so importing at call time keeps references
mutually consistent.
"""
from __future__ import annotations

import pytest


class _FakeEntity:
    id = "e1"
    name = "Acme"


def _wire(monkeypatch, *, configuration="bpc_restricted"):
    from billing.services import checkout

    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: "cus_1")
    monkeypatch.setattr(
        checkout, "get_or_create_billing_management_configuration",
        lambda: configuration,
    )

    sessions = []
    monkeypatch.setattr(
        checkout, "create_billing_portal_session",
        lambda customer_id, return_url, configuration=None, flow_data=None: (
            sessions.append({"configuration": configuration, "flow": flow_data})
            or {"url": "https://portal.example/s"}
        ),
    )
    return checkout, sessions


def test_management_portal_uses_the_restricted_configuration(monkeypatch):
    checkout, sessions = _wire(monkeypatch)

    checkout.open_billing_management_portal(_FakeEntity(), "https://app.example/back")

    assert sessions[0]["configuration"] == "bpc_restricted"


def test_management_portal_refuses_rather_than_opening_the_default_portal(monkeypatch):
    """This used to fall back to the default portal when the config couldn't be resolved
    — i.e. a transient Stripe blip handed the customer a Cancel button. Being briefly
    unable to show invoices is recoverable; a customer cancelling through Stripe is not."""
    checkout, sessions = _wire(monkeypatch, configuration=None)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.open_billing_management_portal(_FakeEntity(), "https://app.example/back")

    assert exc.value.status == 503
    assert sessions == []  # no session opened at all


def test_payment_method_update_is_also_bounded_by_the_configuration(monkeypatch):
    """The flow deep-link only decides where the session OPENS. Without a configuration
    the customer can still reach the default portal's home — and its Cancel button."""
    checkout, sessions = _wire(monkeypatch)

    checkout.open_payment_method_update(_FakeEntity(), "https://app.example/back")

    assert sessions[0]["configuration"] == "bpc_restricted"
    assert sessions[0]["flow"]["type"] == "payment_method_update"


def test_payment_method_update_refuses_without_a_configuration(monkeypatch):
    checkout, sessions = _wire(monkeypatch, configuration=None)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.open_payment_method_update(_FakeEntity(), "https://app.example/back")

    assert exc.value.status == 503
    assert sessions == []


def test_the_client_itself_refuses_a_session_with_no_configuration():
    """Belt and braces at the Stripe boundary: the parameter is required and empty is
    rejected, so a future caller can't reopen this hole by simply omitting it."""
    from billing.services import stripe_client

    with pytest.raises(ValueError, match="requires a configuration"):
        stripe_client.create_billing_portal_session("cus_1", "https://back", "")


def test_no_entity_customer_still_raises_before_any_portal_call(monkeypatch):
    checkout, sessions = _wire(monkeypatch)
    monkeypatch.setattr(checkout, "_customer_id_for_entity", lambda eid: None)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.open_billing_management_portal(_FakeEntity(), "https://app.example/back")

    assert exc.value.status == 409
    assert sessions == []
