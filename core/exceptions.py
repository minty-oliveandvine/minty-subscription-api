"""Error shape for the billing API.

THE BODY KEY IS ``error``, NOT ``detail``.

This service takes over Flask's subscription routes byte for byte (``routes/portal.py``,
the module actions in ``entity/routes/settings.py``, the billing routes in
``entity/routes/create.py``), and every one of them answers ``{"error": "<sentence>"}``:
``lib/payerPortal.ts`` reads ``body.error`` at every call site, and the module page's
inline scripts render ``data.error`` straight into the toast. billing-backend's ``detail``
would reach them as an undefined message and a blank toast. Same choice, and the same
reasoning, as onboarding-backend's ``core/exceptions.py``.

Money errors keep Flask's status codes, because the clients branch on them: a declined
card is 402, missing billing consent is 403, a company that is not on your billing
account is 404, a double purchase is 409. Collapsing them would turn "your card was
declined" into "something went wrong".
"""

import logging

from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as SchemaValidationError

logger = logging.getLogger("billing-api")

# Shown when we have nothing specific and useful to say. Keep it cause-neutral: it fires
# for unknown reasons, so it must not assert one. Same sentence as the other two Django
# services and the frontends' fallback copy.
HOUSE_FALLBACK = "Something went wrong on my end. Mind trying again?"


class BillingError(Exception):
    """A subscription rule refused the request. Rendered with the status the rule names
    (default 400) and the message as-is - the message is user-facing copy, written for
    the toast it lands in."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PermissionDeniedError(Exception):
    """Authenticated, but not allowed to do this to this company. 403."""


class NotFoundError(Exception):
    """The named row does not exist, or is not the caller's to see. 404."""


class UpstreamError(Exception):
    """A call to Flask (``core.flask_client``) or to Stripe failed.

    Carries the status the upstream answered with, so a Flask 403 on an invitation or a
    Stripe card error reaches the client as itself rather than flattened into a 500.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# django-ninja puts the param source in the first segment of ``loc`` and, for a body
# model, the schema argument name in the second. Neither means anything to the person
# reading the message.
_LOC_NOISE = frozenset({
    "body", "query", "path", "form", "header", "cookie", "file",
    "payload", "data", "request",
})


def _field_names(errors):
    """Readable field names from ninja's ``loc`` tuples, in order, deduplicated."""
    names = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        parts = [str(p) for p in (err.get("loc") or ()) if not isinstance(p, bool)]
        parts = [p for p in parts if not p.isdigit()]
        # Strip the source/wrapper segments. If that leaves nothing, the loc named no
        # real field, so say nothing rather than naming 'body'.
        meaningful = [p for p in parts if p not in _LOC_NOISE]
        if not meaningful:
            continue
        name = meaningful[-1].replace("_", " ").strip()
        if name and name not in names:
            names.append(name)
    return names


def _join(names):
    if len(names) == 1:
        return names[0]
    return "{} and {}".format(", ".join(names[:-1]), names[-1])


def validation_message(errors):
    """Turn ninja's ``[{type, loc, msg}, ...]`` into a single sentence.

    Lifted from billing-backend/core/exceptions.py, which solved this for the same reason:
    whatever reaches a user has to be a sentence, not serialised JSON in a toast.
    """
    names = _field_names(errors)
    if not names:
        return "Some of those details didn't look right. Mind checking them?"
    joined = _join(names)
    if all(str(e.get("type", "")).startswith("missing")
           for e in errors if isinstance(e, dict)):
        return f"I still need {joined} to continue."
    return f"Mind checking {joined}? That didn't look quite right."


def register_exception_handlers(api):
    @api.exception_handler(AuthenticationError)
    def on_unauthorized(request, exc):
        """401 with ``{"error": "Unauthorized"}`` - ninja's default says ``detail``.

        An auth failure happens BEFORE the endpoint is reached, so no handler below sees
        it; without this one the commonest status the portal meets - an expired token -
        arrives in a shape ``payerPortal.ts`` cannot read. Flask's ``_unauthorized``
        answers ``{"error": ...}`` on exactly this path. Matched.
        """
        return api.create_response(request, {"error": "Unauthorized"}, status=401)

    @api.exception_handler(SchemaValidationError)
    def on_schema_validation(request, exc):
        # The structured errors stay in the log; the body carries the sentence. 400, as
        # every hand-written validation failure in Flask's subscription routes is.
        logger.warning("Schema validation error: %s", exc.errors)
        return api.create_response(
            request, {"error": validation_message(exc.errors)}, status=400
        )

    @api.exception_handler(BillingError)
    def on_billing(request, exc):
        logger.warning("Billing rule refused (%s): %s", exc.status, str(exc))
        return api.create_response(request, {"error": str(exc)}, status=exc.status)

    @api.exception_handler(PermissionDeniedError)
    def on_permission_denied(request, exc):
        logger.warning("Permission denied: %s", str(exc))
        return api.create_response(request, {"error": str(exc)}, status=403)

    @api.exception_handler(NotFoundError)
    def on_not_found(request, exc):
        logger.warning("Not found: %s", str(exc))
        return api.create_response(request, {"error": str(exc)}, status=404)

    @api.exception_handler(UpstreamError)
    def on_upstream(request, exc):
        logger.warning("Upstream error (%s): %s", exc.status, str(exc))
        return api.create_response(request, {"error": str(exc)}, status=exc.status)

    @api.exception_handler(Exception)
    def on_unhandled(request, exc):
        logger.exception("Unhandled error: %s", str(exc))
        return api.create_response(request, {"error": HOUSE_FALLBACK}, status=500)
