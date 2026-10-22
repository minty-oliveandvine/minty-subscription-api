"""The one log line (minty_shared.logctx) as this service writes it: a trace per request, taken
from a well-formed ``X-Request-ID`` and echoed back; the user and entity bound only once auth has
authorised them; the route name as the fallback step; the header forwarded to Flask; and a fresh
trace for each scheduler pass."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from minty_shared import logctx

from billing.tests.conftest import make_token

pytestmark = pytest.mark.django_db

HEX8 = re.compile(r"[0-9a-f]{8}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.addFilter(logctx.ContextFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def lines():
    """Every record on the service's two loggers (``.http`` does not propagate)."""
    capture = _Capture()
    loggers = [logging.getLogger("billing-api"), logging.getLogger("billing-api.http")]
    for lg in loggers:
        lg.addHandler(capture)
    yield capture.records
    for lg in loggers:
        lg.removeHandler(capture)


def _access(records):
    (record,) = [r for r in records if r.name == "billing-api.http"]
    return record


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def test_well_formed_trace_is_kept_and_echoed(client, lines):
    res = client.get("/healthz", HTTP_X_REQUEST_ID="deadbeef")
    assert res["X-Request-ID"] == "deadbeef"
    record = _access(lines)
    assert record.trace == "deadbeef"
    assert record.step == "healthz"
    assert re.fullmatch(r"GET /healthz 200 \d+ms", record.getMessage())
    # The two extras other code reads off the record.
    assert (record.endpoint, record.method) == ("/healthz", "GET")


@pytest.mark.parametrize("incoming", [None, "short", "DEADBEEF", "deadbeef\nINFO forged"])
def test_a_missing_or_malformed_trace_is_replaced(client, lines, incoming):
    headers = {"HTTP_X_REQUEST_ID": incoming} if incoming is not None else {}
    res = client.get("/healthz", **headers)
    assert HEX8.fullmatch(res["X-Request-ID"])
    assert _access(lines).trace == res["X-Request-ID"]


def test_member_request_carries_user_entity_and_route(client, lines, user, entity):
    res = client.get(
        f"/api/entities/{entity.id}/modules", **_bearer(make_token(user.id, entity_id=entity.id))
    )
    assert res.status_code == 200
    record = _access(lines)
    assert (record.user, record.entity) == (str(user.id), str(entity.id))
    assert record.step == "module_page"


def test_a_company_without_a_role_is_never_bound(client, lines, other_user, entity):
    res = client.get(
        "/api/me/subscriptions", **_bearer(make_token(other_user.id, entity_id=entity.id))
    )
    assert res.status_code == 200
    record = _access(lines)
    assert (record.user, record.entity) == (str(other_user.id), "-")


def test_a_refused_request_names_nobody_but_keeps_its_trace(client, lines, user):
    res = client.get(
        "/api/me/subscriptions", HTTP_X_REQUEST_ID="feedface", **_bearer(make_token(user.id, minutes=-1))
    )
    assert res.status_code == 401
    rejected = [r for r in lines if r.getMessage().startswith("Auth rejected")]
    assert rejected and all(r.trace == "feedface" and r.user == "-" for r in rejected)
    assert (_access(lines).user, _access(lines).entity) == ("-", "-")


def test_nothing_outlives_the_request(client, user):
    client.get("/api/me/subscriptions", **_bearer(make_token(user.id)))
    assert logctx.current() == {"trace": "-", "user": "-", "entity": "-", "step": "-"}


def test_calls_to_flask_carry_the_trace(monkeypatch, settings):
    import requests

    from core import flask_client

    settings.PETTY_CASH_URL = "http://flask.test"
    sent = {}

    def fake_request(method, url, headers=None, **kwargs):
        sent.update(headers)
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(requests, "request", fake_request)
    logctx.start_trace("a1b2c3d4")
    try:
        request = SimpleNamespace(headers={"Authorization": "Bearer t"})
        assert flask_client.forward(request, "/api/x") == ({"ok": True}, 200)
    finally:
        logctx.reset()
    assert sent["X-Request-ID"] == "a1b2c3d4"


def test_each_scheduler_pass_has_its_own_trace(monkeypatch):
    from billing import scheduler
    from billing.services import daily

    @contextmanager
    def lock():
        yield True

    seen = []
    monkeypatch.setattr(daily, "daily_lock", lock)
    monkeypatch.setattr(daily, "run_daily", lambda now, **kw: seen.append(logctx.current()) or {})
    scheduler.run_pass_now(mode=scheduler.LIGHT)
    scheduler.run_pass_now(mode=scheduler.FULL)
    assert [s["step"] for s in seen] == ["scheduler.light", "scheduler.full"]
    assert all(HEX8.fullmatch(s["trace"]) for s in seen)
    assert seen[0]["trace"] != seen[1]["trace"]
    assert logctx.current()["trace"] == "-"


def test_a_sign_in_logs_nothing_derived_from_the_key(client, lines, user, settings):
    import hashlib

    res = client.get("/api/me/subscriptions", **_bearer(make_token(user.id)))
    assert res.status_code == 200
    key_hash = hashlib.sha256(settings.SECRET_KEY.encode()).hexdigest()[:16]
    text = "\n".join(r.getMessage() for r in lines)
    assert key_hash not in text and "key_len" not in text and "key_hash" not in text
