"""The payer portal: the fifteen ``/api/me/*`` routes of Flask's ``routes/portal.py``.

Person-scoped (``SelfBearerAuth``): every row is found by the caller's ``user_id``, never
by the company in the token or the ``X-Entity-Id`` header. Paths, methods, JSON and status
codes are Flask's byte for byte, so minty-web's ``features/subscription/api/payerPortal.ts``
is billing-frontend's ``lib/payerPortal.ts`` with a new base URL.

Part 2 step 3 replaces each stub with the port of the matching Flask view over
``billing/services``; step 2 ports the services first.
"""

from ninja import Router

from billing.api._stub import not_implemented
from core.auth import SelfBearerAuth

me_router = Router(auth=SelfBearerAuth())

#: (method, path) - the contract, also walked by billing/tests/test_dark.py and e2e/.
ROUTES = (
    ("GET", "/subscriptions"),
    ("GET", "/subscriptions/subscriber-options"),
    ("POST", "/subscriptions/invite-admin"),  # forwards to Flask (core/flask_client.py)
    ("POST", "/subscriptions/transfer"),
    ("POST", "/subscriptions/transfer/respond"),
    ("POST", "/subscriptions/transfer/cancel"),
    ("GET", "/subscriptions/transfers"),
    ("GET", "/invoices"),
    ("GET", "/billing/payment-methods"),
    ("POST", "/billing/payment-methods/setup-intent"),
    ("POST", "/billing/payment-methods/confirm"),
    ("POST", "/billing/payment-methods/default"),
    ("GET", "/billing/entity-payment-method"),
    ("POST", "/billing/entity-payment-method"),
    ("POST", "/billing/payment-methods/update"),
    ("POST", "/billing/payment-methods/remove"),
)

for _method, _path in ROUTES:
    me_router.add_api_operation(
        _path,
        [_method],
        not_implemented,
        operation_id="me_" + _method.lower() + _path.replace("/", "_").replace("-", "_"),
        summary=f"{_method} /api/me{_path} (stub)",
    )
