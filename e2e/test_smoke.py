"""The contract a deployment must honour, live or dark - the checks Part 2 step 7's runbook
runs after the deploy (``curl BILLING_API/healthz`` = 200, ``/api/me/subscriptions`` = 404
with CORS headers while dark)."""

from __future__ import annotations

import pytest
import requests

from e2e.conftest import WEB_ORIGIN, mint, subscriptions_dark

PORTAL = "/api/me/subscriptions"
NOBODY = "00000000-0000-0000-0000-000000000000"


def test_healthz(base_url):
    res = requests.get(f"{base_url}/healthz", timeout=10)
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "service": "minty-billing-api"}


def test_openapi_document_is_readable(base_url):
    res = requests.get(f"{base_url}/api/openapi.json", timeout=10)
    assert res.status_code == 200
    assert PORTAL in res.json()["paths"]


def test_preflight_from_minty_web_succeeds(base_url):
    res = requests.options(
        f"{base_url}{PORTAL}",
        headers={
            "Origin": WEB_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,x-entity-id",
        },
        timeout=10,
    )
    assert res.status_code == 200
    assert res.headers.get("Access-Control-Allow-Origin") == WEB_ORIGIN
    assert "x-entity-id" in res.headers.get("Access-Control-Allow-Headers", "").lower()


@pytest.mark.skipif(not subscriptions_dark(), reason="the service runs live (E2E_SUBSCRIPTIONS != 0)")
class TestDark:
    def test_portal_is_404_with_cors(self, base_url):
        res = requests.get(f"{base_url}{PORTAL}", headers={"Origin": WEB_ORIGIN}, timeout=10)
        assert res.status_code == 404
        assert res.json() == {"error": "not_found"}
        assert res.headers.get("Access-Control-Allow-Origin") == WEB_ORIGIN

    def test_a_valid_token_changes_nothing(self, base_url, credentials):
        token = mint(credentials["secret"], credentials["user_id"])
        res = requests.get(
            f"{base_url}{PORTAL}", headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        assert res.status_code == 404
        assert res.json() == {"error": "not_found"}

    def test_module_page_notice_and_onboarding_are_404(self, base_url):
        for path in (
            f"/api/entities/{NOBODY}/modules",
            f"/api/entities/{NOBODY}/subscription-notice",
            "/api/onboarding/payment-method",
        ):
            res = requests.get(f"{base_url}{path}", timeout=10)
            assert res.status_code == 404, path
            assert res.json() == {"error": "not_found"}, path


@pytest.mark.skipif(subscriptions_dark(), reason="the service runs dark (E2E_SUBSCRIPTIONS=0)")
class TestLive:
    def test_unauthenticated_is_401(self, base_url):
        res = requests.get(f"{base_url}{PORTAL}", timeout=10)
        assert res.status_code == 401
        assert res.json() == {"error": "Unauthorized"}

    def test_forged_token_is_refused(self, base_url, credentials):
        token = mint("not-the-shared-key-not-the-shared-key", credentials["user_id"])
        res = requests.get(
            f"{base_url}{PORTAL}", headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        assert res.status_code == 401

    def test_flask_shaped_token_is_accepted(self, base_url, credentials):
        # Accepted = past the door. 501 while the routes are stubs (Part 2 step 1), 200 once
        # step 3 fills them; never 401/403/404.
        token = mint(credentials["secret"], credentials["user_id"])
        res = requests.get(
            f"{base_url}{PORTAL}",
            headers={"Authorization": f"Bearer {token}", "Origin": WEB_ORIGIN},
            timeout=10,
        )
        assert res.status_code not in (401, 403, 404), res.text
        assert res.headers.get("Access-Control-Allow-Origin") == WEB_ORIGIN

    def test_module_page_needs_a_company_the_caller_belongs_to(self, base_url, credentials):
        if not credentials["entity_id"]:
            pytest.skip("set E2E_MINTY_ENTITY for the module-page check")
        eid = credentials["entity_id"]
        token = mint(credentials["secret"], credentials["user_id"])
        # Unscoped token, no header: refused. Same token with X-Entity-Id: through the door.
        bare = requests.get(
            f"{base_url}/api/entities/{eid}/modules",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert bare.status_code == 401
        scoped = requests.get(
            f"{base_url}/api/entities/{eid}/modules",
            headers={"Authorization": f"Bearer {token}", "X-Entity-Id": eid},
            timeout=10,
        )
        assert scoped.status_code not in (401, 403, 404), scoped.text
