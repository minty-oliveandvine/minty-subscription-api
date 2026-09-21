"""``UpdatedAtMixin`` is the SQLite mirror of ``01``'s ``set_updated_at()`` trigger: on an
UPDATE the database writes ``updated_at``, whatever the application sent - an explicit stamp
included - and the INSERT stamps are the application's. ``EntityFunctionMap`` is in the
trigger list, so it is the table under test here (``_write_pairs`` writes it).

The moving stamp is asserted against a value it can never be (a 2020 date), not against the
previous stamp: inside a Postgres test transaction ``now()`` is the transaction START, so
"later than before" is not something a test may expect of the database clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db.models.expressions import Combinable

from shared_models.models import EntityFunctionMap

from .conftest import make_entity, make_user, seed_module

pytestmark = pytest.mark.django_db

STALE = datetime(2020, 1, 1, tzinfo=UTC)
CHOSEN = datetime(2024, 6, 1, 12, 30, tzinfo=UTC)


def _row():
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=())
    fn = seed_module()
    return EntityFunctionMap.objects.create(
        entity_id=entity.id, entity_function_id=fn.id, is_enabled=False,
        created_at=STALE, updated_at=STALE, disabled_at=STALE,
    )


def test_an_insert_keeps_the_stamp_it_was_given():
    row = _row()
    row.refresh_from_db()
    assert row.updated_at == STALE


def test_a_plain_save_moves_the_stamp_to_the_database_clock():
    row = _row()
    row.is_enabled = True
    row.save()
    assert not isinstance(row.updated_at, Combinable), "refreshed, never the expression"
    assert row.updated_at != STALE
    row.refresh_from_db()
    assert row.updated_at != STALE


def test_a_save_with_update_fields_moves_the_stamp_too():
    row = _row()
    row.is_enabled = True
    row.save(update_fields=["is_enabled"])
    row.refresh_from_db()
    assert row.is_enabled is True
    assert row.updated_at != STALE


def test_an_explicit_stamp_on_an_update_is_the_databases_not_the_callers():
    row = _row()
    row.is_enabled = True
    row.updated_at = CHOSEN
    row.save(update_fields=["is_enabled", "updated_at"])
    assert row.updated_at != CHOSEN, "refreshed to what the row holds"
    row.refresh_from_db()
    assert row.updated_at != CHOSEN, "the trigger's rule: the caller's stamp never lands"
    assert row.updated_at != STALE


def test_a_bulk_update_moves_the_stamp_when_told_to():
    """``QuerySet.update`` bypasses ``save()``; the store's bulk writes pass ``updated_at=Now()``
    themselves - on Postgres the trigger does it anyway, on SQLite this is the only way."""
    from django.db.models.functions import Now

    row = _row()
    EntityFunctionMap.objects.filter(pk=row.pk).update(is_enabled=True, updated_at=Now())
    row.refresh_from_db()
    assert row.is_enabled is True
    assert row.updated_at != STALE
