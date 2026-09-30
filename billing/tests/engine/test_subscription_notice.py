"""The subscription notice shown on both landing pages.

Nothing in the product told a customer their trial was about to lapse or their card
had been declined — the settings page held all of it, and you only saw it if you went
looking. This is the interruption: one modal, once per entity per login, on the Petty
Cash dashboard and the Payment landing page.

Two things are worth pinning down and neither is the copy:

* **The list is the point.** A company can be past due on one module and winding down
  another. Picking "the most important" one and hiding the rest would be a lie of
  omission, so every applicable item appears, ordered by severity.
* **Only the payer is offered an action.** ``@require_subscription_payer`` refuses
  everyone else server-side, so showing a co-admin a "Pay now" button produces a click
  that fails. They are told who to ask instead.

``get_module_cards`` is stubbed throughout. It is exercised by its own tests and needs
a full Stripe-shaped fixture set; what is new here is the reading of those cards, so
the tests feed synthetic ones and assert on the reading.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .fakes import fake_model

UTC = UTC


def _card(code, name, **overrides):
    """A module card with everything switched off — override only what matters."""
    card = {
        "code": code,
        "name": name,
        "subscription_status": None,
        "needs_card": False,
        "needs_consent_only": False,
        "pending_cancel": False,
        "access_end_long": None,
        "period_end": None,
        "period_end_long": None,
        "period_end_short": None,
    }
    card.update(overrides)
    return card


@pytest.fixture
def stub_user(app, monkeypatch):
    """Stand a payer in front of ``User.query.get`` without touching the database.

    What is under test is how a payer is turned into the "ask this person" line —
    name joining and the email fallback — not persistence. Going through the real
    table would need the second schema attached in the same connection the builder
    runs on, which buys nothing here.
    """
    from types import SimpleNamespace

    def _install(**fields):
        user = SimpleNamespace(**fields)
        # Replace the whole class on the module the builder imports it from at call time
        # (``store._by_pk(User, ...)`` -> ``User.objects.filter(pk=...).first()``).
        monkeypatch.setattr("shared_models.models.User", fake_model([user]))
        return user

    return _install


@pytest.fixture
def notices(app, monkeypatch):
    """Call build_subscription_notices over a fixed card list, with auth stubbed out.

    Depends on ``app`` because importing the service standalone trips a circular
    import through models.db — the app fixture is what bootstraps that graph.
    """
    from billing.services import entity_modules as svc

    def _run(cards, *, can_manage=True, payer=None, user_id="user-1", group=None):
        monkeypatch.setattr(svc, "get_module_cards", lambda _eid: cards)
        # The card the company is billed on - None reads as "told" (today's banner).
        monkeypatch.setattr(
            "billing.services.store.billing_group_for_entity",
            lambda *_a, **_k: group,
        )
        monkeypatch.setattr(
            "core.policy.has_permission_by_user_id",
            lambda *_a, **_k: can_manage,
        )
        monkeypatch.setattr(
            "billing.services.store.may_manage_subscription",
            lambda *_a, **_k: can_manage,
        )
        monkeypatch.setattr(
            "billing.services.store.payer_for_entity",
            lambda *_a, **_k: payer,
        )
        with app.app_context():
            return svc.build_subscription_notices("entity-1", user_id)

    return _run


# --- what gets said ---------------------------------------------------------


def test_nothing_wrong_says_nothing(notices):
    """A healthy entity must not be interrupted at all."""
    result = notices([_card("PETTY_CASH", "Petty Cash"), _card("PAYMENT_REQUEST", "Payment")])

    assert result["items"] == []
    assert result["severity"] is None


def test_past_due_names_the_module_and_the_deadline(notices):
    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due",
               access_end_long="19 Aug 2026")]
    )

    item = result["items"][0]
    assert item["kind"] == "past_due"
    assert item["severity"] == "critical"
    assert item["module"] == "Payment"
    assert "Payment" in item["title"]
    assert "19 Aug 2026" in item["detail"]


def test_past_due_while_the_processor_failed_is_not_a_failed_payment(notices, monkeypatch):
    """Held in its grace because the PROCESSOR failed: the card was never asked, the customer
    was not emailed, and the app must not tell them their payment failed either."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from billing.services import clock, policy

    now = datetime(2027, 3, 10, tzinfo=UTC)
    monkeypatch.setattr(clock, "now", lambda: now)
    monkeypatch.setattr(policy, "current", lambda: policy.DEFAULTS)
    silent = SimpleNamespace(dunning_started_at=None, paid_through=now - timedelta(days=2))
    told = SimpleNamespace(dunning_started_at=now, paid_through=now - timedelta(days=2))
    card = _card("PAYMENT_REQUEST", "Payment", subscription_status="past_due")

    assert notices([card], group=silent)["items"] == []
    assert notices([card], group=told)["items"][0]["kind"] == "past_due"


def test_past_due_without_a_deadline_still_says_something_actionable(notices):
    """access_end_long is None once the date has passed — don't render "None"."""
    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due")]
    )

    assert "None" not in result["items"][0]["detail"]
    assert "payment method" in result["items"][0]["detail"]


def test_trial_without_a_card_is_distinguished_from_one_needing_consent(notices):
    """Two reasons a trial won't convert, and the fix differs — so must the copy."""
    no_card = notices(
        [_card("PETTY_CASH", "Petty Cash", needs_card=True,
               period_end_long="19 Aug 2026")]
    )["items"][0]
    consent = notices(
        [_card("PETTY_CASH", "Petty Cash", needs_card=True, needs_consent_only=True,
               period_end_long="19 Aug 2026")]
    )["items"][0]

    assert no_card["kind"] == "needs_card"
    assert "Add a payment method" in no_card["title"]

    assert consent["kind"] == "needs_consent"
    assert "Confirm billing" in consent["title"]
    assert "other companies" in consent["detail"]


def test_pending_cancel_reports_when_access_ends(notices):
    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", pending_cancel=True, access_end_long="1 Sep 2026")]
    )

    item = result["items"][0]
    assert item["kind"] == "pending_cancel"
    assert "1 Sep 2026" in item["detail"]
    assert "won't be billed again" in item["detail"]


# --- the trial-ending window ------------------------------------------------


def test_a_trial_ending_soon_is_announced(notices):
    soon = datetime.now(UTC) + timedelta(days=2)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               period_end=soon, period_end_long="10 Aug 2026")]
    )

    assert result["items"][0]["kind"] == "trial_ending"
    assert result["items"][0]["severity"] == "info"


def test_a_trial_ending_far_off_is_announced_too(notices):
    """No window: a running trial carries its first-charge date from day one.

    This used to be silent outside a 7-day window. It isn't, because the point of
    the line is that nobody can say they were never told the date — which is only
    true if it is there before the last week.
    """
    later = datetime.now(UTC) + timedelta(days=45)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               period_end=later, period_end_long="30 Sep 2026")]
    )

    assert result["items"][0]["kind"] == "trial_ending"
    assert "30 Sep 2026" in result["items"][0]["detail"]


def test_a_trial_titles_the_state_not_a_countdown(notices):
    """"is ending" on day one of thirty reads as a bug."""
    later = datetime.now(UTC) + timedelta(days=45)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               period_end=later, period_end_long="30 Sep 2026")]
    )

    assert result["items"][0]["title"] == "Petty Cash is on a free trial"


def test_a_trial_past_its_end_date_is_not_announced(notices):
    """It has ended, not "is ending" — the sweep is what speaks next."""
    past = datetime.now(UTC) - timedelta(days=3)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               period_end=past, period_end_long="1 Aug 2026")]
    )

    assert result["items"] == []


def test_a_trial_still_being_closed_out_keeps_its_notice(notices):
    """The one exception, and it does not contradict the rule above.

    ``trial_closing`` means the term has passed but the subscription pass has not closed
    the trial out yet and the customer still has access — a window of under an hour. The
    notice carries "the first charge is coming, on this date", so dropping it here would
    remove that message in the final minutes before the charge, which is when it is most
    worth having on screen. It would also make the page visibly rearrange itself for a
    state nobody can act on.

    The card above has no ``trial_closing`` flag at all, which is what keeps a genuinely
    stale trial silent.
    """
    past = datetime.now(UTC) - timedelta(minutes=20)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               period_end=past, period_end_long="12 Aug 2026", trial_closing=True)]
    )

    assert [i["kind"] for i in result["items"]] == ["trial_ending"]
    assert result["items"][0]["title"] == "Petty Cash is on a free trial"


def test_the_window_can_be_restored(notices, monkeypatch):
    """TRIAL_ENDING_SOON_DAYS = int goes back to warning only near the end."""
    monkeypatch.setattr(
        "billing.services.entity_modules.TRIAL_ENDING_SOON_DAYS", 7
    )
    later = datetime.now(UTC) + timedelta(days=45)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               period_end=later, period_end_long="30 Sep 2026")]
    )

    assert result["items"] == []


def test_a_trial_that_wont_convert_reports_the_fix_not_the_countdown(notices):
    """needs_card wins over trial_ending: "add a card" is the actionable half."""
    soon = datetime.now(UTC) + timedelta(days=1)
    result = notices(
        [_card("PETTY_CASH", "Petty Cash", subscription_status="trialing",
               needs_card=True, period_end=soon, period_end_long="5 Aug 2026")]
    )

    assert [i["kind"] for i in result["items"]] == ["needs_card"]


# --- the list ---------------------------------------------------------------


def test_every_applicable_item_appears_not_just_the_worst(notices):
    """The whole reason this is a list: one company, two different problems."""
    result = notices(
        [
            _card("PETTY_CASH", "Petty Cash", pending_cancel=True,
                  access_end_long="1 Sep 2026"),
            _card("PAYMENT_REQUEST", "Payment", subscription_status="past_due",
                  access_end_long="19 Aug 2026"),
        ]
    )

    assert len(result["items"]) == 2
    assert {i["module"] for i in result["items"]} == {"Petty Cash", "Payment"}


def test_items_are_ordered_worst_first(notices):
    """Card order is catalog order; the modal's order must be severity."""
    result = notices(
        [
            _card("PETTY_CASH", "Petty Cash", pending_cancel=True,
                  access_end_long="1 Sep 2026"),
            _card("PAYMENT_REQUEST", "Payment", subscription_status="past_due",
                  access_end_long="19 Aug 2026"),
        ]
    )

    assert [i["kind"] for i in result["items"]] == ["past_due", "pending_cancel"]
    assert result["severity"] == "critical"


def test_the_notice_is_entity_wide_not_per_module(notices):
    """The Petty Cash dashboard reports a Payment problem, and vice versa.

    Billing is per payer and the anchor is shared, so a declined card is a property
    of the company. A landing page that only spoke for its own module would leave the
    user staring at a working page while the other module dies.
    """
    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due",
               access_end_long="19 Aug 2026")]
    )

    assert result["items"][0]["module_code"] == "PAYMENT_REQUEST"


# --- who may act ------------------------------------------------------------


def test_the_payer_is_offered_the_action(notices):
    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due")],
        can_manage=True,
    )

    assert result["can_manage"] is True


def test_a_non_payer_is_told_who_to_ask_instead(notices, stub_user):
    """A button @require_subscription_payer would refuse must not be offered."""
    stub_user(first_name="Pay", last_name="Er", email="payer@test.com")

    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due")],
        can_manage=False,
        payer="payer-id",
        user_id="someone-else",
    )

    assert result["can_manage"] is False
    assert result["payer"]["name"] == "Pay Er"
    assert result["payer"]["email"] == "payer@test.com"


def test_a_payer_with_no_name_falls_back_to_their_email(notices, stub_user):
    """The modal prints name-or-email; a blank name must not render as empty."""
    stub_user(first_name="", last_name="", email="payer@test.com")

    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due")],
        can_manage=False,
        payer="payer-id",
        user_id="someone-else",
    )

    assert result["payer"]["name"] == ""
    assert result["payer"]["email"] == "payer@test.com"


def test_the_payer_is_not_named_to_themselves(notices):
    """"Managed by you" is noise — the payer already has the buttons."""
    result = notices(
        [_card("PAYMENT_REQUEST", "Payment", subscription_status="past_due")],
        payer="user-1",
        user_id="user-1",
    )

    assert result["payer"] is None


# --- once per entity per login ----------------------------------------------


def test_the_notice_is_claimed_once_per_session():
    from billing.services.notices import claim_subscription_notice

    session: dict = {}

    assert claim_subscription_notice(session, "entity-1") is True
    assert claim_subscription_notice(session, "entity-1") is False
    assert claim_subscription_notice(session, "entity-1") is False


def test_each_entity_is_claimed_separately():
    """Switching companies is a new question, even in the same login."""
    from billing.services.notices import claim_subscription_notice

    session: dict = {}

    assert claim_subscription_notice(session, "entity-1") is True
    assert claim_subscription_notice(session, "entity-2") is True
    assert claim_subscription_notice(session, "entity-1") is False


def test_a_fresh_login_asks_again():
    """The flag lives in the session, so a new session shows the notice again —
    a problem the user didn't fix is worth repeating tomorrow."""
    from billing.services.notices import claim_subscription_notice

    assert claim_subscription_notice({}, "entity-1") is True
    assert claim_subscription_notice({}, "entity-1") is True


def test_claim_reassigns_rather_than_mutating():
    """Flask's session only marks itself dirty on __setitem__ — an in-place append
    would be silently dropped, and the modal would return on every page load."""
    from billing.services.entity_modules import NOTICE_SEEN_SESSION_KEY
    from billing.services.notices import claim_subscription_notice

    original: list[str] = []
    session = {NOTICE_SEEN_SESSION_KEY: original}

    claim_subscription_notice(session, "entity-1")

    assert original == []
    assert session[NOTICE_SEEN_SESSION_KEY] == ["entity-1"]


def test_no_entity_id_claims_nothing():
    from billing.services.notices import claim_subscription_notice

    assert claim_subscription_notice({}, "") is False


def test_the_claim_is_spent_even_when_there_was_nothing_to_show():
    """Documenting the trade-off, because it is surprising.

    The claim is consumed before the notice is built, so a visit that had nothing
    to report still burns the session's one look. That is deliberate — checking
    first is what keeps the billing queries off every dashboard load — but it means
    a state that becomes true mid-session is not announced until the next login.
    The ``?notice=1`` debug bypass exists for exactly this.
    """
    from billing.services.notices import claim_subscription_notice

    session: dict = {}
    assert claim_subscription_notice(session, "entity-1") is True  # nothing to show
    assert claim_subscription_notice(session, "entity-1") is False  # now there is


# --- the JSON endpoint the Payment frontend calls ---------------------------
#
# The billing frontend lives on another origin and talks to its own backend for
# everything else; subscription state exists only here. Its billing JWT is signed
# with this app's SECRET_KEY, so the token it already holds is the credential.
