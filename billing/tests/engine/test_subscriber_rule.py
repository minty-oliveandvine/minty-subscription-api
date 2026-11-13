"""ONLY THE SUBSCRIBER CHANGES A COMPANY'S SUBSCRIPTION (owner, 2026-11-13).

``entities.subscriber_user_id`` (minty-db v0.9) is who holds a company's subscription: its creator
(onboarding writes it), moved by a handover, and the person who confirms billing becomes
``payer_user_id``. Once there is a payer the column mirrors it.

* Only the subscriber may start a trial, activate, change modules or hand it over. The old rule
  - any admin may act while nobody pays - is gone.
* A company with NO subscriber is view-only for everyone until one is assigned.
* A handover BEFORE activation moves only the column: no charge, no consent, no nomination.
"""

from __future__ import annotations

import pytest

from .conftest import make_entity, make_user, seed_country, seed_currency, seed_module, seed_plan

pytestmark = pytest.mark.django_db

MODULE = "PETTY_CASH"


@pytest.fixture
def shop():
    """A company with one module, a sellable plan, its creator (admin) and a second admin."""
    currency = seed_currency("HKD")
    country = seed_country(currency)
    owner = make_user("owner@test.com", first_name="Olive", last_name="Owner")
    entity = make_entity(owner, currency=currency, country=country, modules=(MODULE,))
    seed_module(MODULE, "Petty Cash")
    seed_plan(MODULE, 28000, display_name="Petty Cash")
    other = _join(entity, "other@test.com", "admin")
    return owner, other, entity


def _join(entity, email, role):
    from minty_db.models import UserEntity

    person = make_user(email)
    UserEntity.objects.create(user_id=person.id, entity_id=entity.id, role=role, approved=True)
    return person


def _trial(entity):
    from billing.services import store

    return store.upsert_module_row(entity.id, MODULE, phase="trial")


def _designate(entity, user):
    from minty_db.models import Entity

    Entity.objects.filter(id=str(entity.id)).update(subscriber_user_id=user.id if user else None)


def _subscriber_column(entity):
    from minty_db.models import Entity

    value = Entity.objects.values_list("subscriber_user_id", flat=True).get(id=str(entity.id))
    return str(value) if value else None


# --- the rule ---------------------------------------------------------------------------------


def test_an_unassigned_company_is_managed_by_nobody(shop):
    owner, other, entity = shop
    _trial(entity)

    from billing.services import store

    assert store.subscriber_for_entity(entity.id) is None
    assert not store.may_manage_subscription(entity.id, owner.id)
    assert not store.may_manage_subscription(entity.id, other.id)


def test_only_the_designated_subscriber_manages_before_billing_is_confirmed(shop):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import store

    assert store.subscriber_for_entity(entity.id) == str(owner.id)
    assert store.may_manage_subscription(entity.id, owner.id)
    assert not store.may_manage_subscription(entity.id, other.id)


def test_the_payer_is_the_subscriber_once_billing_is_confirmed(shop):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import store

    store.establish_entity_payer(entity.id, owner.id)

    assert store.subscriber_for_entity(entity.id) == str(owner.id)
    assert _subscriber_column(entity) == str(owner.id)
    assert not store.may_manage_subscription(entity.id, other.id)


def test_a_payer_move_moves_the_column_with_it(shop):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import store

    store.establish_entity_payer(entity.id, owner.id)
    store.transfer_entity_payer(entity.id, other.id)

    assert _subscriber_column(entity) == str(other.id)
    assert store.may_manage_subscription(entity.id, other.id)
    assert not store.may_manage_subscription(entity.id, owner.id)


# --- the list ---------------------------------------------------------------------------------


def test_the_list_names_the_designated_subscriber_and_only_they_may_activate(shop):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import portal

    (mine,) = portal.build_payer_subscriptions(owner.id)["entities"]
    (theirs,) = portal.build_payer_subscriptions(other.id)["entities"]

    assert mine["can_manage"] is True
    assert mine["has_subscriber"] is False  # billing not confirmed: the row offers Activate
    assert mine["subscriber"]["id"] == str(owner.id)
    assert theirs["can_manage"] is False
    assert theirs["subscriber"]["name"] == "Olive Owner"


def test_an_unassigned_company_is_view_only_on_the_list(shop):
    owner, other, entity = shop
    _trial(entity)

    from billing.services import portal

    for person in (owner, other):
        (row,) = portal.build_payer_subscriptions(person.id)["entities"]
        assert row["can_manage"] is False
        assert row["subscriber"] is None
        assert row["subscriber_set"] is False


# --- placing a company on a billing account ---------------------------------------------------


def test_only_the_subscriber_places_a_company_nobody_pays_for(shop):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    group = store.create_billing_account(other.id, "pm_other", billing_company="Other Ltd")
    with pytest.raises(PaymentMethodError) as refused:
        billing_accounts.move_company(other.id, entity.id, group.id)
    assert refused.value.status == 404


# --- a handover before billing is confirmed ---------------------------------------------------


def test_the_subscriber_hands_over_before_activation_and_only_the_column_moves(shop, monkeypatch):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import store, transfers

    monkeypatch.setattr(transfers, "_notify", lambda *a, **k: None)

    ok, message, offer = transfers.offer_transfer(owner.id, entity.id, other.id)
    assert ok, message

    accepted, message, _ = transfers.respond_to_transfer(other.id, offer["id"], accept=True)
    assert accepted, message

    assert _subscriber_column(entity) == str(other.id)
    assert store.payer_for_entity(entity.id) is None  # still nobody billed
    assert store.nomination_for_entity(entity.id, other.id) is None
    assert store.may_manage_subscription(entity.id, other.id)
    assert not store.may_manage_subscription(entity.id, owner.id)


def test_only_the_subscriber_may_offer_a_handover_before_activation(shop, monkeypatch):
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import transfers

    monkeypatch.setattr(transfers, "_notify", lambda *a, **k: None)

    ok, message, _ = transfers.offer_transfer(other.id, entity.id, owner.id)
    assert not ok
    assert "Only the company's subscriber" in message


def test_an_unassigned_company_has_nothing_to_hand_over(shop, monkeypatch):
    owner, other, entity = shop
    _trial(entity)

    from billing.services import transfers

    monkeypatch.setattr(transfers, "_notify", lambda *a, **k: None)

    ok, message, _ = transfers.offer_transfer(owner.id, entity.id, other.id)
    assert not ok
    assert "hasn't been set" in message


def test_the_handover_screen_opens_for_the_designated_subscriber_only(shop):
    """``subscriber-options`` (who it could be handed to) answers the subscriber before billing
    is confirmed too; anyone else - and everyone while nobody holds it - gets the 404."""
    owner, other, entity = shop
    _trial(entity)

    from billing.services import portal

    assert portal.build_subscriber_options(owner.id, entity.id) is None
    _designate(entity, owner)
    options = portal.build_subscriber_options(owner.id, entity.id)
    assert options is not None
    assert portal.build_subscriber_options(other.id, entity.id) is None


def test_an_offer_before_activation_tells_the_recipient_nothing_is_billed(shop, monkeypatch):
    """The accept screen asks for a billing account only when billing is confirmed: before it,
    the handover moves who holds the subscription and nothing else."""
    owner, other, entity = shop
    _trial(entity)
    _designate(entity, owner)

    from billing.services import transfers

    monkeypatch.setattr(transfers, "_notify", lambda *a, **k: None)
    ok, message, _ = transfers.offer_transfer(owner.id, entity.id, other.id)
    assert ok, message

    (incoming,) = transfers.incoming_transfers_payload(other.id)
    assert incoming["billing_confirmed"] is False
    assert incoming["blockers"] == []
