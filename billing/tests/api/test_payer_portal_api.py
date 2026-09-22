"""The payer portal's routes: the HTTP half of Minty's ``tests/test_payer_portal_api.py``.

The read model itself is pinned next door (``billing/tests/engine/test_payer_portal_api.py``).
What is worth pinning HERE is the shell every route shares and the gate in front of it:

* **The gate is the payer, and there is no id to tamper with.** The endpoint takes no
  entity, filters on ``payer_user_id``, and therefore cannot be pointed at somebody else's
  companies. What the tests can check is the other half: that a token without a user is
  refused, and that an entity-scoped token is NOT refused - the portal is reached from the
  entity list with an unscoped one and from inside a company with a scoped one, so
  requiring or forbidding the claim would break one of the two paths the UI uses.

* **The handover routes answer the same way.** Four routes, one shared shell: 401 without a
  bearer, 400 only for a MISSING routing id, 422 with a full sentence for a stated refusal,
  500 without the stack trace for a surprise, and CORS on every response including the
  errors. 422 and not 403: the service's refusals are written to be read by the person who
  clicked, and the client only shows the server's words when they look like prose.

* **A datetime in a payload renders as Flask's ``jsonify`` rendered it** (RFC 822), because
  billing-frontend's ``payerPortal.ts`` learned to parse that form and minty-web inherits it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from django.conf import settings

from billing.tests.api.conftest import ORIGIN, bearer, post_json, preflight
from billing.tests.engine.test_payer_portal_api import NOW, _row, payer_portal  # noqa: F401

pytestmark = pytest.mark.django_db

PORTAL = "/api/me/subscriptions"


# --- The gate ----------------------------------------------------------------


def test_no_bearer_is_refused(client):
    assert client.get(PORTAL).status_code == 401


def test_a_token_this_app_did_not_sign_is_refused(client, user):
    response = client.get(PORTAL, **bearer(user, key="not-the-secret-not-the-secret-not-the-secret"))
    assert response.status_code == 401


def test_a_token_with_no_user_claim_is_refused(client, entity):
    # Signed by Flask's key, unexpired, and still nobody: the claim the whole portal keys on
    # is missing, so the door stays shut.
    token = jwt.encode(
        {"entity_id": str(entity.id), "exp": datetime.now(UTC) + timedelta(minutes=5)},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    response = client.get(PORTAL, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 401


def test_an_entity_scoped_token_is_accepted(client, user, monkeypatch):
    """Profile is reached with an UNSCOPED token and the screen spans entities, so the
    ``entity_id`` claim is not a gate here. Pinning it would refuse the only path the UI
    actually uses - and the company named need not even be one the caller belongs to."""
    from billing.services import portal as portal_service

    monkeypatch.setattr(
        portal_service,
        "build_payer_subscriptions",
        lambda *_a, **_k: {"entities": [], "total": 0},
    )

    response = client.get(
        PORTAL, HTTP_ORIGIN=ORIGIN, **bearer(user, entity_id="e9-not-a-company-of-theirs")
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response["Access-Control-Allow-Origin"] == ORIGIN
    # What matters is that Origin is pinned, so a response cached for one origin is never
    # replayed to another.
    assert "origin" in response["Vary"].lower()


def test_preflight_answers_without_a_token(client):
    response = preflight(client, PORTAL, "GET")
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_a_payer_with_nothing_gets_the_empty_table_not_an_error(client, user):
    """No stub at all: the real read model over an empty database. An empty table is the
    honest answer for a person who pays for nothing, and it must be a 200 the page renders,
    not a 500 it retries."""
    response = client.get(PORTAL, **bearer(user))
    assert response.status_code == 200
    payload = response.json()
    assert payload["entities"] == []
    assert payload["total"] == 0


def test_internal_sort_keys_never_reach_the_response(client, user, payer_portal):  # noqa: F811
    """``_date`` / ``_next_date`` exist only to sort on. Leaking a datetime into JSON would
    expose an unformatted field the client would use - through THIS encoder it would render
    rather than crash, which is exactly why the key has to be asserted absent here."""
    payer_portal(
        rows=[_row("PAYMENT_REQUEST", "trial", entity_id="e1", trial_end=NOW + timedelta(days=3))],
        entities=[{"id": "e1", "name": "Apple", "country": "HK"}],
    )

    response = client.get(PORTAL, **bearer(user))

    assert response.status_code == 200
    entity = response.json()["entities"][0]
    assert "_next_date" not in entity
    assert all("_date" not in m for m in entity["modules"])


def test_a_failing_read_model_is_a_500_in_the_house_words(client, user, monkeypatch):
    """Unlike the notice endpoint there is no useful empty answer here - an empty table
    reads as "you pay for nothing", which is a worse lie than an error the page can retry
    from."""
    from billing.services import portal as portal_service

    def _boom(*_a, **_k):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(portal_service, "build_payer_subscriptions", _boom)

    response = client.get(PORTAL, HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert response.status_code == 500
    assert response.json() == {"error": "Could not load your subscriptions."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN


# --- Subscriber options --------------------------------------------------------


def test_the_subscriber_options_route_needs_an_entity(client, user):
    response = client.get(f"{PORTAL}/subscriber-options", **bearer(user))
    assert response.status_code == 400
    assert response.json() == {"error": "entity is required"}


def test_a_company_you_do_not_pay_for_is_a_404_and_not_a_403(client, user, monkeypatch):
    """"You are not the payer" and "no such company" are the same answer to someone who
    should not be asking. Telling them apart would confirm an id."""
    from billing.services import portal as portal_service

    monkeypatch.setattr(portal_service, "build_subscriber_options", lambda *_a, **_k: None)

    response = client.get(
        f"{PORTAL}/subscriber-options?entity=e9", HTTP_ORIGIN=ORIGIN, **bearer(user)
    )
    assert response.status_code == 404
    assert response.json() == {"error": "That company isn't on your billing account."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN


# --- The handover routes -------------------------------------------------------


def _post(client, user, path, body):
    return post_json(client, path, body, HTTP_ORIGIN=ORIGIN, **bearer(user))


def test_initiating_a_handover_needs_a_bearer(client):
    response = post_json(client, f"{PORTAL}/transfer", {"entity": "e1", "to_user": "u2"})
    assert response.status_code == 401


def test_initiating_a_handover_needs_an_entity(client, user):
    response = _post(client, user, f"{PORTAL}/transfer", {"to_user": "u2"})
    assert response.status_code == 400
    assert "entity" in response.json()["error"]


def test_initiating_a_handover_needs_a_recipient(client, user):
    response = _post(client, user, f"{PORTAL}/transfer", {"entity": "e1"})
    assert response.status_code == 400
    assert "to_user" in response.json()["error"]


def test_a_body_that_is_not_json_is_read_as_empty(client, user):
    """Flask's ``get_json(silent=True) or {}``: a body that is not a JSON object is an empty
    one, so the answer is the missing-field 400 the client knows, not a parse error."""
    response = client.post(
        f"{PORTAL}/transfer", data="not json at all", content_type="application/json", **bearer(user)
    )
    assert response.status_code == 400
    assert response.json() == {"error": "entity is required"}


def test_a_refused_handover_is_a_422_with_the_reason_in_words(client, user, monkeypatch):
    """The client replaces anything that looks like a machine code with generic copy, so
    a refusal has to arrive as a sentence or the user is told nothing."""
    from billing.services import transfers

    monkeypatch.setattr(
        transfers, "offer_transfer",
        lambda *_a, **_k: (False, "That person needs to be an admin of this company first.", None),
    )

    response = _post(client, user, f"{PORTAL}/transfer", {"entity": "e1", "to_user": "u2"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert " " in error and error[0].isupper()
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_a_successful_offer_returns_it(client, user, monkeypatch):
    from billing.services import transfers

    seen = {}

    def _offer(user_id, entity_id, to_user_id):
        seen.update(user=user_id, entity=entity_id, to_user=to_user_id)
        return True, "The handover request has been sent.", {"id": "t1"}

    monkeypatch.setattr(transfers, "offer_transfer", _offer)

    response = _post(client, user, f"{PORTAL}/transfer", {"entity": "e1", "to_user": "u2"})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "message": "The handover request has been sent.", "transfer": {"id": "t1"},
    }
    # The caller is the TOKEN's user, as a hyphenated string - what the service compares.
    assert seen == {"user": str(user.id), "entity": "e1", "to_user": "u2"}


def test_an_unexpected_failure_is_a_500_not_a_stack_trace(client, user, monkeypatch):
    from billing.services import transfers

    def _boom(*_a, **_k):
        raise RuntimeError("processor unreachable")

    monkeypatch.setattr(transfers, "offer_transfer", _boom)

    response = _post(client, user, f"{PORTAL}/transfer", {"entity": "e1", "to_user": "u2"})

    assert response.status_code == 500
    assert "unreachable" not in response.json()["error"]
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_responding_needs_a_transfer_id(client, user):
    response = _post(client, user, f"{PORTAL}/transfer/respond", {"accept": True})
    assert response.status_code == 400
    assert response.json() == {"error": "transfer is required"}


def test_accepting_passes_the_flag_through(client, user, monkeypatch):
    from billing.services import transfers

    seen = {}

    def _respond(user_id, transfer_id, *, accept):
        seen.update(user=user_id, transfer=transfer_id, accept=accept)
        return True, "You're now the subscriber for this company.", {"id": transfer_id}

    monkeypatch.setattr(transfers, "respond_to_transfer", _respond)

    response = _post(client, user, f"{PORTAL}/transfer/respond", {"transfer": "t1", "accept": True})

    assert response.status_code == 200
    assert seen == {"user": str(user.id), "transfer": "t1", "accept": True}


def test_declining_is_the_same_route_with_the_flag_off(client, user, monkeypatch):
    from billing.services import transfers

    seen = {}
    monkeypatch.setattr(
        transfers, "respond_to_transfer",
        lambda u, t, *, accept: (seen.update(accept=accept) or
                                 (True, "You've declined the handover.", None)),
    )

    response = _post(client, user, f"{PORTAL}/transfer/respond", {"transfer": "t1", "accept": False})

    assert response.status_code == 200
    assert seen["accept"] is False
    # No transfer to hand back: the key is absent, not null.
    assert response.json() == {"ok": True, "message": "You've declined the handover."}


def test_cancelling_needs_a_transfer_id(client, user):
    response = _post(client, user, f"{PORTAL}/transfer/cancel", {})
    assert response.status_code == 400


def test_cancelling_reports_the_service_s_refusal(client, user, monkeypatch):
    from billing.services import transfers

    monkeypatch.setattr(
        transfers, "cancel_transfer",
        lambda *_a, **_k: (False, "That handover is already being processed."),
    )

    response = _post(client, user, f"{PORTAL}/transfer/cancel", {"transfer": "t1"})

    assert response.status_code == 422
    assert "already being processed" in response.json()["error"]


def test_the_inbox_is_scoped_to_the_token_not_the_request(client, user, monkeypatch):
    """The one recipient-scoped read in this file. Every other portal query filters on
    ``payer_user_id``; this one deliberately returns companies the caller does NOT pay
    for, so the scoping has to come from the token and nowhere else."""
    from billing.services import transfers

    seen = {}
    monkeypatch.setattr(
        transfers, "incoming_transfers_payload",
        lambda uid: seen.setdefault("uid", uid) and [] or [{"id": "t1"}],
    )

    response = client.get(f"{PORTAL}/transfers?user_id=someone-else", **bearer(user))

    assert response.status_code == 200
    assert seen["uid"] == str(user.id), "the query string must not be able to redirect this"
    assert response.json() == {"transfers": [{"id": "t1"}]}


def test_the_inbox_needs_a_bearer(client):
    assert client.get(f"{PORTAL}/transfers").status_code == 401


def test_the_handover_routes_answer_preflight_without_a_token(client):
    for path, method in (
        (f"{PORTAL}/transfer", "POST"),
        (f"{PORTAL}/transfer/respond", "POST"),
        (f"{PORTAL}/transfer/cancel", "POST"),
        (f"{PORTAL}/transfers", "GET"),
    ):
        response = preflight(client, path, method)
        assert response.status_code == 200, path
        assert response["Access-Control-Allow-Origin"] == ORIGIN, path


# --- What the encoder does -----------------------------------------------------


def test_a_raw_datetime_renders_as_flask_did(client, user, monkeypatch):
    """``transfers._as_dict`` hands ``expires_at`` back as a datetime and Flask's ``jsonify``
    wrote it as an RFC 822 date; the client parses that form, so this encoder must too -
    never ISO 8601, never a crash."""
    from billing.services import transfers

    monkeypatch.setattr(
        transfers, "incoming_transfers_payload",
        lambda uid: [{"id": "t1", "expires_at": datetime(2026, 8, 6, 12, 0, tzinfo=UTC)}],
    )

    response = client.get(f"{PORTAL}/transfers", **bearer(user))

    assert response.status_code == 200
    assert response.json()["transfers"][0]["expires_at"] == "Thu, 06 Aug 2026 12:00:00 GMT"


# --- The invitation: the one write this service forwards to Flask --------------------
#
# No Flask test to port: Flask's view called ``send_invite`` in-process. Here the write is
# ``core.flask_client.forward`` with the caller's own bearer, so what is pinned is the
# forwarding itself - the token travels unchanged, Flask's refusal travels back as a
# sentence, and Flask being down is the route's own 500 and never a stack trace.


class _FlaskAnswer:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def invite_setup(user, entity, monkeypatch):
    """The caller pays for ``entity`` (the store says so) and is its admin (the fixture row),
    so both of the service's own gates pass and the call reaches the forward."""
    from billing.services import store as sub_store

    monkeypatch.setattr(sub_store, "payer_for_entity", lambda eid: str(user.id) if str(eid) == str(entity.id) else None)

    calls = []

    def _install(answer):
        import requests

        def _request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(requests, "request", _request)
        return calls

    return _install


def test_inviting_needs_an_entity(client, user):
    response = _post(client, user, f"{PORTAL}/invite-admin", {"email": "new.admin@example.com"})
    assert response.status_code == 400
    assert response.json() == {"error": "entity is required"}


def test_inviting_forwards_the_callers_own_bearer_to_flask(client, user, entity, invite_setup):
    calls = invite_setup(_FlaskAnswer(200, {"status": "success", "email_sent": True}))
    headers = bearer(user)

    response = post_json(
        client, f"{PORTAL}/invite-admin",
        {"entity": str(entity.id), "email": "new.admin@example.com"}, **headers,
    )

    assert response.status_code == 200, response.content
    assert response.json()["ok"] is True
    (method, url, kwargs), = calls
    assert method == "POST"
    assert url == "http://flask.invalid/api/onboarding/invite"
    # The proxy carries no privilege of its own: Flask sees exactly the caller's token.
    assert kwargs["headers"]["Authorization"] == headers["HTTP_AUTHORIZATION"]
    assert kwargs["json"] == {"entity_id": str(entity.id), "email": "new.admin@example.com", "role": "admin"}


def test_flasks_refusal_comes_back_as_a_422_in_its_own_words(client, user, entity, invite_setup):
    invite_setup(_FlaskAnswer(409, {"error": "An invitation is already pending for that address."}))

    response = _post(client, user, f"{PORTAL}/invite-admin",
                     {"entity": str(entity.id), "email": "new.admin@example.com"})

    assert response.status_code == 422
    assert response.json() == {"error": "An invitation is already pending for that address."}
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_an_unreachable_flask_is_the_routes_500_not_a_stack_trace(client, user, entity, invite_setup):
    import requests

    invite_setup(requests.ConnectionError("connection refused"))

    response = _post(client, user, f"{PORTAL}/invite-admin",
                     {"entity": str(entity.id), "email": "new.admin@example.com"})

    assert response.status_code == 500
    assert response.json() == {"error": "Could not send that invitation."}


def test_a_company_the_caller_does_not_pay_for_is_refused_before_flask_is_asked(client, user, entity, invite_setup):
    calls = invite_setup(_FlaskAnswer(200, {"status": "success", "email_sent": True}))

    response = _post(client, user, f"{PORTAL}/invite-admin",
                     {"entity": "00000000-0000-0000-0000-000000000000", "email": "new.admin@example.com"})

    assert response.status_code == 422
    assert response.json() == {"error": "That company isn't on your billing account."}
    assert calls == []


def test_a_company_row_carries_when_it_was_created(client, user, entity, monkeypatch):
    """The step-3 addition minty-web's Manage Subscriptions list asked for: ``created_at`` on
    each company row, ISO like ``date_iso`` because it is sorted on, not read. Everything
    else on the row is Flask's."""
    from types import SimpleNamespace

    from billing.services import store as sub_store

    monkeypatch.setattr(
        sub_store, "module_rows_for_payer",
        lambda uid: [SimpleNamespace(entity_id=str(entity.id), function_code="PETTY_CASH",
                                     phase="trial", trial_end=None, app_access_until=None,
                                     first_billed_at=None)],
    )

    response = client.get(PORTAL, **bearer(user))

    assert response.status_code == 200
    (row,) = response.json()["entities"]
    assert row["entity_id"] == str(entity.id)
    assert row["created_at"] == entity.created_at.isoformat()
    assert row["settings_path"] == f"/entity/settings/module/{entity.id}"
