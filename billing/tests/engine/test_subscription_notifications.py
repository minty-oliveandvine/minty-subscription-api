"""Tests for billing email.

The subscription engine used to be silent: every lifecycle event was computed, stored
and acted on, and the customer found out either by opening the settings page or by being
locked out. These cover the three properties that make wiring email into it safe.

1. **It never sends twice.** The jobs that trigger these emails are documented as safe to
   re-run at any cadence, and that is only true of state reconciliation — a send is not
   idempotent. Most of the file is this.
2. **It never raises.** These are called from inside jobs that move money. A dead SMTP
   host must not abort a renewal run halfway through a batch of payers.
3. **It never says the wrong thing.** In particular the trial-ending warning, whose whole
   value is telling a customer whether their trial WILL convert — getting that backwards
   either nags someone who was fine or reassures someone about to lose access.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail as django_mail

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(db):
    """Flask's per-test database (rows DELETEd afterwards) is pytest-django's ``db``
    (a transaction rolled back afterwards). The helpers below take it for parity."""
    return _DbShim()


class _DbShim:
    """What the ported helpers still reach for: ``db.session.refresh(row)``."""

    class session:  # noqa: N801
        @staticmethod
        def refresh(row):
            row.refresh_from_db()

        @staticmethod
        def commit():
            pass


def _company(db, label, **fields):
    """A real company row for a test label: entity_module_subscription.entity_id is a uuid FK
    to entities since C7, so the labels the tests used to write ("e1") become rows.
    ``fields`` (``business_email``, ``timezone``) are set on it."""
    from shared_models.models import Entity

    eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"notify-company-{label}"))
    entity, _ = Entity.objects.get_or_create(
        id=eid, defaults={"name": f"Company {label}", "status": "disconnected"}
    )
    if fields:
        for name, value in fields.items():
            setattr(entity, name, value)
        entity.save(update_fields=list(fields))
    return eid


def _make_payer(db, email="payer@test.com"):
    from shared_models.models import User

    uid = str(uuid.uuid4())
    User.objects.create(
        id=uid,
        email=email,
        username=f"{uid}@u",
        first_name="Sam",
        last_name="Payer",
        password="not-checked-here",
        system_role="normal",
        approved=True,
    )
    return uid


class _Mail:
    """Flask-Mail's fake became a view over Django's in-memory outbox (pytest-django runs
    every test on the locmem backend): ``sent`` is ``django.core.mail.outbox``; ``fail``
    makes the backend raise, like a dead SMTP host."""

    def __init__(self, fail=False):
        self.fail = fail

    @property
    def sent(self):
        return django_mail.outbox


@pytest.fixture
def mail(settings, monkeypatch):
    """Install the failing-on-demand outbox and a public base url for the duration of a
    test. ``SUBSCRIPTION_EMAIL`` is cleared so the sender tests below start from the
    fallback whatever the developer's shell exports."""
    from django.core.mail.backends import locmem

    fake = _Mail()
    original = locmem.EmailBackend.send_messages

    def send_messages(self, messages):
        if fake.fail:
            raise RuntimeError("smtp refused")
        return original(self, messages)

    monkeypatch.setattr(locmem.EmailBackend, "send_messages", send_messages)
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.MINTY_PUBLIC_URL = "https://app.minty.test"
    settings.DEFAULT_FROM_EMAIL = "noreply@minty.test"
    settings.SUBSCRIPTION_EMAIL = None
    django_mail.outbox = []
    return fake


# ---------------------------------------------------------------------------
# Deduplication — the property the whole email log exists for
# ---------------------------------------------------------------------------


def test_the_same_notification_is_only_ever_sent_once(app, db_session, mail):
    """``retry-dunning`` and ``sweep-access`` are safe to run hourly BECAUSE re-running
    them changes nothing. Email is the one effect that breaks that, so it is deduped in
    the database rather than by the runner remembering."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)

        first = notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-1", context={}
        )
        second = notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-1", context={}
        )

        assert first is True
        assert second is False
        assert len(mail.sent) == 1


def test_a_different_dedupe_key_is_a_different_email(app, db_session, mail):
    """A payer who lapses, recovers, and lapses again months later must hear about the
    second episode. The key is per-episode for exactly this."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)

        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-1", context={})
        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-2", context={})

        assert len(mail.sent) == 2


def test_a_send_that_failed_is_retried_on_the_next_run(app, db_session, mail):
    """A mail outage must not silently swallow a dunning notice. The claim row is left at
    ``failed``, which the next pass picks up — unlike a successful one, which is never
    touched again."""
    from billing.services import notify
    from billing.services.notify import STATUS_FAILED, STATUS_SENT
    from shared_models.models import SubscriptionEmailLog

    with app.app_context():
        payer = _make_payer(db_session)

        mail.fail = True
        assert notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-1", context={}
        ) is False
        row = SubscriptionEmailLog.objects.get(dedupe_key="ep-1")
        assert row.status == STATUS_FAILED
        assert "smtp refused" in (row.error or "")

        mail.fail = False
        assert notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-1", context={}
        ) is True
        assert len(mail.sent) == 1
        assert SubscriptionEmailLog.objects.get(dedupe_key="ep-1").status == (
            STATUS_SENT
        )


def test_one_claim_row_per_notification_not_one_per_attempt(app, db_session, mail):
    """The unique constraint is on (event, dedupe_key). A retried send reuses its row, so
    a fortnight of SMTP trouble leaves one row, not fourteen."""
    from billing.services import notify
    from shared_models.models import SubscriptionEmailLog

    with app.app_context():
        payer = _make_payer(db_session)
        mail.fail = True
        for _ in range(3):
            notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="ep-1", context={})

        assert SubscriptionEmailLog.objects.filter(dedupe_key="ep-1").count() == 1


# ---------------------------------------------------------------------------
# Never raising — these run inside jobs that move money
# ---------------------------------------------------------------------------


def test_a_dead_mail_server_does_not_raise(app, db_session, mail):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        mail.fail = True
        assert notify.notify(
            payer, notify.RENEWAL_FAILED, dedupe_key="k", context={"total": 40000}
        ) is False


def test_no_mail_configured_does_not_raise(app, db_session, settings):
    """Flask's "no mail extension" is Django's console backend - the settings' own default
    when EMAIL_HOST is unset. Skipped, not spent: no dedupe row is written, so the notice
    still goes out on the first run that has a mail host."""
    from billing.services import notify
    from shared_models.models import SubscriptionEmailLog

    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    with app.app_context():
        payer = _make_payer(db_session)
        assert notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={}
        ) is False
        assert SubscriptionEmailLog.objects.filter(dedupe_key="k").count() == 0


def test_an_smtp_backend_without_a_host_is_not_configured_either(settings):
    from billing.services import notify

    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = ""
    assert notify.mail_configured() is False
    settings.EMAIL_HOST = "smtp.example.test"
    assert notify.mail_configured() is True


def test_a_payer_with_no_email_address_is_skipped_without_claiming(app, db_session, mail):
    """Checked before the claim, so an address added later still gets the email rather
    than deduping against a send that never happened."""
    from billing.services import notify
    from shared_models.models import SubscriptionEmailLog, User

    with app.app_context():
        payer = _make_payer(db_session, email="nobody@test.com")
        User.objects.filter(id=payer).update(email=None, xero_email=None)

        assert notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={}
        ) is False
        assert SubscriptionEmailLog.objects.filter(dedupe_key="k").count() == 0


def test_every_event_renders_from_an_empty_context(app, db_session, mail):
    """Nothing in ``_COPY`` may require a context key to render.

    These are composed inside exception handlers in billing jobs, where the context is
    assembled from whatever the failure path happened to know. A builder that assumes a
    key would turn a missing entity name into a swallowed notification — the customer
    hears nothing, and the only trace is a log line."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        for index, event in enumerate(notify.EVENTS):
            assert notify.notify(
                payer, event, dedupe_key=f"k{index}", context={}
            ) is True, f"{event} did not render from an empty context"

        assert len(mail.sent) == len(notify.EVENTS)
        for message in mail.sent:
            assert message.subject
            assert "None" not in message.subject


def test_an_unknown_event_is_refused_rather_than_rendered(app, db_session, mail):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        assert notify.notify(payer, "invented_event", dedupe_key="k", context={}) is False
        assert mail.sent == []


def test_a_batch_drains_even_when_one_entry_is_broken(app, db_session, mail):
    """``notify_many`` is called at the end of a billing run. One malformed event must not
    cost the other payers their notification."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        sent = notify.notify_many([
            (payer, "invented_event", "a", {}),
            (payer, notify.PAYMENT_RECOVERED, "b", {}),
        ])
        assert sent == 1
        assert len(mail.sent) == 1


# ---------------------------------------------------------------------------
# Copy — what the customer actually reads
# ---------------------------------------------------------------------------


def test_a_trial_that_needs_action_says_what_to_do_and_by_when(app, db_session, mail,
                                                              monkeypatch):
    """The single most valuable email in the system: it arrives while the customer can
    still prevent the lapse.

    The clock is frozen rather than left to run. The heading and subject are a COUNTDOWN
    now, so a fixed ``trial_end`` would quietly start rendering "ends today" the moment
    the date passed, and the test would fail for a reason that has nothing to do with the
    behaviour it is guarding.
    """
    from billing.services import notify

    frozen = datetime(2026, 9, 1, tzinfo=UTC)
    monkeypatch.setattr(notify.clock, "now", lambda: frozen)

    with app.app_context():
        payer = _make_payer(db_session)
        notify.notify(payer, notify.TRIAL_ENDING, dedupe_key="k", context={
            "entity_id": _company(db_session, "e1"),
            "entity_name": "Olive Ltd",
            "codes": ["PETTY_CASH"],
            "trial_end": frozen + timedelta(days=7),
            "amount": 28000,
            "currency": "HKD",
            "needs_card": True,
        })

        message = mail.sent[0]
        # The subject echoes the heading rather than naming the company. That makes the
        # company name in the BODY load-bearing, not decorative: a payer with several
        # companies cannot act on "your trial" without it.
        assert message.subject == "Your Minty trial ends in 7 days"
        assert "Trial Ending in 7 days" in message.html
        assert "Olive Ltd" in message.html
        assert "choose a subscription plan" in message.html
        assert "8 Sep 2026" in message.html
        assert "Go to Manage Subscription" in message.html
        # The module page is minty-web's, reached through Flask's re-handoff (see
        # ``notify.settings_url``); the ampersand is HTML-escaped by the template.
        eid = _company(db_session, "e1")
        assert (
            f"https://app.minty.test/handoff/minty-web?next=%2Fsubscription%2Fentities%2F{eid}"
            f"%2Fmodules&amp;entity_id={eid}"
        ) in message.html


def test_the_trial_countdown_never_reads_as_a_negative(app, db_session, mail,
                                                       monkeypatch):
    """The warning job can run late. "Trial Ending in -1 days" is the kind of thing
    customers screenshot, so the countdown degrades to a phrase that stays true."""
    from billing.services import notify

    frozen = datetime(2026, 9, 1, tzinfo=UTC)
    monkeypatch.setattr(notify.clock, "now", lambda: frozen)

    with app.app_context():
        payer = _make_payer(db_session)
        for key, offset, expected in (("past", -3, "today"),
                                      ("same", 0, "today"),
                                      ("one", 1, "in 1 day")):
            notify.notify(payer, notify.TRIAL_ENDING, dedupe_key=key, context={
                "entity_id": _company(db_session, "e1"), "entity_name": "Olive Ltd", "codes": ["PAYMENT_REQUEST"],
                "trial_end": frozen + timedelta(days=offset), "needs_card": True,
            })
            assert mail.sent[-1].subject == f"Your Minty trial ends {expected}"


def test_the_two_blocked_trial_states_now_read_identically(app, db_session, mail):
    """``needs_card`` and ``needs_consent`` used to produce different wording. They no
    longer do — one "action needed" body covers both, and the in-app banner is what still
    distinguishes "add a payment method" from "confirm billing for this company".

    Asserted rather than assumed, because the merge is easy to half-undo: re-adding a
    branch here would resurrect the bug where a consent-blocked payer was told to add a
    card they could already see on their own billing page.
    """
    from billing.services import notify

    base = {
        "entity_id": _company(db_session, "e1"),
        "entity_name": "Olive Ltd",
        "codes": ["PETTY_CASH"],
        "trial_end": datetime(2026, 9, 1, tzinfo=UTC),
    }

    with app.app_context():
        payer = _make_payer(db_session)
        notify.notify(payer, notify.TRIAL_ENDING, dedupe_key="no-card",
                      context={**base, "needs_card": True, "needs_consent": False})
        notify.notify(payer, notify.TRIAL_ENDING, dedupe_key="consent",
                      context={**base, "needs_card": False, "needs_consent": True})

        no_card, consent = mail.sent[0], mail.sent[1]
        assert no_card.subject == consent.subject
        assert no_card.html == consent.html


def test_the_trial_email_states_no_price(app, db_session, mail):
    """Deliberate, and worth pinning so nobody helpfully adds one back.

    A converting trial on an entity that already pays for a sibling module is charged the
    marginal step up to the bundle, NOT the full line price. The old email printed the
    line price under "Monthly after trial" precisely because labelling it "First charge"
    would have stated a number the customer never sees on their card. The redesign drops
    the figure entirely, which is safe — but re-adding it without that context is how it
    comes back labelled wrongly.
    """
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        notify.notify(payer, notify.TRIAL_ENDING, dedupe_key="k", context={
            "entity_id": _company(db_session, "e1"), "entity_name": "Olive Ltd", "codes": ["PAYMENT_REQUEST"],
            "trial_end": datetime(2026, 9, 1, tzinfo=UTC),
            "amount": 40000, "currency": "HKD", "needs_card": True,
        })

        html = mail.sent[0].html
        assert "400.00" not in html
        assert "First charge" not in html
        assert "Monthly after trial" not in html


def test_the_trial_date_is_the_day_on_the_companys_calendar(app, db_session, mail,
                                                            monkeypatch):
    """Dates are written in the company's time zone - Hong Kong when it never set one.

    20:00 UTC on 8 September is already 9 September in Hong Kong, and the countdown has to
    agree with the date it names: counted in UTC this read "ends in 7 days ... before
    8 Sep" to someone whose calendar said the trial had a day longer.
    """
    from billing.services import notify

    frozen = datetime(2026, 9, 1, tzinfo=UTC)
    monkeypatch.setattr(notify.clock, "now", lambda: frozen)
    trial_end = datetime(2026, 9, 8, 20, tzinfo=UTC)

    with app.app_context():
        payer = _make_payer(db_session)
        for key, zone, date_line, countdown in (
            ("unset", None, "9 Sep 2026", "in 8 days"),
            ("london", "Europe/London", "8 Sep 2026", "in 7 days"),
            # A zone nobody can resolve is dated in the default, not dropped.
            ("unknown", "Mars/Olympus", "9 Sep 2026", "in 8 days"),
        ):
            notify.notify(payer, notify.TRIAL_ENDING, dedupe_key=key, context={
                "entity_id": _company(db_session, key, timezone=zone),
                "entity_name": "Olive Ltd", "codes": ["PETTY_CASH"],
                "trial_end": trial_end, "needs_card": True,
            })
            message = mail.sent[-1]
            assert date_line in message.html, key
            assert message.subject == f"Your Minty trial ends {countdown}", key


def test_links_are_dropped_rather_than_pointed_at_localhost(app, db_session, mail, settings):
    """Sent from CLI jobs, where ``url_for(_external=True)`` silently yields
    http://localhost — a link that looks real and goes nowhere."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        settings.MINTY_PUBLIC_URL = None
        notify.notify(payer, notify.TRIAL_ENDING, dedupe_key="k", context={
            "entity_id": _company(db_session, "e1"), "entity_name": "Olive Ltd",
            "codes": ["PAYMENT_REQUEST"], "trial_end": datetime(2026, 9, 1, tzinfo=UTC),
            "needs_card": True,
        })

        html = mail.sent[0].html
        assert "localhost" not in html
        assert "Go to Manage Subscription" not in html
        # The words still carry the message without the button.
        assert "choose a subscription plan" in html


def test_the_images_travel_with_the_message_not_over_http(app, db_session, mail):
    """A remote <img> is a broken grey box whenever the client blocks images — which
    Gmail and Outlook both do by default — or whenever PUBLIC_URL isn't publicly
    reachable. The first live send went out with a logo pointing at localhost:5001.

    Attached, they render offline, behind image blocking, and whatever PUBLIC_URL says.
    Both parts get the same treatment: the masthead logo and the event's illustration.
    """
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        notify._image_cache.clear()
        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={})

        message = mail.sent[0]
        assert f'src="cid:{notify.LOGO_CID}"' in message.html
        assert f'src="cid:{notify.ILLUSTRATION_CID}"' in message.html
        # Nothing is fetched over the wire to render either image.
        assert "/static/img/" not in message.html

        assert len(message.attachments) == 2
        for part, cid in zip(message.attachments,
                             (notify.LOGO_CID, notify.ILLUSTRATION_CID)):
            assert part.content_type == "image/png"
            assert part.disposition == "inline"
            assert part.data[:8] == b"\x89PNG\r\n\x1a\n"
            # Angle brackets in the header, none in the src — mismatching the pair is the
            # usual reason an inline image silently fails to resolve.
            assert part.headers["Content-ID"] == f"<{cid}>"


def test_a_missing_image_drops_it_rather_than_dangling(app, db_session, mail,
                                                       monkeypatch):
    """No bytes means no <img> — never a reference to a part that isn't attached, which
    renders as the same broken box the attachment exists to avoid.

    This is also what lets an event ship before its illustration has been drawn.
    """
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        monkeypatch.setattr(notify, "image_bytes", lambda *parts: None)
        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={})

        message = mail.sent[0]
        assert "cid:" not in message.html
        assert "<img" not in message.html
        assert message.attachments == []
        # The email still says everything it needs to.
        assert "successfully received your payment" in message.html


def test_an_event_with_no_illustration_still_sends(app, db_session, mail, monkeypatch):
    """The logo is present, the event's art is not. Half-dressed has to work, because
    three events are shipping ahead of their illustrations."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        real = notify.image_bytes
        # Matched on the exact path, not on a substring: the logo now lives under
        # ``img/email/`` too, so anything looser suppresses both and tests nothing.
        art_path = notify.illustration_path(notify.PAYMENT_RECOVERED)
        monkeypatch.setattr(
            notify, "image_bytes",
            lambda *parts: None if parts == art_path else real(*parts),
        )
        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={})

        message = mail.sent[0]
        assert f"cid:{notify.LOGO_CID}" in message.html
        assert notify.ILLUSTRATION_CID not in message.html
        assert len(message.attachments) == 1


def test_billing_mail_comes_from_its_own_address(app, db_session, mail, settings):
    """Billing does not share a From with invitations and sign-in codes.

    An invitation comes from a colleague; a dunning notice comes from the company about to
    switch your access off. Recipients filter and search on the sender, so a customer
    hunting for "that email about my payment" should not have to know it arrived from an
    address with `invite` in it.
    """
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        settings.SUBSCRIPTION_EMAIL = "subscription@minty.test"
        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={})

        assert mail.sent[0].sender == "subscription@minty.test"


def test_billing_mail_falls_back_to_the_shared_address(app, db_session, mail, settings):
    """An environment that has not yet verified the dedicated sender with the relay keeps
    sending. An unverified From is rejected or spam-filed and ``notify`` swallows SMTP
    errors by design, so failing over to the working address beats failing invisibly."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        settings.SUBSCRIPTION_EMAIL = None
        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={})

        assert mail.sent[0].sender == settings.DEFAULT_FROM_EMAIL


def test_an_inline_logo_makes_the_message_related_not_mixed(app):
    """``multipart/mixed`` says "a body, and separately some files" — the wrong statement
    about an image the body references by cid. Under it several Outlook builds render the
    logo inline AND list it as a paperclip, so a billing notice looks like it encloses a
    file. ``multipart/related`` (RFC 2387) says the parts are one document."""
    from billing.services import notify

    with app.app_context():
        message = notify.InlineImageMessage(
            subject="s", sender="a@b.c", recipients=["x@y.z"], html="<p>hi</p>"
        )
        message.attach("logo.png", "image/png", b"\x89PNG\r\n\x1a\n", "inline",
                       headers={"Content-ID": f"<{notify.LOGO_CID}>"})
        raw = message.as_bytes().decode("utf-8", "replace")

        assert 'Content-Type: multipart/related; type="multipart/alternative"' in raw
        assert "multipart/mixed" not in raw


def test_a_real_enclosure_stays_mixed(app):
    """The flip is only correct for parts the body references. A genuine enclosure — a
    PDF invoice, say — belongs in ``mixed``, and must not be dragged along."""
    from billing.services import notify

    with app.app_context():
        message = notify.InlineImageMessage(
            subject="s", sender="a@b.c", recipients=["x@y.z"], html="<p>hi</p>"
        )
        message.attach("invoice.pdf", "application/pdf", b"%PDF-1.4", "attachment")
        raw = message.as_bytes().decode("utf-8", "replace")

        assert "multipart/mixed" in raw
        assert "multipart/related" not in raw


def test_an_unreachable_public_url_is_reported_once(app, db_session, mail, monkeypatch, settings):
    """A developer's PUBLIC_URL reaching production mail is otherwise silent: every
    message looks perfect and every button lands on a host only the sender can resolve.

    Once per process, not per send — a nightly run mailing forty payers must not print
    forty copies of one configuration problem."""
    from billing.services import notify

    warnings = []
    monkeypatch.setattr(notify, "_warned_unreachable", False)
    monkeypatch.setattr(
        notify.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
    )

    with app.app_context():
        settings.MINTY_PUBLIC_URL = "https://localhost:5001"
        payer = _make_payer(db_session)
        for index in range(3):
            notify.notify(
                payer, notify.PAYMENT_RECOVERED, dedupe_key=f"k{index}", context={}
            )

        assert len(mail.sent) == 3
        unreachable = [w for w in warnings if "no recipient can reach" in w]
        assert len(unreachable) == 1, warnings


def test_a_xero_only_user_is_still_reachable(app, db_session, mail):
    """A user who signed up through Xero may have no personal ``email`` at all, and is
    just as capable of owing money as anyone else."""
    from billing.services import notify
    from shared_models.models import User

    with app.app_context():
        payer = _make_payer(db_session)
        User.objects.filter(id=payer).update(email=None, xero_email="sam@xero.test")

        assert notify.notify(
            payer, notify.PAYMENT_RECOVERED, dedupe_key="k", context={}
        ) is True
        assert mail.sent[0].to == ["sam@xero.test"]


# ---------------------------------------------------------------------------
# Where the emails go — a company's inbox before the payer's own
# ---------------------------------------------------------------------------
#
# The money emails follow the user's order (2026-09-30): the billing account's billing
# email, else the business email every company on the account shares, else the payer.
# The invoice's Bill to follows the same rule, so the inbox an email reaches is the one
# its invoice names. The trial warning goes to its company's business email, else the
# payer; handover notices always go to the person.

DECLINE = {"total": 40000, "currency": "HKD"}


def _billing_account(payer, *, email="accounts@olive.test", company="Olive Holdings Ltd"):
    from billing.services import store

    return store.create_billing_account(
        payer, f"pm_{uuid.uuid4().hex[:8]}", billing_email=email, billing_company=company
    )


def _on_account(db, payer, account, entity_id, *, paid_for=True):
    """Put a company on ``account``. ``paid_for=False`` is a company that has LEFT: its
    nomination kept as history, no module row of this payer's any more."""
    from shared_models.models import EntityBillingGroup, EntityModuleSubscription

    EntityBillingGroup.objects.create(
        entity_id=entity_id, payer_user_id=payer, billing_group_id=account.id,
        source="chosen",
    )
    if paid_for:
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()), entity_id=entity_id, function_code="PETTY_CASH",
            payer_user_id=payer, phase="trial",
            trial_end=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_a_money_email_goes_to_the_accounts_billing_email(app, db_session, mail):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer)

        assert notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                             context={**DECLINE, "billing_group_id": account.id}) is True

        assert mail.sent[0].to == ["accounts@olive.test"]


@pytest.mark.parametrize("event", ["renewal_failed", "dunning_retry_failed",
                                   "payment_recovered"])
def test_every_money_email_goes_to_the_billing_email(app, db_session, mail, event):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer)

        assert notify.notify(payer, event, dedupe_key="k",
                             context={"billing_group_id": account.id}) is True

        assert mail.sent[0].to == ["accounts@olive.test"]


def test_an_account_with_no_billing_email_mails_the_payer_as_before(app, db_session, mail):
    """The common case: an account opened without one (every backfilled account), whose
    companies gave no business email either."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer, email=None)
        _on_account(db_session, payer, account, _company(db_session, "e1"))

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})

        assert mail.sent[0].to == ["payer@test.com"]


def test_with_no_billing_email_the_business_email_its_companies_share_is_used(
    app, db_session, mail
):
    """Case and blanks do not split an inbox: "AP@…" and "ap@…" are one address, and a
    company that gave none does not stand in the way."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer, email=None)
        for label, email in (("e1", "ap@group.test"), ("e2", " AP@Group.test "), ("e3", None)):
            _on_account(db_session, payer, account,
                        _company(db_session, label, business_email=email))

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})

        assert mail.sent[0].to == ["AP@Group.test"]


def test_companies_with_different_business_emails_mail_the_payer(app, db_session, mail):
    """Never one company's inbox about the charges for all of them: an account can pay for
    separate businesses, and picking one would show it another's bill."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer, email=None)
        _on_account(db_session, payer, account,
                    _company(db_session, "e1", business_email="ap@olive.test"))
        _on_account(db_session, payer, account,
                    _company(db_session, "e2", business_email="ap@lemon.test"))

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})

        assert mail.sent[0].to == ["payer@test.com"]


def test_a_company_that_left_the_account_is_never_mailed(app, db_session, mail):
    """A nomination outlives a handover as history. The company that left must not
    receive the account's mail - nor stop the ones still on it from agreeing."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer, email=None)
        _on_account(db_session, payer, account,
                    _company(db_session, "gone", business_email="ap@gone.test"),
                    paid_for=False)
        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k1",
                      context={**DECLINE, "billing_group_id": account.id})

        _on_account(db_session, payer, account,
                    _company(db_session, "stays", business_email="ap@stays.test"))
        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k2",
                      context={**DECLINE, "billing_group_id": account.id})

        assert [m.to for m in mail.sent] == [["payer@test.com"], ["ap@stays.test"]]


def test_a_billing_email_beats_the_business_email(app, db_session, mail):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer)
        _on_account(db_session, payer, account,
                    _company(db_session, "e1", business_email="hello@olive.test"))

        notify.notify(payer, notify.PAYMENT_RECOVERED, dedupe_key="k",
                      context={"billing_group_id": account.id})

        assert mail.sent[0].to == ["accounts@olive.test"]


def test_the_shared_business_email_rule():
    """The pure rule, on its own: one address between them, else nothing."""
    from billing.services import store

    assert store.shared_business_email(["", None, " AP@x.test ", "ap@x.test"]) == "AP@x.test"
    assert store.shared_business_email(["a@x.test", "b@x.test"]) is None
    assert store.shared_business_email([None, "  "]) is None
    assert store.shared_business_email([]) is None


def test_the_trial_warning_goes_to_the_companys_business_email(app, db_session, mail):
    """A trial usually has no billing account yet, so there is no billing email to ask:
    the company's business email (onboarding step 1), else the payer."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        for label, email in (("e1", "hello@olive.test"), ("e2", None)):
            notify.notify(payer, notify.TRIAL_ENDING, dedupe_key=label, context={
                "entity_id": _company(db_session, label, business_email=email),
                "entity_name": "Olive Ltd", "codes": ["PETTY_CASH"],
                "trial_end": datetime(2026, 9, 1, tzinfo=UTC), "needs_card": True,
            })

        assert [m.to for m in mail.sent] == [["hello@olive.test"], ["payer@test.com"]]


def test_an_email_naming_no_account_mails_the_payer(app, db_session, mail):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        _billing_account(payer)

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k", context=DECLINE)

        assert mail.sent[0].to == ["payer@test.com"]


def test_somebody_elses_account_is_never_mailed(app, db_session, mail):
    """Only an account of THIS payer's counts: an id from anywhere else is not a reason to
    send one customer's money email to another's inbox."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        stranger = _make_payer(db_session, email="stranger@test.com")
        theirs = _billing_account(stranger, email="accounts@stranger.test")

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": theirs.id})

        assert mail.sent[0].to == ["payer@test.com"]


def test_handover_notices_stay_with_the_person(app, db_session, mail):
    """About the person, not the company: neither a billing email nor a business email
    takes a handover notice away from the payer who asked."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer)
        company = _company(db_session, "e1", business_email="hello@olive.test")

        notify.notify(payer, notify.SUBSCRIBER_TRANSFER_DECLINED, dedupe_key="h",
                      context={"billing_group_id": account.id, "entity_id": company})

        assert [m.to for m in mail.sent] == [["payer@test.com"]]


def test_a_long_billing_email_is_logged_as_sent_not_sent_again(app, db_session, mail):
    """The log keeps 200 characters and a billing email may have 255. Uncut, the save AFTER
    a successful send failed (on Postgres), the claim stayed ``failed``, and the same email
    went out again on every run."""
    from billing.services import notify
    from shared_models.models import SubscriptionEmailLog

    address = "a" * 240 + "@olive.test"
    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer, email=address)
        context = {**DECLINE, "billing_group_id": account.id}

        assert notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k", context=context)
        assert notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k", context=context) is False

        assert mail.sent[0].to == [address]
        assert len(mail.sent) == 1
        row = SubscriptionEmailLog.objects.get(event=notify.RENEWAL_FAILED, dedupe_key="k")
        assert row.status == notify.STATUS_SENT
        assert row.recipient == address[: notify.RECIPIENT_MAX]


def test_the_replay_redirect_also_catches_a_billing_address(app, db_session, mail,
                                                             monkeypatch):
    """A replay mails whoever is reading it, never the inbox the data names - a billing
    email or a business email included, or a decline or a trial warning would slip out."""
    from billing.management.commands.replay_scenarios import _patch_notify
    from billing.services import notify

    # Restored after the test: the harness patches the module for the rest of its run.
    monkeypatch.setattr(notify, "recipient_for", notify.recipient_for)
    monkeypatch.setattr(notify, "address_for", notify.address_for)
    with app.app_context():
        payer = _make_payer(db_session)
        account = _billing_account(payer)
        _patch_notify({"notify_to": "reader+replay@test.com", "user_id": payer,
                       "email": "payer@test.com"})

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})
        notify.notify(payer, notify.TRIAL_ENDING, dedupe_key="t", context={
            "entity_id": _company(db_session, "e1", business_email="hello@olive.test"),
            "trial_end": datetime(2026, 9, 1, tzinfo=UTC), "needs_card": True,
        })

        assert [m.to for m in mail.sent] == [["reader+replay@test.com"]] * 2


# ---------------------------------------------------------------------------
# Call sites — that the billing jobs hand over the right events
# ---------------------------------------------------------------------------


def test_a_retry_that_then_succeeded_does_not_send_a_failure_notice():
    """A recovered payer lands in BOTH ``retried`` and ``recovered``. Mailing from inside
    the loop would send "failed again" moments before "you're all settled"."""
    from billing.services import dunning

    captured = []
    original = dunning.__dict__.get("_notify_dunning")
    assert original is not None

    import billing.services.notify as notify_mod

    real_many = notify_mod.notify_many
    notify_mod.notify_many = lambda events: captured.extend(events) or len(events)
    try:
        dunning._notify_dunning(
            retried=[{"user_id": "u1", "attempts": 1, "_episode": "u1:E"}],
            recovered=[{"user_id": "u1", "attempts": 1, "_episode": "u1:E"}],
            given_up=[],
        )
    finally:
        notify_mod.notify_many = real_many

    events = {event for _, event, _, _ in captured}
    assert notify_mod.PAYMENT_RECOVERED in events
    assert notify_mod.DUNNING_RETRY_FAILED not in events


def test_one_account_recovering_does_not_silence_anothers_failed_retry(monkeypatch):
    """Two cards are two episodes. Suppressed by PAYER, card B's "your payment failed again"
    vanished whenever card A recovered in the same pass - the one notice B needed."""
    import billing.services.notify as notify_mod
    from billing.services import dunning

    captured = []
    monkeypatch.setattr(notify_mod, "notify_many",
                        lambda events: captured.extend(events) or len(events))

    dunning._notify_dunning(
        retried=[{"user_id": "u1", "billing_group_id": "gA", "attempts": 2, "_episode": "u1:A"},
                 {"user_id": "u1", "billing_group_id": "gB", "attempts": 2, "_episode": "u1:B"}],
        recovered=[{"user_id": "u1", "billing_group_id": "gA", "attempts": 2,
                    "_episode": "u1:A"}],
        given_up=[],
    )

    sent = {(event, context["billing_group_id"]) for _, event, _, context in captured}
    assert sent == {(notify_mod.PAYMENT_RECOVERED, "gA"),
                    (notify_mod.DUNNING_RETRY_FAILED, "gB")}


def test_dunning_scaffolding_does_not_leak_into_the_reported_result():
    """``_episode`` exists to build a dedupe key. ``collect_due``'s documented return
    shape is unchanged by notification being bolted on."""
    import billing.services.notify as notify_mod
    from billing.services import dunning

    entries = [{"user_id": "u1", "attempts": 0, "_episode": "u1:E"}]
    real_many = notify_mod.notify_many
    notify_mod.notify_many = lambda events: 0
    try:
        dunning._notify_dunning(retried=[], recovered=entries, given_up=[])
    finally:
        notify_mod.notify_many = real_many

    assert entries[0] == {"user_id": "u1", "attempts": 0}


def test_the_retired_events_stay_retired(app):
    """Seven emails were deliberately switched off. Each removal has a consequence that is
    invisible from the call site, so re-adding one should be a decision, not a reflex:

      trial_converted            a converting trial is silent, and so is its first charge
      renewal_paid               the receipt: a charge that succeeds is silent (2026-09-30,
                                 not in the approved designs)
      trial_expired              a lapsed trial is silent; the module page shows it
                                 (2026-09-30, not in the approved designs)
      trial_ending/will-convert  a cleanly converting trial gets no advance warning at all
      account_closed             superseded, then the thing meant to supersede it went too
      subscriber_transfer_failed the accepting user sees the decline in the browser
      access_revoked             NOTHING now announces a revocation -- a dunning give-up
                                 ends in silence, and a cancellation is never confirmed

    The strings themselves must never be reused for a different meaning either: a dedupe
    row written years ago would suppress the new email for anyone who received the old.
    """
    from billing.services import notify

    retired = ("trial_converted", "renewal_paid", "trial_expired", "account_closed",
               "subscriber_transfer_failed", "access_revoked")
    assert [e for e in retired if e in notify._COPY] == []
    assert [e for e in retired if e in notify.EVENTS] == []
    # The live set, stated once so a silent addition shows up here: exactly the eight
    # approved billing designs plus the setup reminder (Figma 2969:1368, 2026-10-01).
    assert sorted(notify._COPY) == sorted([
        notify.TRIAL_ENDING, notify.RENEWAL_FAILED, notify.DUNNING_RETRY_FAILED,
        notify.PAYMENT_RECOVERED, notify.SUBSCRIBER_TRANSFER_REQUESTED,
        notify.SUBSCRIBER_TRANSFER_ACCEPTED, notify.SUBSCRIBER_TRANSFER_DECLINED,
        notify.SUBSCRIBER_TRANSFER_EXPIRED, notify.ONBOARDING_REMINDER,
    ])


def test_renewal_declines_are_deduped_on_the_billing_period(app, db_session, mail):
    """The same key that stops the payer being CHARGED twice for a period stops them
    being MAILED twice about it, so a re-run is consistent in both."""
    from billing.services import renewals

    with app.app_context():
        payer = _make_payer(db_session)
        entry = {
            "user_id": payer,
            "period_start": datetime(2026, 8, 1, tzinfo=UTC),
            "period_end": datetime(2026, 9, 1, tzinfo=UTC),
            "total": 40000,
            "currency": "HKD",
            "lines": ["Olive Ltd — Super Minty"],
        }
        renewals._notify_renewals(failed=[entry])
        renewals._notify_renewals(failed=[entry])

        assert len(mail.sent) == 1


def test_trials_ending_soon_are_found_in_a_one_day_window(app, db_session):
    """A daily run tiles the calendar exactly once per trial. A cumulative "within N
    days" filter would re-match the same trial every day and lean entirely on the email
    log to stay quiet."""
    from billing.services import store
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        now = datetime(2026, 8, 4, 12, tzinfo=UTC)
        payer = _make_payer(db_session)
        for days, code in ((3, "PAYMENT_REQUEST"), (5, "PETTY_CASH")):
            EntityModuleSubscription.objects.create(
                id=str(uuid.uuid4()),
                entity_id=_company(db_session, f"e{days}"),
                function_code=code,
                payer_user_id=payer,
                phase="trial",
                trial_end=now + timedelta(days=days),
            )

        start = now + timedelta(days=3)
        found = store.trials_ending_between(start, start + timedelta(days=1))

        assert [row.function_code for row in found] == ["PAYMENT_REQUEST"]


def test_the_warning_window_is_day_aligned_not_run_time_aligned(
    app, db_session, mail, monkeypatch
):
    """A window of ``[now + 3d, now + 4d)`` only tiles if the job runs at EXACTLY
    24-hour intervals. Cron does not: a run at 08:10 followed by one at 08:15 leaves a
    five-minute hole, and a trial ending inside it is never warned about at all.

    Found against real data. A trial ending 13:00 HKT — 05:00 UTC — fell before a window
    that opened at 08:10 UTC and was silently skipped, which is the worst failure this
    email has, because it is the one notification that could have prevented the lapse.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        payer = _make_payer(db_session)
        # Ends EARLIER in the day than the job runs — the case that used to be missed.
        trial_end = datetime(2026, 8, 20, 5, 0, tzinfo=UTC)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()),
            entity_id=_company(db_session, "e1"),
            function_code="PAYMENT_REQUEST",
            payer_user_id=payer,
            phase="trial",
            trial_end=trial_end,
        )

        # Job runs at 08:10 UTC, three days out. Run-time-aligned this finds nothing.
        # ``monkeypatch.setattr`` rather than a plain assignment: ``clock.now`` is a
        # module-level function, so assigning to it and then ``del``-ing removes the real
        # one from the module namespace for every test that follows.
        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 17, 8, 10, tzinfo=UTC)
        )
        result = checkout.notify_trials_ending(days_before=3)

        assert [w["entity_id"] for w in result["warned"]] == [_company(db_session, "e1")]


def test_a_trial_that_will_convert_cleanly_is_not_warned_at_all(app, db_session,
                                                                monkeypatch):
    """The filter moved from the COPY to the SEND.

    This email used to go to every trial in the window and pick one of three wordings,
    one of which amounted to "your trial ends soon, do nothing". That trains people to
    skim past the one trial email that does need acting on, so a trial with a card saved
    and this company authorised is now not mailed at all — and with the receipt retired,
    neither is its first charge.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        payer = _make_payer(db_session)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()),
            entity_id=_company(db_session, "e1"),
            function_code="PAYMENT_REQUEST",
            payer_user_id=payer,
            phase="trial",
            trial_end=datetime(2026, 8, 20, 5, 0, tzinfo=UTC),
        )

        monkeypatch.setattr(checkout, "_trial_has_card", lambda uid: True)
        monkeypatch.setattr(checkout.store, "has_billing_consent",
                            lambda eid, uid: True)
        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 17, 8, 10, tzinfo=UTC)
        )

        result = checkout.notify_trials_ending(days_before=7)

        assert result["warned"] == []
        assert [s["reason"] for s in result["skipped"]] == ["will_convert"]


def test_a_trial_blocked_only_on_consent_is_still_warned(app, db_session, monkeypatch):
    """A card IS saved; this company just was not authorised for it. That trial lapses
    exactly as hard as one with no card at all, so it must still be warned — the send
    gates on ``needs_card OR needs_consent``, not on the card alone."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        payer = _make_payer(db_session)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()),
            entity_id=_company(db_session, "e1"),
            function_code="PAYMENT_REQUEST",
            payer_user_id=payer,
            phase="trial",
            trial_end=datetime(2026, 8, 20, 5, 0, tzinfo=UTC),
        )

        monkeypatch.setattr(checkout, "_trial_has_card", lambda uid: True)
        monkeypatch.setattr(checkout.store, "has_billing_consent",
                            lambda eid, uid: False)
        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 17, 8, 10, tzinfo=UTC)
        )

        result = checkout.notify_trials_ending(days_before=7)

        assert [w["entity_id"] for w in result["warned"]] == [_company(db_session, "e1")]


def test_a_cancelled_trial_is_not_warned_about(app, db_session):
    """Cancelling a trial is a deliberate act. Telling the customer the thing they asked
    to end is about to end is noise, not a service — and unlike ``due_trials``, this
    query has no row to close out, so there is no reason to include them."""
    from billing.services import store
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        now = datetime(2026, 8, 4, 12, tzinfo=UTC)
        payer = _make_payer(db_session)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()),
            entity_id=_company(db_session, "e1"),
            function_code="PAYMENT_REQUEST",
            payer_user_id=payer,
            phase="scheduled_cancel",
            trial_end=now + timedelta(days=3),
        )

        start = now + timedelta(days=3)
        assert store.trials_ending_between(start, start + timedelta(days=1)) == []


def test_a_trial_whose_tile_was_MISSED_is_still_warned(app, db_session, monkeypatch):
    """A day the job does not run must not cost a customer their only actionable notice.

    The window used to be the single calendar day exactly ``days_before`` out. Run daily
    those tile perfectly — but there is no watermark and no backlog, so a day the job is
    down is a hole nothing ever revisits, and the trials whose tile fell in it are never
    warned at all. Their first news is the module going dark.

    Here the job misses 17 Aug and runs on the 18th. Under the old window the 18th looks
    for trials ending 21 Aug and never sees this one; under the current window it catches
    it with two days' notice instead of three.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        payer = _make_payer(db_session)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()),
            entity_id=_company(db_session, "e_missed"),
            function_code="PAYMENT_REQUEST",
            payer_user_id=payer,
            phase="trial",
            trial_end=datetime(2026, 8, 20, 5, 0, tzinfo=UTC),
        )

        # 17 Aug never ran. This is the 18th.
        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 18, 8, 10, tzinfo=UTC)
        )
        result = checkout.notify_trials_ending(days_before=3)

        assert [w["entity_id"] for w in result["warned"]] == [_company(db_session, "e_missed")]


def test_a_trial_ending_TODAY_is_left_to_the_trial_end_job(app, db_session, monkeypatch):
    """The window starts tomorrow.

    ``notify-trial-ending`` runs before ``close-trials`` on the same schedule, so warning
    about a trial ending today would mail "your trial ends soon" minutes before "your
    trial has ended" — two contradictory notices about one trial on one day.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription

    with app.app_context():
        payer = _make_payer(db_session)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()),
            entity_id=_company(db_session, "e_today"),
            function_code="PAYMENT_REQUEST",
            payer_user_id=payer,
            phase="trial",
            trial_end=datetime(2026, 8, 20, 5, 0, tzinfo=UTC),
        )

        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
        )
        result = checkout.notify_trials_ending(days_before=3)

        assert result["warned"] == []


def test_a_trial_warning_reaches_a_payer_the_entity_was_handed_to(
    app, db_session, mail, monkeypatch
):
    """The whole reason "change subscriber" used to refuse an entity on trial.

    The uniqueness constraint is (event, dedupe_key) with ``user_id`` deliberately outside
    it, so the KEY is the only thing that can tell two recipients apart. A handover changes
    neither the entity, nor the codes, nor ``trial_end`` — so a key built from those three
    regenerates identically, ``_claim`` finds the row already sent to the outgoing payer,
    and the person who is actually about to be charged is told nothing.

    That is not a missed reminder. This email branches on ``needs_card`` into "action
    needed" versus "nothing you need to do", so silence is indistinguishable from
    reassurance right up until the money leaves.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription, SubscriptionEmailLog

    with app.app_context():
        outgoing = _make_payer(db_session, email="outgoing@test.com")
        incoming = _make_payer(db_session, email="incoming@test.com")

        trial_end = datetime(2026, 8, 20, 5, 0, tzinfo=UTC)
        row = EntityModuleSubscription(
            id=str(uuid.uuid4()), entity_id=_company(db_session, "e1"), function_code="PAYMENT_REQUEST",
            payer_user_id=outgoing, phase="trial", trial_end=trial_end,
        )
        row.save(force_insert=True)

        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 17, 8, 10, tzinfo=UTC)
        )

        # Warned once, to the payer at the time.
        assert [w["entity_id"] for w in
                checkout.notify_trials_ending(days_before=3)["warned"]] == [_company(db_session, "e1")]

        # The handover: only the payer moves. Entity, codes and trial_end are untouched,
        # which is exactly why an entity-scoped key would swallow the second warning.
        row.payer_user_id = incoming
        row.save(update_fields=["payer_user_id"])

        assert [w["entity_id"] for w in
                checkout.notify_trials_ending(days_before=3)["warned"]] == [_company(db_session, "e1")]

        sent = list(SubscriptionEmailLog.objects.filter(event="trial_ending"))
        assert len(sent) == 2, "each payer gets their own warning"
        assert {log.user_id for log in sent} == {outgoing, incoming}
        assert len({log.dedupe_key for log in sent}) == 2, "the keys must differ"


def test_the_same_payer_is_still_only_warned_once(app, db_session, mail, monkeypatch):
    """Payer-scoping the key must not become a licence to re-send. The warning window
    re-matches the same trial on every day it spans, and the log is the only thing
    keeping that quiet."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from shared_models.models import EntityModuleSubscription, SubscriptionEmailLog

    with app.app_context():
        payer = _make_payer(db_session)
        EntityModuleSubscription.objects.create(
            id=str(uuid.uuid4()), entity_id=_company(db_session, "e1"), function_code="PAYMENT_REQUEST",
            payer_user_id=payer, phase="trial",
            trial_end=datetime(2026, 8, 20, 5, 0, tzinfo=UTC),
        )

        monkeypatch.setattr(
            checkout.clock, "now", lambda: datetime(2026, 8, 17, 8, 10, tzinfo=UTC)
        )
        checkout.notify_trials_ending(days_before=3)
        checkout.notify_trials_ending(days_before=3)

        assert SubscriptionEmailLog.objects.filter(event="trial_ending").count() == 1


# ---------------------------------------------------------------------------
# The payment-failed emails' date: the last full day to pay (decided 2026-09-30)
# ---------------------------------------------------------------------------
# Figma's "[date]". Access runs out at ``paid_through`` + the past-due window
# (``dunning.suspension_at``); from that instant "Pay now" is refused, so the email names
# the day BEFORE it, on the account's calendar - the zone every company on it shares, else
# Hong Kong.


def _declined_account(db, payer, *, suspends_at, zones=(None,), in_dunning=True):
    """A billing account whose past-due access runs out at ``suspends_at``, with one
    company on it per entry in ``zones`` (each company's ``timezone``)."""
    from billing.services import policy

    account = _billing_account(payer)
    window = policy.current().past_due_window_days
    account.paid_through = suspends_at - timedelta(days=window)
    account.dunning_started_at = account.paid_through if in_dunning else None
    account.save(update_fields=["paid_through", "dunning_started_at"])
    for index, zone in enumerate(zones):
        _on_account(db, payer, account,
                    _company(db, f"declined-{suspends_at:%H}-{index}", timezone=zone))
    return account


@pytest.mark.parametrize("event", ["renewal_failed", "dunning_retry_failed"])
@pytest.mark.parametrize("suspends_at, printed", [
    # 10:00 in Hong Kong on the 16th: paying later that day is already too late.
    (datetime(2026, 10, 16, 2, tzinfo=UTC), "15 Oct 2026"),
    # 04:00 in Hong Kong on the 16th, still the 15th in UTC - the HK calendar decides.
    (datetime(2026, 10, 15, 20, tzinfo=UTC), "15 Oct 2026"),
])
def test_a_decline_names_the_last_full_day_to_pay(app, db_session, mail, event,
                                                 suspends_at, printed):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _declined_account(db_session, payer, suspends_at=suspends_at)

        assert notify.notify(payer, event, dedupe_key="k",
                             context={**DECLINE, "billing_group_id": account.id}) is True

        assert (f"will be suspended if the payment is not received by {printed}."
                in mail.sent[0].html)


def test_an_account_whose_companies_share_a_zone_is_dated_on_that_calendar(
    app, db_session, mail
):
    """02:00 UTC on the 16th is still the 15th in New York, so New York's last full day is
    the 14th - Hong Kong's would be the 15th."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _declined_account(
            db_session, payer, suspends_at=datetime(2026, 10, 16, 2, tzinfo=UTC),
            zones=("America/New_York", "America/New_York"),
        )

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})

        assert "received by 14 Oct 2026." in mail.sent[0].html


def test_companies_on_different_calendars_are_dated_in_hong_kong(app, db_session, mail):
    """A company that never set a zone counts as Hong Kong, so it differs from New York."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _declined_account(
            db_session, payer, suspends_at=datetime(2026, 10, 16, 2, tzinfo=UTC),
            zones=("America/New_York", None),
        )

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})

        assert "received by 15 Oct 2026." in mail.sent[0].html


def test_an_account_not_in_dunning_names_no_date(app, db_session, mail):
    """No collection running means no suspension scheduled: no date rather than a wrong one."""
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _declined_account(
            db_session, payer, suspends_at=datetime(2026, 10, 16, 2, tzinfo=UTC),
            in_dunning=False,
        )

        notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                      context={**DECLINE, "billing_group_id": account.id})

        html = mail.sent[0].html
        assert "will be suspended if the payment is not received." in html
        assert "received by" not in html


def test_a_deadline_the_caller_gives_wins(app, db_session, mail):
    from billing.services import notify

    with app.app_context():
        payer = _make_payer(db_session)
        account = _declined_account(
            db_session, payer, suspends_at=datetime(2026, 10, 16, 2, tzinfo=UTC),
        )

        notify.notify(payer, notify.DUNNING_RETRY_FAILED, dedupe_key="k", context={
            "billing_group_id": account.id,
            "deadline": datetime(2026, 11, 3, 2, tzinfo=UTC),
        })

        assert "received by 2 Nov 2026." in mail.sent[0].html


def test_a_failed_read_still_sends_and_is_recorded_once(app, db_session, mail,
                                                        monkeypatch, caplog):
    """A database error while reading the account aborts the transaction. Left that way,
    the commit AFTER a successful send failed, the claim stayed ``failed``, and the email
    went out again on every run. Each read is its own savepoint, so it is undone, logged,
    and the email sent without the date."""
    from django.db import connection

    from billing.services import notify, store
    from shared_models.models import SubscriptionEmailLog

    def _broken(group_id):
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM no_such_table_for_this_test")

    with app.app_context():
        payer = _make_payer(db_session)
        account = _declined_account(
            db_session, payer, suspends_at=datetime(2026, 10, 16, 2, tzinfo=UTC),
        )
        monkeypatch.setattr(store, "billing_group", _broken)
        context = {**DECLINE, "billing_group_id": account.id}

        assert notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k", context=context)
        assert notify.notify(payer, notify.RENEWAL_FAILED, dedupe_key="k",
                             context=context) is False

        assert len(mail.sent) == 1
        assert "will be suspended if the payment is not received." in mail.sent[0].html
        row = SubscriptionEmailLog.objects.get(event=notify.RENEWAL_FAILED, dedupe_key="k")
        assert row.status == notify.STATUS_SENT
        assert "could not read the payment deadline" in caplog.text


def test_pay_by_is_the_day_before_on_the_given_calendar(app):
    import pytz

    from billing.services import notify

    hong_kong = pytz.timezone("Asia/Hong_Kong")
    # Local midnight: nothing of the 16th is payable, so the 15th is the last full day.
    assert notify.pay_by(datetime(2026, 10, 15, 16, tzinfo=UTC), hong_kong) == "15 Oct 2026"
    assert notify.pay_by(datetime(2026, 10, 16, 2, tzinfo=UTC)) == "15 Oct 2026"
    assert notify.pay_by(None) == ""


def test_suspension_is_paid_through_plus_the_window():
    from types import SimpleNamespace

    from billing.services import dunning

    paid = datetime(2026, 10, 1, 2, tzinfo=UTC)
    assert dunning.suspension_at(SimpleNamespace(paid_through=paid), 15) == datetime(
        2026, 10, 16, 2, tzinfo=UTC
    )
    assert dunning.suspension_at(SimpleNamespace(paid_through=None), 15) is None
