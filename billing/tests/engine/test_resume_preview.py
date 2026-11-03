"""What the "Resume module" dialog quotes.

This function is mirrored by hand from ``_bill_reinstatement_in_house`` rather than sharing
a pure helper with it, and it has now shipped two wrong numbers: a first version quoted
ZERO because it did not subtract the resuming module from ``before``, and a later one
quoted the BUNDLE price as the ongoing monthly on an entity where the other module was
also winding down. Both were silent — a plausible figure in the right currency, on the
screen where somebody decides whether to restart a subscription.

So the point of this file is narrow and worth stating: the dialog quotes TWO amounts that
come from TWO DIFFERENT SETS, and conflating them is the whole failure mode.

    charged today   from what the period was PAID FOR — a module winding down was paid
                    for, so resuming beside it is an upgrade to the bundle they still are
                    until it goes.

    monthly         from what will still be BILLING FORWARD afterwards — a module winding
                    down will not be there, so it must not be in the recurring price.

They coincide whenever nothing else is cancelling, which is why this went unnoticed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
ANCHOR = datetime(2026, 6, 13, 12, tzinfo=UTC)
ACCESS_UNTIL = datetime(2026, 9, 18, 12, tzinfo=UTC)
# Inside the period NOW falls in (13 Aug - 13 Sep). An extension reaching PAST the period
# end leaves nothing to charge — the preview skips it — so a row that is meant to cost
# something on resume has to stop before it. This is what a cancellation swept by the
# 13 Aug renewal looks like: invoiced, and its days running out on the 25th.
COVERED_TO = datetime(2026, 8, 25, 12, tzinfo=UTC)

PRICES = {
    frozenset({"PETTY_CASH"}): (28000, "Petty Cash"),
    frozenset({"PAYMENT_REQUEST"}): (28000, "Payment Request"),
    frozenset({"PAYMENT_REQUEST", "PETTY_CASH"}): (40000, "Super Minty"),
}


def _row(code, phase, *, first_billed=NOW - timedelta(days=90), ext_state=None,
         access_until=None):
    return SimpleNamespace(
        function_code=code,
        phase=phase,
        payer_user_id="payer-1",
        app_access_until=(
            access_until if access_until is not None
            else (ACCESS_UNTIL if phase == "scheduled_cancel" else None)
        ),
        first_billed_at=first_billed,
        trial_end=None,
        # ``invoiced`` is what makes resuming cost anything: a still-PENDING extension is
        # discarded for free, so the preview skips it. Tests that want a real charge have
        # to say so.
        extension_state=ext_state,
        extension_amount=None,
    )


def _wire(monkeypatch, rows, *, covered):
    """Mock the catalog and the cycle. ``covered`` is what the period was PAID FOR —
    ``_billed_codes_in_house``'s answer, which includes modules winding down."""
    from billing.services import checkout, clock, store

    monkeypatch.setattr(clock, "now", lambda: NOW)
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: list(rows))
    monkeypatch.setattr(store, "billing_cycle_for_user", lambda uid: (ANCHOR, "HKD"))
    monkeypatch.setattr(store, "paid_through_for_user",
                        lambda uid: datetime(2026, 9, 13, 12, tzinfo=UTC))
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: datetime(2026, 9, 13, 12, tzinfo=UTC))
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: set(covered))

    def _plan(codes):
        found = PRICES.get(frozenset(str(c).upper() for c in codes))
        if not found:
            return None
        amount, name = found
        return SimpleNamespace(amount=amount, display_name=name, currency="HKD")

    monkeypatch.setattr(store, "billing_plan_for_codes", _plan)
    return checkout


def _preview(checkout, code="PETTY_CASH"):
    entity = SimpleNamespace(id="e1", name="Demo Co")
    user = SimpleNamespace(id="payer-1")
    return checkout.preview_reinstate_modules(entity, user, [code])


# --- the recurring price ----------------------------------------------------------


def test_resuming_beside_a_live_module_quotes_the_bundle(monkeypatch):
    """The other module keeps running, so the entity really will be on Super Minty."""
    checkout = _wire(
        monkeypatch,
        [_row("PAYMENT_REQUEST", "active"), _row("PETTY_CASH", "scheduled_cancel")],
        covered={"PAYMENT_REQUEST", "PETTY_CASH"},
    )

    assert _preview(checkout)["monthly"] == 40000


def test_resuming_when_the_other_module_is_also_leaving_quotes_the_solo_price(monkeypatch):
    """THE BUG. Both cancelling: resuming one brings back only that one, and the other
    still lapses — so the ongoing cost is the solo price. Quoting the bundle told the
    customer they would pay 120/month more than they will."""
    checkout = _wire(
        monkeypatch,
        [_row("PAYMENT_REQUEST", "scheduled_cancel"), _row("PETTY_CASH", "scheduled_cancel")],
        # Both were PAID FOR this period, which is exactly why the two sets diverge.
        covered={"PAYMENT_REQUEST", "PETTY_CASH"},
    )

    assert _preview(checkout)["monthly"] == 28000


def test_the_only_module_on_the_entity_quotes_its_own_price(monkeypatch):
    checkout = _wire(
        monkeypatch, [_row("PETTY_CASH", "scheduled_cancel")], covered={"PETTY_CASH"}
    )

    assert _preview(checkout)["monthly"] == 28000


def test_a_module_that_already_lapsed_does_not_count_toward_the_bundle(monkeypatch):
    """``cancelled`` is terminal — it is not coming back on its own, so it cannot be part
    of what the entity pays from next period."""
    checkout = _wire(
        monkeypatch,
        [_row("PAYMENT_REQUEST", "cancelled"), _row("PETTY_CASH", "scheduled_cancel")],
        covered={"PETTY_CASH"},
    )

    assert _preview(checkout)["monthly"] == 28000


def test_a_past_due_module_still_counts(monkeypatch):
    """``past_due`` is billing forward: the subscription has not ended and the money is
    still owed, so it is part of what the entity will be paying."""
    checkout = _wire(
        monkeypatch,
        [_row("PAYMENT_REQUEST", "past_due"), _row("PETTY_CASH", "scheduled_cancel")],
        covered={"PAYMENT_REQUEST", "PETTY_CASH"},
    )

    assert _preview(checkout)["monthly"] == 40000


# --- and the two amounts stay distinct --------------------------------------------


def _capture_build(monkeypatch):
    """Record every ``build_change`` call the preview makes, in order."""
    from billing.services import changes

    seen = []

    def _build(entity_id, name, before, after, period, at):
        seen.append({"before": set(before), "after": set(after)})
        return SimpleNamespace(total=3484)

    monkeypatch.setattr(changes, "build_change", _build)
    return seen


def test_the_charge_today_still_counts_a_module_that_is_winding_down(monkeypatch):
    """The other half of the rule, and the reason this cannot be fixed by using one set
    for both figures. A cancelling module that was PAID FOR this period is still on the
    line for the days that remain, so the charge now is the upgrade against it, not a
    fresh join. Only the RECURRING figure drops it.

    Payment Request is the one winding down and its extension is still PENDING — it was
    cancelled inside this period, after the renewal that billed it, so it genuinely was
    paid for and its days run past the period end. Petty Cash is the one being resumed,
    and its extension is INVOICED, which is what makes resuming cost anything at all.

    This test used to assert nothing: both rows defaulted to no extension state, so the
    preview skipped straight past ``build_change`` and the assertion sat behind an
    ``if seen:`` that was never true.
    """
    checkout = _wire(
        monkeypatch,
        [
            _row("PAYMENT_REQUEST", "scheduled_cancel", ext_state="pending"),
            _row("PETTY_CASH", "scheduled_cancel", ext_state="invoiced",
                 access_until=COVERED_TO),
        ],
        covered={"PAYMENT_REQUEST", "PETTY_CASH"},
    )
    seen = _capture_build(monkeypatch)

    result = _preview(checkout)

    assert result["monthly"] == 28000, "recurring drops the module that is leaving"
    assert seen, "the preview must actually price something"
    assert "PAYMENT_REQUEST" in seen[0]["before"], (
        "the charge today still prices against the days already paid for"
    )


def test_after_a_renewal_a_resume_is_a_FRESH_JOIN_not_a_bundle_upgrade(monkeypatch):
    """The undercharge this pairing was hiding.

    Both modules were cancelled and the renewal has been through: it advanced the payer's
    paid_through and stamped BOTH extensions invoiced, while billing neither module for
    the period that just started. So nothing is on the plan line, and resuming one is a
    fresh join at its standalone price — not an upgrade against a module the customer was
    never charged for.

    ``covered`` is empty here because that is what ``_billed_codes_in_house`` now returns
    for two swept rows; the fix that makes it empty lives in ``access.is_covered_this_period``
    and is pinned in tests/test_change_billing.py.
    """
    checkout = _wire(
        monkeypatch,
        [
            _row("PAYMENT_REQUEST", "scheduled_cancel", ext_state="invoiced",
                 access_until=COVERED_TO + timedelta(days=1)),
            _row("PETTY_CASH", "scheduled_cancel", ext_state="invoiced",
                 access_until=COVERED_TO),
        ],
        covered=set(),
    )
    seen = _capture_build(monkeypatch)

    _preview(checkout)

    assert seen, "the preview must actually price something"
    assert seen[0]["before"] == set(), "nothing is on the line: this is a join"
    assert seen[0]["after"] == {"PETTY_CASH"}


def test_resuming_BOTH_quotes_what_the_two_renew_calls_will_actually_charge(monkeypatch):
    """The dialog posts one list; the commit then calls /renew once per module.

    So the second module is priced against a line that already has the first back on it —
    ``_reactivate_module_in_house`` writes phase=ACTIVE before the next call runs. Seeding
    the covered set with the whole wanted list instead priced both as if the other were
    already there, quoting two bundle steps where the commit collects a join and then a
    step.
    """
    checkout = _wire(
        monkeypatch,
        [
            _row("PAYMENT_REQUEST", "scheduled_cancel", ext_state="invoiced",
                 access_until=COVERED_TO + timedelta(days=1)),
            _row("PETTY_CASH", "scheduled_cancel", ext_state="invoiced",
                 access_until=COVERED_TO),
        ],
        covered=set(),
    )
    seen = _capture_build(monkeypatch)

    entity = SimpleNamespace(id="e1", name="Demo Co")
    user = SimpleNamespace(id="payer-1")
    checkout.preview_reinstate_modules(entity, user, ["PAYMENT_REQUEST", "PETTY_CASH"])

    assert [s["before"] for s in seen] == [set(), {"PAYMENT_REQUEST"}], (
        "BILL joins an empty line; PETTY_CASH is then a step up from BILL"
    )
    assert [s["after"] for s in seen] == [{"PAYMENT_REQUEST"}, {"PAYMENT_REQUEST", "PETTY_CASH"}]
