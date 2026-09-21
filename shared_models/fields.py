"""A CharField that declares which Postgres enum type backs it.

``models.CharField(choices=...)`` says nothing about the column's database type, so the
schema audit (Minty/docs/schema/generators/audit_models.py) could not tell an enum column
from a varchar. ``PgEnumField("system_role", choices=SystemRole.choices)`` names the type;
the audit reads the first positional argument. At runtime it is an ordinary CharField —
psycopg2 sends the value as text and Postgres casts it to the enum, rejecting unknown words.
"""

from django.db import models


class CharNField(models.CharField):
    """``char(n)`` in Postgres (``country_code CHAR(2)``, ``currency_code CHAR(3)``); an
    ordinary CharField everywhere else. The audit maps it to ``character``."""

    def db_type(self, connection):
        if connection.vendor == "postgresql":
            return f"char({self.max_length})"
        return super().db_type(connection)


class PgEnumField(models.CharField):
    def __init__(self, pg_type: str, *args, **kwargs):
        self.pg_type = pg_type
        kwargs.setdefault("max_length", 40)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, path, [self.pg_type, *args], kwargs
