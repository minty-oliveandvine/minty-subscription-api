"""Verifying Flask's token - and only verifying it.

This service mints nothing (cross-cutting rule 1). A token is accepted when it is signed
with the shared ``SECRET_KEY``, unexpired and names a real user; the portal (``/api/me/*``)
takes it with or without a company, the module page (``/api/entities/*``) only when the
caller holds a role on the company named by the token or the ``X-Entity-Id`` header.

"Accepted" here means the request reaches the handler rather than being stopped at the door
with 401: the portal answers its empty table (step 3 slice A), the module page its page model
(slice B) - 200 both.
"""

from __future__ import annotations

import uuid

import pytest

from billing.tests.conftest import make_token

pytestmark = pytest.mark.django_db

PORTAL_ACCEPTED = 200  # /api/me/subscriptions, the empty table for a payer with nothing
MODULE_PAGE_ACCEPTED = 200  # /api/entities/{id}/modules, the page model of a company with nothing


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ---- the portal: person-scoped ----------------------------------------------------------


def test_unscoped_flask_token_reaches_the_portal(client, user):
    res = client.get("/api/me/subscriptions", **_bearer(make_token(user.id)))
    assert res.status_code == PORTAL_ACCEPTED


def test_scoped_flask_token_reaches_the_portal_too(client, user, entity):
    res = client.get("/api/me/subscriptions", **_bearer(make_token(user.id, entity_id=entity.id)))
    assert res.status_code == PORTAL_ACCEPTED


def test_portal_tolerates_a_company_the_caller_has_no_role_on(client, other_user, entity):
    # SelfBearerAuth: identity is not in doubt, the company is irrelevant to /api/me.
    res = client.get(
        "/api/me/subscriptions", **_bearer(make_token(other_user.id, entity_id=entity.id))
    )
    assert res.status_code == PORTAL_ACCEPTED


def test_forged_token_is_refused(client, user):
    res = client.get("/api/me/subscriptions", **_bearer(make_token(user.id, key="not-the-shared-key-not-the-shared-key")))
    assert res.status_code == 401
    assert res.json() == {"error": "Unauthorized"}


def test_expired_token_is_refused(client, user):
    res = client.get("/api/me/subscriptions", **_bearer(make_token(user.id, minutes=-1)))
    assert res.status_code == 401


def test_unknown_user_is_refused(client, db):
    res = client.get("/api/me/subscriptions", **_bearer(make_token(uuid.uuid4())))
    assert res.status_code == 401


def test_missing_bearer_is_refused(client, db):
    assert client.get("/api/me/subscriptions").status_code == 401
    assert client.get("/api/me/subscriptions", HTTP_AUTHORIZATION="Basic abc").status_code == 401


# ---- the module page: company-scoped ---------------------------------------------------


def test_member_reaches_the_module_page(client, user, entity):
    res = client.get(f"/api/entities/{entity.id}/modules", **_bearer(make_token(user.id, entity_id=entity.id)))
    assert res.status_code == MODULE_PAGE_ACCEPTED


def test_unscoped_token_plus_header_reaches_the_module_page(client, user, entity):
    # The page reached FROM the portal: unscoped token, company in X-Entity-Id.
    res = client.get(
        f"/api/entities/{entity.id}/modules",
        HTTP_X_ENTITY_ID=str(entity.id),
        **_bearer(make_token(user.id)),
    )
    assert res.status_code == MODULE_PAGE_ACCEPTED


def test_non_member_is_refused_on_the_module_page(client, other_user, entity):
    res = client.get(
        f"/api/entities/{entity.id}/modules",
        **_bearer(make_token(other_user.id, entity_id=entity.id)),
    )
    assert res.status_code == 401


def test_unscoped_token_without_header_is_refused_on_the_module_page(client, user, entity):
    # BearerAuth: an unscoped token opens a company route only with X-Entity-Id.
    res = client.get(f"/api/entities/{entity.id}/modules", **_bearer(make_token(user.id)))
    assert res.status_code == 401


def test_system_superadmin_reads_any_company(client, other_user, entity):
    other_user.system_role = "superadmin"
    other_user.save(update_fields=["system_role"])
    res = client.get(
        f"/api/entities/{entity.id}/modules",
        **_bearer(make_token(other_user.id, entity_id=entity.id, system_role="superadmin")),
    )
    assert res.status_code == MODULE_PAGE_ACCEPTED
