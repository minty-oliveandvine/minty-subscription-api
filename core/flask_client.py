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

from django.conf import settings
from django.http import JsonResponse
from minty_shared.django import client as shared_client

from core.exceptions import HOUSE_FALLBACK, UpstreamError

#: What the client is told when Flask cannot be reached or answers with something that is
#: not JSON. Cause-neutral on purpose; the house sentence has exactly one definition.
UNREACHABLE = HOUSE_FALLBACK


def bearer_from(request) -> str:
    """The caller's Authorization header, verbatim (``minty_shared.django.client``)."""
    try:
        return shared_client.bearer_from(request)
    except shared_client.UpstreamUnavailable as exc:
        raise UpstreamError(exc.message, status=exc.status) from None


def forward(request, path: str, *, method: str = "POST", json=None, params=None):
    """Call Flask as the current caller and return ``(payload, status)``.

    The status is Flask's own, not flattened: a 403 on an invitation the caller may not
    send and a 409 on one that already exists mean something specific to the portal. The
    transport, the trace header and the 502/504 for no usable answer are
    ``minty_shared.django.client``'s.
    """
    try:
        return shared_client.forward(
            request,
            path,
            base=settings.PETTY_CASH_URL,
            timeout=settings.FLASK_PROXY_TIMEOUT,
            method=method,
            json=json,
            params=params,
        )
    except shared_client.UpstreamUnavailable as exc:
        raise UpstreamError(exc.message, status=exc.status) from None


def proxy(request, path: str, *, method: str = "POST", json=None, params=None):
    """:func:`forward`, wrapped as a ``JsonResponse`` a ninja view can return directly.

    An ``HttpResponse`` sidesteps ninja's response-model machinery, so Flask's status and
    body reach the client exactly as sent - which is the entire job of a proxy.
    """
    payload, status = forward(request, path, method=method, json=json, params=params)
    return JsonResponse(payload, status=status, safe=False)
