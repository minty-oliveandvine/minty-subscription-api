"""HTTP-level smoke tests against a RUNNING minty-subscription-api.

Nothing is started here (the same rule as the Playwright suites of the web apps): point
``E2E_BASE_URL`` at a service that is already up - ``manage.py runserver 8000``, the docker
stack, or a deployment - and run ``pytest e2e``. Every test skips with a reason when the
service is not answering, and the token tests skip without the credentials.

    E2E_BASE_URL                  default http://127.0.0.1:8000 (see the note at BASE_URL)
    E2E_MINTY_WEB_URL             the minty-web origin the CORS assertions use, default http://localhost:3000
    E2E_PAYMENT_REQUEST_WEB_URL   the payment-request web origin (the notice's caller), default http://localhost:3020
    E2E_JWT_SECRET                the SECRET_KEY shared with Minty - mints a Flask-shaped token (never commit it)
    E2E_MINTY_USER                a real user id in the service's database (Minty/scripts/e2e_seed.py --print)
    E2E_MINTY_ENTITY              optional: a company that user administers, for the module-page check

WHY THIS MINTS ITS OWN TOKEN: a test cannot go through Minty's login (email OTP), but it
holds the same SECRET_KEY, so it mints the same token Flask's ``_generate_module_token``
would. Nothing is bypassed - the service verifies signature, expiry and the user exactly as
it does Flask's.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import requests

# 127.0.0.1, not localhost: on Windows "localhost" resolves to ::1 first, the dev server listens
# on IPv4 only, and every request stalls ~2 s before falling back (a 0.2 s suite took 18 s).
BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WEB_ORIGIN = os.environ.get("E2E_MINTY_WEB_URL", "http://localhost:3000").rstrip("/")
# The payment-request UI (minty-payment-request-web), the notice's caller.
PAYMENTS_ORIGIN = os.environ.get("E2E_PAYMENT_REQUEST_WEB_URL", "http://localhost:3020").rstrip("/")


def reachable() -> bool:
    try:
        return requests.get(f"{BASE_URL}/healthz", timeout=5).status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(scope="session")
def base_url() -> str:
    if not reachable():
        pytest.skip(f"minty-subscription-api is not answering at {BASE_URL} (set E2E_BASE_URL)")
    return BASE_URL


@pytest.fixture(scope="session")
def credentials() -> dict:
    secret = os.environ.get("E2E_JWT_SECRET")
    user_id = os.environ.get("E2E_MINTY_USER")
    if not secret or not user_id:
        pytest.skip("set E2E_JWT_SECRET (Minty SECRET_KEY) and E2E_MINTY_USER for the token tests")
    return {
        "secret": secret,
        "user_id": user_id,
        "entity_id": os.environ.get("E2E_MINTY_ENTITY", ""),
    }


def mint(secret: str, user_id: str, *, entity_id: str = "", minutes: int = 30, **overrides) -> str:
    """The claims Minty puts in the module token."""
    import jwt

    now = datetime.now(UTC)
    claims = {
        "user_id": user_id,
        "entity_id": entity_id,
        "xero_org_id": "",
        "role": "admin",
        "system_role": "normal",
        "module": "billing",
        "sid": "e2e",
        "billing_enabled": True,
        "petty_cash_enabled": True,
        "exp": now + timedelta(minutes=minutes),
        "iat": now,
    }
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256")
