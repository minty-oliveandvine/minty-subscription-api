"""Repo-root pytest hooks.

Postgres test mode (MINTY_TEST_PG_URI set): minty-db's plugin builds the test database from its
packaged ``01_schema_rebased.sql`` and hands it to pytest-django, so the models run against
the real schema, which SQLite (tables built from the models) can never do. Unset, the plugin
does nothing and the SQLite path in ``config/settings_test.py`` applies.

The id guard: ids are ``uuid.UUID`` objects since minty-db (owner, 2026-10-28), while request
paths, token claims and JSON carry text. ``UUID("x") == "x"`` is quietly False, so a missed
conversion skips a match without raising. Under test, comparing a UUID with a str raises
instead. Dict and set lookups don't compare unless hashes collide, so the guard can't see a
UUID key looked up with text (or the reverse); those are converted at the edge.
"""

from __future__ import annotations

import re
import uuid

pytest_plugins = ["minty_db.testing.pg"]

_uuid_eq = uuid.UUID.__eq__
# Only uuid-shaped text is a missed conversion; Django itself compares a lookup value with "".
_UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")


def _strict_eq(self: uuid.UUID, other: object):
    if isinstance(other, str) and _UUID_TEXT.fullmatch(other.strip("{}")):
        raise TypeError(f"comparing UUID {self} with the str {other!r}: convert one side first")
    return _uuid_eq(self, other)


uuid.UUID.__eq__ = _strict_eq  # type: ignore[method-assign]
