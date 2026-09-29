"""The charge that settles a subscriber handover, at accept.

The new payer is billed for the window the OLD payer's money does not reach — old payer
paid to 12 Sept, new payer's anchor is the 1st, so the new payer pays 12 Sept -> 1 Oct.

Two things here are not like any other charge in this codebase, and both have a test
below because both fail silently:

  the period is derived from ``at``, not from ``now``. ``at`` is in the FUTURE at accept
  time, and a period taken from ``now`` puts it past ``period.end`` — so ``prorate``
  returns 0, ``build_change`` returns None, and the caller reads None as "nothing owed"
  and hands the company over for free;

  the idempotency key is passed IN, carrying the offer's attempt number. Fixed within an
  attempt so a double-click is refused, different across attempts so a declined card can
  be retried — because voiding an invoice deliberately keeps its row and its key claimed,
  and a stable key would jam the retry forever.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

# The new payer bills on the 1st. "Now" is 20 Aug — the moment the offer is accepted.
ANCHOR = datetime(2027, 8, 1, tzinfo=UTC)
NOW = datetime(2027, 8, 20, tzinfo=UTC)
# The old payer is paid up to here, so this is where the new payer's liability starts.
AT = datetime(2027, 9, 12, tzinfo=UTC)
PERIOD_END = datetime(2027, 10, 1, tzinfo=UTC)

KEY = "transfer-t1-1"


class _Plan:
    display_name = "Super Minty"
    amount = 30000
    currency = "HKD"


class _Group:
    """The card the INCOMING payer nominated for this company. Required, not defaulted:
    a handover charges the person taking it on, and only on a card they chose for it."""

    def __init__(self, id="g_new", card="pm_new"):
        self.id = id
        self.payer_user_id = "new-payer"
        self.stripe_payment_method_id = card
        self.paid_through = None


def _wire(monkeypatch, *, anchor=ANCHOR, issued=None, raises=None, existing=None,
          group=_Group()):
    """Mock the store and the gateway; return (checkout, calls)."""
    from billing.services import billing_gateway, checkout, renewals, store

    calls = {"issued": [], "voided": [], "paid_through": [], "anchored": []}

    monkeypatch.setattr(store, "billing_cycle_for_user",
                        lambda uid: (anchor, "HKD"))
    monkeypatch.setattr(store, "billing_plan_for_codes", lambda codes: _Plan())
    monkeypatch.setattr(store, "billing_group_for_entity",
                        lambda eid, uid=None: group)
    # Recorded under the payer so the assertions read the same: the cycle that starts
    # here is the new payer's card, and they have exactly one.
    monkeypatch.setattr(
        store, "set_group_paid_through",
        lambda gid, until: calls["paid_through"].append(("new-payer", until)),
    )
    monkeypatch.setattr(store, "start_billing_cycle",
                        lambda uid, at, cur: calls["anchored"].append((uid, at, cur)))
    monkeypatch.setattr(checkout, "_entity_invoice_name", lambda eid: "Bakery Ltd")
    monkeypatch.setattr(checkout, "_void_unpaid_invoice",
                        lambda inv, eid, what: calls["voided"].append(inv))
    monkeypatch.setattr(renewals, "_already_invoiced",
                        lambda cid, key, **kw: existing)

    def _issue(cid, invoice, **kw):
        calls["issued"].append((cid, invoice, kw))
        if raises is not None:
            raise raises
        return issued if issued is not None else {"id": "in_1", "status": "paid"}

    monkeypatch.setattr(billing_gateway, "issue_invoice", _issue)
    return checkout, calls


def _charge(checkout, **kw):
    return checkout._bill_transfer_in_house(
        "e1", "new-payer", "cus_new", {"PAYMENT_REQUEST"}, at=AT, idempotency_key=KEY, **kw
    )


# --- the period comes from `at` -------------------------------------------------


def test_the_window_charged_starts_where_the_old_payer_s_money_ends(monkeypatch):
    """The whole feature in one assertion: 12 Sept -> 1 Oct, not 20 Aug -> 1 Sept."""
    checkout, calls = _wire(monkeypatch)

    result = _charge(checkout)

    assert result["paid"] is True
    assert result["period_end"] == PERIOD_END
    _cid, invoice, _kw = calls["issued"][0]
    # 19 of the 31 days in [1 Sept, 1 Oct) — priced from `at`, not from the whole period.
    assert invoice.total == pytest.approx(round(30000 * 19 / 30), abs=1)


def test_a_future_window_is_not_silently_free(monkeypatch):
    """The trap. Deriving the period from ``now`` (20 Aug) puts ``at`` (12 Sept) past
    ``period.end``, so ``prorate`` returns 0 and ``build_change`` returns None — which the
    caller reads as "nothing was owed" and treats as success. The company would change
    hands for nothing, with no error anywhere."""
    checkout, calls = _wire(monkeypatch)

    _charge(checkout)

    _cid, invoice, _kw = calls["issued"][0]
    assert invoice.total > 0, "a future handover window must still be charged"


def test_the_memo_states_the_real_span(monkeypatch):
    """The invoice's own period_start/period_end record the payer's WHOLE period, so
    without the memo the customer sees a part-month charge with nothing explaining it."""
    checkout, calls = _wire(monkeypatch)

    _charge(checkout)

    memo = calls["issued"][0][2]["memo"]
    assert "Bakery Ltd" in memo and "12 Sep 2027" in memo


# --- the key ---------------------------------------------------------------------


def test_the_caller_s_key_is_used_verbatim(monkeypatch):
    """It carries the offer's attempt number. Derived here instead — from ``at``, which is
    stored and does not move between retries — it would be identical on every attempt and
    jam the retry, because voiding keeps the key claimed."""
    checkout, calls = _wire(monkeypatch)

    _charge(checkout)

    kwargs = calls["issued"][0][2]
    assert kwargs["idempotency_key"] == KEY
    assert kwargs["metadata"]["transfer_key"] == KEY
    assert kwargs["metadata"]["entity_id"] == "e1"


def test_an_already_paid_key_is_adopted_not_charged_again(monkeypatch):
    """The crash window: a previous attempt collected the money and died before the payer
    pointer moved. Charging again would take it twice."""
    from billing.services import store

    checkout, calls = _wire(monkeypatch, existing="paid")
    monkeypatch.setattr(
        store, "invoice_for_key",
        lambda key: type("R", (), {"external_id": "in_old"})(),
    )

    result = _charge(checkout)

    assert result["paid"] is True
    assert result["invoice_id"] == "in_old"
    assert calls["issued"] == [], "must not raise a second document"


def test_an_open_key_refuses_rather_than_double_billing(monkeypatch):
    """Raised but unpaid. A second document against the same window is two bills."""
    checkout, calls = _wire(monkeypatch, existing="open")

    result = _charge(checkout)

    assert result["paid"] is False
    assert calls["issued"] == []


# --- failure leaves the entity alone ---------------------------------------------


def test_a_decline_voids_the_invoice_and_reports_failure(monkeypatch):
    """A decline arrives as an exception out of ``Invoice.pay``, by which point the
    document is finalized and OPEN. Left there it becomes the first thing dunning chases,
    for a handover that never happened."""
    from billing.services.billing_gateway import BillingError

    checkout, calls = _wire(
        monkeypatch, raises=BillingError("declined", invoice_id="in_bad"),
    )

    result = _charge(checkout)

    assert result["paid"] is False
    assert result["period_end"] is None
    assert calls["voided"] == ["in_bad"]


def test_an_unpaid_status_is_also_a_failure(monkeypatch):
    checkout, calls = _wire(monkeypatch, issued={"id": "in_2", "status": "open"})

    result = _charge(checkout)

    assert result["paid"] is False
    assert calls["voided"] == ["in_2"]


# --- the unanchored new payer -----------------------------------------------------


def test_an_unanchored_payer_is_anchored_at_the_handover_not_at_now(monkeypatch):
    """Their cycle begins where the old payer's money ends, so the handover has no seam
    and the first charge is one clean period. Anchoring at ``now`` instead — which is what
    every other caller of ``start_billing_cycle`` does — would start their cycle on
    whichever day they happened to click accept."""
    checkout, calls = _wire(monkeypatch, anchor=None)
    # start_billing_cycle is mocked, so the re-read still answers None unless we move it.
    from billing.services import store

    anchors = iter([(None, "HKD"), (AT, "HKD")])
    monkeypatch.setattr(store, "billing_cycle_for_user", lambda uid: next(anchors))

    result = _charge(checkout)

    assert calls["anchored"] == [("new-payer", AT, "HKD")]
    # A full period from the handover, not a part-month.
    assert result["paid"] is True
    _cid, invoice, _kw = calls["issued"][0]
    assert invoice.total == 30000
    # First charge on the account, so the cycle is ESTABLISHED here.
    assert calls["paid_through"] == [("new-payer", result["period_end"])]
    # The anchor established HERE is what comes back, not the None it started as — this
    # is the case the accept cannot re-derive later, because a second read cannot tell an
    # anchor this charge created from one the payer already had.
    assert result["anchor"] == AT


def test_the_cycle_charged_against_is_reported_back(monkeypatch):
    """The accept stamps ``accepted_anchor_at`` from this. Without it the offer row records
    what was billed but not which cycle the entity landed on — and the anchor is per payer,
    so a handover always moves it."""
    checkout, _calls = _wire(monkeypatch)

    result = _charge(checkout)

    assert result["anchor"] == ANCHOR


def test_a_failed_charge_reports_no_cycle(monkeypatch):
    """Nothing was collected, so there is no cycle the entity can be said to be on."""
    checkout, _calls = _wire(monkeypatch, issued={"id": "in_1", "status": "open"})

    result = _charge(checkout)

    assert result["paid"] is False
    assert result["anchor"] is None


def test_an_established_payer_s_cycle_is_never_advanced(monkeypatch):
    """``paid_through`` is the ACCOUNT's marker. Moving it for one entity's charge
    announces that every other entity on the account is settled too, silently cancelling
    their renewal."""
    checkout, calls = _wire(monkeypatch)

    _charge(checkout)

    assert calls["paid_through"] == []


# --- the quote and the charge agree ----------------------------------------------


def test_the_quote_matches_what_is_actually_charged(monkeypatch):
    """``preview_reinstate_modules`` is what happens when a preview mirrors a charge by
    hand instead of sharing it — it shipped quoting zero. These call one function."""
    checkout, calls = _wire(monkeypatch)

    quote = checkout.quote_transfer_charge("e1", "new-payer", {"PAYMENT_REQUEST"}, at=AT)
    _charge(checkout)

    _cid, invoice, _kw = calls["issued"][0]
    assert quote["amount"] == invoice.total
    assert quote["period_end"] == PERIOD_END
    assert quote["anchor_is_new"] is False


def test_quoting_an_unanchored_payer_writes_nothing(monkeypatch):
    """Opening the accept screen must have no side effect — it reports what the anchor
    WOULD become, it does not set it."""
    checkout, calls = _wire(monkeypatch, anchor=None)

    quote = checkout.quote_transfer_charge("e1", "new-payer", {"PAYMENT_REQUEST"}, at=AT)

    assert quote["anchor_is_new"] is True
    assert quote["anchor_at"] == AT
    assert calls["anchored"] == []
