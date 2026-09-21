"""In-memory stand-ins for a model's ``objects`` manager.

Minty's suite drove the read models off ``SimpleNamespace(query=...)`` fakes shaped like
SQLAlchemy's ``Model.query``; the ported services read ``Model.objects`` instead, and this is
the same idea in that shape. ``fake_model(rows)`` answers the handful of queryset calls the
engine makes - ``filter`` (``field=value``, ``field__in=values``, ``pk`` as ``id``),
``order_by`` (``"field"`` / ``"-field"``), ``first``, ``get``, ``exists``, ``count``,
``values_list``, ``in_bulk``, ``prefetch_related`` (a no-op), ``none``, iteration and
slicing - over plain rows. A row that lacks a filtered attribute MATCHES (the fakes of
Minty's suite answered any id with the one row they held), which keeps the ported tests'
one-row fakes working.
"""

from __future__ import annotations

from types import SimpleNamespace


class DoesNotExist(Exception):
    pass


class MultipleObjectsReturned(Exception):
    pass


class FakeQuerySet:
    def __init__(self, rows, model=None):
        self._rows = list(rows)
        self.model = model

    # --- filtering ------------------------------------------------------------------
    def _match(self, row, key, value):
        field, _, op = key.partition("__")
        if field == "pk":
            field = "id"
        if not hasattr(row, field):
            return True
        actual = getattr(row, field)
        if op == "in":
            return actual in set(value) or str(actual) in {str(v) for v in value}
        if op == "isnull":
            return (actual is None) is bool(value)
        if op in ("", "exact"):
            return actual == value or str(actual) == str(value)
        raise NotImplementedError(f"fake queryset: unsupported lookup {key!r}")

    def filter(self, *args, **kwargs):
        rows = [r for r in self._rows if all(self._match(r, k, v) for k, v in kwargs.items())]
        return FakeQuerySet(rows, self.model)

    def exclude(self, **kwargs):
        rows = [r for r in self._rows if not all(self._match(r, k, v) for k, v in kwargs.items())]
        return FakeQuerySet(rows, self.model)

    def order_by(self, *fields):
        rows = list(self._rows)
        for field in reversed(fields):
            reverse = field.startswith("-")
            name = field.lstrip("-")
            rows.sort(key=lambda r: (getattr(r, name, None) is None, getattr(r, name, None)), reverse=reverse)
        return FakeQuerySet(rows, self.model)

    def prefetch_related(self, *args, **kwargs):
        return self

    def select_related(self, *args, **kwargs):
        return self

    def annotate(self, **kwargs):
        return self

    def none(self):
        return FakeQuerySet([], self.model)

    def all(self):
        return FakeQuerySet(self._rows, self.model)

    # --- terminal reads -------------------------------------------------------------
    def first(self):
        return self._rows[0] if self._rows else None

    def get(self, **kwargs):
        rows = self.filter(**kwargs)._rows if kwargs else self._rows
        if not rows:
            raise DoesNotExist(kwargs)
        if len(rows) > 1:
            raise MultipleObjectsReturned(kwargs)
        return rows[0]

    def exists(self):
        return bool(self._rows)

    def count(self):
        return len(self._rows)

    def values_list(self, *fields, flat=False):
        if flat:
            return FakeQuerySet([getattr(r, fields[0]) for r in self._rows], self.model)
        return FakeQuerySet([tuple(getattr(r, f) for f in fields) for r in self._rows], self.model)

    def in_bulk(self, ids=None, *, field_name="id"):
        wanted = None if ids is None else {str(i) for i in ids}
        return {
            getattr(r, field_name): r
            for r in self._rows
            if wanted is None or str(getattr(r, field_name, "")) in wanted
        }

    def update(self, **fields):
        for row in self._rows:
            for key, value in fields.items():
                setattr(row, key, value)
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    def __bool__(self):
        return bool(self._rows)

    def __getitem__(self, item):
        result = self._rows[item]
        return FakeQuerySet(result, self.model) if isinstance(item, slice) else result


def fake_model(rows=(), *, name="FakeModel"):
    """A model-shaped object: ``.objects`` over ``rows``, plus ``DoesNotExist``."""
    model = SimpleNamespace(__name__=name, DoesNotExist=DoesNotExist, MultipleObjectsReturned=MultipleObjectsReturned)
    model.objects = FakeQuerySet(rows, model)
    return model
