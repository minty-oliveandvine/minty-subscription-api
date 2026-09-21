"""Unit tests for in-house module access decisions.

Access is the riskiest thing moving off Stripe. A wrong invoice can be credited; a wrong
access answer locks a paying customer out of something they bought, or hands a
non-paying one a module for free. Both directions are tested here.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from billing.services.access import (
    CANCELLED_GRACE_DAYS,
    PAST_DUE_GRACE_DAYS,
    access_end,
    grants_access,
    is_billing_forward,
    is_paid_module,
    is_subscribed,
)


def dt(y, m, d, hour=12):
    return datetime(y, m, d, hour, tzinfo=UTC)


PERIOD_END = dt(2027, 3, 8)


# --- precedence ----------------------------------------------------------------


def test_app_access_until_outranks_everything():
    """The app's own promise wins over a lapsed period AND a cancelled subscription:
    those days were charged for (or promised), and the processor knows nothing about
    them."""
    promised = dt(2027, 4, 1)

    assert access_end(
        phase="cancelled",
        app_access_until=promised,
        period_end=dt(2027, 1, 1),          # long past
        cancelled_at=dt(2027, 1, 1),
    ) == promised


def test_cancelling_in_the_app_grants_30_days_through_app_access_until():
    """THE case the closed-account grace must not be used for. Cancelling in Minty
    charges a prorated extension and promises 30 days; that promise arrives as
    ``app_access_until``, NOT as a grace window — which is why CANCELLED_GRACE_DAYS can
    stay 0 without shortening anyone's cancellation."""
    cancelled_on = dt(2027, 3, 1)
    promised = cancelled_on + timedelta(days=30)  # checkout.PAID_CANCEL_ACCESS_DAYS

    assert access_end(
        phase="scheduled_cancel", app_access_until=promised, period_end=PERIOD_END
    ) == promised
    assert grants_access(
        PERIOD_END + timedelta(days=20),  # well past the paid period
        phase="scheduled_cancel", app_access_until=promised, period_end=PERIOD_END,
    )


def test_a_trial_runs_to_its_own_end_date():
    """No Stripe object exists during a trial, so nothing else can answer."""
    assert access_end(phase="trial", trial_end=dt(2027, 3, 20)) == dt(2027, 3, 20)


def test_an_expired_trial_reports_when_access_ended_but_never_grants():
    """The end date is returned for display ("access ended on ..."), not because it is
    still live. ``grants_access`` refuses an expired row outright rather than trusting
    the timestamp — "expired" is a decision already taken, and a bad backfill or clock
    skew must not be able to reopen it."""
    ended = dt(2027, 1, 1)

    assert access_end(phase="expired", trial_end=ended, period_end=PERIOD_END) == ended
    assert not grants_access(dt(2026, 12, 1), phase="expired", trial_end=ended)  # before it
    assert not grants_access(dt(2027, 6, 1), phase="expired", trial_end=ended)  # after it
    # Even a nonsensically future end date cannot resurrect it.
    assert not grants_access(dt(2027, 1, 1), phase="expired", trial_end=dt(2099, 1, 1))


def test_a_scheduled_cancel_without_an_extension_ends_with_the_paid_period():
    """It never got its extension recorded. Falling through to the paid rules would
    grant a whole period the customer was never billed for."""
    assert access_end(phase="scheduled_cancel", period_end=PERIOD_END) == PERIOD_END


# --- paid, by account state ----------------------------------------------------


def test_an_active_account_gets_exactly_its_period():
    assert access_end(phase="active", period_end=PERIOD_END) == PERIOD_END


def test_past_due_extends_the_period_by_the_grace_window():
    """A failed renewal is usually an expired card, not a customer leaving."""
    assert access_end(
        phase="past_due", period_end=PERIOD_END
    ) == PERIOD_END + timedelta(days=PAST_DUE_GRACE_DAYS)


def test_a_closure_the_app_did_not_arrange_gets_no_grace():
    """A processor-side cancellation, or an account closed after dunning gave up. Giving
    time here would stack free access on top of the past-due window for somebody who
    stopped paying. An in-app cancellation is unaffected — it carries app_access_until."""
    closed = dt(2027, 2, 20)  # 16 days before the period ends

    assert CANCELLED_GRACE_DAYS == 0
    assert access_end(
        phase="cancelled", period_end=PERIOD_END, cancelled_at=closed
    ) == closed
    assert not grants_access(
        closed + timedelta(days=1),
        phase="cancelled", period_end=PERIOD_END, cancelled_at=closed,
    )


def test_non_payment_does_not_stack_past_due_grace_and_closure_grace():
    """The path that would quietly give away 40 days: past-due 10, then closure 30."""
    past_due_until = access_end(
        phase="past_due", period_end=PERIOD_END
    )
    closed_until = access_end(
        phase="cancelled", period_end=PERIOD_END, cancelled_at=past_due_until,
    )

    assert past_due_until == PERIOD_END + timedelta(days=PAST_DUE_GRACE_DAYS)
    assert closed_until == past_due_until  # nothing added on top


def test_a_paid_module_with_no_period_grants_nothing():
    """No anchor means nothing was ever billed — better to deny than to invent a period."""
    assert access_end(phase="active", period_end=None) is None


# --- is it billed? (guards the manual module toggle) ---------------------------
#
# Distinct from access: a module can grant access without being billed (a trial), and
# be billed while winding down (a cancellation inside its paid extension). A billed
# module must be cancelled through billing rather than switched off, or access and
# payment fall out of step.


def test_an_active_paid_module_is_billed():
    assert is_paid_module(phase="active", has_been_billed=True)


def test_a_past_due_module_still_counts_as_billed():
    """Behavioural change from the Stripe-backed check, and the reason for it: the old
    guard required Stripe status ``active``, so a failing card let the module be toggled
    off — bypassing billing on exactly the accounts where money is in question.

    Note the phase is ``past_due``, NOT ``active``: ``mirror.phase_for_view`` maps the
    Stripe status straight onto its own phase, so a predicate that only looked at
    ``active`` would miss this entirely."""
    assert is_paid_module(phase="past_due", has_been_billed=True)


def test_a_running_trial_is_not_billed_and_stays_toggleable():
    """Nothing is being charged, so there is no billing to route the change through."""
    assert not is_paid_module(phase="trial", has_been_billed=False)


def test_a_cancelled_PAID_module_is_still_billed_during_its_extension():
    """It was charged a prorated extension, so access is paid for until it lapses."""
    assert is_paid_module(phase="scheduled_cancel", has_been_billed=True)


def test_a_cancelled_TRIAL_is_not_billed_despite_sharing_the_phase():
    """Both land in ``scheduled_cancel``; only one involves money. Without the billing
    line to tell them apart, cancelling a free trial would wrongly lock the toggle."""
    assert not is_paid_module(phase="scheduled_cancel", has_been_billed=False)


def test_an_expired_module_is_not_billed():
    assert not is_paid_module(phase="expired", has_been_billed=True)


# --- does the entity already have it? (the double-buy guard) -------------------


def test_a_live_paid_module_cannot_be_bought_again():
    assert is_subscribed(phase="active")


def test_a_module_on_a_free_trial_cannot_be_bought():
    """Selling it mid-trial would take money for something the customer is getting free
    and leave two claims on one module. It converts on its own at term end.

    The old Stripe-backed guard was blind to this: an app-level trial creates no
    subscription, so a trialing module looked unsold."""
    assert is_subscribed(phase="trial")


def test_a_cancelled_module_is_not_blocked_here():
    """Buying it again would also be wrong, but it is refused by the cancellation-window
    guard instead — that one can say "use Renew", which tells the customer what to do."""
    assert not is_subscribed(phase="scheduled_cancel")


def test_an_expired_module_can_be_bought_again():
    """Nothing is held any more."""
    assert not is_subscribed(phase="expired")


# --- will they be charged again? (the billing summary) -------------------------


def test_only_a_live_paid_module_counts_toward_the_ongoing_cost():
    assert is_billing_forward(phase="active")


def test_a_trial_contributes_nothing_to_the_summary():
    """It converts later, but the summary answers "what am I paying" — and during a
    trial the answer is nothing."""
    assert not is_billing_forward(phase="trial")


def test_a_winding_down_module_is_not_part_of_the_ongoing_cost():
    """It may still be accessible, and may even bill a final extension, but it will not
    be charged again."""
    assert not is_billing_forward(phase="scheduled_cancel")
    assert not is_billing_forward(phase="expired")


def test_past_due_is_owned_billed_and_still_costed():
    """All three say yes, and each for its own reason — the module is held, money is
    involved, and it will be charged again. Getting any of them wrong on this phase is
    expensive: selling it twice, letting it be switched off, or telling a customer they
    owe nothing at the moment they owe most."""
    assert is_subscribed(phase="past_due")
    assert is_paid_module(phase="past_due", has_been_billed=True)
    assert is_billing_forward(phase="past_due")


def test_a_cancelled_subscription_is_finished_on_every_count():
    assert not is_subscribed(phase="cancelled")
    assert not is_paid_module(phase="cancelled", has_been_billed=True)
    assert not is_billing_forward(phase="cancelled")


def test_the_three_predicates_disagree_where_they_should():
    """They look interchangeable and are not. A trial is owned but not billed and not
    part of the ongoing cost; a cancelling module still involves money but will not be
    charged again. Collapsing any pair of these sells a module twice, locks a free
    toggle, or quotes the wrong price."""
    #                          subscribed  paid_module  billing_forward
    assert (is_subscribed(phase="trial"),
            is_paid_module(phase="trial", has_been_billed=False),
            is_billing_forward(phase="trial")) == (True, False, False)

    assert (is_subscribed(phase="scheduled_cancel"),
            is_paid_module(phase="scheduled_cancel", has_been_billed=True),
            is_billing_forward(phase="scheduled_cancel")) == (False, True, False)

    assert (is_subscribed(phase="active"),
            is_paid_module(phase="active", has_been_billed=True),
            is_billing_forward(phase="active")) == (True, True, True)


def test_owning_and_being_billed_are_different_questions():
    """``is_subscribed`` blocks a purchase; ``is_paid_module`` blocks the manual toggle.
    A trial answers them oppositely — it is owned, but nothing is being charged for it —
    and conflating the two would either sell it twice or lock a free toggle."""
    assert is_subscribed(phase="trial")
    assert not is_paid_module(phase="trial", has_been_billed=False)


# --- the boundary --------------------------------------------------------------


def test_access_ends_exactly_at_the_boundary_not_after():
    """Half-open, matching billing periods: the instant access ends, it is gone."""
    end = PERIOD_END
    assert grants_access(end - timedelta(seconds=1), phase="active", period_end=end)
    assert not grants_access(end, phase="active", period_end=end)


def test_grace_actually_extends_past_the_period():
    """The regression that matters: a past-due customer must NOT be cut off at the
    period end while their card is being fixed."""
    just_after = PERIOD_END + timedelta(days=1)

    assert not grants_access(just_after, phase="active", period_end=PERIOD_END)
    assert grants_access(
        just_after, phase="past_due", period_end=PERIOD_END
    )


# --- the phase a failed renewal leaves behind ------------------------------------
#
# access_end reads the MODULE phase, not the account's dunning state. Under Stripe the
# past_due phase arrived from the webhook mirror; in-house nothing wrote it, so a
# declined renewal left every row `active` and access died the same day the card did.
# The whole PAST_DUE_GRACE_DAYS policy was unreachable. Live runs caught it.


def test_a_past_due_row_gets_the_grace_an_active_one_does_not():
    """The two differ ONLY by phase, and that is the whole point: nothing about the
    dates says a renewal failed."""
    period_end = datetime(2027, 3, 8, 13, tzinfo=UTC)

    assert access_end(phase="active", period_end=period_end) == period_end
    assert access_end(phase="past_due", period_end=period_end) == (
        period_end + timedelta(days=PAST_DUE_GRACE_DAYS)
    )


def test_access_survives_a_declined_renewal_for_exactly_the_grace_window():
    """Ten days to fix a card, and not an eleventh."""
    period_end = datetime(2027, 3, 8, 13, tzinfo=UTC)
    kwargs = {"phase": "past_due", "period_end": period_end}

    last_day = period_end + timedelta(days=PAST_DUE_GRACE_DAYS - 0.5)
    past_it = period_end + timedelta(days=PAST_DUE_GRACE_DAYS + 0.5)

    assert grants_access(last_day, **kwargs)
    assert not grants_access(past_it, **kwargs)


def test_a_past_due_module_is_still_owned_and_still_billed():
    """It must not become re-purchasable while the customer owes for it — that would
    charge them a second time for something they already have."""
    assert is_subscribed(phase="past_due")
    assert is_paid_module(phase="past_due", has_been_billed=True)
    assert is_billing_forward(phase="past_due")
