"""The notice endpoint: the HTTP half of Minty's ``tests/test_subscription_notice.py``.

The notice itself is pinned next door (``billing/tests/engine/test_subscription_notice.py``).
What is worth pinning HERE is the gate, because the caller is another origin's browser holding
a token minted for the payment module:

* a token that names a company is held to it (403 ``entity_mismatch`` for another company's
  path), but a token that names NO company - the refresh path mints through the payment
  module's backend and need not preserve the claim - falls back to the caller's membership of
  the company in the PATH, which is what authorises the read in any case;
* membership is re-checked, never trusted from the claim - a token outlives a revoked role;
* a notice must never take the landing page down with it: a builder failure is ``{"items":
  []}`` with 200, and the answer carries ``settings_path`` (a Minty PATH the frontend wraps in
  ``buildMintyEnterUrl``), never a bare origin.

Where the door differs from Flask's: a stranger is refused by the auth class with 401 where
Flask answered 403 ``not_a_member``; billing-frontend treats every non-200 as "no notice".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from django.conf import settings

from billing.tests.api.conftest import ORIGIN, bearer, preflight
from shared_models.models import Entity, UserEntity

pytestmark = pytest.mark.django_db


def _url(entity):
    return f"/api/entities/{entity.id if hasattr(entity, 'id') else entity}/subscription-notice"


def _token(user_id, *, entity_id="", expired=False):
    """The payment module's token: ``user_id``, ``entity_id`` (maybe empty), ``module``."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "user_id": str(user_id),
            "entity_id": str(entity_id or ""),
            "module": "billing",
            "exp": now - timedelta(minutes=1) if expired else now + timedelta(minutes=30),
            "iat": now,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def notice_builder(monkeypatch):
    """The builder stubbed; ``items`` sets what it answers, an exception instance raises."""
    from billing.services import entity_modules

    state = {"items": [], "seen": []}

    def _build(entity_id, user_id):
        state["seen"].append((str(entity_id), str(user_id)))
        if isinstance(state["items"], Exception):
            raise state["items"]
        return {"items": list(state["items"]), "can_manage": True, "payer": None, "severity": None}

    monkeypatch.setattr(entity_modules, "build_subscription_notices", _build)
    return state


def test_notice_api_requires_a_token(client, entity):
    assert client.get(_url(entity)).status_code == 401


def test_notice_api_rejects_a_garbage_token(client, entity):
    assert client.get(_url(entity), HTTP_AUTHORIZATION="Bearer not-a-jwt").status_code == 401


def test_notice_api_rejects_an_expired_token(client, user, entity):
    res = client.get(_url(entity), **_auth(_token(user.id, entity_id=entity.id, expired=True)))
    assert res.status_code == 401


def test_a_token_for_one_entity_cannot_read_another(client, user, entity, countries, notice_builder):
    """A token that names a company is held to it."""
    other = Entity.objects.create(id=str(uuid.uuid4()), name="Other Co", country_code="HK", status="connected")
    UserEntity.objects.create(user_id=user.id, entity_id=other.id, role="admin", approved=True)

    res = client.get(_url(other), **_auth(_token(user.id, entity_id=entity.id)))

    assert res.status_code == 403
    assert res.json() == {"error": "entity_mismatch"}
    assert notice_builder["seen"] == []


def test_a_token_with_no_entity_claim_falls_back_to_membership(client, user, entity, notice_builder):
    """The refresh path goes through the payment module's backend, which need not preserve
    the claim. Requiring it would lock out every user whose 30-minute token rolled over - and
    it is not what authorises the read; membership is. No ``X-Entity-Id`` either: the
    frontend sends only the bearer."""
    res = client.get(_url(entity), **_auth(_token(user.id, entity_id="")))

    assert res.status_code == 200
    assert notice_builder["seen"] == [(str(entity.id), str(user.id))]


def test_a_claimless_token_still_cannot_read_a_stranger_entity(client, other_user, entity, notice_builder):
    """Dropping the claim check must not drop the authorisation with it."""
    res = client.get(_url(entity), **_auth(_token(other_user.id, entity_id="")))

    assert res.status_code == 401
    assert notice_builder["seen"] == []


def test_membership_is_rechecked_not_trusted_from_the_claim(client, other_user, entity, notice_builder):
    """A token outlives a revoked membership; the claim is not proof of access."""
    res = client.get(_url(entity), **_auth(_token(other_user.id, entity_id=entity.id)))

    assert res.status_code == 401
    assert notice_builder["seen"] == []


def test_a_system_superadmin_reads_any_company(client, other_user, entity, notice_builder):
    other_user.system_role = "superadmin"
    other_user.save(update_fields=["system_role"])

    res = client.get(_url(entity), **bearer(other_user, system_role="superadmin"))

    assert res.status_code == 200


def test_notice_api_returns_the_items_and_a_minty_settings_path(client, user, entity, notice_builder):
    notice_builder["items"] = [{"kind": "past_due", "severity": "critical", "module": "Payment"}]

    res = client.get(_url(entity), HTTP_ORIGIN=ORIGIN, **_auth(_token(user.id, entity_id=entity.id)))

    assert res.status_code == 200
    body = res.json()
    assert body["items"][0]["kind"] == "past_due"
    assert body["can_manage"] is True
    # A PATH, not a URL: the frontend wraps it in buildMintyEnterUrl so its token buys a
    # Flask session. A bare origin would land on the login form instead. The path is
    # Flask's hand-over to minty-web's module page for this company.
    assert body["settings_path"] == (
        "/handoff/minty-web?next=%2Fentity%2F"
        f"{entity.id}%2Fcompany%2Fsettings%2Fmodules&entity_id={entity.id}"
    )
    assert res["Access-Control-Allow-Origin"] == ORIGIN


def test_notice_api_survives_a_builder_failure(client, user, entity, notice_builder):
    """A notice must never take the landing page down with it."""
    notice_builder["items"] = RuntimeError("stripe down")

    res = client.get(_url(entity), **_auth(_token(user.id, entity_id=entity.id)))

    assert res.status_code == 200
    assert res.json() == {"items": []}


def test_the_real_builder_over_an_empty_company_says_nothing(client, user, entity):
    """No stub: a company with nothing has no notice, and that is a 200 with an empty list."""
    res = client.get(_url(entity), **_auth(_token(user.id, entity_id=entity.id)))
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_notice_api_answers_the_cors_preflight(client, entity):
    res = preflight(client, _url(entity), "GET")

    assert res.status_code == 200
    assert res["Access-Control-Allow-Origin"] == ORIGIN
    assert "authorization" in res["Access-Control-Allow-Headers"].lower()
    # Without Vary a cached response for one origin could be replayed to another.
    assert "origin" in res["Vary"].lower()
