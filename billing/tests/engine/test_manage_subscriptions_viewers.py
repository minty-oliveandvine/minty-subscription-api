"""WHO SEES A COMPANY ON MANAGE SUBSCRIPTIONS, AND WHO MAY CHANGE IT (owner, 2026-11-12).

* Every admin and accountant of a company sees it on the list, whoever pays for it.
* Only the SUBSCRIBER (the payer) may change it: ``can_manage`` is true for the payer's own
  rows, and for a company nobody pays for yet only to an admin (who becomes its subscriber by
  activating). Every other row is read-only and names the real subscriber.
* Cashiers and shop managers are not shown a company they do not pay for.

The writes stay refused to anyone but the payer by the routes themselves
(``test_module_settings_api``, ``test_billing_accounts_api``); this file pins the list.
"""

from __future__ import annotations

import pytest

from .conftest import make_entity, make_user, seed_country, seed_currency, seed_module, seed_plan

pytestmark = pytest.mark.django_db

MODULE = "PETTY_CASH"


@pytest.fixture
def shop():
    """An admin and a company with one module and a sellable plan."""
    currency = seed_currency("HKD")
    country = seed_country(currency)
    owner = make_user("owner@test.com", first_name="Olive", last_name="Owner")
    entity = make_entity(owner, currency=currency, country=country, modules=(MODULE,))
    seed_module(MODULE, "Petty Cash")
    seed_plan(MODULE, 28000, display_name="Petty Cash")
    return owner, entity


def _trial(entity):
    from billing.services import store

    return store.upsert_module_row(entity.id, MODULE, phase="trial")


def _join(entity, email, role, *, approved=True):
    from minty_db.models import UserEntity

    person = make_user(email)
    UserEntity.objects.create(
        user_id=person.id, entity_id=entity.id, role=role, approved=approved
    )
    return person


def _paid_by(owner, entity):
    from billing.services import store

    _trial(entity)
    store.establish_entity_payer(entity.id, owner.id)


def _list(user):
    from billing.services import portal

    return portal.build_payer_subscriptions(user.id)


@pytest.mark.parametrize("role", ["accountant", "admin"])
def test_an_accountant_or_co_admin_sees_a_company_someone_else_pays_for(shop, role):
    owner, entity = shop
    _paid_by(owner, entity)
    viewer = _join(entity, f"{role}@test.com", role)

    body = _list(viewer)

    (item,) = body["entities"]
    assert item["entity_id"] == str(entity.id)
    assert item["can_manage"] is False
    assert item["has_subscriber"] is True
    assert item["subscriber"]["id"] == str(owner.id)
    assert item["subscriber"]["name"] == "Olive Owner"
    assert body["total"] == 1


def test_the_payer_may_manage_their_own_row(shop):
    owner, entity = shop
    _paid_by(owner, entity)

    (item,) = _list(owner)["entities"]

    assert item["can_manage"] is True
    assert item["subscriber"]["id"] == str(owner.id)


@pytest.mark.parametrize("role", ["cashier", "shop_manager"])
def test_a_cashier_or_shop_manager_is_not_shown_the_company(shop, role):
    owner, entity = shop
    _paid_by(owner, entity)
    viewer = _join(entity, f"{role}@test.com", role)

    assert _list(viewer)["entities"] == []


def test_an_unapproved_membership_shows_nothing(shop):
    owner, entity = shop
    _paid_by(owner, entity)
    viewer = _join(entity, "pending@test.com", "accountant", approved=False)

    assert _list(viewer)["entities"] == []


def test_with_no_subscriber_an_admin_may_activate_and_an_accountant_only_views(shop):
    owner, entity = shop
    _trial(entity)
    accountant = _join(entity, "accountant@test.com", "accountant")

    (admin_row,) = _list(owner)["entities"]
    (accountant_row,) = _list(accountant)["entities"]

    assert admin_row["can_manage"] is True
    assert admin_row["has_subscriber"] is False
    assert admin_row["subscriber"] is None
    assert accountant_row["can_manage"] is False
    assert accountant_row["has_subscriber"] is False
    assert accountant_row["subscriber"] is None


def test_search_and_sort_by_subscriber_survive_a_row_with_no_subscriber(shop):
    """A row with ``subscriber: null`` used to raise a TypeError in the search and the sort."""
    owner, entity = shop
    _trial(entity)

    from billing.services import portal

    assert portal.build_payer_subscriptions(owner.id, query="acme")["total"] == 1
    assert portal.build_payer_subscriptions(owner.id, query="nothing-like-it")["total"] == 0
    assert portal.build_payer_subscriptions(owner.id, sort="subscriber")["total"] == 1


def test_read_only_rows_are_searchable_by_their_subscriber(shop):
    owner, entity = shop
    _paid_by(owner, entity)
    viewer = _join(entity, "accountant@test.com", "accountant")

    from billing.services import portal

    assert portal.build_payer_subscriptions(viewer.id, query="olive owner")["total"] == 1


def test_the_viewers_own_and_read_only_rows_count_together(shop):
    """Paging counts every row the viewer is shown, theirs and the read-only ones."""
    owner, entity = shop
    _paid_by(owner, entity)
    viewer = _join(entity, "accountant@test.com", "accountant")

    currency = seed_currency("HKD")
    country = seed_country(currency)
    theirs = make_entity(viewer, name="Viewer Shop", currency=currency, country=country)
    _paid_by(viewer, theirs)

    body = _list(viewer)

    assert body["total"] == 2
    rows = {i["entity_name"]: i["can_manage"] for i in body["entities"]}
    assert rows == {"Acme Shop": False, "Viewer Shop": True}
