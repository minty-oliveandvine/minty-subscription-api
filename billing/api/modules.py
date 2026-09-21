"""The module settings page: one page model and nineteen actions per company.

Company-scoped (``BearerAuth``, the API default): the caller must hold a role on the
entity, resolved from the token's ``entity_id`` or the ``X-Entity-Id`` header (the page
reached from the portal carries an unscoped token and sends the header). Reading the page
needs ``MODULE_VIEW`` (cashier and up), every action ``MODULE_MANAGE`` (admin and up) AND
``store.may_manage_subscription`` - the ``@require_subscription_payer`` port: only the
payer, or a member with billing consent on a company that has no payer yet, may act.

Flask served this page from Jinja (``templates/entity/partials/module_*.html``) with the
actions as ``POST /entity/settings/module/<org_id>/<action>`` in ``entity/routes/settings.py``
(lines 1419-2431). minty-web renders it now, from ``GET /api/entities/{id}/modules``: the
cards and their states, the summary, the panel, the next payment, ``can_manage_modules``,
the payer and the consent-takeover prompt.
"""

from ninja import Router

from billing.api._stub import not_implemented
from core.auth import EntityBearerAuth

modules_router = Router(auth=EntityBearerAuth())

#: The nineteen action names, Flask's spelling. ``POST /api/entities/{id}/modules/{action}``
#: with any other word is a 404 from step 3 on (a stub answers 501 for all of them today).
ACTIONS = (
    "checkout",
    "authorize-billing",
    "payment-methods",
    "payment-methods/setup-intent",
    "payment-methods/confirm",
    "payment-methods/default",
    "restart-quote",
    "restart-billing",
    "confirm-billing",
    "checkout-complete",
    "start-trial",
    "resume-preview",
    "subscribe-preview",
    "cancel-preview",
    "retry-payment",
    "cancel",
    "payment-method",
    "renew",
    "manage-billing",
)


@modules_router.get("/{entity_id}/modules", summary="The page model (stub)")
def module_page(request, entity_id: str):
    return not_implemented(request)


@modules_router.post("/{entity_id}/modules/{path:action}", summary="One of ACTIONS (stub)")
def module_action(request, entity_id: str, action: str):
    return not_implemented(request)
