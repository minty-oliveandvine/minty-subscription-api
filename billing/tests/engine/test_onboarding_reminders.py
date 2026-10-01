"""The "finish setting up your company" reminder (``billing.services.onboarding_reminders``).

The user's rules (2026-10-01): 1, 3 and 7 days after the last activity, to the person who
started the company, at most once each per quiet spell, never once setup is finished - and
not to the companies abandoned long before this shipped.

``entities.updated_at`` belongs to a trigger, and a test transaction freezes ``now()``, so
the tests move the CLOCK (``now``) past the row's real ``updated_at`` rather than moving the
row back in time.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail as django_mail
from django.core.management import call_command


@pytest.fixture
def mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.MINTY_PUBLIC_URL = "https://app.minty.test"
    settings.DEFAULT_FROM_EMAIL = "noreply@minty.test"
    settings.ONBOARDING_EMAIL = "onboarding@minty.test"
    django_mail.outbox = []
    return django_mail


def _user(email, *, active=True):
    from shared_models.models import User

    uid = str(uuid.uuid4())
    User.objects.create(id=uid, email=email, username=email, first_name="Sam", last_name="Lee",
                        password="not-checked-here", system_role="normal", approved=True,
                        is_active=active)
    return uid


def _member(user_id, entity_id, *, role="admin", joined=None, approved=True):
    from shared_models.models import UserEntity

    UserEntity.objects.create(user_id=user_id, entity_id=entity_id, role=role, approved=approved,
                              created_at=joined or datetime(2026, 9, 1, tzinfo=UTC))


def _company(*, status="onboarding", saved_step=None, name="Acme Shop", creator=None):
    """A company in setup, created by ``creator`` (a new user when not given). Returns
    ``(entity_id, creator_id, updated_at)``."""
    from shared_models.models import Entity

    eid = str(uuid.uuid4())
    Entity.objects.create(id=eid, name=name, status=status, onboarding_saved_step=saved_step)
    creator = creator or _user(f"owner-{eid[:8]}@test.com")
    _member(creator, eid)
    updated_at = Entity.objects.values_list("updated_at", flat=True).get(id=eid)
    return eid, creator, updated_at


def _run(now):
    from billing.services import _context
    from billing.services.onboarding_reminders import notify_unfinished_onboarding

    with _context.scope():
        return notify_unfinished_onboarding(now)


# --- When -----------------------------------------------------------------------------


@pytest.mark.parametrize("hours, stage", [
    (0, None), (23, None), (24, 1), (71, 1), (72, 3), (167, 3), (168, 7), (215, 7), (216, None),
])
def test_each_reminder_has_its_window(hours, stage):
    from billing.services.onboarding_reminders import stage_for

    assert stage_for(timedelta(hours=hours)) == stage


@pytest.mark.django_db
def test_nothing_goes_out_before_a_day_of_quiet_or_after_the_last_window(mail):
    eid, _, updated = _company()

    assert _run(updated + timedelta(hours=23))["reminded"] == []
    assert _run(updated + timedelta(days=9))["reminded"] == []
    assert mail.outbox == []


@pytest.mark.django_db
def test_three_reminders_then_silence(mail):
    """Day 1, 3 and 7 - each once, however often the pass runs in between."""
    eid, creator, updated = _company()

    for day in (1, 1.5, 2, 3, 4, 6, 7, 8, 10, 20):
        _run(updated + timedelta(days=day))

    assert len(mail.outbox) == 3
    from shared_models.models import SubscriptionEmailLog

    keys = sorted(SubscriptionEmailLog.objects.filter(event="onboarding_reminder")
                  .values_list("dedupe_key", flat=True))
    assert [k.rsplit(":", 1)[1] for k in keys] == ["1", "3", "7"]
    assert all(k.startswith(f"{eid}:{creator}:") for k in keys)


@pytest.mark.django_db
def test_a_finished_company_is_never_reminded(mail):
    _company(status="connected")
    _, _, updated = _company(status="disconnected")

    assert _run(updated + timedelta(days=1))["reminded"] == []
    assert mail.outbox == []


# --- Who ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_it_goes_to_the_person_who_started_the_company(mail):
    """The earliest approved admin - not an admin who joined later, nor an earlier
    non-admin - and to that person, not the company's business email."""
    from shared_models.models import Entity

    creator = _user("creator@test.com")
    eid, _, updated = _company(creator=creator)
    Entity.objects.filter(id=eid).update(business_email="accounts@acme.test")
    updated = Entity.objects.values_list("updated_at", flat=True).get(id=eid)
    _member(_user("later-admin@test.com"), eid, joined=datetime(2026, 9, 5, tzinfo=UTC))
    _member(_user("early-cashier@test.com"), eid, role="cashier",
            joined=datetime(2026, 8, 1, tzinfo=UTC))

    result = _run(updated + timedelta(days=1))

    assert [r["user_id"] for r in result["reminded"]] == [creator]
    (message,) = mail.outbox
    assert message.to == ["creator@test.com"]


@pytest.mark.django_db
def test_an_inactive_creator_is_skipped_not_replaced(mail):
    """A deactivated founder is not swapped for whoever else is an admin."""
    creator = _user("gone@test.com", active=False)
    eid, _, updated = _company(creator=creator)
    _member(_user("other-admin@test.com"), eid, joined=datetime(2026, 9, 5, tzinfo=UTC))

    result = _run(updated + timedelta(days=1))

    assert result["reminded"] == []
    assert [s["reason"] for s in result["skipped"]] == ["no active creator"]
    assert mail.outbox == []


# --- What -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_email_lists_what_is_left_and_links_back_into_setup(mail):
    eid, _, updated = _company(saved_step=4, name="Olive  &\nVine Ltd")

    _run(updated + timedelta(days=1))

    (message,) = mail.outbox
    assert message.subject == "Finish setting up Olive & Vine Ltd on Minty"
    assert message.from_email == "onboarding@minty.test"
    html = message.html
    assert f'href="https://app.minty.test/entity/{eid}"' in html
    assert "Connect Xero" in html and "Set up your accounts" in html
    assert "Choose your modules" not in html and "Invite your team" not in html
    assert "Olive &amp; Vine Ltd" in html
    assert "these reminders stop" in html
    assert {a.headers["X-Attachment-Id"] for a in message.attachments} == {"minty-logo", "minty-art"}


@pytest.mark.parametrize("saved, titles", [
    (None, 4), ("x", 4), (1, 4), (2, 4), (3, 3), (4, 2), (5, 1), (8, 1), (9, 0),
])
def test_the_blocks_start_at_the_step_they_saved_on(saved, titles):
    from billing.services.notify import remaining_steps

    blocks = remaining_steps(saved)
    assert len(blocks) == titles
    assert [b["number"] for b in blocks] == [2, 3, 4, 5][4 - titles:]


@pytest.mark.django_db
def test_a_company_with_nothing_left_still_reads_right(mail):
    """Saved at 9 ("All set") but never finalized: no "Still to do" at all."""
    _, _, updated = _company(saved_step=9)

    _run(updated + timedelta(days=1))

    (message,) = mail.outbox
    assert "Still to do" not in message.html
    assert message.html.count("Continue setup") == 1


@pytest.mark.django_db
def test_billing_mail_keeps_its_own_sender(mail, settings):
    from billing.services import notify

    settings.SUBSCRIPTION_EMAIL = "subscription@minty.test"
    assert notify.sender_for(notify.PAYMENT_RECOVERED) == "subscription@minty.test"
    assert notify.sender_for(notify.ONBOARDING_REMINDER) == "onboarding@minty.test"
    settings.ONBOARDING_EMAIL = None
    assert notify.sender_for(notify.ONBOARDING_REMINDER) == "noreply@minty.test"


# --- Wiring ---------------------------------------------------------------------------


def test_the_daily_pass_and_the_command_run_it():
    from billing.management.commands.subscriptions import JOBS
    from billing.services import daily

    assert daily.JOB_ORDER[0] == daily.NOTIFY_ONBOARDING
    assert daily.NOTIFY_ONBOARDING in daily._RUNNERS
    assert daily.NOTIFY_ONBOARDING not in daily.LIGHT_ORDER
    assert daily.NOTIFY_ONBOARDING in JOBS


@pytest.mark.django_db
def test_the_preview_renders_every_email_and_claims_nothing(tmp_path, mail):
    from billing.services import notify
    from shared_models.models import SubscriptionEmailLog

    call_command("preview_emails", "--out", str(tmp_path))

    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == len(notify.EVENTS)
    assert any(name.endswith("onboarding_reminder.html") for name in files)
    assert not SubscriptionEmailLog.objects.exists()
    assert mail.outbox == []
