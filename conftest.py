"""Repo-root pytest hooks.

Postgres test mode (MINTY_TEST_PG_URI set): build the test database from the Minty repo's
``docs/schema/01_schema_rebased.sql`` with Minty's ``tests/pg_harness.py`` and hand it to
pytest-django instead of letting it create one. Three codebases, one schema build — the
mirrors in ``shared_models`` are then checked against the real database, which SQLite
(tables built from the models) can never do.

Unset, this file does nothing and the SQLite path in ``config/settings_test.py`` applies.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

if os.environ.get("MINTY_TEST_PG_URI"):

    def _load_harness():
        minty = Path(os.environ.get("MINTY_REPO", r"C:\Github\Minty"))
        path = minty / "tests" / "pg_harness.py"
        if not path.exists():
            raise RuntimeError(f"Minty repo not found at {minty} (set MINTY_REPO); need {path}")
        spec = importlib.util.spec_from_file_location("minty_pg_harness", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
        spec.loader.exec_module(mod)
        return mod

    @pytest.fixture(scope="session")
    def django_db_setup(django_db_blocker):
        """Replace pytest-django's database creation with the schema-file build."""
        from django.conf import settings
        from django.db import connections

        harness = _load_harness()
        built = harness.build()
        try:
            for alias in connections:
                connections[alias].close()
            settings.DATABASES["default"]["NAME"] = built.dbname
            connections["default"].settings_dict["NAME"] = built.dbname
            connections["default"].settings_dict["TEST"]["NAME"] = built.dbname
            with django_db_blocker.unblock():
                with connections["default"].cursor() as cur:
                    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=%s", [settings.DB_SCHEMA])
                    (n,) = cur.fetchone()
                    assert n >= 50, f"harness database has only {n} tables"
            yield
        finally:
            for alias in connections:
                connections[alias].close()
            built.drop()
