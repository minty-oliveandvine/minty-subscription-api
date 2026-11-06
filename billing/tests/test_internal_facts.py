"""The read-only facts minty-accounts-api's hub asks for (``billing/internal.py``; Phase 5 of the
Flask retirement). Flask's ``store_ro.py`` answered these from its own copy of the rules; the
expectations are its, restated against this service's rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from django.conf import settings
from django.test import Client
from minty_db.models import Entity, EntityModuleSubscription, SubscriptionTransfer

from billing.tests.conftest import make_token

pytestmark = pytest.mark.django_db
BASE = "/api/internal/subscriptions"
PC, BILL = "PETTY_CASH", "PAYMENT_REQUEST"


def assertion(scope="subscription-facts", minutes=1, secret=None) -> dict:
    token = jwt.encode({"scope": scope, "entity_id": "*", "exp": datetime.now(UTC) + timedelta(minutes=minutes)},
                       secret or settings.SECRET_KEY, algorithm="HS256")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def row(entity, code=PC, *, phase="trial", payer=None, trial_end=None, until=None, billed=None):
    return EntityModuleSubscription.objects.create(
        entity_id=entity.id, function_code=code, phase=phase, payer_user_id=payer.id if payer else None,
        trial_end=trial_end, app_access_until=until, first_billed_at=billed)


def ask(entity_ids, **auth):
    return Client().post(f"{BASE}/attention", {"entity_ids": [str(i) for i in entity_ids]},
                         content_type="application/json", **(auth or assertion()))


NOW = datetime.now(UTC)


# ---- who may ask -----------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, assertion(scope="xero-access-token"), assertion(minutes=-5),
                                     assertion(secret="not-the-key"),
                                     {"HTTP_AUTHORIZATION": f"Bearer {make_token(uuid.uuid4())}"}])
def test_only_a_scoped_backend_assertion_is_answered(entity, headers):
    resp = Client().get(f"{BASE}/entities/{entity.id}", **headers)
    assert (resp.status_code, resp.json()) == (401, {"error": "Unauthorized"})


# ---- attention -------------------------------------------------------------------------------


def test_a_running_trial_and_a_cancelled_one_badge_with_their_end(entity, other_user):
    other = Entity.objects.create(id=uuid.uuid4(), name="Second Co", country_code="HK", status="connected")
    ends = NOW + timedelta(days=9)
    row(entity, PC, trial_end=ends)
    row(other, BILL, phase="scheduled_cancel", trial_end=NOW + timedelta(days=20), until=ends)

    body = ask([entity.id, other.id]).json()["attention"]

    assert body[str(entity.id)] == {"trials": {PC: ends.isoformat()}, "payment_failed": False}
    assert body[str(other.id)] == {"trials": {BILL: ends.isoformat()}, "payment_failed": False}


def test_a_billed_module_or_an_ended_trial_is_no_trial(entity):
    row(entity, PC, phase="active", trial_end=NOW + timedelta(days=5), billed=NOW - timedelta(days=30))
    row(entity, BILL, trial_end=NOW - timedelta(days=2))  # past the closing window
    assert ask([entity.id]).json()["attention"][str(entity.id)] == {"trials": {}, "payment_failed": False}


def test_a_past_due_module_still_granted_and_told_reads_payment_failed(entity, user):
    # No card on file counts as told (dunning.told_of_failure), and the promise runs to next week.
    row(entity, PC, phase="past_due", payer=user, until=NOW + timedelta(days=7), billed=NOW - timedelta(days=40))
    assert ask([entity.id]).json()["attention"][str(entity.id)]["payment_failed"] is True


def test_past_due_whose_access_has_ended_is_no_longer_flagged(entity, user):
    row(entity, PC, phase="past_due", payer=user, until=NOW - timedelta(days=1), billed=NOW - timedelta(days=40))
    assert ask([entity.id]).json()["attention"][str(entity.id)]["payment_failed"] is False


def test_a_company_with_no_rows_is_absent(entity):
    assert ask([entity.id]).json() == {"attention": {}}


@pytest.mark.parametrize("body", [{}, {"entity_ids": "x"}, {"entity_ids": ["not-a-uuid"]},
                                  {"entity_ids": [str(uuid.uuid4()) for _ in range(501)]}])
def test_a_bad_list_is_refused(body):
    resp = Client().post(f"{BASE}/attention", body, content_type="application/json", **assertion())
    assert resp.status_code == 422


# ---- one company -----------------------------------------------------------------------------


def test_the_company_facts_name_the_payer_the_billed_modules_and_the_handover(entity, user, other_user):
    row(entity, PC, phase="active", payer=user, billed=NOW - timedelta(days=10))
    row(entity, BILL, phase="scheduled_cancel", payer=user, trial_end=NOW + timedelta(days=3))  # a cancelled trial
    SubscriptionTransfer.objects.create(entity_id=entity.id, from_user_id=user.id, to_user_id=other_user.id,
                                        status="pending", expires_at=NOW + timedelta(days=7))

    body = Client().get(f"{BASE}/entities/{entity.id}", **assertion()).json()

    assert body == {"payer": str(user.id), "payers": [str(user.id)], "pending_transfer_to": str(other_user.id),
                    "billed_modules": [PC]}


def test_a_company_with_no_subscription_has_no_payer(entity):
    assert Client().get(f"{BASE}/entities/{entity.id}", **assertion()).json() == {
        "payer": None, "payers": [], "pending_transfer_to": None, "billed_modules": []}


# ---- one payer -------------------------------------------------------------------------------


def test_every_company_a_person_pays_for_in_any_phase(entity, user):
    other = Entity.objects.create(id=uuid.uuid4(), name="Second Co", country_code="HK", status="connected")
    row(entity, PC, phase="cancelled", payer=user)
    row(other, PC, phase="past_due", payer=user)
    body = Client().get(f"{BASE}/payers/{user.id}/entities", **assertion()).json()
    assert body == {"entities": sorted([{"id": str(entity.id), "name": "Payer Trading Co"},
                                        {"id": str(other.id), "name": "Second Co"}], key=lambda e: e["id"])}
