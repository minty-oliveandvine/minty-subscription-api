"""Unit tests for mid-period change billing.

The expected figures are ORACLE values — what Stripe actually charged on a test clock
while it was still the biller, captured before the cutover. They are the reason this can
be trusted: the in-house arithmetic reproduces them exactly.

    28.00  upgrade 280 -> 400, 7 of 30 days   (-65.33 credit + 93.33 charge)
   373.33  new line joining mid-period, 28 of 30 days
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from billing.services.billing import Period, plan_code

# The price catalog is patched BY DOTTED PATH, not via an imported reference (Minty's
# conftest re-imports project modules mid-session; kept as the module was written).
_CATALOG = "billing.services.catalog"


class _Plan:
    def __init__(self, name, amount, currency="HKD"):
        self.display_name = name
        self.amount = amount
        self.currency = currency


PLANS = {
    "BILL": _Plan("Payment Request", 28000),
    "PETTY_CASH": _Plan("Petty Cash", 28000),
    "BILL+PETTY_CASH": _Plan("Super Minty", 40000),
}


def _wire(monkeypatch, plans=None):
    from billing.services import changes, store
    catalog = PLANS if plans is None else plans
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        # keyed by plan words (BILL), asked with module codes (PAYMENT_REQUEST): plan_code maps
        lambda codes: catalog.get(plan_code(codes)),
    )
    return changes


# --- ORACLE: figures Stripe actually charged -----------------------------------


def test_oracle_upgrade_credits_the_old_price_and_charges_the_new(monkeypatch):
    """28.00 net = -65.33 unused Petty Cash + 93.33 remaining bundle, 7 of 30 days.

    Both lines are kept rather than the net: a customer shown only "28.00" cannot
    reconcile it against the 280.00 they paid three weeks earlier."""
    changes = _wire(monkeypatch)
    period = Period(datetime(2026, 9, 8, 13, tzinfo=UTC),
                    datetime(2026, 10, 8, 13, tzinfo=UTC))

    invoice = changes.build_change(
        "e1", "Alpha Co", ["PETTY_CASH"], ["PETTY_CASH", "PAYMENT_REQUEST"],
        period, datetime(2026, 10, 1, 13, tzinfo=UTC),
    )

    assert [line.amount for line in invoice.lines] == [-6533, 9333]
    assert invoice.total == 2800


def test_oracle_a_new_line_joining_mid_period_has_nothing_to_credit(monkeypatch):
    """373.33 for 28 of 30 days. The entity was not paying for this period at all, so
    crediting anything would refund money never taken."""
    changes = _wire(monkeypatch)
    period = Period(datetime(2026, 11, 8, 13, tzinfo=UTC),
                    datetime(2026, 12, 8, 13, tzinfo=UTC))

    invoice = changes.build_change(
        "e2", "Beta Co", [], ["PETTY_CASH", "PAYMENT_REQUEST"],
        period, datetime(2026, 11, 10, 13, tzinfo=UTC),
    )

    assert len(invoice.lines) == 1
    assert invoice.total == 37333


# --- what must NOT be billed ---------------------------------------------------


def test_a_downgrade_bills_nothing_and_credits_nothing(monkeypatch):
    """Access continues to what was already paid for. Crediting the unused time would
    refund days the customer still gets — the cancel path owns that decision and
    charges a prorated extension instead."""
    changes = _wire(monkeypatch)
    period = Period(datetime(2026, 9, 8, 13, tzinfo=UTC),
                    datetime(2026, 10, 8, 13, tzinfo=UTC))

    assert changes.build_change(
        "e1", "Alpha Co", ["PETTY_CASH", "PAYMENT_REQUEST"], ["PAYMENT_REQUEST"],
        period, datetime(2026, 10, 1, 13, tzinfo=UTC),
    ) is None


def test_no_change_bills_nothing(monkeypatch):
    changes = _wire(monkeypatch)
    period = Period(datetime(2026, 9, 8, 13, tzinfo=UTC),
                    datetime(2026, 10, 8, 13, tzinfo=UTC))

    assert changes.build_change(
        "e1", "Alpha Co", ["PAYMENT_REQUEST"], ["PAYMENT_REQUEST"], period,
        datetime(2026, 10, 1, 13, tzinfo=UTC),
    ) is None


def test_an_unpriceable_TARGET_is_refused_not_guessed(monkeypatch):
    """Summing standalone prices would silently overcharge by the bundle discount."""
    changes = _wire(monkeypatch, plans={"BILL": PLANS["BILL"]})
    period = Period(datetime(2026, 9, 8, 13, tzinfo=UTC),
                    datetime(2026, 10, 8, 13, tzinfo=UTC))

    assert changes.build_change(
        "e1", "Alpha Co", [], ["PAYMENT_REQUEST", "PETTY_CASH"], period,
        datetime(2026, 10, 1, 13, tzinfo=UTC),
    ) is None


def test_an_unpriceable_CURRENT_set_is_refused_too(monkeypatch):
    """Without the old price there is no way to know what to credit — billing the full
    new price on top of one the customer already paid would double-charge them."""
    changes = _wire(monkeypatch, plans={"BILL+PETTY_CASH": PLANS["BILL+PETTY_CASH"]})
    period = Period(datetime(2026, 9, 8, 13, tzinfo=UTC),
                    datetime(2026, 10, 8, 13, tzinfo=UTC))

    assert changes.build_change(
        "e1", "Alpha Co", ["PETTY_CASH"], ["PAYMENT_REQUEST", "PETTY_CASH"], period,
        datetime(2026, 10, 1, 13, tzinfo=UTC),
    ) is None


# --- billing twice -------------------------------------------------------------


def test_the_change_key_distinguishes_two_changes_in_the_same_minute(monkeypatch):
    """A customer can legitimately make two different changes to one entity moments
    apart; a key on time alone would silently swallow the second."""
    changes = _wire(monkeypatch)
    at = datetime(2026, 10, 1, 13, tzinfo=UTC)

    assert changes.change_key("e1", at, ["PAYMENT_REQUEST"]) != changes.change_key(
        "e1", at, ["PAYMENT_REQUEST", "PETTY_CASH"]
    )
    # ...and is stable for the same change, so a retry after a crash is recognised.
    assert changes.change_key("e1", at, ["PETTY_CASH", "PAYMENT_REQUEST"]) == changes.change_key(
        "e1", at, ["PAYMENT_REQUEST", "PETTY_CASH"]
    )


def _wire_found(monkeypatch, found):
    """An earlier attempt at the change raised ``found`` and died before recording it."""
    from billing.services import billing_gateway, store

    calls = {"issued": [], "recorded": [], "keys": []}
    # The row that attempt reserved under the change's key: never confirmed (no processor
    # id), and long past IN_FLIGHT, so the processor is asked by metadata.
    reserved = _Reservation(external_id=None, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(billing_gateway, "find_invoice_by_metadata",
                        lambda cid, k, v: found)
    monkeypatch.setattr(billing_gateway, "issue_invoice",
                        lambda *a, **k: calls["issued"].append(a) or {"id": "in_new"})
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda key: calls["keys"].append(key) or reserved)
    monkeypatch.setattr(
        billing_gateway, "record_found_invoice",
        lambda record, invoice: calls["recorded"].append((record, invoice))
        or invoice["status"],
    )
    return calls, reserved


def test_an_already_invoiced_change_is_not_charged_again(monkeypatch):
    changes = _wire(monkeypatch)
    found = {"id": "in_old", "status": "paid"}
    calls, reserved = _wire_found(monkeypatch, found)
    period = Period(datetime(2026, 11, 8, 13, tzinfo=UTC),
                    datetime(2026, 12, 8, 13, tzinfo=UTC))
    at = datetime(2026, 11, 10, 13, tzinfo=UTC)

    result = changes.issue_change(
        "cus_1", "e2", "Beta Co", [], ["PETTY_CASH", "PAYMENT_REQUEST"], period, at,
    )

    assert result["id"] == "in_old"
    assert calls["issued"] == []          # nothing charged
    # ...and the row that earlier attempt reserved says what the processor says, rather than
    # the "draft, no link, no paid date" it was left at.
    assert calls["keys"] == [changes.change_key("e2", at, ["PETTY_CASH", "PAYMENT_REQUEST"])]
    assert calls["recorded"] == [(reserved, found)]


def test_a_draft_an_earlier_attempt_left_is_reported_as_stranded(monkeypatch, caplog):
    """Never finalized, so never charged, and nothing will ever finalize it. The callers
    refuse anything unpaid and withdraw it - but that it happened has to be said, at ERROR."""
    changes = _wire(monkeypatch)
    calls, _reserved = _wire_found(monkeypatch, {"id": "in_draft", "status": "draft"})
    period = Period(datetime(2026, 11, 8, 13, tzinfo=UTC),
                    datetime(2026, 12, 8, 13, tzinfo=UTC))

    result = changes.issue_change(
        "cus_1", "e2", "Beta Co", [], ["PETTY_CASH"], period,
        datetime(2026, 11, 10, 13, tzinfo=UTC),
    )

    assert result["id"] == "in_draft"     # handed back: every caller refuses what is unpaid
    assert calls["issued"] == []
    stranded = [r for r in caplog.records
                if r.levelname == "ERROR" and "STRANDED DRAFT in_draft" in r.getMessage()]
    assert len(stranded) == 1
    assert "withdrawn" in stranded[0].getMessage()


# --- "was this change already raised?" reads OUR row first ----------------------
#
# It used to LIST every invoice the customer ever had at the processor, on every change.
# The reservation row (claimed before the charge, UNIQUE) answers it in one lookup; the
# processor is asked only about a row it never confirmed.


class _Reservation:
    def __init__(self, external_id, created_at, id="row1"):
        self.id = id
        self.external_id = external_id
        self.created_at = created_at


_PERIOD = Period(datetime(2026, 11, 8, 13, tzinfo=UTC), datetime(2026, 12, 8, 13, tzinfo=UTC))
_AT = datetime(2026, 11, 10, 13, tzinfo=UTC)


def _wire_lookup(monkeypatch, record):
    """``record`` is the row under the change's key (None: never claimed). Asking the
    processor by metadata fails the test unless a test allows it."""
    from billing.services import billing_gateway, store

    calls = {"issued": [], "discarded": [], "searched": [], "rechecked": []}
    monkeypatch.setattr(store, "invoice_for_key", lambda key: record)
    monkeypatch.setattr(store, "discard_invoice", lambda rid: calls["discarded"].append(rid))
    monkeypatch.setattr(
        billing_gateway, "find_invoice_by_metadata",
        lambda cid, k, v: calls["searched"].append(v) or None,
    )
    monkeypatch.setattr(billing_gateway, "issue_invoice",
                        lambda *a, **k: calls["issued"].append(a) or {"id": "in_new"})
    return calls


def test_a_change_never_raised_costs_no_processor_call(monkeypatch):
    changes = _wire(monkeypatch)
    calls = _wire_lookup(monkeypatch, None)

    result = changes.issue_change("cus_1", "e2", "Beta Co", [], ["PETTY_CASH"], _PERIOD, _AT)

    assert result == {"id": "in_new"}
    assert calls["searched"] == []        # no LIST of the customer's invoices
    assert len(calls["issued"]) == 1


def test_a_confirmed_row_is_read_back_by_its_id_not_searched_for(monkeypatch):
    from billing.services import billing_gateway

    changes = _wire(monkeypatch)
    record = _Reservation(external_id="in_old", created_at=_AT)
    calls = _wire_lookup(monkeypatch, record)
    monkeypatch.setattr(
        billing_gateway, "recheck",
        lambda rec: calls["rechecked"].append(rec) or {"id": "in_old", "status": "paid"},
    )

    result = changes.issue_change("cus_1", "e2", "Beta Co", [], ["PETTY_CASH"], _PERIOD, _AT)

    assert result["id"] == "in_old"
    assert calls["rechecked"] == [record]
    assert calls["searched"] == [] and calls["issued"] == []


def test_a_reservation_the_processor_never_got_is_discarded_and_the_change_charged(monkeypatch):
    """Left in place, the claimed key would refuse this change for good."""
    changes = _wire(monkeypatch)
    record = _Reservation(external_id=None, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    calls = _wire_lookup(monkeypatch, record)

    changes.issue_change("cus_1", "e2", "Beta Co", [], ["PETTY_CASH"], _PERIOD, _AT)

    assert calls["searched"] == [changes.change_key("e2", _AT, ["PETTY_CASH"])]
    assert calls["discarded"] == ["row1"]
    assert len(calls["issued"]) == 1


def test_a_reservation_still_in_flight_is_refused_as_claimed_never_discarded(monkeypatch):
    """Another press's charge whose create has not answered yet: discarding its row and
    charging again would bill the customer twice the moment the first one lands."""
    from billing.services import billing_gateway

    changes = _wire(monkeypatch)
    record = _Reservation(external_id=None, created_at=datetime.now(UTC))
    calls = _wire_lookup(monkeypatch, record)

    with pytest.raises(billing_gateway.BillingError) as raised:
        changes.issue_change("cus_1", "e2", "Beta Co", [], ["PETTY_CASH"], _PERIOD, _AT)

    assert raised.value.claimed and raised.value.retryable
    assert calls["searched"] == [] and calls["discarded"] == [] and calls["issued"] == []


# --- where "before" comes from --------------------------------------------------
#
# The arithmetic above was already right. What was wrong was its INPUT: the callers
# derived "what this entity already bills" from the payer's Stripe subscription items,
# which do not exist once billing is in-house. Every upgrade therefore arrived here with
# before=set(), took the join branch, and charged the standalone price on top of the
# period the customer had already paid for.
#
# A live run billed 65.33 for adding Payment Request to an entity already on Petty Cash,
# where the oracle above says 28.00. These tests pin the derivation, not the sums —
# passing before_codes in by hand (as every test above does) cannot catch it.


class _Row:
    def __init__(self, code, phase="active", first_billed_at=datetime(2026, 7, 28, tzinfo=UTC),
                 ext_state=None, app_access_until=None):
        self.function_code = code
        self.phase = phase
        self.payer_user_id = "u1"
        # None = never charged for. It is what separates a cancelled PAID module (whose
        # period is bought and paid for) from a cancelled TRIAL (which bought nothing).
        self.first_billed_at = first_billed_at
        # The two per-row facts the account's paid_through cannot supply: whether the
        # renewal has already swept this module off the invoice, and whether the days it
        # bought have run out. Default None/None = "still inside the period it paid for".
        self.extension_state = ext_state
        self.app_access_until = app_access_until


def _stub_rows(monkeypatch, rows, paid_through=datetime(2026, 8, 28, 13, tzinfo=UTC)):
    """Module rows + the payer's paid-through, which _billed_codes_in_house needs to know
    whether a module winding down is still inside the period it paid for."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from billing.services import store

    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: rows)
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    monkeypatch.setattr(checkout.clock, "now", lambda: datetime(2026, 8, 20, 13, tzinfo=UTC))


def test_billed_codes_come_from_the_module_rows_not_stripe(monkeypatch):
    """The whole bug in one assertion: an entity on Petty Cash must not look empty."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(monkeypatch, [_Row("petty_cash")])

    assert checkout._billed_codes_in_house("e1") == {"PETTY_CASH"}


def test_a_trialing_module_is_not_billed_yet_so_does_not_count(monkeypatch):
    """Counting it would price the change against a plan nobody is paying for."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(monkeypatch, [_Row("PETTY_CASH", "active"), _Row("PAYMENT_REQUEST", "trial")])

    assert checkout._billed_codes_in_house("e1") == {"PETTY_CASH"}


def test_a_cancelling_module_counts_while_its_paid_period_runs(monkeypatch):
    """It is winding down, but the customer PAID for it to the period end — so for the
    days before that end the line holds it, and adding a module is an upgrade to the
    bundle rather than a fresh join. The re-price happens at the period end, on the
    renewal, which is where the cancelled module actually leaves."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(monkeypatch, [_Row("PETTY_CASH", "scheduled_cancel")])

    assert checkout._billed_codes_in_house("e1") == {"PETTY_CASH"}


def test_a_cancelling_module_stops_counting_once_its_period_ends(monkeypatch):
    """Past paid_through it is gone: nothing covers those days, so a change priced then
    must not credit or bundle against it."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(
        monkeypatch, [_Row("PETTY_CASH", "scheduled_cancel")],
        paid_through=datetime(2026, 8, 19, 13, tzinfo=UTC),   # yesterday
    )

    assert checkout._billed_codes_in_house("e1") == set()


def test_a_cancelling_module_stops_counting_once_the_RENEWAL_has_passed_it_by(monkeypatch):
    """The account's paid_through is not enough, and this is the case it gets wrong.

    A renewal advances paid_through for the whole PAYER while deliberately leaving the
    cancelling module off the invoice (``renewals.billable_codes_by_entity``). So the date
    alone says "covered" for a period the module was never billed for — and reinstating it
    was then priced as a bundle upgrade (the 120 step) instead of the fresh join (280) it
    is, with a credit line for unused time on a module that had no line to credit.

    The per-row half of the answer is the extension: the same run that moves paid_through
    stamps it invoiced.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(
        monkeypatch,
        [_Row("PETTY_CASH", "scheduled_cancel", ext_state="invoiced",
              app_access_until=datetime(2026, 9, 18, 13, tzinfo=UTC))],  # still running
    )

    assert checkout._billed_codes_in_house("e1") == set()


def test_a_cancellation_that_owed_NOTHING_stops_counting_when_its_access_runs_out(monkeypatch):
    """The hole the extension state cannot close.

    An extension is only recorded when the amount is positive, so a cancellation worth
    nothing never gets the invoiced stamp. ``terminate_lapsed_module`` would move the row
    to ``cancelled`` eventually, but the daily sweep lags and is skipped when a money job
    failed — so the access end has to be consulted directly.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(
        monkeypatch,
        [_Row("PETTY_CASH", "scheduled_cancel", ext_state=None,
              app_access_until=datetime(2026, 8, 19, 13, tzinfo=UTC))],  # ran out
    )

    assert checkout._billed_codes_in_house("e1") == set()


def test_a_cancelled_trial_never_counts(monkeypatch):
    """Winding down like the one above, but nothing was ever charged for it — there is no
    covered period to price against, so counting it would discount against a plan the
    customer has never paid a penny for."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(
        monkeypatch, [_Row("PETTY_CASH", "scheduled_cancel", first_billed_at=None)]
    )

    assert checkout._billed_codes_in_house("e1") == set()


def test_a_past_due_module_still_counts(monkeypatch):
    """The money is owed, not written off. Treating it as unbilled would charge the new
    module's standalone price on top of a period already invoiced."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    _stub_rows(monkeypatch, [_Row("PETTY_CASH", "past_due")])

    assert checkout._billed_codes_in_house("e1") == {"PETTY_CASH"}


def test_converting_a_trial_prices_it_against_what_the_entity_ALREADY_bills(monkeypatch):
    """The regression. Entity is live on Petty Cash; its Payment Request trial converts.

    The change must be measured 280 -> 400 (the bundle margin), never as a fresh join at
    Payment Request's standalone 280. Asserted at the seam where the live run went wrong:
    what the conversion hands to the biller.
    """
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from billing.services import store

    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_1")
    monkeypatch.setattr(checkout, "trial_payment_method", lambda cid: "pm_1")
    # The card THIS company is nominated onto — what the conversion actually charges.
    monkeypatch.setattr(store, "card_for_entity", lambda eid, uid=None: "pm_1")
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: True)
    monkeypatch.setattr(f"{_CATALOG}.plan_for_module", lambda code: object())
    monkeypatch.setattr(checkout, "_finish_conversion", lambda *a, **k: None)
    monkeypatch.setattr(
        store, "module_rows_for_entity",
        lambda eid: [_Row("PETTY_CASH", "active"), _Row("PAYMENT_REQUEST", "trial")],
    )

    seen = {}

    def _bill(entity_id, payer_user_id, customer_id, current, codes, **kw):
        seen["current"], seen["codes"] = set(current), set(codes)
        return datetime(2026, 10, 8, 13, tzinfo=UTC)

    monkeypatch.setattr(checkout, "_bill_module_change_in_house", _bill)

    billable, doomed = checkout._convert_due_trials("e1", [_Row("PAYMENT_REQUEST", "trial")])

    assert seen["current"] == {"PETTY_CASH"}   # was set() — the bug
    assert seen["codes"] == {"PAYMENT_REQUEST"}
    assert len(billable) == 1 and doomed == []
