"""The projection writer produces Flask's rows, column for column.

``entity_function_map`` has two writers during Part 2 - Flask while dark, this service when
live - and Flask's gate reads whichever wrote last. So the row this service writes must be
indistinguishable from the one ``blueprints/entity/services/modules._write_pairs`` writes:
explicit UTC stamps on insert (``created_at``, ``updated_at``, and ``enabled_at`` OR
``disabled_at``), ``created_by`` the acting user or NULL, and on an update only the flipped
stamp plus ``updated_at`` moving while ``created_at`` / ``created_by`` stay put.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from billing.services import entity_modules
from billing.services.entity_modules import (
    ACTOR_CLI,
    ACTOR_SUBSCRIPTION,
    MODULE_CODES,
    apply_module_selections,
    set_entity_module,
)
from shared_models.models import EntityFunctionMap, EntityModuleSubscription

from .conftest import make_entity, make_user, seed_modules

pytestmark = pytest.mark.django_db


STALE = datetime(2020, 1, 1, tzinfo=UTC)


def _row(entity_id, fn_id):
    return EntityFunctionMap.objects.get(entity_id=entity_id, entity_function_id=fn_id)


def _paid_row(entity, owner, *, phase="active", billed=True):
    return EntityModuleSubscription.objects.create(
        entity_id=entity.id,
        function_code="PETTY_CASH",
        payer_user_id=owner.id,
        phase=phase,
        first_billed_at=datetime(2026, 1, 1, tzinfo=UTC) if billed else None,
    )


def test_an_insert_carries_flasks_stamps_and_the_actor_as_created_by():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=())

    before = datetime.now(UTC)
    payload, status = set_entity_module(entity.id, "PETTY_CASH", True, actor=ACTOR_CLI, user_id=owner.id)
    after = datetime.now(UTC)

    assert status == 200
    assert payload == {"modules": {"PETTY_CASH": True, "PAYMENT_REQUEST": False}}
    row = _row(entity.id, fns["PETTY_CASH"].id)
    assert row.is_enabled is True
    assert row.created_by == str(owner.id)
    assert before <= row.created_at <= after
    assert row.updated_at == row.created_at, "Flask stamps both with the same instant"
    assert row.enabled_at == row.created_at
    assert row.disabled_at is None
    for stamp in (row.created_at, row.updated_at, row.enabled_at):
        assert stamp.tzinfo is not None, "aware UTC, never naive"


def test_a_disabled_insert_stamps_disabled_at_not_enabled_at():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=())

    _, status = set_entity_module(entity.id, "PAYMENT_REQUEST", False, actor=ACTOR_CLI)
    assert status == 200
    row = _row(entity.id, fns["PAYMENT_REQUEST"].id)
    assert row.is_enabled is False
    assert row.enabled_at is None
    assert row.disabled_at == row.created_at
    assert row.created_by is None, "no user -> NULL, as for the CLI and the pass"


def test_an_update_moves_only_the_flipped_stamp_and_updated_at():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=())
    # A row written long ago (an INSERT keeps its stamps on both backends), so that "moved"
    # is a comparison with 2020 - inside a Postgres test transaction now() is the transaction
    # start, so "later than the previous stamp" is not something the database clock promises.
    EntityFunctionMap.objects.create(
        entity_id=entity.id, entity_function_id=fns["PETTY_CASH"].id, is_enabled=True,
        created_by=owner.id, enabled_at=STALE, created_at=STALE, updated_at=STALE,
    )

    _, status = set_entity_module(entity.id, "PETTY_CASH", False, actor=ACTOR_CLI, user_id=None)
    assert status == 200
    row = _row(entity.id, fns["PETTY_CASH"].id)
    assert row.is_enabled is False
    assert row.created_at == STALE
    assert row.created_by == str(owner.id), "history is preserved on update"
    assert row.enabled_at == STALE, "the OTHER stamp is untouched"
    assert row.disabled_at is not None and row.disabled_at > STALE
    assert row.updated_at > STALE

    # Writing the same state again bumps updated_at only.
    _, status = set_entity_module(entity.id, "PETTY_CASH", False, actor=ACTOR_CLI)
    again = _row(entity.id, fns["PETTY_CASH"].id)
    assert again.disabled_at == row.disabled_at
    assert again.enabled_at == STALE
    assert again.updated_at >= row.updated_at


def test_apply_module_selections_writes_every_canonical_module():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=())

    payload, status = apply_module_selections(entity.id, ["PETTY_CASH"], actor="onboarding", user_id=owner.id)
    assert status == 200
    assert payload == {"modules": {"PETTY_CASH": True, "PAYMENT_REQUEST": False}}
    assert EntityFunctionMap.objects.filter(entity_id=entity.id).count() == len(MODULE_CODES)
    assert _row(entity.id, fns["PAYMENT_REQUEST"].id).is_enabled is False


def test_a_paid_module_cannot_be_switched_off_by_hand():
    seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=("PETTY_CASH",))
    _paid_row(entity, owner)

    payload, status = set_entity_module(entity.id, "PETTY_CASH", False, actor=ACTOR_CLI)
    assert status == 409
    assert "active subscription" in payload["error"]
    assert entity_modules._enabled_state(entity.id)["PETTY_CASH"] is True


def test_the_subscription_lifecycle_may_switch_a_paid_module_off():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=("PETTY_CASH",))
    _paid_row(entity, owner)

    _, status = set_entity_module(entity.id, "PETTY_CASH", False, actor=ACTOR_SUBSCRIPTION)
    assert status == 200
    assert _row(entity.id, fns["PETTY_CASH"].id).is_enabled is False


def test_a_trial_may_be_switched_off_by_hand():
    seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=("PETTY_CASH",))
    _paid_row(entity, owner, phase="trial", billed=False)

    _, status = set_entity_module(entity.id, "PETTY_CASH", False, actor=ACTOR_CLI)
    assert status == 200


def test_a_missing_catalog_row_is_a_500_not_a_grant():
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=())

    payload, status = set_entity_module(entity.id, "PETTY_CASH", True, actor=ACTOR_CLI)
    assert status == 500
    assert "catalog" in payload["error"].lower()
    assert EntityFunctionMap.objects.filter(entity_id=entity.id).count() == 0
