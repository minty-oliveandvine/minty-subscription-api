"""Field types the mirrors need beyond Django's own.

``PgEnumField`` names the Postgres enum type behind a text column so the schema audit
(Minty/docs/schema/generators/audit_models.py) can tell an enum from a varchar; ``CharNField``
is ``char(n)``; ``MintyUUIDField`` hands uuids back as text, which is what the whole engine was
written against.
"""

import uuid

from django.db import models


class CharNField(models.CharField):
    """``char(n)`` in Postgres (``country_code CHAR(2)``, ``currency_code CHAR(3)``); an
    ordinary CharField everywhere else. The audit maps it to ``character``."""

    def db_type(self, connection):
        if connection.vendor == "postgresql":
            return f"char({self.max_length})"
        return super().db_type(connection)


class PgEnumField(models.CharField):
    """A text column backed by a Postgres enum type. The first positional argument names the
    type; the audit reads it. At runtime it is an ordinary CharField - the driver sends the
    value as text and Postgres casts it to the enum, rejecting unknown words."""

    def __init__(self, pg_type: str, *args, **kwargs):
        self.pg_type = pg_type
        kwargs.setdefault("max_length", 40)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, path, [self.pg_type, *args], kwargs


class MintyUUIDField(models.UUIDField):
    """A ``uuid`` column whose Python value is the hyphenated lowercase STRING.

    Flask declares every uuid column ``MintyUuid(as_uuid=False)`` (``blueprints/shared/
    column_types.py``), so the subscription engine compares ids with ``==``, uses them as dict
    keys and builds dedupe keys with f-strings - all against ``str``. Django's ``UUIDField``
    returns ``uuid.UUID``, and ``uuid.UUID(x) == "x"`` is False: a plain UUIDField would make
    hundreds of ported comparisons fail silently. This field keeps the column type (the
    audit maps it to ``uuid``; ``get_internal_type`` still says ``UUIDField``, so the SQLite
    backend's hex converter runs first) and converts on the way out.

    On the way IN it accepts ``str``, ``UUID`` or None; a malformed string raises Django's
    ``ValidationError`` (Postgres would have raised ``DataError``; callers that must answer
    "no such row" for garbage catch it - see ``billing.services.store._by_pk``).

    A value ASSIGNED in memory is not normalised (``obj.id = uuid4()`` stays a ``UUID`` until
    the row is read back), so fixtures and services pass ``str(uuid.uuid4())``; the domain
    tables' primary keys default to exactly that.
    """

    def to_python(self, value):
        value = super().to_python(value)
        return None if value is None else str(value)

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(super().to_python(value))

    def get_db_prep_value(self, value, connection, prepared=False):
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = super().to_python(value)
        return super().get_db_prep_value(value, connection, prepared)


def new_id() -> str:
    """A fresh primary key the way Flask makes them: ``str(uuid.uuid4())``."""
    return str(uuid.uuid4())
