"""The cancellation confirmation dialog quotes what cancelling will actually do.

``preview_cancel_module`` feeds the dialog the two facts it exists to state: when access
ends, and what is still owed. Cancelling can put money on the next invoice, so a preview
that drifts from the commit is worse than no dialog — it is a quoted figure the customer
was never charged, or was charged without being shown.

The load-bearing property, and the reason ``_paid_cancel_terms`` was extracted, is that
preview and commit share ONE calculation. ``test_preview_matches_what_cancelling_records``
is what holds that: it previews, then cancels, and asserts the recorded row carries the
previewed date and amount.

Pricing is the part most easily got wrong, and it is measured against what is LEAVING —
not against the line the entity keeps. Petty Cash cancelled on its own is charged its 280
list price for the extra days, because nothing else is leaving with it; cancel Payment
Request too and the pair is worth the 400 bundle between them, allocated in sorted code
order — Payment Request takes the 280 and Petty Cash the 120 step (see
checkout._leaving_marginal). The shares are uneven; the total is what the dialog quotes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

_CATALOG = "billing.services.catalog"

# Anchor and "now" pinned: the extension is a proration off the period containing
# paid_through, so a floating clock makes the amount unreproducible.
_ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
_NOW = datetime(2027, 1, 20, 13, tzinfo=UTC)
_PAID_THROUGH = datetime(2027, 2, 8, 13, tzinfo=UTC)


class _FakeEntity:
    id = "e1"
    name = "Acme"


class _FakeUser:
    id = "u1"
    email = "u1@example.com"


class _Row:
    def __init__(self, code="PETTY_CASH", phase="active", trial_end=None, payer="u1"):
        self.entity_id = "e1"
        self.function_code = code
        self.payer_user_id = payer
        self.phase = phase
        self.trial_end = trial_end
        self.app_access_until = None
        self.first_billed_at = None
        # Read by the re-pricing pass that runs after a cancellation: only rows with a
        # PENDING extension are re-priced, and these rows have none.
        self.extension_state = None
        self.extension_amount = None


class _Plan:
    def __init__(self, amount, display_name="plan"):
        self.amount = amount
        self.display_name = display_name
        self.currency = "HKD"


def _setup(monkeypatch, *, row, siblings=None, paid_through=_PAID_THROUGH):
    """Wire the store around one entity holding ``siblings`` (default: the bundle)."""
    from billing.services import checkout, store
    from billing.services import clock as clock_mod

    monkeypatch.setattr(clock_mod, "now", lambda: _NOW)

    rows = siblings if siblings is not None else [
        _Row("PETTY_CASH"), _Row("PAYMENT_REQUEST"),
    ]
    calls = {"writes": [], "access": [], "audit": []}

    monkeypatch.setattr(store, "module_row", lambda eid, code: row)
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: rows)
    monkeypatch.setattr(store, "billing_cycle_for_user", lambda uid: (_ANCHOR, "HKD"))
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        lambda codes: (
            _Plan(40000, "Super Minty") if len(set(codes)) > 1
            else _Plan(28000, sorted(codes)[0].title().replace("_", " "))
        ),
    )
    monkeypatch.setattr(
        store, "upsert_module_row",
        lambda e, code, payer, **f: calls["writes"].append((e, code, payer, f)),
    )
    monkeypatch.setattr(
        store, "record_action", lambda **kw: calls["audit"].append(kw)
    )
    monkeypatch.setattr(
        checkout, "_set_module_access",
        lambda eid, code, enabled: calls["access"].append((eid, code, enabled)),
    )
    # money.format_minor reads currency_info for decimal places; pin it so the
    # formatted strings below don't depend on a seeded table.
    from billing.services import money
    monkeypatch.setattr(money, "decimal_places", lambda code: 2)
    return checkout, calls


# --- the property that matters -------------------------------------------------


def test_preview_matches_what_cancelling_records(monkeypatch):
    """Preview, then cancel: the row must carry the previewed date and amount.

    This is the whole point of sharing ``_paid_cancel_terms``. If these ever diverge,
    the dialog is quoting a number the customer will not be billed.
    """
    checkout, calls = _setup(monkeypatch, row=_Row("PETTY_CASH"))

    preview = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")
    checkout.cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    _e, _code, _payer, fields = calls["writes"][0]
    assert fields["app_access_until"] == preview["access_end"]
    assert fields["extension_amount"] == preview["amount"]
    assert preview["amount"] > 0  # otherwise this asserts nothing


def test_preview_writes_nothing(monkeypatch):
    """It runs on a dialog open — it must not schedule the cancellation it describes."""
    checkout, calls = _setup(monkeypatch, row=_Row("PETTY_CASH"))

    checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert calls["writes"] == []
    assert calls["access"] == []
    assert calls["audit"] == []


# --- paid ----------------------------------------------------------------------


def test_a_lone_leaver_is_quoted_its_own_price(monkeypatch):
    """Petty Cash is the only module leaving, so the extra days are charged at its own
    280 — the 120 margin is what it was worth to a subscription that KEPT Payment
    Request, and that is not what these days are.

    Access ends at max(paid_through, now + grace). With paid_through 19 days out and a
    30-day window, the window wins and the days BEYOND paid_through are what's owed.
    """
    checkout, _ = _setup(monkeypatch, row=_Row("PETTY_CASH"))

    p = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert p["kind"] == "paid"
    assert p["currency"] == "HKD"
    assert p["remaining"] == ["PAYMENT_REQUEST"]
    # Survivor's price, so the dialog can say what billing continues at.
    assert p["remaining_amount"] == "280.00"
    # 28000 x 11/31 — off its own price, not the 12000 margin (which would be 4258).
    assert p["amount"] == 9935
    assert p["access_end"] > _PAID_THROUGH
    assert p["charged_now"] is False


def test_cancelling_the_last_module_reports_no_survivor(monkeypatch):
    """Nothing left to bill: the dialog says billing for the company ends."""
    checkout, _ = _setup(
        monkeypatch, row=_Row("PETTY_CASH"), siblings=[_Row("PETTY_CASH")]
    )

    p = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert p["kind"] == "paid"
    assert p["remaining"] == []
    assert p["remaining_amount"] is None
    # With no survivor the whole line price is marginal, so there IS an extension.
    assert p["amount"] > 0


def test_nothing_owed_when_access_is_already_paid_through(monkeypatch):
    """A paid_through beyond the cancellation window adds no days, so no charge.

    The dialog then says "nothing further to pay" rather than showing a 0.00 amount.
    """
    far = _NOW + timedelta(days=120)
    checkout, _ = _setup(monkeypatch, row=_Row("PETTY_CASH"), paid_through=far)

    p = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PETTY_CASH")

    assert p["amount"] == 0
    assert p["amount_formatted"] is None
    assert p["access_end"] == far


# --- trials --------------------------------------------------------------------


def test_trial_preview_keeps_the_free_days_and_owes_nothing(monkeypatch):
    """Cancelling a trial means "don't convert me", not "end it now"."""
    trial_end = _NOW + timedelta(days=9)
    checkout, _ = _setup(
        monkeypatch, row=_Row("PAYMENT_REQUEST", phase="trial", trial_end=trial_end)
    )

    p = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    assert p["kind"] == "trial"
    assert p["access_end"] == trial_end
    assert p["amount"] == 0
    assert p["amount_formatted"] is None


def test_expired_trial_preview_has_nothing_left_to_keep(monkeypatch):
    checkout, _ = _setup(
        monkeypatch,
        row=_Row("PAYMENT_REQUEST", phase="trial", trial_end=_NOW - timedelta(days=1)),
    )

    p = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    assert p["kind"] == "trial_expired"
    assert p["access_end"] is None
    assert p["amount"] == 0


# --- guards --------------------------------------------------------------------


def test_preview_of_an_unsubscribed_module_reports_rather_than_raises(monkeypatch):
    """The dialog needs something to show; a 500 would just look broken."""
    checkout, _ = _setup(monkeypatch, row=None)

    p = checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "PAYMENT_REQUEST")

    assert p["kind"] == "none"
    assert p["error"]


def test_preview_requires_a_code(monkeypatch):
    checkout, _ = _setup(monkeypatch, row=_Row("PETTY_CASH"))

    with pytest.raises(checkout.CheckoutError):
        checkout.preview_cancel_module(_FakeEntity(), _FakeUser(), "")


def test_previewing_a_PAIR_quotes_the_BUNDLE_not_two_solos(monkeypatch):
    """Both halves of a bundle dropped in one click.

    Previewed independently each one looks like a lone leaver at its 280 list price
    (9935 apiece, 19870 quoted) while the cancellations, run in sequence, price them as
    the bundle — so the dialog named 560/month where the invoice collects 400. Told what
    else is going, the preview prices the set under the plan that covers them: Payment
    Request takes the 280 and Petty Cash the 120 step, 14193 in total.

    The shares are UNEVEN, which is why ``leaving_total`` exists at all — this module's
    own 4258 is not a figure the customer can reconcile against anything.
    """
    checkout, _ = _setup(monkeypatch, row=_Row("PETTY_CASH"))

    p = checkout.preview_cancel_module(
        _FakeEntity(), _FakeUser(), "PETTY_CASH", ["PAYMENT_REQUEST"]
    )

    assert p["amount"] == 4258, "the 12000 step x 11/31 — PETTY_CASH sorts second"
    assert p["leaving_count"] == 2
    assert p["leaving_label"] == "Super Minty"
    assert p["leaving_total"] == 14193, "both halves, which is what the invoice holds"
    assert p["leaving_total_formatted"] == "141.93"


def test_the_grouped_total_is_the_same_whichever_card_the_dialog_opened_from(monkeypatch):
    """The pair is worth 400 between them however the customer got to the dialog.

    ``amount`` is this row's share and differs by which module was clicked — 9935 for the
    one that sorts first, 4258 for the other. ``leaving_total`` is the decision, and must
    not. Cancelling from the Payment Request card and from the Petty Cash card are the
    same act, and a dialog whose headline figure depended on that would be quoting the
    allocation rather than the price.
    """
    checkout, _ = _setup(monkeypatch, row=_Row("PETTY_CASH"))
    from_petty = checkout.preview_cancel_module(
        _FakeEntity(), _FakeUser(), "PETTY_CASH", ["PAYMENT_REQUEST"]
    )

    checkout, _ = _setup(monkeypatch, row=_Row("PAYMENT_REQUEST"))
    from_bill = checkout.preview_cancel_module(
        _FakeEntity(), _FakeUser(), "PAYMENT_REQUEST", ["PETTY_CASH"]
    )

    assert from_petty["amount"] == 4258
    assert from_bill["amount"] == 9935, "BILL sorts first, so it carries the 280"
    assert from_petty["leaving_total"] == from_bill["leaving_total"] == 14193
