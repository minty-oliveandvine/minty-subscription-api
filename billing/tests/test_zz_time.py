"""The default zone is spelled once, in ``minty_shared.time.DEFAULT_ENTITY_TIMEZONE``: no
application string here names it, so a company is never dated in a zone someone typed."""

from __future__ import annotations

from pathlib import Path

from minty_shared.testing import guards

ROOT = Path(__file__).resolve().parents[2]


def test_no_application_string_names_the_default_zone():
    hits = guards.string_literals(ROOT, "Asia/Hong_Kong", skip=("tests/", "billing/tests/", "e2e/", "migrations/"))
    assert hits == [], "use minty_shared.time.DEFAULT_ENTITY_TIMEZONE"
