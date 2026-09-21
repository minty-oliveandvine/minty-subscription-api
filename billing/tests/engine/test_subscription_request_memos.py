"""Two reads that were repeated in the render path, now memoized for the request.

Both follow the pattern already used by ``policy.current``, ``money.decimal_places`` and
``clock.now``: cached on the request scope (what ``g`` was), so the lifetime is one request and
each context has its own.

  * ``customer_default_payment_method`` — a NETWORK round trip to Stripe, made on every
    module-settings render, on the payer portal, and once per entity per login on the
    dashboard, synchronously in front of the response.
  * ``catalog.available_plans`` — two queries per call, and ``plan_for_module`` rebuilt
    the whole list to answer for a single code, inside per-row loops.

The card memo needs invalidation and the catalog one does not, and that asymmetry is the
point: we write the customer's default payment method ourselves, mid-request, and then ask
whether one is on file. Nothing writes the price catalog during a request.
"""
from __future__ import annotations

import pytest

# --- the Stripe card lookup ------------------------------------------------------


def _wire_customer(monkeypatch, pm="pm_1"):
    """Count the Stripe round trips behind ``customer_default_payment_method``."""
    stripe_client = pytest.importorskip("billing.services.stripe_client")  # slice B

    calls: list[str] = []

    def _retrieve(customer_id):
        calls.append(customer_id)
        return {"invoice_settings": {"default_payment_method": pm}} if pm else {}

    monkeypatch.setattr(stripe_client, "retrieve_customer", _retrieve)
    return stripe_client, calls


def test_the_card_is_read_from_stripe_once_per_request(app, monkeypatch):
    """The whole point. ``get_module_cards`` is not the only caller in a request."""
    stripe_client, calls = _wire_customer(monkeypatch)

    with app.test_request_context():
        assert stripe_client.customer_default_payment_method("cus_1") == "pm_1"
        assert stripe_client.customer_default_payment_method("cus_1") == "pm_1"
        assert stripe_client.customer_default_payment_method("cus_1") == "pm_1"

    assert calls == ["cus_1"], "one round trip, not three"


def test_no_card_is_cached_too(app, monkeypatch):
    """``None`` is an answer, and on a trial it is the common one — the case most worth
    not asking twice. Caching only truthy results would leave the hot path uncached."""
    stripe_client, calls = _wire_customer(monkeypatch, pm=None)

    with app.test_request_context():
        assert stripe_client.customer_default_payment_method("cus_1") is None
        assert stripe_client.customer_default_payment_method("cus_1") is None

    assert calls == ["cus_1"]


def test_each_customer_is_cached_separately(app, monkeypatch):
    """The nightly dunning pass walks many payers in one context; they must not share
    one answer."""
    stripe_client = pytest.importorskip("billing.services.stripe_client")  # slice B

    monkeypatch.setattr(
        stripe_client,
        "retrieve_customer",
        lambda cid: {"invoice_settings": {"default_payment_method": f"pm_for_{cid}"}},
    )

    with app.test_request_context():
        assert stripe_client.customer_default_payment_method("cus_1") == "pm_for_cus_1"
        assert stripe_client.customer_default_payment_method("cus_2") == "pm_for_cus_2"


def test_the_memo_does_not_survive_the_request(app, monkeypatch):
    """A card added between two page loads must be seen on the second one."""
    stripe_client, calls = _wire_customer(monkeypatch)

    with app.test_request_context():
        stripe_client.customer_default_payment_method("cus_1")
    with app.test_request_context():
        stripe_client.customer_default_payment_method("cus_1")

    assert calls == ["cus_1", "cus_1"]


def test_saving_a_card_invalidates_the_memo(app, monkeypatch):
    """The correctness case, not a freshness nicety.

    Paid checkout captures a card and THEN asks whether one is on file to decide if it
    may subscribe directly. Answering from a memo taken before the capture would send the
    payer back to save a card they had just saved.
    """
    stripe_client = pytest.importorskip("billing.services.stripe_client")  # slice B

    saved: dict = {}
    calls: list[str] = []

    def _retrieve(customer_id):
        calls.append(customer_id)
        return {"invoice_settings": {"default_payment_method": saved.get(customer_id)}}

    class _Customer:
        @staticmethod
        def modify(customer_id, **kwargs):
            saved[customer_id] = kwargs["invoice_settings"]["default_payment_method"]
            return {"id": customer_id}

    monkeypatch.setattr(stripe_client, "retrieve_customer", _retrieve)
    monkeypatch.setattr(
        stripe_client, "get_stripe", lambda: type("S", (), {"Customer": _Customer})
    )

    with app.test_request_context():
        assert stripe_client.customer_default_payment_method("cus_1") is None
        stripe_client.set_customer_default_payment_method("cus_1", "pm_new")
        assert stripe_client.customer_default_payment_method("cus_1") == "pm_new"

    assert calls == ["cus_1", "cus_1"], "the second read must go back to Stripe"


def test_the_lookup_still_works_with_no_app_context(app, monkeypatch):
    """CLI and worker paths have no ``g``. They lose the memo, not the answer."""
    stripe_client, calls = _wire_customer(monkeypatch)

    assert stripe_client.customer_default_payment_method("cus_1") == "pm_1"
    assert stripe_client.customer_default_payment_method("cus_1") == "pm_1"
    assert calls == ["cus_1", "cus_1"]


# --- the price catalog -----------------------------------------------------------


def _wire_catalog(monkeypatch):
    """Count how many times the list is actually BUILT (two queries each, uncached)."""
    from billing.services import catalog

    builds: list[int] = []

    def _ids():
        builds.append(1)
        return {}

    monkeypatch.setattr(catalog, "_function_ids", _ids)
    monkeypatch.setattr(catalog, "_single_plans", lambda: [])
    return catalog, builds


def test_the_catalog_is_built_once_per_request(app, monkeypatch):
    catalog, builds = _wire_catalog(monkeypatch)

    with app.test_request_context():
        catalog.available_plans()
        catalog.available_plans()

    assert len(builds) == 1


def test_plan_for_module_no_longer_rebuilds_the_catalog(app, monkeypatch):
    """The loop that motivated this: ``plan_for_module`` is called per row in the
    trial-conversion job, and each call rebuilt the whole list to scan it once."""
    catalog, builds = _wire_catalog(monkeypatch)

    with app.test_request_context():
        for code in ("PETTY_CASH", "PAYMENT_REQUEST", "PETTY_CASH", "PAYMENT_REQUEST"):
            catalog.plan_for_module(code)

    assert len(builds) == 1


def test_a_new_request_rebuilds_the_catalog(app, monkeypatch):
    """A price change between two page loads must be picked up on the second."""
    catalog, builds = _wire_catalog(monkeypatch)

    with app.test_request_context():
        catalog.available_plans()
    with app.test_request_context():
        catalog.available_plans()

    assert len(builds) == 2


def test_the_catalog_still_builds_with_no_app_context(app, monkeypatch):
    catalog, builds = _wire_catalog(monkeypatch)

    assert catalog.available_plans() == []
    assert catalog.available_plans() == []
    assert len(builds) == 2, "no g to cache on — correct, just uncached"
