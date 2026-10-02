"""The contract a deployment must honour - the checks the runbook runs after each deploy
(``curl BILLING_API/healthz`` = 200, ``/api/me/subscriptions`` = 401 without a token)."""

from __future__ import annotations

import pytest
import requests

from e2e.conftest import PAYMENTS_ORIGIN, WEB_ORIGIN, mint

PORTAL = "/api/me/subscriptions"


def test_healthz(base_url):
    res = requests.get(f"{base_url}/healthz", timeout=10)
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "service": "minty-subscription-api"}


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
        # Past the door and answered: the portal's table for this payer (step 3), with CORS.
        token = mint(credentials["secret"], credentials["user_id"])
        res = requests.get(
            f"{base_url}{PORTAL}",
            headers={"Authorization": f"Bearer {token}", "Origin": WEB_ORIGIN},
            timeout=10,
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert isinstance(body["entities"], list) and "total" in body
        assert res.headers.get("Access-Control-Allow-Origin") == WEB_ORIGIN

    def test_the_billing_accounts_answer_for_the_payer(self, base_url, credentials):
        # 08-A / 08-B's read, live: the accounts and the one payer-wide next billing date.
        # Read only - no Stripe write happens here (the wallet read is a Stripe LIST).
        token = mint(credentials["secret"], credentials["user_id"])
        res = requests.get(
            f"{base_url}/api/me/billing/accounts?countries=1",
            headers={"Authorization": f"Bearer {token}", "Origin": WEB_ORIGIN},
            timeout=20,
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert isinstance(body["accounts"], list) and isinstance(body["countries"], list)
        assert "next_billing" in body and "payer" in body
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
        assert scoped.status_code == 200, scoped.text
        page = scoped.json()
        assert page["entity_id"] == eid and isinstance(page["cards"], list)
        assert "can_manage_modules" in page and "viewer" in page

    def test_the_notice_answers_for_a_member(self, base_url, credentials):
        if not credentials["entity_id"]:
            pytest.skip("set E2E_MINTY_ENTITY for the notice check")
        eid = credentials["entity_id"]
        token = mint(credentials["secret"], credentials["user_id"], entity_id=eid)
        res = requests.get(
            f"{base_url}/api/entities/{eid}/subscription-notice",
            headers={"Authorization": f"Bearer {token}", "Origin": PAYMENTS_ORIGIN},
            timeout=10,
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert isinstance(body["items"], list)
        assert body["settings_path"].startswith("/handoff/minty-web?")
        assert f"entity_id={eid}" in body["settings_path"]
        assert res.headers.get("Access-Control-Allow-Origin") == PAYMENTS_ORIGIN
