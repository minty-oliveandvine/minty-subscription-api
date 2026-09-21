"""Subscription policy: the tunable windows, read from ``billing_policy``.

The five numbers that used to be Python constants — trial length, post-cancellation
access, the past-due window and the retry schedule — now live in a row so changing them
is an UPDATE rather than a deploy. This module is the only reader.

THREE PROPERTIES THAT MATTER.

1. **The pure modules stay pure.** ``access`` and ``dunning`` are plain values in,
   decisions out: no ORM, no clock, no Stripe. That is what lets every money and access
   rule be tested without a database, and it is not given up for configurability. They
   take the windows as PARAMETERS, defaulted to the in-code constants. This module
   resolves the live values and the impure callers pass them in — the same pattern
   ``billing`` uses for currency decimal places.

2. **A bad edit fails SAFE.** The database can enforce that ``past_due_window_days`` is
   a positive integer; it cannot enforce that the last retry lands two days inside it,
   because that needs a subquery a CHECK cannot have. So the cross-field rules are
   checked HERE, and a row that violates them falls back to the shipped defaults with a
   loud log. The worst outcome of a careless UPDATE is that nothing changes.

3. **Fallback is per-GROUP, not wholesale.** ``trial_days`` and
   ``paid_cancel_access_days`` are independent; the past-due window and the retry offsets
   are one coupled decision. A broken retry schedule reverts the dunning pair only — it
   does not silently undo a trial length somebody deliberately set.

Cached on the request scope (``billing.services._context``), so a renewal run that prices many
payers reads the row once.
"""
from __future__ import annotations

from dataclasses import dataclass

from billing.services import _context
from billing.services import access as access_rules
from billing.services import dunning as dunning_rules
from billing.services._log import logger

_G_KEY = "_subscription_policy"

# The shipped values, and the fallback when the row is missing, unreadable or incoherent.
# Sourced from the modules that own each rule so there is still ONE definition of each
# default — a second copy here would be the very drift this table is meant to remove.
DEFAULT_TRIAL_DAYS = 30
DEFAULT_PAID_CANCEL_ACCESS_DAYS = 30
DEFAULT_PAST_DUE_WINDOW_DAYS = access_rules.PAST_DUE_GRACE_DAYS
DEFAULT_RETRY_OFFSETS_DAYS = dunning_rules.RETRY_OFFSETS_DAYS

# A retry fired at the very end of the window could not clear before access was cut, so
# the attempt would be pointless. Mirrors ``test_the_last_retry_leaves_time_to_settle``.
MIN_SETTLE_GAP_DAYS = 2


@dataclass(frozen=True)
class Policy:
    """The live policy. Immutable: callers read it, they do not adjust it."""

    trial_days: int
    paid_cancel_access_days: int
    past_due_window_days: int
    retry_offsets_days: tuple[int, ...]

    @property
    def max_attempts(self) -> int:
        """How many retries the schedule allows — its LENGTH, not its last value."""
        return len(self.retry_offsets_days)


DEFAULTS = Policy(
    trial_days=DEFAULT_TRIAL_DAYS,
    paid_cancel_access_days=DEFAULT_PAID_CANCEL_ACCESS_DAYS,
    past_due_window_days=DEFAULT_PAST_DUE_WINDOW_DAYS,
    retry_offsets_days=DEFAULT_RETRY_OFFSETS_DAYS,
)


def _parse_offsets(raw) -> tuple[int, ...] | None:
    """``"1,3,5,7"`` -> ``(1, 3, 5, 7)``. None if it is not a list of integers."""
    if not raw:
        return None
    try:
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        return tuple(int(p) for p in parts) or None
    except (TypeError, ValueError):
        return None


def _non_negative(value, default: int, field: str) -> int:
    """An independent day-count. Falls back alone — see property 3 in the module doc."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.error("policy: {} is not a number ({!r}); using {}", field, value, default)
        return default
    if number < 0:
        logger.error("policy: {} is negative ({}); using {}", field, number, default)
        return default
    return number


def _dunning_pair(window, offsets) -> tuple[int, tuple[int, ...]]:
    """The past-due window and the retry schedule, which stand or fall TOGETHER.

    Returns the defaults for BOTH if either is unusable or they do not cohere — a valid
    window with an invalid schedule is still an incoherent policy, and applying half of
    it is how you end up retrying a card past the day access was revoked.
    """
    default = (DEFAULT_PAST_DUE_WINDOW_DAYS, DEFAULT_RETRY_OFFSETS_DAYS)

    try:
        window = int(window)
    except (TypeError, ValueError):
        logger.error("policy: past_due_window_days is not a number ({!r})", window)
        return default

    parsed = _parse_offsets(offsets)
    if parsed is None:
        logger.error("policy: retry_offsets_days is not a list of integers ({!r})", offsets)
        return default

    problems = []
    if window < 1:
        problems.append(f"past_due_window_days must be at least 1, got {window}")
    if any(offset <= 0 for offset in parsed):
        # Day 0 is the failure itself; a retry there would fire in the same run.
        problems.append(f"every retry offset must be positive, got {parsed}")
    if list(parsed) != sorted(set(parsed)):
        # Not merely unsorted — duplicates too. ``next_attempt_at`` indexes by attempt
        # count, so a repeated or out-of-order offset schedules a retry in the past.
        problems.append(f"retry offsets must strictly increase, got {parsed}")
    if parsed and max(parsed) >= window:
        problems.append(
            f"last retry (day {max(parsed)}) must fall inside the "
            f"{window}-day window, or it charges a card whose access has ended"
        )
    elif parsed and window - max(parsed) < MIN_SETTLE_GAP_DAYS:
        problems.append(
            f"last retry (day {max(parsed)}) leaves under {MIN_SETTLE_GAP_DAYS} days "
            f"to settle before the {window}-day window closes"
        )

    if problems:
        logger.error(
            "policy: dunning settings are incoherent, falling back to the shipped "
            "schedule ({} day window, retries {}). Problems: {}",
            DEFAULT_PAST_DUE_WINDOW_DAYS, DEFAULT_RETRY_OFFSETS_DAYS, "; ".join(problems),
        )
        return default

    return window, parsed


def _load() -> Policy:
    """Read and validate the row. Never raises — policy must not break a page."""
    try:
        from shared_models.models import BillingPolicy

        row = BillingPolicy.objects.filter(pk=1).first()
    except Exception:
        logger.exception("policy: could not read billing_policy; using shipped defaults")
        return DEFAULTS

    if row is None:
        # Not necessarily a fault — an app running before the migration, or a test app on
        # SQLite where the schema is not materialised. Quiet, because the defaults ARE
        # the shipped behaviour.
        logger.debug("policy: no billing_policy row; using shipped defaults")
        return DEFAULTS

    window, offsets = _dunning_pair(row.past_due_window_days, row.retry_offsets_days)
    return Policy(
        trial_days=_non_negative(row.trial_days, DEFAULT_TRIAL_DAYS, "trial_days"),
        paid_cancel_access_days=_non_negative(
            row.paid_cancel_access_days,
            DEFAULT_PAID_CANCEL_ACCESS_DAYS,
            "paid_cancel_access_days",
        ),
        past_due_window_days=window,
        retry_offsets_days=offsets,
    )


def current() -> Policy:
    """The live policy, cached for this request/CLI invocation."""
    if not _context.active():
        return _load()
    cached = _context.get(_G_KEY)
    if cached is None:
        cached = _load()
        _context.set(_G_KEY, cached)
    return cached
