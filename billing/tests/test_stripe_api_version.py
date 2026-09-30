"""The Stripe API version is PINNED, and the build fails before an SDK upgrade can move it.

Left alone, ``stripe.api_version`` is whatever the installed stripe-python defaults to. The
version that bites is ``2025-03-31.basil`` (stripe-python 12's default): it REMOVED Invoice
``charge`` and ``payment_intent``, which ``billing_gateway`` reads to record the card that paid
and to spot a payment Stripe has cancelled for good. Both readers have harmless-looking
answers for a missing field - "no card to record", "the payment is not dead" - so a
``requirements.txt`` bump would have broken them in production without a sound.

So: every request is made at ``STRIPE_API_VERSION``; these tests fail the build the moment
the installed SDK stops matching it; and at run time a real invoice missing one of those
fields is logged at ERROR rather than read as an ordinary answer.
"""

from __future__ import annotations

import stripe

from billing.services import billing_gateway, stripe_client

MIGRATE = (
    "Move billing_gateway's readers from Invoice.charge / Invoice.payment_intent to "
    "invoice.payments (expand payments.data.payment.payment_intent) - see "
    "docs.stripe.com/changelog/basil/2025-03-31 - then change STRIPE_API_VERSION."
)


# --- the build fails first ------------------------------------------------------------------


def test_every_request_is_made_at_the_pinned_version(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = "sk_test_pinned"
    monkeypatch.setattr(stripe, "api_key", None)
    monkeypatch.setattr(stripe, "api_version", "2099-01-01.elsewhere")

    assert stripe_client.get_stripe().api_version == stripe_client.STRIPE_API_VERSION


def test_the_installed_sdk_speaks_the_pinned_version():
    from stripe._api_version import _ApiVersion

    assert _ApiVersion.CURRENT == stripe_client.STRIPE_API_VERSION, (
        f"stripe-python {stripe.VERSION} defaults to {_ApiVersion.CURRENT}, but the billing "
        f"code reads invoices as {stripe_client.STRIPE_API_VERSION} sends them. {MIGRATE}"
    )


def test_the_sdk_still_knows_the_invoice_fields_the_gateway_reads():
    missing = {"charge", "payment_intent"} - set(stripe.Invoice.__annotations__)

    assert not missing, (
        f"stripe-python {stripe.VERSION}'s Invoice has no {sorted(missing)}. {MIGRATE}"
    )


# --- and at run time, a drift is loud ---------------------------------------------------------


def _invoice(**fields):
    """A REAL Stripe object, as a response is built - not a hand-made dict."""
    return stripe.Invoice.construct_from({"object": "invoice", **fields}, "sk_test_x")


def _mismatches(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelname == "ERROR" and "no longer matches STRIPE_API_VERSION" in r.getMessage()]


def test_an_open_invoice_with_no_payment_intent_field_is_loud(caplog):
    basil = _invoice(id="in_basil", status="open")          # the field is gone, not null

    assert billing_gateway._payment_is_dead(basil) is False
    assert len(_mismatches(caplog)) == 1
    assert "in_basil" in _mismatches(caplog)[0]


def test_a_paid_invoice_with_no_charge_field_is_loud(monkeypatch, caplog):
    class _Stripe:
        class Invoice:
            @staticmethod
            def retrieve(invoice_id, expand=None):
                return _invoice(id=invoice_id, status="paid")

    class _Record:
        id = "row_1"
        payment_method = None

    monkeypatch.setattr(billing_gateway, "get_stripe", lambda: _Stripe)

    billing_gateway._capture_payment_method(_Record(), "in_basil")

    assert len(_mismatches(caplog)) == 1


def test_the_pinned_shape_and_hand_made_fakes_are_not_mistaken_for_a_drift(caplog):
    """A null field is an ordinary answer at the pinned version, and a plain dict is a test
    stand-in whose missing keys say nothing about the API."""
    assert billing_gateway._payment_is_dead(_invoice(id="in_a", status="open",
                                                     payment_intent=None)) is False
    assert billing_gateway._payment_is_dead({"id": "in_b", "status": "open"}) is False

    assert _mismatches(caplog) == []
