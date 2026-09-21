"""Unit tests for Minty's own billing arithmetic.

The cases marked ORACLE are not invented: they are the exact figures Stripe produced on
a test clock, captured while Stripe was still the billing engine. They are the reason to
build this now rather than after the cutover — once billing moves in-house there is
nothing left to check the arithmetic against, and this arithmetic decides what customers
are charged.

Provenance of the oracle figures (test-clock runs, HKD minor units):

    280.00 first purchase at the anchor                      in_1TvUfN...
     28.00 mid-cycle upgrade 280 -> 400, 7 of 30 days        in_1TvUfZ...
           = -65.33 unused Petty Cash + 93.33 remaining bundle
    373.33 new line joining mid-cycle, 28 of 30 days         in_1TvUgL...
     89.03 marginal 120 over 23 of 31 days                   in_1TvD2J...
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from billing.services.billing import (
    Period,
    add_months,
    billed_days,
    cancel_access_end,
    cancellation_invoice,
    change_invoice,
    change_line,
    change_memo,
    extension_charge,
    join_invoice,
    join_memo,
    line_description,
    marginal_amount,
    period_containing,
    prorate,
    renewal_invoice,
    renewal_memo,
    start_line,
)


def dt(y, m, d, hour=13):
    return datetime(y, m, d, hour, tzinfo=UTC)


# --- periods ------------------------------------------------------------------


def test_period_containing_is_derived_from_the_anchor():
    anchor = dt(2026, 9, 8)
    period = period_containing(anchor, dt(2026, 11, 10))
    assert period.start == dt(2026, 11, 8)
    assert period.end == dt(2026, 12, 8)


def test_a_moment_exactly_on_the_anchor_starts_the_new_period():
    """Half-open periods: the instant one ends is the instant the next begins, so a
    charge at the anchor belongs to the new period rather than a zero-length tail."""
    anchor = dt(2026, 9, 8)
    assert period_containing(anchor, dt(2026, 10, 8)).start == dt(2026, 10, 8)
    assert period_containing(anchor, dt(2026, 10, 8) - timedelta(seconds=1)).start == dt(2026, 9, 8)


def test_month_end_anchors_clamp_without_sticking():
    """A 31st anchor has no counterpart in February, but clamping must not be permanent —
    stepping month-by-month would strand the anchor on the 28th forever."""
    anchor = dt(2027, 1, 31)
    assert add_months(anchor, 1) == dt(2027, 2, 28)
    assert add_months(anchor, 2) == dt(2027, 3, 31)  # not 28 Mar
    assert period_containing(anchor, dt(2027, 3, 15)).start == dt(2027, 2, 28)


def test_periods_tile_without_gaps_or_overlap():
    anchor = dt(2026, 1, 31)
    at = anchor
    for _ in range(14):
        period = period_containing(anchor, at)
        assert period.contains(at)
        following = period_containing(anchor, period.end)
        assert following.start == period.end  # no gap, no overlap
        at = period.end + timedelta(days=1)


def test_naive_datetimes_are_rejected():
    """A silent UTC assumption would shift a period boundary by the host's offset, which
    changes what somebody is charged."""
    with pytest.raises(ValueError):
        period_containing(datetime(2026, 9, 8), dt(2026, 10, 1))


# --- proration ----------------------------------------------------------------


def test_prorate_is_by_elapsed_time_not_whole_days():
    """Periods run 28-31 days, so the fraction has to come from the real span."""
    period = Period(dt(2026, 9, 8), dt(2026, 10, 8))  # 30 days
    assert prorate(40000, period, dt(2026, 10, 1)) == 9333  # 7/30
    assert prorate(40000, period, period.start) == 40000
    assert prorate(40000, period, period.end) == 0


def test_prorate_handles_a_part_day():
    """36 hours left of a 30-day period — 1.5/30 of the price, not 1/30 or 2/30. Whole-
    day arithmetic would round this to one of those and be wrong by up to a day's fee."""
    period = Period(dt(2026, 9, 8), dt(2026, 10, 8))
    thirty_six_hours_left = dt(2026, 10, 7, hour=1)
    assert prorate(40000, period, thirty_six_hours_left) == 2000  # 40000 * 1.5/30


# --- ORACLE: figures Stripe actually produced ---------------------------------


def test_oracle_mid_cycle_upgrade_280_to_400():
    """in_1TvUfZ... -65.33 credit + 93.33 charge = 28.00 net, 7 of 30 days."""
    period = Period(dt(2026, 9, 8), dt(2026, 10, 8))
    adjustment = change_line(28000, 40000, period, dt(2026, 10, 1))

    assert adjustment.credit == -6533
    assert adjustment.charge == 9333
    assert adjustment.net == 2800


def test_oracle_new_line_joining_mid_cycle():
    """in_1TvUgL... 373.33 for 28 of 30 days, with nothing to credit back."""
    period = Period(dt(2026, 11, 8), dt(2026, 12, 8))
    adjustment = start_line(40000, period, dt(2026, 11, 10))

    assert adjustment.credit == 0
    assert adjustment.charge == 37333


def test_oracle_marginal_bundle_over_a_31_day_period():
    """in_1TvD2J... 89.03 — the marginal 120 across 23 of 31 days. A 30-day assumption
    would give 92.00 here, which is why the fraction comes from the actual period."""
    period = Period(dt(2026, 8, 20), dt(2026, 9, 20))
    assert prorate(12000, period, dt(2026, 8, 28)) == 8903


def test_oracle_month_end_anchor_springs_back_after_clamping():
    """Confirmed against Stripe on a 31 Jan anchor (clock_1TvW7h..., 6/6 lines agreed):

        31 Jan -> 28 Feb -> 31 Mar -> 30 Apr -> 31 May

    Stripe returns to the 31st rather than staying on the 28th. Had it stuck, every
    period from March on would have been three days short, permanently -- and this is
    the case no customer in the main harness exercises.
    """
    anchor = dt(2027, 1, 31, hour=12)

    assert period_containing(anchor, dt(2027, 2, 14, hour=12)) == Period(
        dt(2027, 1, 31, hour=12), dt(2027, 2, 28, hour=12)
    )
    assert period_containing(anchor, dt(2027, 3, 1, hour=12)) == Period(
        dt(2027, 2, 28, hour=12), dt(2027, 3, 31, hour=12)
    )
    assert period_containing(anchor, dt(2027, 4, 1, hour=12)) == Period(
        dt(2027, 3, 31, hour=12), dt(2027, 4, 30, hour=12)
    )
    assert period_containing(anchor, dt(2027, 5, 1, hour=12)) == Period(
        dt(2027, 4, 30, hour=12), dt(2027, 5, 31, hour=12)
    )


def test_oracle_a_leap_day_anchor_keeps_day_29_rather_than_the_month_end():
    """The rule is DAY-of-month clamping, not last-day-of-month tracking.

    The 31 Jan run could not tell those apart — 31 Jan is both "day 31" and "the last day
    of January", and every period it produced is identical under either. A 29 Feb anchor
    separates them, and Stripe answered day-29 (clock_1TvWhN..., 14/14 periods agreed
    across 14 consecutive months):

        29 Feb 2028 -> 29 Mar 2028      last-day-of-month would give 31 Mar
        29 Jan 2029 -> 28 Feb 2029      clamped: Feb 2029 is short
        28 Feb 2029 -> 29 Mar 2029      sprang back to 29, not 31

    Had it been last-day-of-month, every month-end anchor would drift by up to two days
    from March onward.
    """
    anchor = dt(2028, 2, 29, hour=12)

    assert period_containing(anchor, dt(2028, 3, 1, hour=12)) == Period(
        dt(2028, 2, 29, hour=12), dt(2028, 3, 29, hour=12)
    )
    # A year on, February itself is short: clamp, then spring back.
    assert period_containing(anchor, dt(2029, 2, 1, hour=12)) == Period(
        dt(2029, 1, 29, hour=12), dt(2029, 2, 28, hour=12)
    )
    assert period_containing(anchor, dt(2029, 3, 1, hour=12)) == Period(
        dt(2029, 2, 28, hour=12), dt(2029, 3, 29, hour=12)
    )


def test_oracle_proration_inside_a_short_month():
    """Same run: swapping 280 -> 400 on 14 Feb, half way through a 28-DAY period, gave
    -140.00 / +200.00. Exactly half, because the denominator is the real period length.
    A hardcoded 30 days would have produced -130.67 / +186.67."""
    period = Period(dt(2027, 1, 31, hour=12), dt(2027, 2, 28, hour=12))
    adjustment = change_line(28000, 40000, period, dt(2027, 2, 14, hour=12))

    assert adjustment.credit == -14000
    assert adjustment.charge == 20000
    assert adjustment.net == 6000


def test_oracle_a_full_period_is_never_prorated():
    """in_1TvUfN... 280.00 — bought at the anchor, so the whole period is charged."""
    period = Period(dt(2026, 9, 8), dt(2026, 10, 8))
    assert start_line(28000, period, period.start).charge == 28000


def test_credit_and_charge_use_the_same_fraction():
    """The net of a swap must equal the price difference scaled to the time left. If the
    two sides ever used different fractions the customer would be silently over- or
    under-charged by the drift."""
    period = Period(dt(2026, 11, 8), dt(2026, 12, 8))
    at = dt(2026, 11, 22)
    adjustment = change_line(28000, 40000, period, at)

    assert adjustment.net == prorate(40000 - 28000, period, at)


def test_a_downgrade_reports_a_credit_rather_than_deciding_policy():
    """Whether to issue it is the caller's call — today's cancel path suppresses it
    because access continues to app_access_until."""
    period = Period(dt(2026, 11, 8), dt(2026, 12, 8))
    adjustment = change_line(40000, 28000, period, dt(2026, 11, 22))

    assert adjustment.credit < 0 and adjustment.charge > 0
    assert adjustment.net < 0


# --- ORACLE: the cancel path (clock_1TvWEO..., all checks passed) --------------
#
# Anchor 8 Jan, bundle 400, PETTY_CASH cancelled on 20 Jan. Stripe's anchor invoice came
# to 322.58 = 280.00 for the swapped-down line + 42.58 extension, with NO proration line.


def test_oracle_cancel_extension_charges_only_the_days_past_the_anchor():
    """42.58 = marginal 120 x 11/31. Everything up to the anchor was already paid."""
    period = Period(dt(2027, 1, 8, hour=12), dt(2027, 2, 8, hour=12))  # 31 days
    access_end = cancel_access_end(period.end, dt(2027, 1, 20, hour=12), extension_days=30)

    assert access_end == dt(2027, 2, 19, hour=12)  # 20 Jan + 30d beats 8 Feb
    assert extension_charge(12000, period, access_end) == 4258


def test_oracle_the_extension_uses_the_marginal_price_not_the_list_price():
    """Petty Cash lists at 280, but dropping it from a 400 bundle leaves Bill at 280 —
    so it was only ever worth 120. Billing 280 would charge more than it cost."""
    assert marginal_amount(40000, 28000) == 12000
    # Nothing survives: the whole line was the module.
    assert marginal_amount(28000, None) == 28000


def test_cancel_access_is_only_ever_extended_never_shortened():
    """A customer who has already paid to the end of a long period keeps it, even though
    now + 30 days would fall earlier."""
    period_end = dt(2027, 6, 30, hour=12)
    assert cancel_access_end(period_end, dt(2027, 1, 20, hour=12), extension_days=30) == period_end


def test_no_extension_is_charged_when_access_ends_at_the_anchor():
    """No overhang means nothing to bill — the period they're in is already covered."""
    period = Period(dt(2027, 1, 8, hour=12), dt(2027, 2, 8, hour=12))
    assert extension_charge(12000, period, period.end) == 0
    assert extension_charge(12000, period, dt(2027, 1, 30, hour=12)) == 0


def test_a_cancellation_never_credits_the_unused_time():
    """The override that makes the cancel path diverge from Stripe's default. Access runs
    to access_end, so crediting the unused period would pay the customer for days they
    still get. Verified on the oracle run: the anchor invoice carried NO proration line.

    Expressed here as the deliberate absence of a change_line() call — the engine offers
    the credit (see test_a_downgrade_reports_a_credit...), and the cancel path declines it.
    """
    period = Period(dt(2027, 1, 8, hour=12), dt(2027, 2, 8, hour=12))
    access_end = cancel_access_end(period.end, dt(2027, 1, 20, hour=12), extension_days=30)

    billed = extension_charge(marginal_amount(40000, 28000), period, access_end)

    # 280.00 next period + 42.58 overhang; nothing returned for the abandoned bundle.
    assert billed == 4258
    assert 28000 + billed == 32258


# --- invoice construction ------------------------------------------------------


def test_a_renewal_names_every_entity_on_its_own_line():
    """The whole point of billing in-house. Under subscription billing these were two
    identical "1 x Super Minty 400.00" lines with nothing to tell them apart."""
    period = Period(dt(2026, 12, 8), dt(2027, 1, 8))
    invoice = renewal_invoice(
        [
            ("e_a", "Alpha Co", "Super Minty", 40000),
            ("e_b", "Beta Co", "Super Minty", 40000),
        ],
        period,
        "hkd",
    )

    assert [line.description for line in invoice.lines] == [
        "Alpha Co - Super Minty",
        "Beta Co - Super Minty",
    ]
    assert invoice.total == 80000
    assert invoice.entity_ids == ("e_a", "e_b")


def test_a_mid_period_join_is_a_single_prorated_charge():
    """Reproduces the 373.33 conversion: nothing to credit, because the entity was not
    billed for this period before."""
    period = Period(dt(2026, 11, 8), dt(2026, 12, 8))
    invoice = join_invoice(
        "e_b", "Beta Co", "Super Minty", 40000,
        period, dt(2026, 11, 10), "hkd",
    )

    assert invoice.total == 37333
    # The line IDENTIFIES; the memo explains the proration (see join_memo).
    assert invoice.lines[0].description == (
        "Beta Co - Super Minty"
    )


def test_a_price_change_shows_both_halves_not_just_the_net():
    """Reproduces the 28.00 upgrade. A customer shown only "28.00" cannot reconcile it
    against the 280.00 they paid three weeks earlier."""
    period = Period(dt(2026, 9, 8), dt(2026, 10, 8))
    invoice = change_invoice(
        "e_a", "Alpha Co", "Petty Cash", "Super Minty",
        28000, 40000, period, dt(2026, 10, 1), "hkd",
    )

    assert [line.amount for line in invoice.lines] == [-6533, 9333]
    assert invoice.total == 2800
    # Both lines name the SAME entity, so the credit keeps a marker — without it they
    # differ only by sign.
    assert invoice.lines[0].description == "Alpha Co - Petty Cash (unused time)"
    assert invoice.lines[1].description == (
        "Alpha Co - Super Minty"
    )


def test_a_cancellation_bills_only_the_extension_and_never_a_credit():
    """Reproduces the 42.58 extension. No credit line: access runs to access_end, so
    returning the unused period would pay for days the customer still gets."""
    period = Period(dt(2027, 1, 8, hour=12), dt(2027, 2, 8, hour=12))
    access_end = cancel_access_end(period.end, dt(2027, 1, 20, hour=12), extension_days=30)

    invoice = cancellation_invoice(
        "e_a", "Alpha Co", "Petty Cash", marginal_amount(40000, 28000),
        period, access_end, "hkd",
    )

    assert invoice.total == 4258
    assert len(invoice.lines) == 1
    assert all(line.amount > 0 for line in invoice.lines)


def test_no_invoice_when_a_cancellation_owes_nothing():
    """Access ending at the anchor means no overhang — and an empty invoice is worse
    than none, so the caller gets None to act on."""
    period = Period(dt(2027, 1, 8, hour=12), dt(2027, 2, 8, hour=12))
    assert cancellation_invoice(
        "e_a", "Alpha Co", "Petty Cash", 12000, period, period.end, "hkd"
    ) is None


def test_every_line_carries_its_own_entity():
    """Attribution lives on the line, not in processor metadata. Stripe stamps a
    SUBSCRIPTION's metadata onto all its lines, which named one entity for every charge
    — the bug that made its invoices unreadable."""
    period = Period(dt(2026, 12, 8), dt(2027, 1, 8))
    invoice = renewal_invoice(
        [("e_a", "Alpha Co", "Super Minty", 40000), ("e_b", "Beta Co", "Super Minty", 40000)],
        period, "hkd",
    )

    assert [line.entity_id for line in invoice.lines] == ["e_a", "e_b"]


# --- line text ----------------------------------------------------------------


def test_line_text_leads_with_the_entity():
    """The whole reason billing moved in-house: Stripe composes subscription line text
    from the PRODUCT name and refuses to let it be rewritten, so two entities on the
    bundle were indistinguishable."""
    assert line_description("Alpha Co", "Super Minty") == (
        "Alpha Co - Super Minty"
    )


def test_a_line_names_the_entity_and_product_and_nothing_else():
    """A line is the field a customer scans to find WHICH of their companies a charge
    belongs to. It used to carry a sentence about proration instead, while the memo said
    almost nothing; the proration story now lives in the memo."""
    at = dt(2026, 11, 10)
    assert line_description("Beta Co", "Super Minty", kind="remaining", at=at) == (
        "Beta Co - Super Minty"
    )
    assert line_description("Beta Co", "Super Minty", kind="full") == "Beta Co - Super Minty"


def test_only_the_credit_line_is_marked_because_only_it_is_ambiguous():
    """An upgrade's two lines name the same entity. Everything else is disambiguated by
    the entity+product pair already."""
    at = dt(2026, 11, 10)
    assert line_description("Beta Co", "Petty Cash", kind="unused", at=at) == (
        "Beta Co - Petty Cash (unused time)"
    )


# --- the memo -------------------------------------------------------------------
#
# The invoice's explanation. It carries the event and the arithmetic, so "why 207.74 and
# not 280.00?" is answered on the document rather than in a support ticket.


def test_the_memo_quotes_days_but_the_MONEY_is_still_seconds():
    """Days are for reading. Prorating by them would change the charge — a period is
    28-31 days, so the fraction has to come from the real span."""
    period = Period(dt(2027, 3, 4), dt(2027, 4, 4))       # 31 days
    at = dt(2027, 3, 12)

    charged, total = billed_days(period, at)
    assert (charged, total) == (23, 31)
    # The money is unchanged and still comes from prorate(), not from 23/31 of anything
    # a human rounded.
    assert prorate(28000, period, at) == 20774


def test_a_join_memo_explains_why_the_first_charge_is_short():
    period = Period(dt(2027, 3, 4), dt(2027, 4, 4))
    memo = join_memo("Entity B", "Payment Request", 20774, period, dt(2027, 3, 12))

    assert "Entity B started Payment Request on 12 Mar 2027" in memo
    assert "23 of 31 days" in memo
    assert "207.74" in memo


def test_a_join_on_the_anchor_says_full_period_not_31_of_31():
    """The first conversion sets the anchor, so it is charged in full. Reporting it as a
    proration would invite the customer to check arithmetic that never happened."""
    period = Period(dt(2027, 2, 4), dt(2027, 3, 4))
    memo = join_memo("Entity A", "Super Minty", 40000, period, dt(2027, 2, 4))

    assert "in full" in memo
    assert "of" not in memo.split("Charged for")[1].split(":")[0]


def test_a_change_memo_carries_both_halves_and_the_net():
    """The net is a property of the INVOICE, not of either line — neither line is 73.03."""
    period = Period(dt(2027, 2, 4), dt(2027, 3, 4))
    memo = change_memo("Entity B", "Payment Request", "Super Minty",
                       -17042, 24345, period, dt(2027, 2, 15))

    assert "changed from Payment Request to Super Minty" in memo
    assert "170.42" in memo and "243.45" in memo
    assert "Net 73.03" in memo


def test_a_renewal_memo_can_only_summarise():
    """One invoice, many entities — so no per-entity arithmetic is possible here."""
    period = Period(dt(2027, 3, 4), dt(2027, 4, 4))

    assert renewal_memo(period, 3) == (
        "Minty subscription, 4 Mar - 4 Apr 2027. 3 entities."
    )
    assert renewal_memo(period, 1) == (
        "Minty subscription, 4 Mar - 4 Apr 2027. 1 entity."
    )


def test_a_renewal_memo_calls_out_a_cancellation_charge():
    """The one line on a renewal a customer will not be expecting."""
    period = Period(dt(2027, 3, 4), dt(2027, 4, 4))

    assert "Includes 1 access extension." in renewal_memo(period, 3, 1)
    assert "Includes 2 access extensions." in renewal_memo(period, 3, 2)
    assert "extension" not in renewal_memo(period, 3, 0)
