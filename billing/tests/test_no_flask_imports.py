"""Nothing under this service imports the Flask world.

The engine was ported from ``Minty/blueprints/subscription/services`` line by line, and a
verbatim copy carries its imports with it: ``flask.g``, ``flask_mail``, ``loguru``, the
SQLAlchemy ``db`` handle, ``blueprints.*``. None of those packages is installed here, so a
leftover would fail at import - but only when that module is first imported, which for a
function-local import can be months after the port. This walks the source instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("billing", "core", "shared_models", "config")

FORBIDDEN_ROOTS = {
    "flask",
    "flask_mail",
    "flask_sqlalchemy",
    "flask_migrate",
    "flask_login",
    "flask_wtf",
    "sqlalchemy",
    "alembic",
    "loguru",
    "werkzeug",
    "jinja2",  # templates render through django.template.loader (the Jinja2 backend)
    "models",  # Flask's ``models.db``
    "blueprints",
    "services",  # Flask's ``services.permission_policy`` -> core.policy here
    "cli",
}


def _python_files():
    for package in PACKAGES:
        yield from sorted((ROOT / package).rglob("*.py"))


def _imported_roots(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module.split(".")[0]


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_flask_world_import(path: Path):
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno} imports {root}"
        for lineno, root in _imported_roots(path)
        if root in FORBIDDEN_ROOTS
    ]
    assert not offenders, "\n".join(offenders)


def test_the_walk_saw_the_engine():
    files = {str(p.relative_to(ROOT)).replace("\\", "/") for p in _python_files()}
    assert "billing/services/store.py" in files
    assert "shared_models/models.py" in files
