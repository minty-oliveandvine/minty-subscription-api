"""The API surface, pinned before it is filled.

The route tables in ``billing/api/*`` are the contract Part 2 step 3 implements and the
frontends are written against; this test keeps them from drifting silently, and checks the
OpenAPI document actually carries every path (the document is what ``docs/openapi.json``
is generated from, and what a reader gets without a token).
"""

from __future__ import annotations

import pytest

from billing.api.me import ROUTES as ME_ROUTES
from billing.api.modules import ACTIONS
from billing.api.onboarding import ROUTES as ONBOARDING_ROUTES

pytestmark = pytest.mark.django_db

#: The fifteen ``/api/me/*`` paths of Flask's routes/portal.py (entity-payment-method
#: answers GET and POST, hence sixteen operations on fifteen paths).
FLASK_PORTAL_PATHS = {
    "/subscriptions",
    "/subscriptions/subscriber-options",
    "/subscriptions/invite-admin",
    "/subscriptions/transfer",
    "/subscriptions/transfer/respond",
    "/subscriptions/transfer/cancel",
    "/subscriptions/transfers",
    "/invoices",
    "/billing/payment-methods",
    "/billing/payment-methods/setup-intent",
    "/billing/payment-methods/confirm",
    "/billing/payment-methods/default",
    "/billing/entity-payment-method",
    "/billing/payment-methods/update",
    "/billing/payment-methods/remove",
}

#: Paths this service adds that Flask never had. Kept apart from the set above so that one
#: goes on documenting the PORT - "what Flask served" is a different question from "what we
#: serve", and folding them together loses the ability to answer either.
#:
#: ``transfer/seen`` records that the payer who offered a handover has been shown how it
#: ended (07-I / A-07 / A-08). Flask had no such screen and no such column.
#:
#: The four ``billing/accounts`` routes are the portal's billing accounts (08-A / 08-B /
#: 08-C): Flask's portal listed cards, never accounts. ``invoices/{invoice_id}/breakdown`` is
#: 08-B's "Billing Breakdown · Download csv": Flask's invoice tab had no breakdown.
#: ``invoices/{invoice_id}/retry`` is 08-B's "Retry payment" on a failed invoice's row: Flask
#: retried only from a company's module card. ``invoices/{invoice_id}/pdf`` is 08-B's
#: "Invoice PDF", drawn as Figma 09-A: Flask linked Stripe's hosted page.
ADDED_PORTAL_PATHS = {
    "/subscriptions/transfer/seen",
    "/billing/accounts",
    "/billing/accounts/update",
    "/billing/accounts/default-card",
    "/billing/accounts/move",
    "/invoices/{invoice_id}/breakdown",
    "/invoices/{invoice_id}/retry",
    "/invoices/{invoice_id}/pdf",
}

#: The nineteen ``POST /entity/settings/module/<org_id>/<action>`` routes of Flask's
#: entity/routes/settings.py (lines 1419-2431).
FLASK_MODULE_ACTIONS = {
    "authorize-billing", "cancel", "cancel-preview", "checkout", "checkout-complete",
    "confirm-billing", "manage-billing", "payment-method", "payment-methods",
    "payment-methods/confirm", "payment-methods/default", "payment-methods/setup-intent",
    "renew", "restart-billing", "restart-quote", "resume-preview", "retry-payment",
    "start-trial", "subscribe-preview",
}

#: Of those, the nine DELETED on 2026-10-01 (the user's rule: a payment method is only ever
#: added through a billing account, in-app - no route may hand the browser to a Stripe-hosted
#: page). Setup-mode Checkout, the Billing Portal, and the module page's copies of the card
#: routes (the payer portal's ``/api/me/billing/*`` are the card routes).
REMOVED_MODULE_ACTIONS = {
    "checkout", "confirm-billing", "checkout-complete", "payment-method", "manage-billing",
    "payment-methods", "payment-methods/setup-intent", "payment-methods/confirm",
    "payment-methods/default",
}

#: The one action Flask never had (2026-10-08). A trial establishes no subscriber, so a company
#: gets one by CONFIRMING billing on a billing account - which is what this does, and the only
#: in-app way it happens.
ADDED_MODULE_ACTIONS = {"activate-subscription"}

#: The nine card / billing-account routes of Flask's entity/routes/create.py (757-1247).
FLASK_ONBOARDING_PATHS = {
    "/payment-method", "/payment-method/setup", "/payment-method/complete",
    "/billing/payment-methods", "/billing/payment-methods/setup-intent",
    "/billing/payment-methods/confirm", "/billing/payment-methods/default",
    "/billing/accounts", "/billing/authorize",
}

#: Of those, the two setup-mode Checkout routes, deleted on 2026-10-01 for the same rule.
REMOVED_ONBOARDING_PATHS = {"/payment-method/setup", "/payment-method/complete"}


def test_the_portal_carries_flasks_fifteen_paths():
    served = {p for _, p in ME_ROUTES}
    assert served == FLASK_PORTAL_PATHS | ADDED_PORTAL_PATHS
    # Every one of Flask's is still here: an addition must never be a replacement.
    assert FLASK_PORTAL_PATHS <= served
    # Flask's sixteen operations plus the eight added paths (one operation each).
    assert len(ME_ROUTES) == 24
    assert ("GET", "/billing/entity-payment-method") in ME_ROUTES
    assert ("POST", "/billing/entity-payment-method") in ME_ROUTES


def test_the_module_page_carries_flasks_actions_less_the_hosted_ones():
    assert set(ACTIONS) == (FLASK_MODULE_ACTIONS - REMOVED_MODULE_ACTIONS) | ADDED_MODULE_ACTIONS
    assert len(ACTIONS) == 11
    assert not set(ACTIONS) & REMOVED_MODULE_ACTIONS


def test_the_onboarding_router_carries_flasks_seven_plus_trials_start():
    paths = {p for _, p in ONBOARDING_ROUTES}
    assert paths == (FLASK_ONBOARDING_PATHS - REMOVED_ONBOARDING_PATHS) | {"/trials/start"}
    assert not paths & REMOVED_ONBOARDING_PATHS
    assert ("GET", "/billing/accounts") in ONBOARDING_ROUTES
    assert ("POST", "/billing/accounts") in ONBOARDING_ROUTES
    assert ("POST", "/trials/start") in ONBOARDING_ROUTES


def test_the_committed_openapi_document_is_current():
    """``docs/openapi.json`` is the contract other repos read (Part 3's type generation starts
    there), so it must be exactly what the API serves. Regenerate with
    ``manage.py export_openapi``."""
    from billing.management.commands.export_openapi import DEFAULT_PATH, render

    assert DEFAULT_PATH.exists(), "docs/openapi.json is missing - run: manage.py export_openapi"
    assert DEFAULT_PATH.read_text(encoding="utf-8") == render(), (
        "docs/openapi.json is stale - run: manage.py export_openapi"
    )


def test_openapi_lists_every_path(client):
    doc = client.get("/api/openapi.json").json()
    paths = set(doc["paths"])
    for _, p in ME_ROUTES:
        assert f"/api/me{p}" in paths, p
    for _, p in ONBOARDING_ROUTES:
        assert f"/api/onboarding{p}" in paths, p
    for p in REMOVED_ONBOARDING_PATHS:
        assert f"/api/onboarding{p}" not in paths, p
    assert "/api/entities/{entity_id}/modules/payment-methods" not in paths
    assert "/api/entities/{entity_id}/modules" in paths
    assert "/api/entities/{entity_id}/modules/{action}" in paths
    assert "/api/entities/{entity_id}/subscription-notice" in paths
    # Every operation is behind the bearer scheme - nothing is public but /healthz.
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            assert op.get("security"), (method, path)


def test_healthz_and_the_openapi_document_need_no_token(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["service"] == "minty-subscription-api"
    assert client.get("/api/openapi.json").status_code == 200


def test_the_cors_preflight_succeeds(client):
    origin = "http://localhost:3000"
    res = client.options(
        "/api/me/subscriptions",
        HTTP_ORIGIN=origin,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,x-entity-id",
    )
    assert res.status_code == 200
    assert res["Access-Control-Allow-Origin"] == origin
    assert "x-entity-id" in res["Access-Control-Allow-Headers"].lower()
