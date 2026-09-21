"""Unit tests for payer(user)-keyed Stripe customer RESOLUTION.

The Stripe customer is owned by the paying user. ``store.customer_id_for_user`` resolves
it from the local ``user_stripe_customer`` mapping; entity-keyed resolution goes
entity -> payer -> customer via ``checkout._customer_id_for_entity``.

Resolution only — nothing here creates. The app no longer creates Stripe customers at
all: a customer must not exist until a card has been saved, so a setup Checkout is
opened without one and Stripe creates it at confirmation. The creation-side tests live
in test_onboarding_payment_method.py / test_subscription_checkout.py, which pin that
invariant from the other direction.

Resolution reads the local mapping first and falls back to a live Stripe search by
``metadata.user_id`` when that row is missing — the mapping is a cache whose writes are
best-effort, so the fallback is what makes losing one survivable. The billing paths use
it; the render path deliberately does not (see the bottom of this file).

NOTE: imports are done INSIDE each test — the conftest ``app`` fixture clears and
re-imports project modules mid-session, so importing at call time keeps references
mutually consistent (matches the other subscription tests).
"""
from __future__ import annotations


def test_customer_id_for_user_reads_the_local_mapping(monkeypatch):
    from billing.services import store

    class _Mapping:
        stripe_customer_id = "cus_local"

    monkeypatch.setattr(store, "customer_mapping_for_user", lambda uid: _Mapping())

    assert store.customer_id_for_user("u1") == "cus_local"


def test_customer_id_for_user_none_when_unmapped(monkeypatch):
    from billing.services import store

    monkeypatch.setattr(store, "customer_mapping_for_user", lambda uid: None)

    assert store.customer_id_for_user("u1") is None


def test_customer_id_for_user_none_without_a_user():
    """A blank payer short-circuits before the query — no mapping can exist for one."""
    from billing.services import store

    assert store.customer_id_for_user(None) is None
    assert store.customer_id_for_user("") is None


def test_seed_mapping_failure_is_swallowed(monkeypatch):
    """A mapping-write failure must not propagate — the customer already exists in
    Stripe, which is the source of truth, so billing can't break because a local seed
    failed."""
    from billing.services import checkout, store

    def _boom(uid, cid):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "upsert_customer_mapping", _boom)

    checkout._seed_user_customer_mapping("u1", "cus_1")  # must not raise


def test_seed_mapping_ignores_a_blank_side(monkeypatch):
    """Neither half of the link is optional: a missing user or customer writes nothing
    rather than a half-formed row."""
    from billing.services import checkout, store

    def _boom(uid, cid):
        raise AssertionError("must not write a mapping with a blank side")

    monkeypatch.setattr(store, "upsert_customer_mapping", _boom)

    checkout._seed_user_customer_mapping(None, "cus_1")
    checkout._seed_user_customer_mapping("u1", None)


def test_customer_id_for_entity_resolves_via_payer(monkeypatch):
    """Entity -> payer (from the module rows) -> customer, both hops local."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "payer_for_entity", lambda eid: "u1")
    monkeypatch.setattr(
        store, "customer_id_for_user", lambda uid: "cus_1" if uid == "u1" else None
    )

    assert checkout._customer_id_for_entity("e1") == "cus_1"


def test_customer_id_for_entity_none_without_a_payer(monkeypatch):
    """An entity with no module row (no subscription/trial yet) has no customer."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "payer_for_entity", lambda eid: None)

    def _boom(uid):
        raise AssertionError("must not resolve a customer without a payer")

    monkeypatch.setattr(store, "customer_id_for_user", _boom)

    assert checkout._customer_id_for_entity("e1") is None
    assert checkout._customer_id_for_entity(None) is None


def test_customer_id_for_entity_none_when_the_payer_has_no_customer(monkeypatch):
    """A payer can exist on the row before they have ever saved a card.

    Stripe has to be asked too: "no mapping row" alone doesn't mean "no customer" (see
    the fallback tests below), so it is the SEARCH coming back empty that settles it."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "payer_for_entity", lambda eid: "u1")
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)
    monkeypatch.setattr(checkout, "find_customer_by_user", lambda uid: None)

    assert checkout._customer_id_for_entity("e1") is None


# --- resolution when the mapping row is missing -------------------------------
#
# The mapping row is a CACHE. ``_seed_user_customer_mapping`` swallows its own write
# failures, so a payer can hold a real, card-bearing Stripe customer and have no row
# here — and every caller draws a different wrong conclusion from "no customer":
# checkout mints a DUPLICATE, and the trial-end job revokes a module the card would
# have paid for. ``metadata.user_id`` is stamped precisely so this is recoverable.


def test_resolve_prefers_the_local_mapping_and_never_calls_stripe(monkeypatch):
    """The common path must not cost a Stripe Customer Search."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_local")

    def _boom(uid):
        raise AssertionError("must not search Stripe when the mapping resolves it")

    monkeypatch.setattr(checkout, "find_customer_by_user", _boom)

    assert checkout._resolve_customer_id("u1") == "cus_local"


def test_resolve_recovers_a_stamped_customer_when_the_mapping_is_missing(monkeypatch):
    """The recovery the ``metadata.user_id`` stamp exists for."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)
    monkeypatch.setattr(
        checkout, "find_customer_by_user", lambda uid: {"id": "cus_stripe"}
    )
    monkeypatch.setattr(store, "upsert_customer_mapping", lambda uid, cid: None)

    assert checkout._resolve_customer_id("u1") == "cus_stripe"


def test_resolve_repairs_the_mapping_on_a_search_hit(monkeypatch):
    """Re-seed on recovery, so a broken payer costs ONE search rather than one per
    request — an unbounded search per page view would be its own outage."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)
    monkeypatch.setattr(
        checkout, "find_customer_by_user", lambda uid: {"id": "cus_stripe"}
    )
    seeded: list = []
    monkeypatch.setattr(
        store, "upsert_customer_mapping",
        lambda uid, cid: seeded.append((uid, cid)),
    )

    checkout._resolve_customer_id("u1")

    assert seeded == [("u1", "cus_stripe")]


def test_resolve_is_none_when_stripe_has_no_customer_either(monkeypatch):
    """A genuinely new payer. Must stay None — the setup checkout depends on it to send
    ``customer_creation="always"`` rather than reusing something."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)
    monkeypatch.setattr(checkout, "find_customer_by_user", lambda uid: None)

    assert checkout._resolve_customer_id("u1") is None


def test_the_reseed_is_what_lets_a_recovered_payer_be_anchored(monkeypatch):
    """The re-seed is load-bearing, not a cache warm-up.

    The anchor and ``paid_through`` live on the SAME ``user_stripe_customer`` row, so
    ``start_billing_cycle`` silently no-ops when it is missing — and
    ``_bill_module_change_in_house`` then gives up with "could not anchor" and expires
    the trial. Recovering the customer id without writing the row would fix the lookup
    and leave billing broken one step later.
    """
    from billing.services import checkout, store

    row_exists = {"value": False}

    monkeypatch.setattr(
        store, "customer_id_for_user",
        lambda uid: "cus_stripe" if row_exists["value"] else None,
    )
    monkeypatch.setattr(
        checkout, "find_customer_by_user", lambda uid: {"id": "cus_stripe"}
    )
    monkeypatch.setattr(
        store, "upsert_customer_mapping",
        lambda uid, cid: row_exists.update(value=True),
    )

    assert checkout._resolve_customer_id("u1") == "cus_stripe"
    # The row now exists, so the anchor has somewhere to be written.
    assert row_exists["value"] is True


def test_resolve_propagates_a_stripe_failure_rather_than_answering_none(monkeypatch):
    """Must NOT degrade to "no customer" when the search itself fails.

    By the time the search runs the mapping row is already missing, so the choice is
    between raising and answering wrongly — and "no customer" is the answer that expires
    a paid trial. Callers handle the raise better: the trial job catches per entity and
    retries, the payment-method endpoint degrades to "no card" on its own.
    """
    import pytest

    from billing.services import checkout, store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)

    def _down(uid):
        raise RuntimeError("stripe unreachable")

    monkeypatch.setattr(checkout, "find_customer_by_user", _down)

    with pytest.raises(RuntimeError):
        checkout._resolve_customer_id("u1")


def test_resolve_short_circuits_without_a_user(monkeypatch):
    """No payer, no search: ``metadata.user_id`` is the only key it has."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)

    def _boom(uid):
        raise AssertionError("must not search Stripe without a payer")

    monkeypatch.setattr(checkout, "find_customer_by_user", _boom)

    assert checkout._resolve_customer_id(None) is None
    assert checkout._resolve_customer_id("") is None


def test_find_customer_by_user_searches_on_the_stamp():
    """The Stripe side of the recovery: keyed on ``metadata.user_id``, not email."""

    def _stripe_with(data):
        class _Stripe:
            class Customer:
                @staticmethod
                def search(query, limit):
                    assert "metadata['user_id']:'u1'" in query
                    return {"data": data}

        return _Stripe()

    import pytest

    from billing.services import stripe_client

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(stripe_client, "get_stripe", lambda: _stripe_with([{"id": "cus_1"}]))
        assert stripe_client.find_customer_by_user("u1") == {"id": "cus_1"}
        mp.setattr(stripe_client, "get_stripe", lambda: _stripe_with([]))
        assert stripe_client.find_customer_by_user("u1") is None
        assert stripe_client.find_customer_by_user(None) is None
    finally:
        mp.undo()


def test_the_entity_resolver_goes_through_the_fallback(monkeypatch):
    """entity -> payer -> customer must inherit the recovery, not stop at the mapping:
    the portal and the buy path both resolve this way."""
    from billing.services import checkout, store

    monkeypatch.setattr(store, "payer_for_entity", lambda eid: "u1")
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)
    monkeypatch.setattr(
        checkout, "find_customer_by_user", lambda uid: {"id": "cus_stripe"}
    )
    monkeypatch.setattr(store, "upsert_customer_mapping", lambda uid, cid: None)

    assert checkout._customer_id_for_entity("e1") == "cus_stripe"


def test_the_render_path_stays_local_only(monkeypatch):
    """``modules._entity_customer_id`` feeds the settings card and must NOT search.

    A payer with no customer would otherwise fire a Stripe Customer Search on every
    page load, forever, and always find nothing. Pinned because the inconsistency with
    the billing paths looks like a bug until you know why it's there."""
    from billing.services import checkout, store
    from billing.services import entity_modules as modules

    monkeypatch.setattr(store, "payer_for_entity", lambda eid: "u1")
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)

    def _boom(uid):
        raise AssertionError("the render path must not search Stripe")

    monkeypatch.setattr(checkout, "find_customer_by_user", _boom)

    assert modules._entity_customer_id("e1") is None
