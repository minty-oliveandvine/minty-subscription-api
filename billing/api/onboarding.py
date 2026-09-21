"""The wizard's money routes, and the trial start.

The nine ``/api/onboarding/{payment-method*,billing/*}`` routes of Flask's
``entity/routes/create.py`` (lines 757-1247), which onboarding-backend proxies here from
Part 2 step 5 (``core/billing_client.py`` there, forwarding the caller's bearer). Plus one
new route, ``POST /trials/start {entity_id} -> {trial_end}``: onboarding-backend's native
``finalize`` flips the company live and then calls this; **it must not fail silently** - a
failure here fails finalize, the All Set screen offers Try again, and both halves are
idempotent (a trial already started is returned, not duplicated).
"""

from ninja import Router

from billing.api._stub import not_implemented

onboarding_router = Router()

ROUTES = (
    ("GET", "/payment-method"),
    ("POST", "/payment-method/setup"),
    ("POST", "/payment-method/complete"),
    ("GET", "/billing/payment-methods"),
    ("POST", "/billing/payment-methods/setup-intent"),
    ("POST", "/billing/payment-methods/confirm"),
    ("POST", "/billing/payment-methods/default"),
    ("GET", "/billing/accounts"),
    ("POST", "/billing/accounts"),
    ("POST", "/billing/authorize"),
    ("POST", "/trials/start"),  # new in Part 2
)

for _method, _path in ROUTES:
    onboarding_router.add_api_operation(
        _path,
        [_method],
        not_implemented,
        operation_id="onboarding_" + _method.lower() + _path.replace("/", "_").replace("-", "_"),
        summary=f"{_method} /api/onboarding{_path} (stub)",
    )
