"""``daily.daily_lock`` - the cluster-wide "a pass is running" lock.

Postgres only for the contention case: the lock is ``pg_try_advisory_lock`` on a dedicated
connection, and SQLite has neither. On SQLite the lock yields True and gets out of the way,
which the first test pins on both backends.
"""

from __future__ import annotations

import pytest
from django.db import connection

from billing.services import daily

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_lock_is_granted_when_nobody_holds_it():
    with daily.daily_lock() as acquired:
        assert acquired is True


@pytest.mark.skipif(connection.vendor != "postgresql", reason="advisory locks are Postgres")
def test_a_second_holder_is_refused_not_queued():
    """The loser must SKIP, not wait: a pass that queues behind the running one and then
    runs the moment it finishes is exactly the double run the lock exists to prevent."""
    other = connection.get_new_connection(connection.get_connection_params())
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (daily._LOCK_KEY,))
            assert cursor.fetchone()[0] is True, "the rival took the lock first"
        with daily.daily_lock() as acquired:
            assert acquired is False
    finally:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (daily._LOCK_KEY,))
        other.close()

    # Released: the next pass gets it.
    with daily.daily_lock() as acquired:
        assert acquired is True


@pytest.mark.skipif(connection.vendor != "postgresql", reason="advisory locks are Postgres")
def test_the_lock_does_not_ride_the_orm_connection():
    """Held on its own connection, so the ORM's autocommit traffic inside the pass cannot
    release it: a query on the default connection while the lock is held changes nothing,
    and a rival still sees it taken."""
    with daily.daily_lock() as acquired:
        assert acquired is True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        rival = connection.get_new_connection(connection.get_connection_params())
        try:
            with rival.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (daily._LOCK_KEY,))
                assert cursor.fetchone()[0] is False, "still held by the pass"
        finally:
            rival.close()


def test_the_key_is_flasks():
    """Two services sharing one database must contend for ONE lock: the key is derived from
    the same name Flask's ``daily.py`` hashes, so a Flask pass and a Django pass on the same
    day exclude each other during the cutover."""
    import hashlib

    expected = int.from_bytes(hashlib.sha256(b"minty:subscriptions:run-daily").digest()[:8], "big", signed=True)
    assert daily._LOCK_KEY == expected
