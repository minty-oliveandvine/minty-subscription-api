"""The schema name is a setting: ``config.settings.DB_SCHEMA`` (``?schema=`` on ``DATABASE_URL``,
the same URL Minty reads; minty_shared.config). No application string may carry it; ``search_path`` and the raw
queries read the setting. Comments and docstrings are free to say it."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from minty_shared.testing import guards

ROOT = Path(__file__).resolve().parents[2]


def test_no_application_string_carries_the_schema_name():
    hits = guards.string_literals(ROOT, "pettycashv3", skip=("tests/", "billing/tests/", "e2e/", "migrations/"))
    assert hits == [], "read settings.DB_SCHEMA instead of spelling the schema"


def test_search_path_follows_the_setting():
    # The production DATABASES (config.settings), not the test override: on SQLite there is
    # no search_path to set, and the point is that the deployed connection follows the setting.
    from config import settings as production

    assert settings.DB_SCHEMA in production.DATABASES["default"]["OPTIONS"]["options"]
    assert production.DB_SCHEMA == settings.DB_SCHEMA
