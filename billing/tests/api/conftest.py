"""Fixtures for the router suite (Part 2 step 3).

The tests under this folder are the HTTP halves of Minty's ``tests/test_*`` files - the ones
step 2 could not port because they drove Flask's test client - now driven through Django's
against the ninja routers. Same names, same assertions, and the same stubs: the service
fixtures they lean on (``wallet``, ``payer_portal``) are IMPORTED from their engine twins
rather than copied, so the two halves of one Flask file keep stubbing the same seams.

Three differences from Flask's client, pinned once here so the tests need not repeat them:

* the token names a REAL user row (``user`` from ``billing/tests/conftest.py``) because
  ``BearerAuth`` loads it; Flask's tests monkeypatched ``User.query.get`` and signed ``u1``;
* a preflight carries ``Origin`` and ``Access-Control-Request-Method`` like a browser's does,
  and django-cors-headers answers it 200 where Flask-CORS answered 204;
* an error is never re-raised into the test: every view catches its own surprises, and the
  client's ``raise_request_exception`` stays on so an uncaught one fails loudly.
"""

from __future__ import annotations

import json

import pytest

from billing.services import _context
from billing.tests.conftest import make_token

ORIGIN = "http://localhost:3002"


class _AppShim:
    """Just enough of Flask's ``app`` for the imported engine fixtures (``wallet``,
    ``payer_portal``), which take it for the scope it used to carry."""

    def app_context(self):
        return _context.scope()

    def test_request_context(self, *args, **kwargs):
        return _context.scope()


@pytest.fixture
def app():
    return _AppShim()


@pytest.fixture(autouse=True)
def _no_stripe(monkeypatch):
    """No router test reaches Stripe; an unstubbed ``get_stripe()`` fails loudly."""
    # ``billing_gateway`` too, before anything is patched: it binds ``get_stripe`` by name on
    # first import, and a first import under a test's fake kept that fake for the rest of the
    # run (see the engine conftest's ``_no_stripe``).
    from billing.services import billing_gateway, stripe_client  # noqa: F401

    def _refuse():
        raise AssertionError("a test reached stripe_client.get_stripe(); stub it")

    monkeypatch.setattr(stripe_client, "get_stripe", _refuse)


def bearer(user, **claims):
    """``Authorization: Bearer <token>`` for ``user`` (a row), unscoped unless told otherwise."""
    return {"HTTP_AUTHORIZATION": f"Bearer {make_token(user.id, **claims)}"}


def post_json(client, path, payload, **headers):
    return client.post(path, data=json.dumps(payload), content_type="application/json", **headers)


def preflight(client, path, method="POST"):
    """What a browser sends before a cross-origin call with a bearer header."""
    return client.options(
        path,
        HTTP_ORIGIN=ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD=method,
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
    )
