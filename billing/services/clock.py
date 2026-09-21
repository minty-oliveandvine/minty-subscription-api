"""Trusted clock for subscription access / grace decisions.

Module access windows (past-due grace, the in-app paid-cancel ``app_access_until``) are
enforced by comparing a stored, absolute end date against "now". If "now" were the app
host's wall clock, a wrong or shifted system clock could wrongly grant or revoke access.

So those decisions call :func:`now` rather than ``datetime.now()``. It answers from the
first available of three sources:

1. **Stripe's** server time, captured from the ``Date`` header of a fetch made during
   this request. The same clock Stripe's own timestamps come from, so comparisons
   against them are exact.
2. **The database's** clock — one ``SELECT now()`` against Postgres, cached per request.
3. The process clock, as a last resort.

Source 2 exists because source 1 is a SIDE EFFECT of calls made for other reasons, and
those calls can move. It has already happened once: source 1 used to hang off
``stripe_client.list_customer_subscriptions``, and when the in-house billing cutover
deleted that function the only writer of Stripe's time went with it, silently. The
capture now sits on three surviving reads instead of one — ``retrieve_customer``,
``list_payment_methods`` and ``find_customer_by_user`` — so no single deletion can
quietly retire it again. If they ALL go, source 2 still holds the line.

The database is the right second source: it is a round trip already being made, on
infrastructure under the same control, and it is shared by every app instance — so two
servers with differently-drifted clocks still agree, which per-host time cannot promise.

All values are cached on the request scope (``billing.services._context`` - what Flask's
``g`` was), so they are per-request/per-command/per-pass and thread-safe (each scope has its
own dict). Outside a scope nothing is cached, and ``database_now`` is skipped, as Flask's was
outside an app context.
"""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from billing.services import _context

_G_KEY = "_subscription_server_now"
_G_DB_KEY = "_subscription_database_now"


def _parse_http_date(value: str | None) -> datetime | None:
    """Parse an HTTP ``Date`` header (RFC 7231) to an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def record_http_date(date_header: str | None) -> None:
    """Record Stripe's server time (a response ``Date`` header) for this context so
    later :func:`now` calls use it. No-op outside an app context or on a bad header."""
    dt = _parse_http_date(date_header)
    if dt is not None and _context.active():
        _context.set(_G_KEY, dt)


def database_now() -> datetime | None:
    """The DATABASE's current time, cached for this request. None if unavailable.

    Deliberately quiet on failure: a clock lookup must never be the thing that breaks a
    request. The caller falls through to the process clock, which is worse but working.
    """
    if not _context.active():
        return None
    cached = _context.get(_G_DB_KEY)
    if cached is not None:
        return cached
    try:
        # Imported here, not at module scope: this module is imported by the Stripe
        # client, and importing the database layer at import time is what the original
        # avoided too.
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT now()")
            row = cursor.fetchone()
        value = row[0] if row else None
    except Exception:  # noqa: BLE001 - a clock lookup must never break a request
        # SQLite has no now(); the process clock takes over, as it did for Flask's
        # test database.
        return None
    if value is None:
        return None
    if isinstance(value, str):  # a driver that hands text back
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    _context.set(_G_DB_KEY, value)
    return value


def now() -> datetime:
    """Trusted current UTC time for access/grace decisions.

    Stripe's captured server time, else the database's, else the process clock — see
    the module docstring for why there are three.

    The order matters, though the REASON for it changed with the in-house billing
    cutover. It used to be coherence: every date being compared against came from Stripe,
    so Stripe's clock was the one that produced them. Those dates now come from our own
    Postgres, so that argument belongs to the database instead.

    Stripe still goes first for a different and still-good reason: it is FREE. Its time
    rode in on a call already made, whereas source 2 costs a ``SELECT now()``. Both are
    externally-disciplined clocks that agree to well inside the day-granularity these
    decisions turn on, so taking the one already in hand loses nothing. What neither may
    degrade to unnoticed is the host's own wall clock, which is the whole point.
    """
    if _context.active():
        recorded = _context.get(_G_KEY)
        if recorded is not None:
            return recorded
        from_db = database_now()
        if from_db is not None:
            return from_db
    return datetime.now(UTC)
