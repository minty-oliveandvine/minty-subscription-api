"""The dark contract, pinned.

While ``SUBSCRIPTION_ENABLED`` is off (the state production cuts over in, Part 2 step 7):

* every API path answers ``404 {"error": "not_found"}`` - the same body Flask's
  ``require_subscriptions_enabled`` gives - whether or not the caller is authenticated;
* the 404 carries CORS headers for an allowed origin, so the browser apps read "not there"
  and not a CORS failure;
* the preflight still succeeds;
* ``/healthz`` and the OpenAPI document answer;
* the scheduler does not start whatever its own switch says, ``tick`` exits 0 having done
  nothing, and ``revoke-ungranted`` refuses.

Live, the same paths reach their handlers - all four routers are filled (step 3).
"""

from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command

from billing.api.me import ROUTES as ME_ROUTES
from billing.api.onboarding import ROUTES as ONBOARDING_ROUTES

ORIGIN = "http://localhost:3002"

pytestmark = pytest.mark.django_db


def _every_path(entity_id="00000000-0000-0000-0000-000000000000"):
    for method, path in ME_ROUTES:
        yield method, f"/api/me{path}"
    for method, path in ONBOARDING_ROUTES:
        yield method, f"/api/onboarding{path}"
    yield "GET", f"/api/entities/{entity_id}/modules"
    yield "POST", f"/api/entities/{entity_id}/modules/start-trial"
    yield "GET", f"/api/entities/{entity_id}/subscription-notice"


def _call(client, method, path, **headers):
    if method == "POST":
        return client.post(path, data="{}", content_type="application/json", **headers)
    return client.get(path, **headers)


def test_every_path_is_404_with_cors_while_dark(client, dark, auth):
    for method, path in _every_path():
        res = _call(client, method, path, HTTP_ORIGIN=ORIGIN, **auth)
        assert res.status_code == 404, (method, path, res.status_code)
        assert res.json() == {"error": "not_found"}, (method, path)
        assert res["Access-Control-Allow-Origin"] == ORIGIN, (method, path)


def test_dark_404_needs_no_token(client, dark):
    res = client.get("/api/me/subscriptions", HTTP_ORIGIN=ORIGIN)
    assert res.status_code == 404
    assert res.json() == {"error": "not_found"}


def test_preflight_still_succeeds_while_dark(client, dark):
    res = client.options(
        "/api/me/subscriptions",
        HTTP_ORIGIN=ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,x-entity-id",
    )
    assert res.status_code == 200
    assert res["Access-Control-Allow-Origin"] == ORIGIN
    assert "x-entity-id" in res["Access-Control-Allow-Headers"].lower()


def test_healthz_and_openapi_answer_while_dark(client, dark):
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json()["service"] == "minty-billing-api"
    assert client.get("/api/openapi.json").status_code == 200


def test_live_paths_reach_their_handlers(client, auth):
    # Live (the suite default), the same path is answered by the portal - a payer with
    # nothing gets the empty table, never the dark gate's 404.
    res = client.get("/api/me/subscriptions", HTTP_ORIGIN=ORIGIN, **auth)
    assert res.status_code == 200
    payload = res.json()
    assert payload["entities"] == [] and payload["total"] == 0
    assert res["Access-Control-Allow-Origin"] == ORIGIN


def test_live_company_paths_reach_their_handlers(client, auth_scoped, entity):
    # The company-scoped routers, live: the notice for a company with nothing is an empty
    # list, the module page its page model - never the dark gate's 404.
    res = client.get(
        f"/api/entities/{entity.id}/subscription-notice", HTTP_ORIGIN=ORIGIN, **auth_scoped
    )
    assert res.status_code == 200
    assert res.json()["items"] == []
    assert res["Access-Control-Allow-Origin"] == ORIGIN
    res = client.get(f"/api/entities/{entity.id}/modules", HTTP_ORIGIN=ORIGIN, **auth_scoped)
    assert res.status_code == 200
    assert res["Access-Control-Allow-Origin"] == ORIGIN


def test_live_unauthenticated_is_401_not_404(client):
    res = client.get("/api/me/subscriptions")
    assert res.status_code == 401
    assert res.json() == {"error": "Unauthorized"}


def test_scheduler_does_not_start_while_dark(dark, settings):
    settings.SUBSCRIPTION_SCHEDULER_ENABLED = True
    from billing.scheduler import start_scheduler

    assert start_scheduler() is None


def test_scheduler_does_not_start_without_its_own_switch(settings):
    settings.SUBSCRIPTION_SCHEDULER_ENABLED = False
    from billing.scheduler import start_scheduler

    assert start_scheduler() is None


def test_tick_exits_zero_and_does_nothing_while_dark(dark, capsys):
    call_command("subscriptions", "tick")
    assert "dark" in capsys.readouterr().out


def test_revoke_ungranted_refuses_while_dark(dark):
    with pytest.raises(CommandError, match="refuses while SUBSCRIPTION_ENABLED is off"):
        call_command("subscriptions", "revoke-ungranted")
