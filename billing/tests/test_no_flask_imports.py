"""Nothing under this service imports the Flask world.

The engine was ported from ``Minty/blueprints/subscription/services`` line by line, and a
verbatim copy carries its imports with it: ``flask.g``, ``flask_mail``, ``loguru``, the
SQLAlchemy ``db`` handle, ``blueprints.*``. None of those packages is installed here, so a
leftover would fail at import - but only when that module is first imported, which for a
function-local import can be months after the port. The walk is every API's
(``minty_shared.testing.guards``); ``jinja2`` is banned here too, because templates render
through django.template.loader.
"""

from __future__ import annotations

from pathlib import Path

from minty_shared.testing import guards

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("billing", "core", "shared_models", "config")


def test_no_flask_world_import():
    assert guards.forbidden_imports(ROOT, PACKAGES, guards.FLASK_WORLD | {"jinja2"}) == []


def test_the_walk_saw_the_engine():
    files = {p.relative_to(ROOT).as_posix() for p in guards.python_files(ROOT, PACKAGES)}
    assert "billing/services/store.py" in files
    assert "shared_models/models.py" in files
