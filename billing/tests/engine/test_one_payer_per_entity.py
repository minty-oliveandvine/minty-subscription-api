"""One payer per entity — enforced in ``store.upsert_module_row``, not merely assumed.

``payer_user_id`` reaches the store as "the acting user" from two directions —
``checkout.start_module_trial`` and ``checkout._grant_purchased_modules`` — so a second
admin starting a trial or buying a module on an entity someone else already pays for
would open a second billing relationship. Nothing downstream models that:
``payer_for_entity`` returns the FIRST row it finds, and ``get_module_cards`` reads one
payer's cycle and applies it to every module on the page.

It was observed live before this guard: one entity holding a paid module on one payer's
cycle and a trial on another payer with no cycle at all. The settings page rendered a
next-invoice date and a prorated conversion figure that belonged to neither of them.

The update path matters as much as the create path — the payer used to be reassigned on
every write, so a second admin merely CANCELLING a module took over the billing.
"""
from __future__ import annotations

import uuid

import pytest


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


def _user(db, email):
    from shared_models.models import User

    row = User(
        id=str(uuid.uuid4()),
        email=email,
        username=email,
        first_name="A",
        last_name="B",
        password="x",
        system_role="normal",
        approved=True,
    )
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _entity(db, name="Acme"):
    from shared_models.models import Entity

    row = Entity(id=str(uuid.uuid4()), name=name, status="disconnected")
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def test_a_second_module_joins_the_entitys_existing_payer(app, db_session):
    """The exact shape seen live: admin A trials Petty Cash, admin B trials Bill."""
    from billing.services import store

    with app.app_context():
        entity = _entity(db_session)
        first = _user(db_session, "first@test.com")
        second = _user(db_session, "second@test.com")

        store.upsert_module_row(entity.id, "PETTY_CASH", first.id, phase="trial")
        store.upsert_module_row(entity.id, "PAYMENT_REQUEST", second.id, phase="trial")

        rows = store.module_rows_for_entity(entity.id)
        assert {r.function_code for r in rows} == {"PETTY_CASH", "PAYMENT_REQUEST"}
        assert {r.payer_user_id for r in rows} == {str(first.id)}, (
            "the second module must join the established payer, not open a second one"
        )
        assert store.payer_for_entity(entity.id) == str(first.id)


def test_an_update_cannot_move_the_payer(app, db_session):
    """Cancelling passes the acting user when the row has no payer of its own, and the
    payer used to be rewritten on every write — so this took the billing over."""
    from billing.services import store

    with app.app_context():
        entity = _entity(db_session)
        owner = _user(db_session, "owner@test.com")
        other = _user(db_session, "other@test.com")

        store.upsert_module_row(entity.id, "PETTY_CASH", owner.id, phase="trial")
        store.upsert_module_row(
            entity.id, "PETTY_CASH", other.id, phase="scheduled_cancel"
        )

        row = store.module_row(entity.id, "PETTY_CASH")
        assert row.phase == "scheduled_cancel", "the field write must still land"
        assert row.payer_user_id == str(owner.id), "but the payer must not move"


def test_the_first_payer_is_still_free_to_be_anyone(app, db_session):
    """The guard fixes the payer, it does not dictate who it is — an entity with no
    rows takes whoever acts first."""
    from billing.services import store

    with app.app_context():
        entity = _entity(db_session)
        someone = _user(db_session, "someone@test.com")

        store.upsert_module_row(entity.id, "PAYMENT_REQUEST", someone.id, phase="trial")

        assert store.payer_for_entity(entity.id) == str(someone.id)


def test_entities_do_not_share_a_payer_with_each_other(app, db_session):
    """One payer per ENTITY, not one payer globally — a different entity is free to
    have a different payer, which is the normal multi-company case."""
    from billing.services import store

    with app.app_context():
        one, two = _entity(db_session, "One"), _entity(db_session, "Two")
        a = _user(db_session, "a@test.com")
        b = _user(db_session, "b@test.com")

        store.upsert_module_row(one.id, "PAYMENT_REQUEST", a.id, phase="trial")
        store.upsert_module_row(two.id, "PAYMENT_REQUEST", b.id, phase="trial")

        assert store.payer_for_entity(one.id) == str(a.id)
        assert store.payer_for_entity(two.id) == str(b.id)


def test_repeat_writes_by_the_established_payer_are_untouched(app, db_session):
    """The common path must not be disturbed: same payer, many writes."""
    from billing.services import store

    with app.app_context():
        entity = _entity(db_session)
        owner = _user(db_session, "owner2@test.com")

        store.upsert_module_row(entity.id, "PAYMENT_REQUEST", owner.id, phase="trial")
        store.upsert_module_row(entity.id, "PAYMENT_REQUEST", owner.id, phase="active")
        store.upsert_module_row(entity.id, "PETTY_CASH", owner.id, phase="active")

        rows = store.module_rows_for_entity(entity.id)
        assert len(rows) == 2
        assert {r.payer_user_id for r in rows} == {str(owner.id)}
        assert store.module_row(entity.id, "PAYMENT_REQUEST").phase == "active"
