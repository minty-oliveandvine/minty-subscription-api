"""The panel's "Next invoice" states what the NEXT RUN will charge, not today's state.

Three sets are in play and only two are obvious:

  Total          what the enabled modules cost per month, trials included, because the
                 trial panel has to preview the price it converts to.
  Next invoice   what ``renewals.build_renewal`` will actually bill on ``paid_through``.
  the difference a trial is in the first and not the second — unless it converts BEFORE
                 the invoice date, in which case it is active by the time the run fires
                 and gets billed after all.

That last clause is the whole point of this file. ``billable_codes_by_entity`` reads
phases when the renewal runs, not when the page was rendered, so a trial ending 20 Aug is
billed by the 28 Aug invoice. Reading only what is active TODAY quoted one module's price
against an invoice that will charge the bundle — 280 shown, 400 taken.

``next_invoice`` is the underlying fact; ``upcoming_charges`` is what the panel renders —
that same renewal plus each trial conversion, in date order, because they are separate
charges on separate dates. The last tests here pin the ordering and that nothing appears
in it twice.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

UTC = UTC

_PAID_THROUGH = datetime(2026, 8, 28, 13, tzinfo=UTC)
_BEFORE = _PAID_THROUGH - timedelta(days=8)    # trial converts inside this period
_AFTER = _PAID_THROUGH + timedelta(days=14)    # trial converts in the NEXT one

SUMMARY = {
    "currency": "HKD",
    "currency_code": "HKD",
    "bundle_amount": Decimal("400"),
    "bundle_codes": ["PAYMENT_REQUEST", "PETTY_CASH"],
}


def _card(code, name, *, status=None, end=None, needs_card=False,
          pending_cancel=False, ext="0"):
    return {
        "code": code,
        "name": name,
        "amount": Decimal("280"),
        "currency_code": "HKD",
        "subscription_status": status,
        "pending_cancel": pending_cancel,
        "needs_card": needs_card,
        "period_end": end,
        "period_end_long": end.strftime("%d %b %Y") if end else None,
        "period_end_short": end.strftime("%d %b") if end else None,
        "access_end_long": None,
        "extension_amount": Decimal(ext),
        "conversion_charge": Decimal(0),
    }


def _panel(app, cards, anchor="28 Jul 2026"):
    from billing.services import entity_modules as modules

    with app.app_context():
        return modules.build_subscription_panel(cards, SUMMARY, anchor)


def test_a_trial_converting_before_the_invoice_date_is_on_that_invoice(app):
    """T6, and the bug this file exists for.

    Payment Request is paid; Petty Cash's trial ends 20 Aug, eight days before the 28 Aug
    invoice. By then both are active, so the renewal bills the bundle.
    """
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert panel["next_invoice"]["date"] == "28 Aug 2026"
    assert panel["next_invoice"]["amount"] == "HKD 400"
    # Total is unchanged: the monthly cost of both modules was always 400.
    assert panel["total"] == "HKD 400"


def test_a_trial_converting_after_the_invoice_date_is_not(app):
    """The mirror case, and the reason this is not just "add every trial".

    A trial ending 11 Sep is still a trial on 28 Aug, so that invoice bills the paid
    module alone — and Total and Next invoice legitimately differ.
    """
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_AFTER),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert panel["next_invoice"]["amount"] == "HKD 280"
    assert panel["total"] == "HKD 400", "still 400 a month once the trial converts"


def test_a_trial_that_will_not_convert_is_never_on_the_invoice(app):
    """It expires instead of converting, so it is not active on the day and not billed —
    even though its date falls before the invoice."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE,
              needs_card=True),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert panel["next_invoice"]["amount"] == "HKD 280"


def test_a_pending_cancel_is_off_the_invoice_but_its_extension_is_on_it(app):
    """A module winding down bills no further, yet the cancel-extension it recorded
    rides the same invoice — so the figure must drop the one and keep the other."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH,
              pending_cancel=True, ext="120"),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert panel["next_invoice"]["amount"] == "HKD 400", "280 recurring + 120 extension"
    assert panel["next_invoice"]["includes_extension"] is True


def test_a_cancellation_is_its_own_row_not_part_of_the_renewal(app):
    """Same invoice, two different charges. "Renewal HKD 400" that is really 280 of
    subscription plus 120 of cancellation is a figure the customer cannot check against
    anything — so the list names each, and only ``next_invoice`` carries the total."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH,
              pending_cancel=True, ext="120"),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert [(u["label"], u["amount"]) for u in panel["upcoming_charges"]] == [
        ("Renewal", "HKD 280"),
        ("Petty Cash cancellation", "HKD 120"),
    ]
    # The invoice itself still totals both — that fact did not change.
    assert panel["next_invoice"]["amount"] == "HKD 400"


def test_a_cancellation_survives_having_no_renewal_to_ride(app):
    """92fb66f4: a trial beside a module winding down. Nothing of this entity renews, so
    there was no renewal row to fold the extension into and the charge vanished from a
    list titled "upcoming charges" — while remaining perfectly real."""
    winding = _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH,
                    pending_cancel=True, ext="62.89")
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        winding,
    ])

    assert panel["next_invoice"] is None, "nothing of this entity is on the next run"
    assert [(u["label"], u["date"]) for u in panel["upcoming_charges"]] == [
        ("Petty Cash converts", "20 Aug 2026"),
        ("Payment Request cancellation", "28 Aug 2026"),
    ]


def test_no_paid_module_means_no_next_invoice_date_to_quote(app):
    """Only trials: the payer's cycle may exist, but nothing of THIS entity is on the
    next run, and the trial rows carry the conversion dates instead."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        _card("PAYMENT_REQUEST", "Payment Request", status="trialing", end=_AFTER),
    ])

    assert panel["next_invoice"] is None


def test_past_due_is_still_billed_and_marked_overdue(app):
    """past_due carries pending_cancel=True — that is the shape get_module_cards builds.

    A failed renewal and a scheduled cancellation share one "winding down" flag, because
    both offer Renew. Billing is a different question, and ``access.is_billing_forward``
    is explicit that past_due counts: the subscription has not ended and the money is
    still owed. Reading pending_cancel alone dropped the module from the panel entirely,
    which printed "No modules enabled — nothing will be billed" to a customer in arrears.

    Written with pending_cancel=True deliberately. The first version of this test passed
    False, a combination the card builder never produces, and so asserted nothing about
    the real page.
    """
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash"),
        _card("PAYMENT_REQUEST", "Payment Request", status="past_due", end=_PAID_THROUGH,
              pending_cancel=True),
    ])

    assert panel["is_empty"] is False, "a module in arrears is still a subscription"
    assert panel["total"] == "HKD 280"
    assert panel["next_invoice"]["overdue"] is True
    assert panel["next_invoice"]["amount"] == "HKD 280"
    assert panel["upcoming_charges"][0]["overdue"] is True


def test_past_due_is_not_listed_as_winding_down(app):
    """"Not billed again" is false for arrears, and it rendered directly above the
    overdue charge for the very same module.

    ``access_end_long`` MUST be set here — it is the grace deadline, and the real card
    carries it for past_due. Without it this test passes on the winding_down list's other
    condition and asserts nothing, which is exactly how it slipped through first time.
    """
    past_due = _card("PAYMENT_REQUEST", "Payment Request", status="past_due", end=_PAID_THROUGH,
                     pending_cancel=True)
    past_due["access_end_long"] = "7 Aug 2026"
    panel = _panel(app, [_card("PETTY_CASH", "Petty Cash"), past_due])

    assert panel["winding_down"] == []


def test_a_scheduled_cancellation_still_winds_down(app):
    """The other half of the same flag: an actual cancellation bills no further, keeps
    access to its date, and must still say so."""
    card = _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH,
                 pending_cancel=True)
    card["access_end_long"] = "12 Sep 2026"
    panel = _panel(app, [_card("PETTY_CASH", "Petty Cash", status="active",
                               end=_PAID_THROUGH), card])

    assert panel["winding_down"] == [
        {"label": "Payment Request", "date": "12 Sep 2026"}
    ]
    # ...and it is off the invoice, unlike past_due.
    assert panel["next_invoice"]["amount"] == "HKD 280"


# --- the rendered list ---------------------------------------------------------


def test_upcoming_charges_are_in_date_order(app):
    """The conversion comes first because it happens first — 20 Aug before 28 Aug.

    Sorted on the raw datetime: the formatted strings sort alphabetically, which would
    put "11 Sep" ahead of "28 Aug".
    """
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert [(u["label"], u["date"]) for u in panel["upcoming_charges"]] == [
        ("Petty Cash converts", "20 Aug 2026"),
        ("Renewal", "28 Aug 2026"),
    ]


def test_a_conversion_and_the_renewal_are_both_listed(app):
    """Two charges, two dates — the renewal covering a converted trial does not replace
    the conversion's own proration, which was collected eight days earlier."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert len(panel["upcoming_charges"]) == 2
    # The renewal bills the bundle; the conversion bills only its catch-up slice.
    assert panel["upcoming_charges"][-1]["amount"] == "HKD 400"


def test_a_trial_that_will_not_convert_is_not_an_upcoming_charge(app):
    """Nothing is charged for a trial that expires, so it must not appear in a list of
    charges. The footer is where that case is stated."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE,
              needs_card=True),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert [u["label"] for u in panel["upcoming_charges"]] == ["Renewal"]
    # ...but it IS still carried as a fact, for the footer to compose from.
    assert [t["label"] for t in panel["trial_conversions"]] == ["Petty Cash"]


def test_trials_only_lists_the_conversions_and_no_renewal(app):
    """No paid module means nothing of this entity is on the payer's next run, so the
    conversions are the whole list."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        _card("PAYMENT_REQUEST", "Payment Request", status="trialing", end=_AFTER),
    ])

    assert [u["label"] for u in panel["upcoming_charges"]] == [
        "Petty Cash converts",
        "Payment Request converts",
    ]
    assert panel["next_invoice"] is None


def test_a_whole_bundle_converting_on_one_day_is_one_row(app):
    """Two modules converting together are billed as the BUNDLE, so they are one row.

    The per-module forecasts are computed sequentially: the first conversion anchors the
    cycle and carries a full period (280), the second is the net of the change into the
    bundle (120). Only their sum is a figure the customer will ever see — the invoice says
    400 — so two rows asked them to reconcile a 280 and a 120 that appear nowhere.
    """
    a = _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE)
    b = _card("PAYMENT_REQUEST", "Payment Request", status="trialing", end=_BEFORE)
    a["conversion_charge"] = Decimal("280")
    b["conversion_charge"] = Decimal("120")

    panel = _panel(app, [a, b])

    assert [(u["label"], u["date"], u["amount"]) for u in panel["upcoming_charges"]] == [
        ("Super Minty converts", "20 Aug 2026", "HKD 400"),
    ]


def test_conversions_on_different_days_stay_separate(app):
    """The grouping is per DAY. Trials a fortnight apart convert a fortnight apart, and
    collapsing them would name money on a date it is not taken."""
    a = _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE)
    b = _card("PAYMENT_REQUEST", "Payment Request", status="trialing", end=_AFTER)
    a["conversion_charge"] = Decimal("280")
    b["conversion_charge"] = Decimal("120")

    panel = _panel(app, [a, b])

    assert [(u["label"], u["amount"]) for u in panel["upcoming_charges"]] == [
        ("Petty Cash converts", "HKD 280"),
        ("Payment Request converts", "HKD 120"),
    ]


def test_a_same_day_conversion_that_is_not_the_bundle_keeps_its_module_name(app):
    """One module converting beside a paid one is not a bundle purchase — it is that
    module, and the row says so. Only a day whose conversions are EXACTLY the bundle's
    codes is renamed."""
    trial = _card("PETTY_CASH", "Petty Cash", status="trialing", end=_PAID_THROUGH)
    trial["conversion_charge"] = Decimal("280")

    panel = _panel(app, [
        trial,
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])

    assert ("Petty Cash converts", "HKD 280") in [
        (u["label"], u["amount"]) for u in panel["upcoming_charges"]
    ]


# --- what the panel says in words ----------------------------------------------


def test_a_paying_panel_has_no_footer(app):
    """It read "Billed HKD 400/mo · next payment 28 Aug 2026" and said nothing new: the
    rate is the Total row directly above it, and the date is the card at the top of the
    page. The template drops the paragraph entirely rather than printing an empty one."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH),
    ])

    assert panel["footer"] == ""


def test_the_states_that_do_have_something_to_say_keep_their_footer(app):
    """The trial footer names when the first charge lands and the empty one states that
    nothing is billed — neither fact appears anywhere else on the panel."""
    trial = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
    ], anchor=None)
    empty = _panel(app, [_card("PETTY_CASH", "Petty Cash")])

    assert "free trial" in trial["footer"]
    assert empty["footer"] == "No modules enabled — nothing will be billed."


def test_the_bundle_note_never_quotes_a_per_module_price(app):
    """It used to append "vs HKD 280 each" once billing had started — a per-module price
    for a plan nobody is billed per module on. The saving beside it already carries the
    comparison, and the note now reads the same trialing or paid."""
    paid = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH),
        _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH),
    ])
    trialing = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_BEFORE),
        _card("PAYMENT_REQUEST", "Payment Request", status="trialing", end=_BEFORE),
    ], anchor=None)

    assert paid["note"] == "Super Minty price — save HKD 160"
    assert paid["note"] == trialing["note"]
    assert "each" not in paid["note"]


# --- which caption the panel's one button carries -------------------------------
# Both captions open the same decision modal, so this is only about which question the
# entity is being asked. The rule is NOT "is there a trial" and NOT "is there an anchor":
# the anchor lives on the payer's account, so a payer already billed for another company
# gives this one an anchor on day one, and a trial with a card and consent needs nothing
# from the customer — it converts by itself.


def test_subscribe_is_offered_only_when_billing_is_not_set_up(app):
    """needs_card is "this trial will NOT convert as things stand" — no card, or this
    company was never authorised for the saved one. That is the whole condition."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_AFTER, needs_card=True),
    ])

    assert panel["primary_action"] == "subscribe_stripe"


def test_a_trial_that_will_convert_is_managed_not_subscribed(app):
    """The regression this rule replaced: every trial said "Subscribe", including ones
    with a card and consent already in place, where there is nothing to subscribe to."""
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="trialing", end=_AFTER),
    ])

    assert panel["primary_action"] == "manage"


def test_a_paid_entity_is_managed(app):
    panel = _panel(app, [
        _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH),
    ])

    assert panel["primary_action"] == "manage"


def test_an_empty_panel_still_offers_a_way_in(app):
    """Nothing enabled, nothing cancelled — the modal is how a module is taken up again,
    whether that is a free trial it has never used or a purchase of one it has."""
    fresh = _card("PETTY_CASH", "Petty Cash")
    fresh["trial_eligible"] = True

    assert _panel(app, [fresh])["primary_action"] == "subscribe_stripe"

    spent = _card("PETTY_CASH", "Petty Cash")
    spent["trial_eligible"] = False

    assert _panel(app, [spent])["primary_action"] == "subscribe_stripe"


def test_a_cancellation_reaches_manage_not_subscribe(app):
    """Re-ticking a cancelled module is the only undo, so it takes precedence over the
    "take something up" caption."""
    winding = _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH,
                    pending_cancel=True)
    winding["access_end_long"] = "12 Sep 2026"

    panel = _panel(app, [winding, _card("PETTY_CASH", "Petty Cash")])

    assert panel["is_empty"] is True
    assert panel["primary_action"] == "manage"


# --- how a cancellation is stated ----------------------------------------------


def test_cancelling_the_bundle_is_one_notice_naming_the_plan(app):
    """Cancelling a two-module bundle is ONE decision. Printing the same date under two
    module names describes it as two, and neither line is the thing that happened."""
    a = _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH,
              pending_cancel=True)
    b = _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH,
              pending_cancel=True)
    a["access_end_long"] = b["access_end_long"] = "19 Oct 2026"

    panel = _panel(app, [a, b])

    assert panel["winding_notices"] == [
        {"label": "Super Minty", "date": "19 Oct 2026", "kind": "paid"}
    ]


def test_cancelling_one_module_names_that_module(app):
    """The other half: one cancellation, and the plan is not what ended."""
    a = _card("PETTY_CASH", "Petty Cash", status="active", end=_PAID_THROUGH,
              pending_cancel=True)
    a["access_end_long"] = "19 Oct 2026"
    b = _card("PAYMENT_REQUEST", "Payment Request", status="active", end=_PAID_THROUGH)

    panel = _panel(app, [a, b])

    assert panel["winding_notices"] == [
        {"label": "Petty Cash", "date": "19 Oct 2026", "kind": "paid"}
    ]


def test_past_due_is_never_called_cancelled(app):
    """It carries the same winding-down flag but nothing was cancelled — dunning is still
    retrying the charge. Saying "cancelled" to someone we are about to bill again is the
    wrong error to make."""
    past_due = _card("PAYMENT_REQUEST", "Payment Request", status="past_due", end=_PAID_THROUGH,
                     pending_cancel=True)
    past_due["access_end_long"] = "7 Aug 2026"

    panel = _panel(app, [_card("PETTY_CASH", "Petty Cash", status="active",
                               end=_PAID_THROUGH), past_due])

    assert panel["winding_notices"] == []


def test_a_cancelled_trial_is_ending_not_cancelled(app):
    """Nothing was bought and nothing ends early — the free days run to the date they
    always would. Calling that "cancelled" describes a purchase that never happened."""
    trial = _card("PAYMENT_REQUEST", "Payment Request", status="trialing", end=_AFTER,
                  pending_cancel=True)
    trial["trial_cancelled"] = True
    trial["access_end_long"] = "11 Sep 2026"

    panel = _panel(app, [_card("PETTY_CASH", "Petty Cash", status="active",
                               end=_PAID_THROUGH), trial])

    assert panel["winding_notices"] == [
        {"label": "Payment Request", "date": "11 Sep 2026", "kind": "trial"}
    ]
