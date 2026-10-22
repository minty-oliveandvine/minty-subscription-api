"""Repo-root pytest hooks: Postgres test mode (MINTY_TEST_PG_URI set) builds the test database from
Minty's ``docs/schema/01_schema_rebased.sql`` and hands it to pytest-django
(``minty_shared.testing.pg``). Unset, it does nothing and the SQLite path in
``config/settings_test.py`` applies."""

pytest_plugins = ["minty_shared.testing.pg"]
