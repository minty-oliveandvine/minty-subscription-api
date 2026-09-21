"""Access is a PROJECTION of the subscription row, and every unknown answers no.

``entity_module_subscription`` is the record of truth; ``entity_function_map.is_enabled``
only caches it so a request costs one indexed lookup instead of a join. That makes two
rules load-bearing, and neither had a test:

* **The gate fails closed.** No map row, or no catalog row, means NO. It used to fall
  through to the catalog's ``is_active``, which granted a module to every entity that
  had never subscribed to it.
* **The sweep revokes what nothing backs.** A module switched on with no subscription
  row is the one inconsistency no event can repair — trials and cancellations fire on
  writes, but "enabled and never subscribed" has no write to hang off. The sweep used
  to skip exactly that case, so it survived forever: the user was inside the module
  while its card still offered "Start free trial".

Mid-onboarding entities are the deliberate exception — the wizard records its Step 2
selection in the map and only starts the trials at finalize.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest


def _is_module_enabled(entity_id, code) -> bool:
    """Flask's gate (``blueprints/entity/routes/modules.py``) stays in Minty until Part 3; what
    these tests pin on the Django side is its twin, ``entity_modules._enabled_state`` - the same
    fail-closed rule (no map row -> False, no catalog row -> False) and, like the gate, "not a
    module code at all" -> False."""
    from billing.services import entity_modules

    if code not in entity_modules.MODULE_CODES:
        return False
    return entity_modules._enabled_state(entity_id)[code]


@pytest.fixture
def db_session(db):
    """Flask's per-test database (rows DELETEd afterwards) is pytest-django's ``db``
    (a transaction rolled back afterwards). The helpers below take it for parity."""
    return _DbShim()


class _DbShim:
    """What the ported helpers still reach for: ``db.session.refresh(row)``."""

    class session:  # noqa: N801
        @staticmethod
        def refresh(row):
            row.refresh_from_db()

        @staticmethod
        def commit():
            pass


def _entity(db, *, status="disconnected", name="Acme"):
    from shared_models.models import Entity

    row = Entity(id=str(uuid.uuid4()), name=name, status=status)
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _function(db, code, *, is_active):
    """A catalog row. ``is_active`` is deliberately varied in these tests — the gate
    must ignore it entirely when deciding who may use a module."""
    from shared_models.models import EntityFunction

    row = EntityFunction(
        id=str(uuid.uuid4()),
        function_code=code,
        function_name=code.title(),
        is_active=is_active,
    )
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _grant(db, entity, fn, *, enabled=True, actor=None):
    # ``actor`` is accepted for the callers' readability only: entity_function_map.created_by
    # is the person (a user id or NULL, schema section 4), never a label.
    from shared_models.models import EntityFunctionMap

    row = EntityFunctionMap(
        entity_id=entity.id,
        entity_function_id=fn.id,
        is_enabled=enabled,
        created_by=None,
    )
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _payer(db):
    from shared_models.models import User

    row = User(
        id=str(uuid.uuid4()),
        email="payer@test.com",
        username="payer@test.com",
        first_name="Pay",
        last_name="Er",
        password="x",
        system_role="normal",
        approved=True,
    )
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _sub_row(db, entity, code, *, phase, trial_end=None, app_access_until=None):
    from shared_models.models import EntityModuleSubscription

    row = EntityModuleSubscription(
        id=str(uuid.uuid4()),
        entity_id=entity.id,
        function_code=code,
        payer_user_id=_payer(db).id,
        phase=phase,
        trial_end=trial_end,
        app_access_until=app_access_until,
    )
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


# --- the gate ---------------------------------------------------------------


def test_no_map_row_denies_even_when_the_catalog_says_active(app, db_session):
    """The catalog's is_active says whether a module is OFFERED, never who may use it.

    This is the exact shape of the production bug: PETTY_CASH sat is_active=TRUE, so
    every entity without a map row resolved to ENABLED for free.
    """

    with app.app_context():
        entity = _entity(db_session)
        _function(db_session, "PETTY_CASH", is_active=True)

        assert _is_module_enabled(entity.id, "PETTY_CASH") is False


def test_missing_catalog_row_denies(app, db_session):
    """Used to return True on the theory that the function row predated the gating
    table. A typo'd or not-yet-seeded code then opened the module to everyone."""

    with app.app_context():
        entity = _entity(db_session)

        assert _is_module_enabled(entity.id, "NOT_A_MODULE") is False


def test_map_row_is_what_decides(app, db_session):

    with app.app_context():
        entity = _entity(db_session)
        # is_active FALSE throughout: an explicit grant must still win.
        fn = _function(db_session, "PAYMENT_REQUEST", is_active=False)

        _grant(db_session, entity, fn, enabled=True)
        assert _is_module_enabled(entity.id, "PAYMENT_REQUEST") is True

        from shared_models.models import EntityFunctionMap

        row = EntityFunctionMap.objects.get(entity_id=entity.id, entity_function_id=fn.id)
        row.is_enabled = False
        row.save(update_fields=["is_enabled"])
        assert _is_module_enabled(entity.id, "PAYMENT_REQUEST") is False


def test_entity_creation_grants_nothing(app, db_session):
    """A new entity holds no module until a trial or subscription starts one."""
    from billing.services.entity_modules import DEFAULT_MODULE_STATE, apply_default_modules

    assert DEFAULT_MODULE_STATE == {"PETTY_CASH": False, "PAYMENT_REQUEST": False}

    with app.app_context():
        entity = _entity(db_session)
        _function(db_session, "PETTY_CASH", is_active=True)
        _function(db_session, "PAYMENT_REQUEST", is_active=True)

        _data, status = apply_default_modules(entity.id)
        assert status == 200

        assert _is_module_enabled(entity.id, "PETTY_CASH") is False
        assert _is_module_enabled(entity.id, "PAYMENT_REQUEST") is False


# --- the sweep --------------------------------------------------------------


def test_sweep_revokes_access_with_no_subscription_row(app, db_session):
    """The case that used to be skipped, and so never got repaired."""
    sweep_expired_module_access = pytest.importorskip("billing.services.access_sweep").sweep_expired_module_access  # slice D

    with app.app_context():
        entity = _entity(db_session)
        pc = _function(db_session, "PETTY_CASH", is_active=False)
        _function(db_session, "PAYMENT_REQUEST", is_active=False)
        _grant(db_session, entity, pc, enabled=True)

        assert _is_module_enabled(entity.id, "PETTY_CASH") is True

        result = sweep_expired_module_access()

        assert {"entity_id": entity.id, "code": "PETTY_CASH"} in result["disabled"]
        assert _is_module_enabled(entity.id, "PETTY_CASH") is False


def test_sweep_exempts_entities_still_onboarding(app, db_session):
    """Between Step 2's selection and finalize's trial start, enabled-with-no-row is
    the expected state — revoking there would leave finalize nothing to start."""
    sweep_expired_module_access = pytest.importorskip("billing.services.access_sweep").sweep_expired_module_access  # slice D

    with app.app_context():
        entity = _entity(db_session, status="onboarding")
        pc = _function(db_session, "PETTY_CASH", is_active=False)
        _function(db_session, "PAYMENT_REQUEST", is_active=False)
        _grant(db_session, entity, pc, enabled=True, actor="onboarding")

        result = sweep_expired_module_access()

        assert result["disabled"] == []
        assert _is_module_enabled(entity.id, "PETTY_CASH") is True


# ``_naive_clock`` went with C7: the subscription tables' timestamps are ``AwareDateTime``,
# which hands back an aware stamp on SQLite too, so the sweep's clock can stay aware.
def test_sweep_terminates_a_cancellation_whose_access_ran_out(app, db_session, monkeypatch):
    """The date that ends access also ends the SUBSCRIPTION.

    Leaving the row on ``scheduled_cancel`` meant a module nobody could use still read
    as mid-cancellation forever — the panel offering Renew for something already over,
    and ``_in_cancellation_window`` refusing to let them buy it again. ``cancelled`` is
    terminal and re-purchasable, which is the state they are actually in.
    """
    sweep_expired_module_access = pytest.importorskip("billing.services.access_sweep").sweep_expired_module_access  # slice D

    with app.app_context():
        entity = _entity(db_session)
        pc = _function(db_session, "PETTY_CASH", is_active=False)
        _function(db_session, "PAYMENT_REQUEST", is_active=False)
        _grant(db_session, entity, pc, enabled=True, actor="subscription")

        past = datetime.now(UTC) - timedelta(days=1)
        row = _sub_row(
            db_session, entity, "PETTY_CASH",
            phase="scheduled_cancel", app_access_until=past,
        )

        result = sweep_expired_module_access()

        assert {"entity_id": entity.id, "code": "PETTY_CASH"} in result["disabled"]
        assert _is_module_enabled(entity.id, "PETTY_CASH") is False
        db_session.session.refresh(row)
        assert row.phase == "cancelled"
        # Cleared: a leftover date on a terminal row is a trap for any reader that
        # checks dates before phases.
        assert row.app_access_until is None


def test_sweep_does_not_terminate_a_cancellation_still_running(app, db_session, monkeypatch):
    """Cancelled but still inside its paid days is NOT over — it is the one state where
    Renew is the right offer, and terminating it early would take that away."""
    sweep_expired_module_access = pytest.importorskip("billing.services.access_sweep").sweep_expired_module_access  # slice D

    with app.app_context():
        entity = _entity(db_session)
        pc = _function(db_session, "PETTY_CASH", is_active=False)
        _function(db_session, "PAYMENT_REQUEST", is_active=False)
        _grant(db_session, entity, pc, enabled=True, actor="subscription")

        future = datetime.now(UTC) + timedelta(days=10)
        row = _sub_row(
            db_session, entity, "PETTY_CASH",
            phase="scheduled_cancel", app_access_until=future,
        )

        result = sweep_expired_module_access()

        assert result["disabled"] == []
        assert _is_module_enabled(entity.id, "PETTY_CASH") is True
        db_session.session.refresh(row)
        assert row.phase == "scheduled_cancel"


def test_sweep_leaves_an_ended_trial_to_the_trial_job(app, db_session, monkeypatch):
    """A trial past its end is NOT terminated here: convert-or-expire is a decision this
    job cannot make, and stamping ``cancelled`` over it would rob the trial-end job of
    the row it converts. Access still comes off — that part is a date, not a decision."""
    sweep_expired_module_access = pytest.importorskip("billing.services.access_sweep").sweep_expired_module_access  # slice D

    with app.app_context():
        entity = _entity(db_session)
        pc = _function(db_session, "PETTY_CASH", is_active=False)
        _function(db_session, "PAYMENT_REQUEST", is_active=False)
        _grant(db_session, entity, pc, enabled=True, actor="subscription")

        past = datetime.now(UTC) - timedelta(days=1)
        row = _sub_row(
            db_session, entity, "PETTY_CASH",
            phase="trial", trial_end=past, app_access_until=past,
        )

        sweep_expired_module_access()

        db_session.session.refresh(row)
        assert row.phase == "trial"


def test_sweep_leaves_a_running_trial_alone(app, db_session):
    """Regression guard: the revoke-on-no-row branch must not swallow live trials."""
    sweep_expired_module_access = pytest.importorskip("billing.services.access_sweep").sweep_expired_module_access  # slice D

    with app.app_context():
        entity = _entity(db_session)
        pc = _function(db_session, "PETTY_CASH", is_active=False)
        _function(db_session, "PAYMENT_REQUEST", is_active=False)
        _grant(db_session, entity, pc, enabled=True, actor="subscription")

        future = datetime.now(UTC) + timedelta(days=10)
        _sub_row(
            db_session, entity, "PETTY_CASH",
            phase="trial", trial_end=future, app_access_until=future,
        )

        result = sweep_expired_module_access()

        assert result["disabled"] == []
        assert _is_module_enabled(entity.id, "PETTY_CASH") is True
