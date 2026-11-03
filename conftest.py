"""Repo-root pytest hooks: minty-db's two plugins.

``minty_db.testing.pg``: with MINTY_TEST_PG_URI set, the test database is built from minty-db's
packaged ``01_schema_rebased.sql``, so the models run against the real schema; unset, the SQLite
path in ``config/settings_test.py`` applies.

``minty_db.testing.uuid_guard``: ids are ``uuid.UUID`` while paths, token claims and JSON carry
text; under test, this service's code comparing a UUID with uuid text raises instead of quietly
not matching.
"""

pytest_plugins = ["minty_db.testing.pg", "minty_db.testing.uuid_guard"]
