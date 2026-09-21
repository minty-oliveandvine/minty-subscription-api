"""When a module's access ends, decided from Minty's own tables.

Verified against ``stripe_state.subscription_access_end`` across every lifecycle state
before anything was switched over. Access is the riskiest thing to move in-house — a
wrong invoice can be credited, but a wrong access answer locks a paying customer out of
something they bought — so the two were run side by side until they agreed for reasons
that were understood.

WHO USES THIS. The module card reads it (``entity.services.modules.get_module_cards``),
as do the three phase predicates below. The webhook access SWEEP deliberately does not:
it exists to reflect Stripe and is retired rather than ported once billing moves
in-house. See the note in ``webhooks._resync_entity_access`` — there is an ordering trap
there that makes a row-based sweep act one event behind.

Pure by construction: plain values in, a datetime or None out. No Stripe, no ORM, no
clock of its own. That keeps every rule below testable without a database and portable
off Stripe, which is the point of owning billing at all.

PRECEDENCE, highest first. The order is the whole design:

1. ``app_access_until`` — the app's own promise. Set when a paid module is cancelled
   under the prorated-extension rule, and when a trial is cancelled but keeps its free
   days. It WINS over everything, including a lapsed period or a closed account, because
   the customer was charged for those days (or promised them) and the processor knows
   nothing about it.
2. ``trial`` — access runs to ``trial_end``. No Stripe object exists during a trial, so
   nothing else can answer.
3. ``expired`` — no access, full stop. A trial that ended without converting.
4. paid, by account state — the period end, extended by a grace window when the account
   is past due or closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# The one extension state this module needs, imported rather than re-spelled: a second
# copy of a status string is a thing that drifts. ``constants`` is dependency-free by
# design — no models, no Stripe — so this does not cost the module its purity.
from billing.services.constants import EXT_INVOICED

# Grace windows (days) beyond the period / closure date during which a lapsing account
# still grants module access.
#
# PAST_DUE = 15: buys time for a card to be fixed before anything is cut off. A failed
# renewal is usually an expired card, not a customer leaving.
#
# This is now the DEFAULT, not the last word: the live value is
# ``billing_policy.past_due_window_days``, resolved by ``services.policy`` and passed to
# ``access_end`` by its callers. It stays here so this module needs no database to be
# tested, and so a policy row that cannot be read has a sane value to fall back to. The
# same number is what ``dunning`` gives up on — one column, so they cannot disagree.
#
# CANCELLED = 0, and that is NOT the same as "cancelling gives you nothing". Cancelling
# IN THE APP grants 30 days — but by a different mechanism: ``checkout.cancel_module``
# charges a prorated extension and stamps ``app_access_until`` (see
# ``PAID_CANCEL_ACCESS_DAYS``), which wins outright at precedence rule 1 above. Those
# days were paid for, so they cannot depend on a grace constant.
#
# This window therefore only covers closure the app did NOT arrange — a processor-side
# cancellation, or an account closed after dunning gave up. Granting time there would
# hand free access to someone who stopped paying: past-due grace, then closure grace on
# top of it.
PAST_DUE_GRACE_DAYS = 15
CANCELLED_GRACE_DAYS = 0

PHASE_TRIAL = "trial"
PHASE_ACTIVE = "active"
PHASE_PAST_DUE = "past_due"
PHASE_SCHEDULED_CANCEL = "scheduled_cancel"
PHASE_CANCELLED = "cancelled"
PHASE_EXPIRED = "expired"

# Phases that never grant access, whatever the dates attached to them say. These are
# decisions already taken, so they are not re-litigated against a timestamp that a bad
# backfill or a clock skew could put in the future.
_TERMINAL_PHASES = (PHASE_EXPIRED, PHASE_CANCELLED)


def access_end(
    *,
    phase: str,
    trial_end: datetime | None = None,
    app_access_until: datetime | None = None,
    period_end: datetime | None = None,
    cancelled_at: datetime | None = None,
    past_due_grace_days: int = PAST_DUE_GRACE_DAYS,
) -> datetime | None:
    """When access to one module ends, or None if it does not currently grant access.

    ``past_due_grace_days`` defaults to the shipped constant so this stays a pure
    function testable without a database. Production callers pass the live value from
    ``policy.current().past_due_window_days`` — the SAME number that decides when dunning
    gives up, so access and collection cannot drift apart.

    ``period_end`` is the payer's ``paid_through`` — read from the billing ACCOUNT, not
    from the module row and not derived from the anchor. Two distinct traps:

    * a DERIVED period always contains "now", so it can never lapse: an ``active`` module
      whose card died would keep access forever. A stored paid-through stops advancing
      the moment a renewal fails, which is exactly the signal the grace window needs;
    * a PER-ROW copy drifts. One payer has one cycle, but each row was only refreshed
      when its own entity was touched — live data showed three rows of one payer holding
      three different dates, the oldest three months stale. Access read that.

    The phase carries the billing state, so there is no separate account status to keep
    in step — ``mirror.phase_for_view`` already maps a failing subscription to
    ``past_due`` and an ended one to ``cancelled``.
    """
    # 1. The app's own promise outranks everything.
    if app_access_until is not None:
        return app_access_until

    # 2. A trial has no billing period to reason about.
    if phase == PHASE_TRIAL:
        return trial_end

    # 3. Finished, one way or another. The date is returned so a caller can render
    #    "access ended on ..." — None renders as nothing at all — but these phases can
    #    never grant access; see ``_TERMINAL_PHASES`` and ``grants_access``.
    if phase == PHASE_EXPIRED:
        return trial_end
    if phase == PHASE_CANCELLED:
        # Cancelled WITHOUT going through the app's paid-cancel flow: a portal
        # cancellation, or a subscription Stripe ended after dunning gave up. The flow
        # that was paid for stamps app_access_until and is handled at rule 1.
        base = cancelled_at or period_end
        return base + timedelta(days=CANCELLED_GRACE_DAYS) if base else None

    # 4. A scheduled cancel with no app_access_until never got its extension recorded.
    #    Falling through to the paid rules would quietly grant a full period the customer
    #    was not billed for, so it ends where it was last paid through.
    if phase == PHASE_SCHEDULED_CANCEL:
        return period_end

    if period_end is None:
        return None

    # 5. The renewal failed. Access continues briefly past what was paid for, so a card
    #    can be fixed before anything is cut off — a failed renewal is usually an expired
    #    card rather than a customer leaving.
    if phase == PHASE_PAST_DUE:
        return period_end + timedelta(days=past_due_grace_days)

    # 6. Paid and healthy: access runs to what has been paid for.
    return period_end


def is_paid_module(*, phase: str, has_been_billed: bool) -> bool:
    """Whether this module is BILLED — as opposed to free, trialing, or finished.

    Distinct from ``grants_access``: a module can grant access without being billed (a
    trial) and can be billed while winding down (a cancellation still inside its paid
    extension). This answers "is money involved", which is what guards the manual module
    toggle — a billed module must be cancelled through billing, not switched off, or
    access and payment fall out of step.

    ``has_been_billed`` separates the two things that share the ``scheduled_cancel``
    phase: a cancelled PAID module has been charged at least once, a cancelled TRIAL
    never has. Without it, cancelling a free trial would wrongly lock the toggle.

    It was once ``has_billing_line``, read off the Stripe subscription item id. That
    named a Stripe artifact rather than the question being asked, and when Minty started
    billing its own invoices no such id was ever written — so every paid module answered
    "no" and the distinction silently inverted. Callers pass
    ``row.first_billed_at is not None``.
    """
    if phase in (PHASE_ACTIVE, PHASE_PAST_DUE):
        return True
    if phase == PHASE_SCHEDULED_CANCEL:
        return has_been_billed
    # trial: free by definition. cancelled/expired: nothing is billed any more.
    return False


def is_subscribed(*, phase: str) -> bool:
    """Whether this entity ALREADY has this module — the double-buy guard.

    Covers a live paid module AND a running trial. A trial already grants the module and
    converts to paid on its own at the end of the term, so selling it again would take
    money for something the customer is currently getting free, and leave two claims on
    one module.

    There is no early exit from a trial: it runs its full ``TRIAL_PERIOD_DAYS`` and
    converts at the end. ``trial_end`` is written once, when the trial starts, and no
    path moves it — cancelling schedules non-conversion but keeps the free days, and
    reactivating restores the phase without touching the term. So "buy it now to start
    paying sooner" is not a thing the customer can be offered, and refusing the purchase
    costs them nothing.

    Note the old Stripe-backed check could not see this at all: an app-level trial
    creates no subscription, so a trialing module looked unsold and was purchasable.

    The remaining phases are excluded on purpose:

    * ``scheduled_cancel`` — buying again would be wrong too, because the extension is
      already queued and access still runs. It is refused just after this by the
      cancellation-window guard, which can say "use Renew" instead of "already have it".
      Handled there so the customer gets the message that tells them what to do.
    * ``cancelled`` / ``expired`` — nothing is held any more, so re-buying is right.

    ``past_due`` counts as held. Selling it again would charge a second time for a module
    the customer already has and has not paid for yet — the worst version of a double
    buy, because it looks like new revenue.
    """
    return phase in (PHASE_ACTIVE, PHASE_TRIAL, PHASE_PAST_DUE)


def is_billing_forward(*, phase: str) -> bool:
    """Whether this module will appear on the NEXT invoice — the billing summary.

    The third of three questions that look identical and are not. Keeping them apart is
    what stops a module being sold twice, or a paid one silently switched off:

        is_subscribed       do they already have it?    active, trial
        is_paid_module      is money involved?          active, cancelling-after-billing
        is_billing_forward  will they be charged again? active only

    A trial is excluded even though it converts later: the summary answers "what am I
    paying", and the answer during a trial is nothing. A cancelling module is excluded
    because it is winding down — it may still be accessible, and may even bill a final
    extension, but it is not part of the ongoing cost.

    ``past_due`` IS included. The subscription has not ended, the money is still owed,
    and dropping it would show a customer nothing due at the moment they owe most.
    """
    return phase in (PHASE_ACTIVE, PHASE_PAST_DUE)


def is_covered_this_period(*, phase: str, first_billed_at, paid_through, now,
                           extension_state=None, app_access_until=None) -> bool:
    """Whether the current period's PLAN LINE already holds this module.

    A fourth question, and the one that prices a mid-period change: what does the line
    hold for the days being billed. It differs from ``is_billing_forward`` in exactly one
    case — a module winding down, BEFORE the renewal that drops it. That module will not
    be charged again, so it is not billing forward; but the customer paid for it through
    the period end, so until then it is on the line, and adding a second module to it is
    an upgrade to the bundle rather than a fresh join. Petty Cash converting on 20 Aug
    beside a Payment Request paid to 28 Aug costs the bundle margin for those 8 days, not
    its standalone price.

    Four conditions keep that narrow:

    * ``first_billed_at`` — a cancelled TRIAL is winding down too, and bought nothing.
    * ``paid_through > now`` — past that date nothing covers the module at all.
    * the extension is not INVOICED. This is the part the account-level ``paid_through``
      cannot say. A renewal advances that date for the WHOLE PAYER while
      ``renewals.billable_codes_by_entity`` deliberately leaves the cancelling module off
      the invoice — so after a renewal the date claims a period the module was never
      billed for. The extension is stamped invoiced by the same run that advances the
      date, which makes it the per-row half of the answer. Without it, reinstating one of
      two cancelled modules after a renewal was priced as a bundle upgrade (the 120 step)
      instead of a fresh join (280), and the invoice carried a credit for "unused time"
      on a module that had no line to credit.
    * ``app_access_until`` has not passed. Belt and braces for the cancellation that owed
      nothing: ``checkout._cancel_module_in_house`` only records an extension when the
      amount is positive, so a zero-amount cancellation NEVER gets the invoiced stamp.
      ``checkout.terminate_lapsed_module`` would eventually move such a row to
      ``cancelled``, but the daily sweep lags and is skipped outright when a money job
      failed (``daily.SWEEP_BLOCKERS``), so the phase cannot be relied on to have caught
      up.

    The last two are NOT redundant. An invoiced extension whose days are still running has
    an ``app_access_until`` in the future, and those days were bought at the marginal
    extension rate on a line of their own — not on the plan line this predicate is about.

    Used for pricing a change INSIDE the period. Pricing access BEYOND it — the
    cancellation extension — asks ``is_billing_forward`` instead, because there the
    winding-down module is no longer on the line.
    """
    if is_billing_forward(phase=phase):
        return True
    if phase != PHASE_SCHEDULED_CANCEL:
        return False
    if first_billed_at is None or paid_through is None or now is None:
        return False
    if paid_through <= now:
        return False
    if extension_state == EXT_INVOICED:
        return False
    if app_access_until is not None and app_access_until <= now:
        return False
    return True


def grants_access(now: datetime, **kwargs) -> bool:
    """Whether the module grants access at ``now``.

    ``now`` is passed in rather than read from a clock so the caller decides what "now"
    means — the trusted Stripe-derived clock today, something else once billing no longer
    talks to Stripe.
    """
    # A finished module never grants, whatever the dates say. ``access_end`` returns an
    # end date for display, and by construction it is past — but these are decisions
    # already taken, so they are not re-litigated against a timestamp that a bad
    # backfill or a clock skew could put in the future.
    if kwargs.get("phase") in _TERMINAL_PHASES:
        return False
    end = access_end(**kwargs)
    return end is not None and end > now
