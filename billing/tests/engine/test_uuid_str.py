"""Ids are strings on every backend - the one property the whole ported engine leans on.

Flask's ``MintyUuid(as_uuid=False)`` gave back hyphenated lowercase ``str``; the services compare
ids with ``==``, key dicts with them and build dedupe keys with f-strings. ``MintyUUIDField`` must
do the same for a primary key, a ForeignKey attname (whose converters resolve through the TARGET
field) and a plain uuid column, through ``get``, ``values_list`` and ``in_bulk`` alike - and a
``UUID`` handed to a filter must still find the row.
"""

from __future__ import annotations

import uuid

import pytest

from shared_models.models import Entity, EntityModuleSubscription, User

pytestmark = pytest.mark.django_db


def _row(user, entity):
    return EntityModuleSubscription.objects.create(
        entity_id=entity.id,
        function_code="PETTY_CASH",
        payer_user_id=user.id,
        phase="trial",
    )


def test_primary_key_and_foreign_key_attnames_read_back_as_str(user, entity):
    created = _row(user, entity)
    assert isinstance(created.id, str), "a defaulted primary key is generated as str"

    row = EntityModuleSubscription.objects.get(pk=created.id)
    for value in (row.id, row.entity_id, row.payer_user_id):
        assert type(value) is str
        assert value == value.lower()
        assert uuid.UUID(value)  # well-formed
    assert row.payer_user_id == str(user.id)
    assert row.entity_id == str(entity.id)


def test_values_list_and_in_bulk_give_str(user, entity):
    created = _row(user, entity)
    (entity_id,) = EntityModuleSubscription.objects.filter(pk=created.id).values_list(
        "entity_id", flat=True
    )
    assert type(entity_id) is str
    bulk = EntityModuleSubscription.objects.in_bulk([created.id])
    assert list(bulk) == [created.id]
    assert all(type(k) is str for k in bulk)


def test_a_uuid_object_in_a_filter_still_matches(user, entity):
    created = _row(user, entity)
    assert EntityModuleSubscription.objects.filter(pk=uuid.UUID(created.id)).count() == 1
    assert EntityModuleSubscription.objects.filter(payer_user_id=uuid.UUID(str(user.id))).count() == 1
    assert EntityModuleSubscription.objects.filter(entity_id=str(entity.id)).count() == 1


def test_read_only_mirrors_are_str_too(user, entity):
    assert type(User.objects.get(pk=user.id).id) is str
    assert type(Entity.objects.get(pk=entity.id).id) is str
    assert type(Entity.objects.get(pk=entity.id).country_code) is str


def test_a_malformed_id_is_a_validation_error_not_a_match(user, entity):
    from django.core.exceptions import ValidationError

    _row(user, entity)
    with pytest.raises(ValidationError):
        EntityModuleSubscription.objects.filter(pk="not-a-uuid").count()
