"""The only module in this service that talks to the Flask app.

WHAT STILL LIVES IN FLASK, AND WHY THIS SERVICE HAS TO CALL IT

Part 2 moves the subscription engine here whole; Flask keeps identity and the
company (Part 3 moves those). Two things the portal does are the company's, not the
subscription's, so they stay behind Flask's routes until then:

  * ``POST /api/me/subscriptions/invite-admin`` - inviting an administrator to a company
    you pay for. The invitation row, the email and the accept link are Flask's
    (``blueprints/user_management``), so this service forwards the call to
    ``POST /api/onboarding/invite`` there.
  * nothing else yet. The list is meant to shrink, never grow: a new Flask call anywhere
    else in this repo is a boundary violation, and concentrating them here means the
    rule is checked by reading one file.

HOW IT AUTHENTICATES

It forwards the caller's own bearer token unchanged. The proxy carries no privilege of
its own, so Flask applies exactly the membership checks it would have applied to a direct
call, and a caller cannot reach through this service to do something Flask would have
refused. There is no service-to-service credential here to leak or to scope wrongly.
(Lifted from onboarding-backend/core/minty_client.py, which made the same choice.)
"""

import logging

import requests
from django.conf import settings
from django.http import JsonResponse

from core.exceptions import HOUSE_FALLBACK, UpstreamError

logger = logging.getLogger("billing-api")

#: What we say when Flask is unreachable or answers in a shape we cannot read. Cause-
#: neutral on purpose: the caller cannot act on "upstream 502" and the portal renders
#: whatever is in ``error`` straight into a toast.
UNREACHABLE = HOUSE_FALLBACK


def _url(path: str) -> str:
    return f"{settings.PETTY_CASH_URL}/{path.lstrip('/')}"


def bearer_from(request) -> str:
    """The caller's Authorization header, verbatim.

    Raises rather than returning empty: every path that forwards has already been through
    ``BearerAuth``, so a missing header here is a programming error in this service.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise UpstreamError(UNREACHABLE, status=500)
    return header


def forward(request, path: str, *, method: str = "POST", json=None, params=None):
    """Call Flask as the current caller and return ``(payload, status)``.

    The status is Flask's own, not flattened: a 403 on an invitation the caller may not
    send and a 409 on one that already exists mean something specific to the portal.
    """
    url = _url(path)
    headers = {
        "Authorization": bearer_from(request),
        "Accept": "application/json",
    }
    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=json,
            params=params,
            timeout=settings.FLASK_PROXY_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("Flask timeout: %s %s", method.upper(), url)
        raise UpstreamError(UNREACHABLE, status=504) from None
    except requests.RequestException as exc:
        logger.warning("Flask call failed: %s %s -- %s", method.upper(), url, exc)
        raise UpstreamError(UNREACHABLE, status=502) from None

    try:
        payload = resp.json()
    except ValueError:
        # An HTML error page or a login redirect body. Do not pass the HTML on: the
        # portal would render markup in a toast.
        logger.warning(
            "Flask returned non-JSON: %s %s -> %s", method.upper(), url, resp.status_code
        )
        raise UpstreamError(UNREACHABLE, status=502) from None

    return payload, resp.status_code


def proxy(request, path: str, *, method: str = "POST", json=None, params=None):
    """:func:`forward`, wrapped as a ``JsonResponse`` a ninja view can return directly.

    An ``HttpResponse`` sidesteps ninja's response-model machinery, so Flask's status and
    body reach the client exactly as sent - which is the entire job of a proxy.
    """
    payload, status = forward(request, path, method=method, json=json, params=params)
    return JsonResponse(payload, status=status, safe=False)
