"""``access_sweep.revoke_ungranted_module_access`` - the launch-day step, on the ORM.

Flask's copy was two raw statements with the schema name interpolated; this one has to pick
the same rows (an enabled map row for a canonical module, no ``entity_module_subscription``
row for that module, entity not mid-onboarding) and write the same update. Minty's
``test_char_subscription_dark`` proved the Flask version on real rows; this is that half.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from billing.services.access_sweep import revoke_ungranted_module_access
from shared_models.models import Entity, EntityFunctionMap, EntityModuleSubscription

from .conftest import make_entity, make_user, seed_modules

pytestmark = pytest.mark.django_db


def _map(entity_id, fn_id):
    return EntityFunctionMap.objects.get(entity_id=entity_id, entity_function_id=fn_id)


@pytest.fixture
def world():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    # Both modules on, only Petty Cash has a subscription row behind it.
    backed = make_entity(owner, name="Backed Ltd", modules=("PETTY_CASH", "PAYMENT_REQUEST"))
    EntityModuleSubscription.objects.create(
        entity_id=backed.id, function_code="PETTY_CASH", payer_user_id=owner.id, phase="active",
        first_billed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # Nothing behind either module.
    bare = make_entity(owner, name="Bare Ltd", modules=("PETTY_CASH",))
    # Mid-wizard: the exemption.
    wizard = make_entity(owner, name="Wizard Ltd", modules=("PETTY_CASH",), status="onboarding")
    # Switched off already: not a grant, so not a hit.
    off = make_entity(owner, name="Off Ltd", modules=())
    EntityFunctionMap.objects.create(
        entity_id=off.id, entity_function_id=fns["PETTY_CASH"].id, is_enabled=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        disabled_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return fns, backed, bare, wizard, off


def test_dry_run_lists_exactly_the_unbacked_grants_and_writes_nothing(world):
    fns, backed, bare, wizard, off = world

    hits = revoke_ungranted_module_access(dry_run=True)

    assert hits == sorted(
        [
            {"entity_id": backed.id, "code": "PAYMENT_REQUEST"},  # on, no row for THIS module
            {"entity_id": bare.id, "code": "PETTY_CASH"},
        ],
        key=lambda h: (h["entity_id"], h["code"]),
    )
    assert _map(backed.id, fns["PAYMENT_REQUEST"].id).is_enabled is True, "dry run wrote nothing"
    assert _map(bare.id, fns["PETTY_CASH"].id).is_enabled is True
    assert Entity.objects.get(pk=wizard.id).status == "onboarding"


def test_apply_switches_the_hits_off_and_nothing_else(world):
    fns, backed, bare, wizard, off = world

    hits = revoke_ungranted_module_access(dry_run=False)

    assert len(hits) == 2
    revoked = _map(bare.id, fns["PETTY_CASH"].id)
    assert revoked.is_enabled is False
    assert revoked.disabled_at is not None
    assert _map(backed.id, fns["PAYMENT_REQUEST"].id).is_enabled is False
    # Untouched: the backed module, the mid-wizard entity, the already-off row.
    assert _map(backed.id, fns["PETTY_CASH"].id).is_enabled is True
    assert _map(wizard.id, fns["PETTY_CASH"].id).is_enabled is True
    assert _map(off.id, fns["PETTY_CASH"].id).disabled_at == datetime(2026, 1, 1, tzinfo=UTC)
    # Idempotent: a second pass finds nothing.
    assert revoke_ungranted_module_access(dry_run=True) == []


def test_nothing_is_ever_granted(world):
    fns, backed, bare, wizard, off = world
    before = EntityFunctionMap.objects.filter(is_enabled=True).count()
    revoke_ungranted_module_access(dry_run=False)
    assert EntityFunctionMap.objects.filter(is_enabled=True).count() < before
    assert _map(off.id, fns["PETTY_CASH"].id).is_enabled is False


def test_an_empty_catalog_is_an_empty_answer(db):
    assert revoke_ungranted_module_access(dry_run=True) == []
