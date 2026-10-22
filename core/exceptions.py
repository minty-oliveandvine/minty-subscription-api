"""This service's refusals, rendered in the one envelope every Minty API answers with
(``minty_shared.django.errors``): ``{"error": "<sentence>"}``, never ``detail`` - the payer
portal reads ``body.error`` at every call site, as it read Flask's.

Money errors keep Flask's status codes, because the clients branch on them: a declined card is
402, missing billing consent is 403, a company that is not on your billing account is 404, a
double purchase is 409. Collapsing them would turn "your card was declined" into "something went
wrong".
"""

import logging

from minty_shared.django.errors import HOUSE_FALLBACK, register_error_handlers

__all__ = [
    "HOUSE_FALLBACK",
    "BillingError",
    "NotFoundError",
    "PermissionDeniedError",
    "UpstreamError",
    "register_exception_handlers",
]

logger = logging.getLogger("billing-api")


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


def register_exception_handlers(api):
    register_error_handlers(
        api,
        logger=logger,
        refusals={
            BillingError: lambda exc: exc.status,
            PermissionDeniedError: 403,
            NotFoundError: 404,
            UpstreamError: lambda exc: exc.status,
        },
    )
