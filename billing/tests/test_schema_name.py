"""The schema name is a setting: ``config.settings.DB_SCHEMA`` (``?schema=`` on ``DATABASE_URL``,
the same URL Minty reads; config/dburl.py). No application string may carry it; ``search_path`` and the raw
queries read the setting. Comments and docstrings are free to say it."""

from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings

ROOT = Path(__file__).resolve().parents[2]
NAME = "pettycashv3"
ALLOWED = {"config/settings.py", "config/dburl.py"}
SKIP = ("tests/", "billing/tests/", "e2e/", "migrations/")


def _docstrings(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_no_application_string_carries_the_schema_name():
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or any(s in rel for s in SKIP) or ".venv" in rel or "node_modules" in rel:
            continue
        source = path.read_text(encoding="utf-8-sig")
        if NAME not in source:
            continue
        tree = ast.parse(source)
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and NAME in node.value and id(node) not in docs:
                hits.append(f"{rel}:{node.lineno}")
    assert hits == [], "read settings.DB_SCHEMA instead of spelling the schema"


def test_search_path_follows_the_setting():
    # The production DATABASES (config.settings), not the test override: on SQLite there is
    # no search_path to set, and the point is that the deployed connection follows the setting.
    from config import settings as production

    assert settings.DB_SCHEMA in production.DATABASES["default"]["OPTIONS"]["options"]
    assert production.DB_SCHEMA == settings.DB_SCHEMA
