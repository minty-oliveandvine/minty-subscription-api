"""``manage.py export_openapi`` - write the API's OpenAPI document to ``docs/openapi.json``.

The committed copy is the contract other repos read (Part 3's type generation starts there),
so it must never drift from what ``/api/openapi.json`` serves: ``billing/tests/test_contract.py``
compares the two and fails with this command's name when they differ. Needs no database - the
document is built from the routers alone."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

#: Repo root / docs / openapi.json.
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"


def render() -> str:
    """The document as the file holds it: stable key order, two-space indent, a final newline."""
    from config.urls import api

    schema = api.get_openapi_schema()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class Command(BaseCommand):
    help = "Write /api/openapi.json to docs/openapi.json (the committed contract)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=str(DEFAULT_PATH), help="where to write (default docs/openapi.json)")
        parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale, write nothing")

    def handle(self, *args, path: str, check: bool, **options):
        target = Path(path)
        document = render()
        if check:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != document:
                self.stderr.write(f"{target} is stale - run: manage.py export_openapi")
                raise SystemExit(1)
            self.stdout.write(f"{target} is current")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8", newline="\n")
        self.stdout.write(f"wrote {target} ({len(document)} bytes)")
