"""Customer-initiated collection: settle a past-due account on the spot.

Automatic retries run on a 1/4/7/10/13-day schedule. Without a manual path a customer
fixes their card and then waits up to three days, still locked out, with nothing to press —
``addPaymentMethod`` saves a card and settles nothing by itself.

``retry_now`` is ``collect_due``'s per-account body minus ONE thing, the schedule gate.
These tests pin exactly that: the pacing is skipped, and every guard around it is not.

  skipped   should_attempt_now  — paces the cron, has no business blocking the customer
  KEPT      should_give_up      — collection must never outlive access
  KEPT      the attempt budget  — bounds how often a card can be hit, however triggered
  KEPT      settle-then-clear   — entitle them to what they just paid for

It keys on the DEBT — the open invoice — not on ``dunning_started_at``. The stamp is
bookkeeping for the schedule; the invoice is what is owed. Asking the stamp first told a
payer with a real unpaid invoice "no outstanding payment" while the page beside the button
read "payment due".

And a payer with NO card is never charged: the attempt cannot succeed, so spending a
retry slot on it would burn the budget one press at a time.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

FAILED_AT = datetime(2027, 2, 9, 13, tzinfo=UTC)
PAID_THROUGH = datetime(2027, 2, 8, 13, tzinfo=UTC)
# One day in: BEFORE the first scheduled retry slot at +1 day would fire on its own, and
# well inside the 15-day window.
NOW = FAILED_AT + timedelta(hours=2)


class _Account:
    """The payer's account. The anchor and the Stripe customer stayed here; the retry
    clock and the paid-through moved onto the CARD — see ``_Group``, which ``_wire``
    builds from these same values so the cases below still read as one account."""

    def __init__(self, *, started=FAILED_AT, attempts=0, paid_through=PAID_THROUGH):
        self.user_id = "u1"
        self.anchor_at = datetime(2027, 1, 8, 13, tzinfo=UTC)
        self.paid_through = paid_through
        self.stripe_customer_id = "cus_u1"
        self.dunning_started_at = started
        self.dunning_attempts = attempts


class _Group:
    """The one card this payer bills on, carrying the account's collection state."""

    def __init__(self, account, id="g1", card="pm_1"):
        self.id = id
        self.payer_user_id = account.user_id
        self.stripe_payment_method_id = card
        self.paid_through = account.paid_through
        self.dunning_started_at = account.dunning_started_at
        self.dunning_attempts = account.dunning_attempts


def _wire(app, monkeypatch, *, account, invoices=None, paid=True, reason="ok",
          card="pm_1", group=None):
    """Point the store, the gateway and the card lookup at fakes; return (dunning, calls)."""
    from billing.services import billing_gateway, clock, dunning, store

    calls = {"attempts": 0, "ended": [], "settled": [], "retried": []}
    group = group if group is not None else (_Group(account) if account else None)

    monkeypatch.setattr(clock, "now", lambda: NOW)
    monkeypatch.setattr(store, "customer_mapping_for_user", lambda uid: account)
    monkeypatch.setattr(store, "billing_groups_for_payer", lambda uid: [group])
    monkeypatch.setattr(
        store, "billing_group_for_entity", lambda eid, uid=None: group
    )
    monkeypatch.setattr(store, "billing_group", lambda gid: group)
    # No local invoice rows behind these fakes: nothing to re-read when nothing is open,
    # and no stranded draft for the period (``dunning._nothing_open_but_owed``).
    monkeypatch.setattr(store, "open_invoices_for_group", lambda gid: [])
    monkeypatch.setattr(store, "invoice_for_key", lambda key: None)
    monkeypatch.setattr(store, "handover_owed", lambda group, now: False)
    # Patched BY DOTTED PATH: retry_now imports it inside the function, so a reference
    # captured here would not be the one it ends up calling.
    monkeypatch.setattr(
        "billing.services.stripe_client."
        "customer_default_payment_method",
        lambda cid: card,
    )

    def _attempt(_group_id):
        calls["attempts"] += 1
        group.dunning_attempts = int(group.dunning_attempts or 0) + 1
        return group.dunning_attempts

    monkeypatch.setattr(store, "record_group_dunning_attempt", _attempt)
    monkeypatch.setattr(
        store, "end_group_dunning",
        lambda gid, *, status="active": calls["ended"].append(
            (account.user_id, status)
        ),
    )
    monkeypatch.setattr(
        dunning, "_settle_period",
        lambda acct, grp, inv: calls["settled"].append(inv["id"] if inv else None),
    )
    monkeypatch.setattr(
        billing_gateway, "open_invoices",
        lambda cid: [] if invoices is None else invoices,
    )

    def _retry(invoice_id, payment_method=None):
        # The card is passed now: an invoice names the one that declined, so a retry
        # that does not say otherwise re-charges it.
        calls["retried"].append(invoice_id)
        calls.setdefault("retried_on", []).append(payment_method)
        return paid, reason

    monkeypatch.setattr(billing_gateway, "retry_invoice", _retry)
    return dunning, calls


INVOICES = [{"id": "in_1"}]


def test_it_charges_even_when_no_scheduled_slot_is_due(app, monkeypatch):
    """THE point. Two hours after the failure no automatic retry is due, and the cron
    would do nothing — the customer pressing Pay now must still be collected."""
    from billing.services import dunning as _d

    account = _Account()
    dunning, calls = _wire(app, monkeypatch, account=account, invoices=INVOICES)

    with app.app_context():
        # Guard the premise: the scheduled path really would decline to act right now.
        assert _d.should_attempt_now(NOW, FAILED_AT, 0, (1, 3, 5, 7), 10) is False

        result = dunning.retry_now("u1")

    assert result["status"] == "paid"
    assert calls["retried"] == ["in_1"]
    assert calls["ended"] == [("u1", "active")]


def test_a_successful_retry_settles_the_period_before_clearing_dunning(app, monkeypatch):
    """Order matters: clearing first would collect the money and leave them unentitled
    until the next monthly run adopted the invoice."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)

    with app.app_context():
        dunning.retry_now("u1")

    assert calls["settled"] == ["in_1"]
    assert calls["ended"] == [("u1", "active")]


def test_pay_now_charges_the_card_the_company_is_on_TODAY(app, monkeypatch):
    """The card on the invoice is the one that declined; the card on the group is the one
    the payer has since chosen.

    ``issue_invoice`` pins the card onto the document, so a retry that does not name one
    re-charges the dead card — every attempt, however many times it was replaced. Pay now
    exists precisely for the customer who has just fixed their card, so charging the old
    one would make the button useless in its only real use case.
    """
    account = _Account()
    group = _Group(account, card="pm_replacement")
    dunning, calls = _wire(
        app, monkeypatch, account=account, invoices=INVOICES, group=group
    )

    with app.app_context():
        assert dunning.retry_now("u1", "e1")["status"] == "paid"

    assert calls["retried_on"] == ["pm_replacement"]


def test_a_decline_leaves_the_account_in_dunning(app, monkeypatch):
    """Still recoverable — they can try another card, and the cron keeps its remaining
    slots. Closing here would end the subscription on one refused attempt."""
    dunning, calls = _wire(
        app, monkeypatch, account=_Account(), invoices=INVOICES,
        paid=False, reason="card_declined",
    )

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "failed"
    assert result["reason"] == "card_declined"
    assert calls["ended"] == [], "a decline must not close collection"
    assert calls["settled"] == []


def test_the_attempt_is_counted_against_the_same_budget(app, monkeypatch):
    """A manual retry is a charge attempt like any other. Sharing the budget is what
    bounds how many times a card can be hit, however the retry was triggered."""
    account = _Account(attempts=1)
    dunning, calls = _wire(app, monkeypatch, account=account, invoices=INVOICES)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert calls["attempts"] == 1
    assert result["attempts"] == 2


def test_it_refuses_past_the_give_up_deadline(app, monkeypatch):
    """Collection must never outlive access. Past the deadline this closes the account
    exactly as the cron would, rather than charging a card for someone locked out."""
    account = _Account(started=FAILED_AT - timedelta(days=30))
    dunning, calls = _wire(app, monkeypatch, account=account, invoices=INVOICES)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "gave_up"
    assert calls["retried"] == [], "no card may be charged past the deadline"
    assert calls["ended"] == [("u1", "closed")]


def test_nothing_owed_closes_dunning_rather_than_charging(app, monkeypatch):
    """Settled elsewhere — a portal payment, a manual charge. Same handling as the
    scheduled path, or the customer has paid and stays locked out."""
    from billing.services import billing_gateway, store

    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=[])

    class _Paid:
        id, external_id, status = "row_p", "in_p", "open"
        idempotency_key = "renewal-u1-20270208-g1"

    # The evidence: the period's invoice is there, and the processor says it was paid.
    monkeypatch.setattr(store, "invoice_for_key", lambda key: _Paid())
    monkeypatch.setattr(billing_gateway, "refresh_record", lambda record: "paid")

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "nothing_owed"
    assert calls["retried"] == []
    assert calls["ended"] == [("u1", "active")]
    assert calls["settled"] == [None]


def test_nothing_open_while_the_periods_invoice_is_a_stranded_draft_keeps_the_episode(
    app, monkeypatch
):
    """The same trap as the scheduled path: a draft nobody finalized is not in the open
    list. The press answers as before, but the episode is NOT closed as settled - the
    renewal pass finishes the draft and dunning goes on until it has."""
    from billing.services import billing_gateway, store

    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=[])

    class _Row:
        id, external_id, status = "row_d", "in_draft", "draft"
        idempotency_key = "renewal-u1-20270208-g1"

    # Only the period this card is behind on counts - asked by its exact key.
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda key: _Row() if key == _Row.idempotency_key else None)
    monkeypatch.setattr(billing_gateway, "refresh_record", lambda record: "draft")

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "nothing_owed"
    assert calls["retried"] == []
    assert calls["ended"] == []
    assert calls["settled"] == []


def test_an_unpaid_invoice_is_collected_even_with_no_dunning_stamp(app, monkeypatch):
    """The reported bug. A past-due module whose account carries no dunning stamp was
    told "There's no outstanding payment on this account" — while the card beside the
    button said "payment due".

    The debt is the open invoice. The stamp only schedules retries, and its absence
    means "nobody has started chasing this", not "nothing is owed".
    """
    dunning, calls = _wire(
        app, monkeypatch, account=_Account(started=None), invoices=INVOICES
    )

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "paid"
    assert calls["retried"] == ["in_1"]
    assert calls["settled"] == ["in_1"]
    # Nothing to clear: collection was never running.
    assert calls["ended"] == []


def test_no_card_is_reported_without_spending_an_attempt(app, monkeypatch):
    """A charge with nothing to charge cannot succeed, so it must not be made.

    Counting a slot for it would spend the retry budget on a guaranteed decline, and
    every press would spend another — leaving the customer fewer automatic retries for
    having tried to help.
    """
    account = _Account(attempts=1)
    dunning, calls = _wire(
        app, monkeypatch, account=account, invoices=INVOICES, card=None
    )

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "no_card"
    assert result["invoice"] == "in_1", "still names the debt it could not collect"
    assert calls["retried"] == [], "no charge may be attempted with no card"
    assert calls["attempts"] == 0, "and no slot may be spent on it"
    assert account.dunning_attempts == 1
    assert calls["ended"] == [], "still owed, so collection stays open"


def test_a_decline_reports_the_processors_reason(app, monkeypatch):
    """"insufficient funds" and "card expired" need different things from the customer."""
    dunning, _ = _wire(
        app, monkeypatch, account=_Account(), invoices=INVOICES,
        paid=False, reason="Your card has insufficient funds.",
    )

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "failed"
    assert result["reason"] == "Your card has insufficient funds."


def test_no_billing_account_is_not_an_error(app, monkeypatch):
    dunning, calls = _wire(app, monkeypatch, account=None, invoices=INVOICES)

    with app.app_context():
        assert dunning.retry_now("u1")["status"] == "nothing_owed"
    assert calls["retried"] == []


# --- WHICH invoice the button charges -------------------------------------------------
#
# The one place the manual path diverges from the scheduled one. The cron chases the
# oldest debt because that is what collections does; a customer pressing Pay now is
# buying their service back, and a bill for a period that ended months ago does not sell
# it to them. Every give-up leaves such a bill behind, so this is the normal case for a
# returning customer, not an exotic one.

# _Account: anchor 2027-01-08, paid_through 2027-02-08 => the current period starts
# 2027-02-08 (``renewals.period_key`` is derived from the period START).
# The GROUP is in the renewal key now: a payer with two cards raises two invoices for
# one period, so the key has to say which card owes this one.
CURRENT_INV = {"id": "in_now", "metadata": {"renewal_key": "renewal-u1-20270208-g1"}}
STALE_INV = {"id": "in_old", "metadata": {"renewal_key": "renewal-u1-20261208-g1"}}


def test_it_charges_the_CURRENT_period_not_the_oldest_open_invoice(app, monkeypatch):
    """Oldest first is what ``open_invoices`` returns and what the cron takes. Taking it
    here charged an abandoned bill, reported "your subscription is active again", and
    left the period they pressed the button about still unpaid."""
    dunning, calls = _wire(
        app, monkeypatch, account=_Account(), invoices=[STALE_INV, CURRENT_INV]
    )

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "paid"
    assert calls["retried"] == ["in_now"]
    assert result["invoice"] == "in_now"


def test_only_older_debt_is_reported_rather_than_charged(app, monkeypatch):
    """Nothing open belongs to the current period — the give-up case. Charging it would
    take money and restore nothing, and whether to pursue lapsed debt is not a decision a
    Pay-now button gets to make. No attempt is spent on it either."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=[STALE_INV])

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "older_debt_only"
    assert result["invoice"] == "in_old"      # named, so support can see what is meant
    assert calls["retried"] == []
    assert calls["attempts"] == 0
    assert calls["ended"] == []


def test_a_mid_period_charge_is_still_collectable(app, monkeypatch):
    """A change or reinstatement invoice carries no ``renewal_key``, so it is not a stale
    renewal — it is a real debt the customer can settle. Refusing it would be the old
    "no outstanding payment" bug in new clothes."""
    change = {"id": "in_c", "metadata": {"change_key": "change-e1-x"}}
    dunning, calls = _wire(
        app, monkeypatch, account=_Account(), invoices=[STALE_INV, change]
    )

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "paid"
    assert calls["retried"] == ["in_c"]


def test_a_successful_manual_payment_switches_the_modules_back_on(app, monkeypatch):
    """``end_group_dunning`` flips the phase, but it is skipped when there is no stamp — the
    exact case this function exists to serve. So the payer paid and stayed switched off
    until the nightly sweep. The scheduled path always restored access explicitly; this
    one only appeared to, because dunning was usually running by the time anyone clicked.
    """
    restored: list = []
    dunning, _ = _wire(
        app, monkeypatch, account=_Account(started=None), invoices=[CURRENT_INV]
    )
    monkeypatch.setattr(dunning, "_restore_access", lambda uid: restored.append(uid))

    with app.app_context():
        assert dunning.retry_now("u1")["status"] == "paid"

    assert restored == ["u1"]


# --- one card and one invoice named outright (the payer portal's invoice row) ----------


def test_a_named_card_is_charged_and_one_not_the_payers_is_not(app, monkeypatch):
    """``group_id`` names the CARD outright - the portal's invoice row knows its billing
    account, not a company. A card that is not this payer's charges nothing, rather than
    falling back to one nobody asked about."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)

    assert dunning.retry_now("u1", group_id="g1")["status"] == "paid"
    assert calls["retried"] == ["in_1"]

    refused = dunning.retry_now("u1", group_id="g-not-theirs")
    assert refused["status"] == "no_card"
    assert calls["retried"] == ["in_1"], "nothing more was charged"


def test_the_invoice_a_row_names_is_charged_only_if_the_rules_pick_it(app, monkeypatch):
    """The row's button may pay only the bill it sits beside. Asked for another one - a page
    read before something moved - it refuses BEFORE a slot is spent."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)

    other = dunning.retry_now("u1", group_id="g1", expect_invoice="in_other")
    assert other["status"] == "not_this_invoice"
    assert (calls["retried"], calls["attempts"]) == ([], 0)

    assert dunning.retry_now("u1", group_id="g1", expect_invoice="in_1")["status"] == "paid"
    assert calls["retried"] == ["in_1"]


# --- an invoice the processor will no longer collect (``dunning._charge``) --------------------
#
# Stripe cancels an invoice's payment once it has been confirmed too many times. Pay now is
# exactly when a customer who has just fixed their card meets that invoice - so it re-issues it
# and charges the replacement, in the same press.


def _dead_first(monkeypatch, *, pays=True, replacement="in_2", raises=None):
    """``in_1`` can no longer be paid; re-issuing it yields ``replacement``, which ``pays``."""
    from billing.services import billing_gateway

    seen = {"retried": [], "refreshed": []}

    def _retry(invoice_id, payment_method=None):
        seen["retried"].append(invoice_id)
        if invoice_id == "in_1":
            return False, billing_gateway.DEAD_PAYMENT
        return (True, None) if pays else (False, "Your card was declined.")

    def _refresh(invoice_id, payment_method=None):
        seen["refreshed"].append(invoice_id)
        if raises is not None:
            raise raises
        return {"id": replacement, "metadata": {"replaces": invoice_id}} if replacement else None

    monkeypatch.setattr(billing_gateway, "retry_invoice", _retry)
    monkeypatch.setattr(billing_gateway, "refresh_invoice", _refresh)
    return seen


def test_pay_now_on_a_dead_invoice_refreshes_and_charges_the_replacement(app, monkeypatch):
    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)
    seen = _dead_first(monkeypatch)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert (result["status"], result["invoice"], result["refreshed"]) == ("paid", "in_2", "in_1")
    assert seen == {"retried": ["in_1", "in_2"], "refreshed": ["in_1"]}
    assert calls["settled"] == ["in_2"]
    assert calls["attempts"] == 1


def test_pay_now_reports_the_replacements_decline_never_the_marker(app, monkeypatch):
    from billing.services import billing_gateway

    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)
    _dead_first(monkeypatch, pays=False)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert (result["status"], result["reason"]) == ("failed", "Your card was declined.")
    assert billing_gateway.DEAD_PAYMENT not in map(str, result.values())
    assert calls["ended"] == []


def test_pay_now_on_an_invoice_that_cannot_be_refreshed_is_not_collectable(app, monkeypatch):
    """Not a decline - nothing was charged - so not the decline's words either."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)
    _dead_first(monkeypatch, replacement=None)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result == {"status": "not_collectable", "attempts": 1, "invoice": "in_1",
                      "reason": None}
    assert (calls["ended"], calls["settled"]) == ([], [])


def test_pay_now_lets_a_processor_failure_during_the_refresh_raise(app, monkeypatch):
    """The routes answer that as "couldn't reach the card processor"; the next press resumes
    the refresh where it stopped."""
    import pytest

    dunning, _calls = _wire(app, monkeypatch, account=_Account(), invoices=INVOICES)
    _dead_first(monkeypatch, raises=RuntimeError("processor unreachable"))

    with app.app_context(), pytest.raises(RuntimeError):
        dunning.retry_now("u1")


def test_a_row_showing_the_replacement_collects_while_the_original_is_still_open(
    app, monkeypatch
):
    """Between the replacement being raised and the original voided, both are open: the row
    shows the replacement (it holds the period key), the rules pick the original (the older of
    two invoices for one period). Charging the original finishes the refresh and collects on
    exactly the replacement the row shows."""
    from billing.services import renewals

    account = _Account()
    key = renewals.period_key(
        "u1", renewals.next_period(account.anchor_at, account.paid_through), "g1"
    )
    both = [{"id": "in_1", "metadata": {"renewal_key": key}},
            {"id": "in_2", "metadata": {"renewal_key": key, "replaces": "in_1"}}]
    dunning, _calls = _wire(app, monkeypatch, account=account, invoices=both)
    _dead_first(monkeypatch)

    with app.app_context():
        result = dunning.retry_now("u1", group_id="g1", expect_invoice="in_2")

    assert (result["status"], result["invoice"]) == ("paid", "in_2")


# --- the processor failing, and a paid charge whatever its recording does ---------------------


def _wire_more(monkeypatch, calls):
    from billing.services import store

    calls["refunded"], calls["released"] = 0, []
    monkeypatch.setattr(
        store, "refund_group_dunning_attempt",
        lambda gid: calls.__setitem__("refunded", calls["refunded"] + 1) or 0,
    )
    monkeypatch.setattr(store, "release_group_grace",
                        lambda gid, at: calls["released"].append(gid) or 0)


def test_pay_now_meeting_an_outage_is_its_own_answer_and_spends_nothing(app, monkeypatch):
    """Not "that card was declined": the customer's card was never asked."""
    from billing.api._retry import retry_answer
    from billing.services import billing_gateway

    dunning, calls = _wire(app, monkeypatch, account=_Account(),
                           invoices=[{"id": "in_1", "metadata": {}}], paid=False,
                           reason=billing_gateway.UNAVAILABLE)
    _wire_more(monkeypatch, calls)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "unavailable"
    assert calls["refunded"] == 1
    answer = retry_answer(result)
    assert answer["ok"] is False
    assert "payment provider" in answer["message"] and "declined" not in answer["message"]


def test_pay_now_that_paid_is_paid_even_if_recording_it_failed(app, monkeypatch):
    """A write failing after the money moved used to turn a collected payment into a 500."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(),
                           invoices=[{"id": "in_1", "metadata": {}}])
    _wire_more(monkeypatch, calls)

    def _db_down(acct, grp, inv):
        raise RuntimeError("database went away")

    monkeypatch.setattr(dunning, "_settle_period", _db_down)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "paid"


def test_pay_now_ends_a_silent_grace(app, monkeypatch):
    """No dunning ran - the processor had failed, and the companies were held past due
    without anyone being told. Paid now, they come back."""
    dunning, calls = _wire(app, monkeypatch, account=_Account(started=None),
                           invoices=[{"id": "in_1", "metadata": {}}])
    _wire_more(monkeypatch, calls)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "paid"
    assert calls["released"] == ["g1"] and calls["ended"] == []



def test_pay_now_at_the_deadline_on_a_card_paid_up_is_settled_not_closed(app, monkeypatch):
    """The same re-check the scheduled run makes at its deadline."""
    dunning, calls = _wire(app, monkeypatch,
                           account=_Account(started=NOW - timedelta(days=16),
                                            paid_through=NOW + timedelta(days=20)),
                           invoices=[])
    _wire_more(monkeypatch, calls)

    with app.app_context():
        result = dunning.retry_now("u1")

    assert result["status"] == "nothing_owed"
    assert calls["ended"] == [("u1", "active")]
