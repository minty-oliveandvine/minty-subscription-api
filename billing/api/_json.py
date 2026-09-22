"""The JSON the routers answer with, in Flask's shape.

Every view here returns an ``HttpResponse`` built with :func:`respond` rather than a ninja
response model: the payloads are the dicts ``billing.services`` builds - Flask's
``jsonify(payload)`` was the whole view - and the clients (minty-web's ``payerPortal.ts``,
billing-frontend's notice fetch) read exactly that shape. Two things ``jsonify`` did that
Django's encoder does not:

* a ``datetime`` / ``date`` renders as an RFC 822 HTTP date (``Tue, 21 Sep 2026 12:00:00 GMT``),
  not ISO 8601 - the services hand a few back raw (``transfers._as_dict``'s ``expires_at`` /
  ``billed_through``, the subscriber screen's ``since``), and a client parsing the Flask form
  must go on parsing it;
* ``Decimal`` renders as a string, ``UUID`` as its hyphenated text.

``body(request)`` is ``request.get_json(silent=True) or {}``: a body that is missing, not
JSON or not an object is an empty dict, never a 400 - the routes decide what is required.

``IsoJSONEncoder`` is the exception for a surface Flask never served as JSON (the module
settings page, Jinja in Flask): there the wire format is the one its client declared, ISO 8601.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from email.utils import format_datetime

from django.http import HttpResponse, JsonResponse


def http_date(moment: datetime | date) -> str:
    """Flask's ``http_date``: aware instants in GMT, naive ones taken as UTC, dates at midnight."""
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return format_datetime(moment.astimezone(UTC), usegmt=True)
    return format_datetime(datetime.combine(moment, time.min, tzinfo=UTC), usegmt=True)


class FlaskJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return http_date(o)
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, (set, frozenset, tuple)):
            return list(o)
        return super().default(o)


class IsoJSONEncoder(FlaskJSONEncoder):
    """The same, with ISO 8601 for datetimes and dates.

    For a surface Flask never served as JSON - the module settings page was Jinja - the wire
    format is the one its only client declared: minty-web's ``ModuleCard`` types ``period_end``
    as an ISO datetime and computes days-remaining from its ``YYYY-MM-DD`` prefix, which an RFC
    822 date would silently fail (``daysUntil`` answers null, the card says "Active").
    """

    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def respond(payload, status: int = 200, *, encoder=FlaskJSONEncoder) -> HttpResponse:
    """``jsonify(payload), status`` - lists allowed (``safe=False``), Flask's encoder unless
    the router hands over another."""
    return JsonResponse(payload, status=status, safe=False, encoder=encoder)


def error(message: str, status: int) -> HttpResponse:
    """The house error body, ``{"error": <sentence>}`` (core/exceptions.py's shape)."""
    return respond({"error": message}, status)


def body(request) -> dict:
    """The JSON object the request carries, or ``{}`` - Flask's ``get_json(silent=True) or {}``."""
    raw = request.body
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def int_arg(request, name: str, default: int) -> int:
    """A positive integer query arg. Junk falls back to the default rather than 400ing -
    paging is navigation, and a mangled ``?page=`` should show page one, not an error."""
    try:
        value = int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def user_id(request) -> str:
    """The authenticated person, as the services want it (a hyphenated str)."""
    return str(request.auth_user.id)
