"""Ids are ``uuid.UUID`` objects on every backend (minty-db, owner 2026-10-28).

Flask's ``MintyUuid(as_uuid=False)`` gave text and the engine was written against it; since
minty-db the models are Django's own ``UUIDField``. A primary key, a ForeignKey attname (whose
converters resolve through the TARGET field) and a plain uuid column all read back as ``UUID``,
through ``get``, ``values_list`` and ``in_bulk`` alike - and text (a token claim, a path) handed
to a filter still finds the row. Comparing a UUID with uuid text raises under test (the root
conftest), so a missed conversion fails instead of quietly not matching.
"""

from __future__ import annotations

import uuid

import pytest
from minty_db.models import Entity, EntityModuleSubscription, User

pytestmark = pytest.mark.django_db


def _row(user, entity):
    return EntityModuleSubscription.objects.create(
        entity_id=entity.id,
        function_code="PETTY_CASH",
        payer_user_id=user.id,
        phase="trial",
    )


def test_primary_key_and_foreign_key_attnames_read_back_as_uuid(user, entity):
    created = _row(user, entity)
    assert isinstance(created.id, uuid.UUID), "a defaulted primary key is generated as a UUID"

    row = EntityModuleSubscription.objects.get(pk=created.id)
    for value in (row.id, row.entity_id, row.payer_user_id):
        assert type(value) is uuid.UUID
    assert row.payer_user_id == user.id
    assert row.entity_id == entity.id


def test_values_list_and_in_bulk_give_uuid(user, entity):
    created = _row(user, entity)
    (entity_id,) = EntityModuleSubscription.objects.filter(pk=created.id).values_list(
        "entity_id", flat=True
    )
    assert type(entity_id) is uuid.UUID
    bulk = EntityModuleSubscription.objects.in_bulk([created.id])
    assert list(bulk) == [created.id]
    assert all(type(k) is uuid.UUID for k in bulk)


def test_text_in_a_filter_still_matches(user, entity):
    created = _row(user, entity)
    assert EntityModuleSubscription.objects.filter(pk=str(created.id)).count() == 1
    assert EntityModuleSubscription.objects.filter(payer_user_id=str(user.id)).count() == 1
    assert EntityModuleSubscription.objects.filter(entity_id=str(entity.id).upper()).count() == 1


def test_read_only_mirrors_are_uuid_too(user, entity):
    assert type(User.objects.get(pk=user.id).id) is uuid.UUID
    assert type(Entity.objects.get(pk=entity.id).id) is uuid.UUID
    assert type(Entity.objects.get(pk=entity.id).country_code) is str


def test_a_malformed_id_is_a_validation_error_not_a_match(user, entity):
    from django.core.exceptions import ValidationError

    _row(user, entity)
    with pytest.raises(ValidationError):
        EntityModuleSubscription.objects.filter(pk="not-a-uuid").count()
