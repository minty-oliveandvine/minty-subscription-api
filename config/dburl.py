"""``DATABASE_URL`` -> Django's ``DATABASES["default"]`` and the schema name.

Plain Python, no Django import: ``docker/entrypoint.sh`` uses it before Django is loaded.
The same module lives in minty-payment-request-api and minty-onboarding-api.

    postgresql://user:pass@host:5432/dbname?schema=pettycashv3[&sslmode=require...]

``schema`` is popped from the query (default ``pettycashv3``) and becomes the connection's
``search_path``; it is never passed to the driver. Every other query parameter is kept, in
``OPTIONS``. User, password and database name are percent-decoded; the port defaults to 5432.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, unquote, urlsplit

DEFAULT_DATABASE_URL = "postgresql://postgres@localhost:5432/postgres"
DEFAULT_SCHEMA = "pettycashv3"
SCHEMES = ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg")


def database_url() -> str:
    """``DATABASE_URL`` from the environment, or the local default."""
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def parse_database_url(url: str) -> tuple[dict, str]:
    """``(db_dict, schema)`` for a Postgres URL. Raises ValueError on any other scheme."""
    parts = urlsplit(url)
    if parts.scheme not in SCHEMES:
        raise ValueError(f"DATABASE_URL must be a postgres URL, not {parts.scheme!r}")
    options = dict(parse_qsl(parts.query, keep_blank_values=True))
    schema = options.pop("schema", "") or DEFAULT_SCHEMA
    options["options"] = f"-c search_path={schema},public"
    db = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or 5432),
        "OPTIONS": options,
    }
    return db, schema
