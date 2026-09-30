"""What to do when a renewal payment fails — retry schedule and give-up rules.

Stripe's Smart Retries are ML-timed and deliberately opaque; there is no point trying to
reproduce them. What matters in-house is a schedule that is PREDICTABLE — a customer can
be told exactly when the next attempt is, and support can explain why access ended on a
particular day — and that stays coherent with the access window.

Pure by construction: plain values in, decisions out. No Stripe, no ORM, no clock. The
caller supplies "now" and persists whatever comes back, which keeps the policy testable
without a database and portable off Stripe.

THE INVARIANT THAT MATTERS. Dunning must finish inside the access grace window
(``access.PAST_DUE_GRACE_DAYS``). Get that wrong in either direction and the system
contradicts itself:

* retries running PAST the grace end keep charging a customer whose access was already
  revoked — billing someone for something they can no longer use;
* giving up WELL BEFORE it ends leaves a customer with days of free access after the
  last attempt, and no way to recover the subscription even if they fix their card.

``test_dunning_finishes_inside_the_access_grace_window`` pins this, so changing either
constant without the other fails loudly rather than drifting.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

# Days after the FIRST failure at which each retry runs. EVERY day from the first to the
# thirteenth: most failures are transient — an expired card that has already been
# replaced, or a temporary hold — and a daily attempt collects on the day the customer
# fixes it rather than up to three days later, which is three more days of an account
# reading as past due to everyone who looks at it.
#
# It stops at 13, not 15, because the schedule has to finish before the window does:
# ``policy._dunning_pair`` rejects any list whose last retry leaves under two days to
# settle, and falls back to the shipped default if it does. Those two quiet days at the
# end are deliberate — automatic attempts have stopped, the customer has been told, and
# they can still pay in the portal before access ends.
RETRY_OFFSETS_DAYS: tuple[int, ...] = tuple(range(1, 14))

MAX_ATTEMPTS = len(RETRY_OFFSETS_DAYS)

# When dunning stops and the subscription is given up on, measured from the first
# failure. Sits at the end of the access grace window: the last retry has had two days
# to settle, and access ends the moment collection does.
GIVE_UP_AFTER_DAYS = 15

# --- Where these values actually come from now ---------------------------------------
#
# Both constants above are DEFAULTS. The live values are
# ``billing_policy.past_due_window_days`` and ``billing_policy.retry_offsets_days``,
# resolved by ``services.policy`` and passed in by ``collect_due``.
#
# The two-constants-must-agree problem is gone at the source: access grace and the
# give-up deadline are ONE column, so ``give_up_at`` and ``access.access_end`` are handed
# the same number and cannot drift. What ``policy`` still has to check is the schedule
# fitting inside it — see ``policy._dunning_pair``.
#
# They stay here so every function below remains pure: plain values in, decisions out,
# no database needed to test the schedule and a sane fallback if the row cannot be read.


def next_attempt_at(
    first_failed_at: datetime,
    attempts: int,
    offsets: tuple[int, ...] = RETRY_OFFSETS_DAYS,
) -> datetime | None:
    """When the next retry is due, or None once the schedule is exhausted.

    ``attempts`` counts retries ALREADY made, so 0 means the first failure has been
    recorded and nothing has been retried yet.

    Timed from the FIRST failure rather than the previous attempt. A retry that is
    delayed — a worker outage, a queue backlog — must not push everything after it back
    and quietly extend dunning past the grace window.
    """
    if attempts < 0:
        raise ValueError("attempts cannot be negative")
    if attempts >= len(offsets):
        return None
    return first_failed_at + timedelta(days=offsets[attempts])


def is_exhausted(attempts: int, offsets: tuple[int, ...] = RETRY_OFFSETS_DAYS) -> bool:
    """Whether every scheduled retry has been made."""
    return attempts >= len(offsets)


def give_up_at(
    first_failed_at: datetime,
    window_days: int = GIVE_UP_AFTER_DAYS,
    access_ends_at: datetime | None = None,
) -> datetime:
    """When to stop trying and cancel, whatever the attempt count says.

    A wall-clock deadline as well as an attempt count, because the two can disagree: a
    worker that was down for a week comes back to a subscription whose retries are all
    still "pending" but which is long past the point of being worth chasing.

    ``access_ends_at`` CLAMPS the deadline, and it is the whole reason this takes a
    second date. The window is one number, but the two halves of going past due count it
    from different instants:

        access ends at      paid_through        + window
        collection ends at  dunning_started_at  + window

    Those coincide only if the renewal ran the moment the period ended. It does not have
    to: ``renewals.due_renewals`` picks up anyone whose ``paid_through`` has passed, so a
    worker outage, a paused cron or a batch ``limit`` pushes the first failure — and with
    it the whole retry schedule — days later. The drift is one-directional, because a
    renewal cannot fail before its period ends, so collection always outlives access.

    Worked through: period ends 1 Mar, window 15, worker down a week. Dunning starts
    8 Mar and would run to 23 Mar, retrying on the 9th, 12th, 15th, 18th and 21st — but
    access ended on the 16th. The last two retries charge a card for a customer who has
    been locked out for days, which is exactly the failure the module docstring opens
    with.

    Clamping fixes the harm without touching the schedule: retries keep their normal
    spacing and simply stop when entitlement does. Passing None keeps the unclamped
    deadline, which is what the pure tests use.
    """
    deadline = first_failed_at + timedelta(days=window_days)
    if access_ends_at is not None and access_ends_at < deadline:
        return access_ends_at
    return deadline


def should_attempt_now(
    now: datetime,
    first_failed_at: datetime,
    attempts: int,
    offsets: tuple[int, ...] = RETRY_OFFSETS_DAYS,
    window_days: int = GIVE_UP_AFTER_DAYS,
    access_ends_at: datetime | None = None,
) -> bool:
    """Whether a retry is due at ``now``.

    False once the schedule is exhausted OR the deadline has passed, so a caller that
    polls cannot keep charging a card past the point of giving up. Because the deadline
    is ``give_up_at``, passing ``access_ends_at`` also stops retries the moment access
    does — no separate check needed here.
    """
    if now >= give_up_at(first_failed_at, window_days, access_ends_at):
        return False
    due = next_attempt_at(first_failed_at, attempts, offsets)
    return due is not None and now >= due


def should_give_up(
    now: datetime,
    first_failed_at: datetime,
    window_days: int = GIVE_UP_AFTER_DAYS,
    access_ends_at: datetime | None = None,
) -> bool:
    """Whether to stop and cancel. The DEADLINE only — never the attempt count.

    Running out of retries means "stop charging the card", not "cancel the
    subscription". Those are different decisions and conflating them costs the customer
    real days: with retries at 1/4/7/10/13 and a 15-day window, cancelling on exhaustion
    would end access on day 13 while the past-due grace still promised 15.

    The gap is the most valuable part of the window. Automatic retries have stopped, so
    the customer has been told to act, and they can still pay in the portal — which
    settles the invoice and recovers the subscription. Cancelling early removes exactly
    that chance.

    Access and collection end together on the same day — by construction when the
    renewal ran on time, and by ``access_ends_at`` clamping the deadline when it did not.
    """
    return now >= give_up_at(first_failed_at, window_days, access_ends_at)


def attempts_remaining(
    attempts: int, offsets: tuple[int, ...] = RETRY_OFFSETS_DAYS
) -> int:
    """How many retries are left — for telling the customer what happens next."""
    return max(0, len(offsets) - attempts)


def told_of_failure(entity_id, now: datetime) -> bool:
    """Whether this COMPANY's past-due state is one its customer was told of
    (``customer_told``), read off the card it is billed on. Unknown is told: an unreadable
    card keeps today's "payment failed". The one rule the banner, the module cards and the
    subscriptions list all ask."""
    from billing.services import policy, store
    from billing.services._log import logger

    try:
        group = store.billing_group_for_entity(entity_id)
        if group is None:
            return True
        return customer_told(group, now, policy.current().past_due_window_days)
    except Exception:
        logger.exception("dunning: could not read the card of entity {}", entity_id)
        return True


def customer_told(group, now: datetime, window_days: int) -> bool:
    """Whether THIS card's past-due state is one its customer has been TOLD about: dunning is
    running (a decline, and its notice), or collection is over.

    A card held past due while the PROCESSOR was failing (``store.hold_group_grace``) was told
    nothing, because there was nothing to tell - its card was never asked. Showing it "payment
    failed" in the app would say what the decision not to email refused to (the user's rule,
    2026-09-30: the processor failing is not the card declining).
    """
    if group.dunning_started_at is not None:
        return True
    return collection_over(group, now, window_days)


def collection_over(group, now: datetime, window_days: int) -> bool:
    """Whether collecting THIS card's debt is over, stamp or no stamp.

    Over past a running episode's give-up deadline, or once the card's access has run out
    (``paid_through`` plus the window). An episode closed by giving up leaves ``paid_through``
    where it stopped, so its abandoned renewal still names "the current period" by key - and
    the business does not chase those (2026-08-11). One rule for everything that would charge
    or offer to: the portal's *Retry payment*, and the renewal pass meeting an unpaid invoice.
    """
    access_ends_at = (
        group.paid_through + timedelta(days=window_days)
        if group.paid_through is not None
        else None
    )
    started = group.dunning_started_at
    if started is not None and should_give_up(now, started, window_days, access_ends_at):
        return True
    return access_ends_at is not None and now >= access_ends_at


# --- Running a collection cycle -------------------------------------------------
#
# Everything above is pure policy. Below is the one function that acts on it, kept
# separate so the schedule stays testable without a database or a processor.


def _account_for(group):
    """The billing account behind a group — for the anchor and the Stripe customer id.

    Those two stayed on the payer when the cycle moved onto the card: every group of a
    payer renews on the same period boundaries, and every invoice is still issued against
    the one Stripe customer. Only the card charged, the date paid through and the retry
    clock are the group's own.
    """
    from billing.services import store

    return store.customer_mapping_for_user(group.payer_user_id)


def _invoices_for_group(invoices: list[dict], group, groups) -> list[dict]:
    """Narrow the payer's open invoices to the ones THIS card owes.

    ``open_invoices`` asks the processor about a CUSTOMER, and a customer now carries one
    document per card per period. Chasing the wrong one takes real money for a debt this
    card does not have, and leaves the debt it does have open.

    ``billing_group`` metadata is stamped on every invoice raised since per-entity cards
    landed. AN INVOICE WITHOUT IT BELONGS TO THE PAYER'S FIRST GROUP — everything raised
    before the cutover was charged to the one card they had, which the backfill turned
    into exactly that group. Attributing them anywhere else would strand real debt on a
    card that never owed it.
    """
    first = str(groups[0].id) if groups else None
    mine = []
    for invoice in invoices:
        stamped = (invoice.get("metadata") or {}).get("billing_group")
        owner = str(stamped) if stamped else first
        if owner == str(group.id):
            mine.append(invoice)
    return mine


def _settle_period(account, group, invoice) -> None:
    """Record the period a recovered payment covers.

    Only for RENEWAL invoices, and only on evidence. Dunning chases the payer's oldest
    open invoice, which may be a mid-period change or a reinstatement — those are paid
    for in full and cover no period, so advancing the cycle off one hands the customer a
    free month. ``renewal_key`` is the marker (see ``renewals.period_key``).

    ``invoice is None`` means the debt was settled somewhere this code cannot see (a
    portal payment, a manual charge) and there is nothing left to read. It is NOT
    advanced: guessing risks the free month, whereas doing nothing self-heals — the next
    renewal run finds the paid invoice by its key and adopts it. The cost is that the
    payer stays unentitled until then, which is why the paid-retry path above does this
    properly rather than relying on it.
    """
    from billing.services import renewals, store
    from billing.services._log import logger

    if invoice is None:
        logger.info(
            "dunning: payer {} settled outside the app; leaving paid_through for the "
            "next renewal run to adopt",
            account.user_id,
        )
        return
    paid_key = (invoice.get("metadata") or {}).get("renewal_key")
    if not paid_key:
        return

    # The anchor is the PAYER'S — one cycle, several invoices — and the paid-through is
    # this CARD'S, because that is what its own money bought.
    anchor = account.anchor_at
    paid_through = group.paid_through
    if anchor is None or paid_through is None:
        return
    period = renewals.next_period(anchor, paid_through)
    # The key must name THIS period, not merely be a renewal key. Both are renewals when
    # a payer carries stale debt — a give-up leaves its unpaid renewal open, and a later
    # reinstatement moves ``paid_through`` past it — so an invoice for May settling while
    # the cycle sits in August would advance August, handing over a free month for a bill
    # belonging to a period that ended long ago. Chained renewals still advance one at a
    # time, because the oldest unpaid period IS the one this computes.
    if not _names_period(paid_key, renewals.period_key(account.user_id, period, group.id)):
        logger.info(
            "dunning: payer {} paid {} which covers an earlier period; leaving "
            "paid_through at {}",
            account.user_id, invoice.get("id"), paid_through,
        )
        return
    store.set_group_paid_through(group.id, period.end)


def _settle_handover(invoice) -> None:
    """Finish the handover whose parked first charge this paid invoice was
    (``transfers.settle_paid_handover``; nothing for any other invoice). Never raises."""
    from billing.services import transfers

    transfers.settle_paid_handover(invoice)


def _current_period_key(account, group) -> str | None:
    """The ``renewal_key`` of the invoice for the period THIS CARD is behind on.

    None when the card has never collected (no anchor or no ``paid_through``), which is
    also the answer to "which invoice recovers it" — there is no period to recover.
    Callers fall back to the oldest open invoice in that case; a card with no billing
    history has no stale renewal for the fallback to pick up by mistake.
    """
    from billing.services import renewals

    anchor = account.anchor_at
    paid_through = group.paid_through
    if anchor is None or paid_through is None:
        return None
    return renewals.period_key(
        account.user_id, renewals.next_period(anchor, paid_through), group.id
    )


def _nothing_open_but_owed(account, group, now) -> bool:
    """Called when the processor holds nothing open for THIS card - the moment dunning would
    call the debt settled elsewhere. True when it is NOT settled.

    Settled is a claim, and it needs EVIDENCE: "nothing open" alone ended dunning as
    recovered, said "Thank you for your payment" and put the companies back for any failure
    that left no open invoice behind - a draft never finalized, a reservation that never
    reached the processor, a renewal that failed before raising anything. So a card still
    BEHIND (``paid_through`` not past ``now``) is owed unless its current period's invoice
    is there and was paid, voided or written off. Owed when that invoice is:

    * missing - nothing was raised for the period (the renewal pass raises it);
    * a reservation with no processor id - nobody knows if it exists (the pass asks);
    * a DRAFT - never finalized, so not billed: a STRANDED DRAFT, which the renewal pass
      finishes (``renewals._renew_one_group``);
    * still OPEN after a re-read that failed - unknown is not settled.

    This card's rows still reading "open" are re-read first. Nothing is open at the
    processor, so each was paid, voided or written off somewhere this code cannot see - and
    until re-read it goes on showing as failed, with Retry payment on it.

    Only the CURRENT period's invoice counts. A draft some other attempt abandoned - a
    purchase retried under a fresh key - is not this card's debt, and must not hold it in
    dunning for ever.
    """
    from billing.services import billing_gateway, store
    from billing.services._log import logger

    for record in store.open_invoices_for_group(group.id):
        billing_gateway.refresh_record(record)
    if store.handover_owed(group, now):
        # A company handed to this payer whose first charge is still owed - no card to take
        # it from yet, or not raised yet (``transfers.collect_due``).
        logger.info("dunning: group {} still owes a handover's first charge", group.id)
        return True
    if group.paid_through is None or group.paid_through > now:
        # Paid up - or a card that never renewed, whose debt is not a period's.
        return False
    key = _current_period_key(account, group)
    current = store.invoice_for_key(key)
    if current is None or not current.external_id:
        logger.warning(
            "dunning: nothing is open for group {}, and the period it is behind on has no "
            "invoice at the processor ({}); not calling it settled", group.id, key,
        )
        return True
    status = billing_gateway.refresh_record(current)
    if status == "draft":
        billing_gateway.stranded_draft(
            current.external_id, current.idempotency_key, "dunning", billing_gateway.RESUMED
        )
        return True
    return status == "open"


def _names_period(renewal_key: str | None, key: str | None) -> bool:
    """Whether an invoice's ``renewal_key`` names the period ``key`` does.

    Exact, in production: ``renewals.period_key`` is never rewritten there. A REPLAY
    (``replay_scenarios``) scopes every key it issues to its run - ``<key>-<the customer's
    last 12 characters>``, so same-day replays do not collide on Stripe's idempotency keys -
    and the data it lives stays on the database afterwards, read by an engine whose keys are
    plain. The scoped form is the same period. Nothing else can start with ``key + "-"``: a
    key ends with the card's id, and another period differs in its date.
    """
    if not renewal_key or not key:
        return False
    return renewal_key == key or renewal_key.startswith(f"{key}-")


def _current_period_invoice(invoices: list[dict], key: str | None) -> dict | None:
    """The open invoice for the CURRENT period, or None if none of them is.

    The one question that decides whether a payment recovers the subscription. Paying the
    oldest open invoice is not recovery when the oldest is a give-up's abandoned renewal
    or a mid-period change — the customer is told they are settled, gets access back, and
    the period they are actually behind on stays unpaid.
    """
    if not key:
        return None
    for invoice in invoices:
        if _names_period((invoice.get("metadata") or {}).get("renewal_key"), key):
            return invoice
    return None


def _manual_target(invoices: list[dict], key: str | None) -> dict | None:
    """Which invoice a customer pressing Pay now should be charged for.

    In order:

    1. the CURRENT period's renewal — what they are behind on, and the only thing that
       gives them their service back;
    2. failing that, the oldest open invoice that is not a renewal for some OTHER period.
       A mid-period change or a reinstatement carries no ``renewal_key`` and is a real
       debt the customer can settle whenever they like; refusing to take it would be the
       old "no outstanding payment" bug wearing a new hat;
    3. None when everything open is a renewal for a period that is not theirs to fix —
       an abandoned give-up bill. Charging it takes money and restores nothing, so the
       caller reports it instead.
    """
    current = _current_period_invoice(invoices, key)
    if current is not None:
        return current
    for invoice in invoices:
        renewal_key = (invoice.get("metadata") or {}).get("renewal_key")
        # No key at all => not a renewal => not stale debt. With no current key to
        # compare against there is nothing to call stale either.
        if not renewal_key or not key:
            return invoice
    return None


def _replaced_by(invoices: list[dict], invoice_id: str) -> dict | None:
    """The open invoice that re-issued ``invoice_id`` (``billing_gateway.refresh_invoice``), or
    None.

    Both can be open at once - between the replacement being raised and the original being
    voided, a moment a crash can stretch until the next attempt. A page drawn then shows the
    REPLACEMENT (it holds the period's key) while these rules still pick the original (the
    older of two invoices for one period), so the row's invoice and the rules' invoice differ
    without anything being wrong: charging the original finishes the re-issue and collects on
    the replacement, the very document the row shows.
    """
    from billing.services import billing_gateway

    for invoice in invoices:
        if (invoice.get("metadata") or {}).get(billing_gateway.REPLACES) == invoice_id:
            return invoice
    return None


class _Charge(NamedTuple):
    """What one charge attempt came to - see ``_charge``."""

    paid: bool
    reason: str | None
    invoice: dict
    refreshed: str | None = None
    stuck: bool = False
    unavailable: bool = False


def _charge(group, invoice: dict) -> _Charge:
    """Charge ONE invoice on the group's current card, re-issuing it first if the processor will
    no longer collect it. The one place both the scheduled run and Pay now charge.

    Stripe cancels an invoice's payment for good once it has been confirmed too many times (ten
    declines in our account); after that no retry can ever succeed, so a customer who fixed
    their card on day eleven could not pay at all. A dead invoice is therefore re-issued as an
    identical replacement and THAT is charged, inside the same attempt: the caller counted the
    slot once, and the card is hit once.

    ``invoice`` on the result is what was actually charged - the replacement when there was a
    refresh, with ``refreshed`` naming the one it replaced - so the caller settles and reports
    the right document. ``stuck`` means the invoice can no longer be paid and could not be
    re-issued automatically (the gateway has logged which, as an ERROR): nothing was charged,
    and it is not a decline.

    The gateway's ``DEAD_PAYMENT`` marker never leaves here. ``unavailable`` means the PROCESSOR
    failed, not the card: nothing was charged and nothing is known about the card, so the
    caller gives the attempt back and tells nobody. A raise does leave - the database could not
    be reached mid-refresh - and the next attempt resumes where it stopped.
    """
    from billing.services import billing_gateway
    from billing.services._log import logger

    card = group.stripe_payment_method_id
    paid, reason = billing_gateway.retry_invoice(invoice["id"], card)
    if reason == billing_gateway.UNAVAILABLE:
        return _Charge(False, None, invoice, unavailable=True)
    if reason != billing_gateway.DEAD_PAYMENT:
        return _Charge(paid, reason, invoice)
    try:
        replacement = billing_gateway.refresh_invoice(invoice["id"], card)
    except Exception as exc:
        if not billing_gateway.retryable(exc):
            raise
        logger.error("dunning: the processor failed re-issuing invoice {}", invoice["id"])
        return _Charge(False, None, invoice, unavailable=True)
    if replacement is None:
        return _Charge(False, None, invoice, stuck=True)
    paid, reason = billing_gateway.retry_invoice(replacement["id"], card)
    if reason == billing_gateway.UNAVAILABLE:
        return _Charge(False, None, replacement, refreshed=invoice["id"], unavailable=True)
    if reason == billing_gateway.DEAD_PAYMENT:
        # A fresh invoice cannot have had its payment cancelled before it was ever tried; if
        # it somehow has, the customer is still owed a sentence, not a marker.
        reason = None
    return _Charge(paid, reason, replacement, refreshed=invoice["id"])


def _restore_access(user_id) -> None:
    """Switch this payer's modules back on now that the balance is settled.

    ``_settle_period`` moves ``paid_through`` and ``end_group_dunning`` moves the phases, but
    neither touches ``entity_function_map`` — and access is a separate write. Without
    this the money is collected, the subscription reads active, and the customer is
    still bounced off every page in it. The daily sweep would eventually notice, so this
    is about WHEN: the customer who just paid to get back in should be back in.

    Never raises. A recovery that collected the money is not undone because a follow-up
    write failed; the sweep is the backstop, and a swallowed error here is visible in the
    log rather than as a lost payment.

    STILL THE WHOLE PAYER even though collection is now per card, and deliberately: the
    sweep decides each company on its own dates and its own phase, so a company still past
    due on the payer's OTHER card is left exactly where it is. Narrowing it to the
    recovered group would only mean doing less work while the customer waits.
    """
    # ``logger`` is imported per-function throughout this module, and this one used to
    # rely on a module-level name that does not exist — so the except branch raised
    # NameError and the "never raises" contract held only while nothing went wrong.
    # Inside ``collect_due`` that surfaced as "cycle failed" on an account whose money had
    # already been collected; on the manual path there is no outer try to hide it.
    from billing.services._log import logger
    from billing.services.access_sweep import sweep_expired_module_access

    try:
        sweep_expired_module_access(payer_user_id=user_id)
    except Exception:
        logger.exception("dunning: could not restore access for payer {}", user_id)


def collect_due(now, limit: int | None = None) -> dict:
    """Run one dunning cycle: retry what is due, give up on what is spent.

    THE UNIT IS A CARD, not a payer. A payer holding two cards can be mid-collection on
    one while the other is renewing normally, and only the failing card's companies are
    past due — so the schedule, the attempt budget and the give-up deadline all belong to
    the card. Under one clock per payer, one dead card put every company that payer paid
    for into the grace window and then terminated them.

    Returns ``{"retried": [...], "recovered": [...], "collected": [...],
    "given_up": [...]}``. A ``retried`` entry names the ``invoice`` it charged; when that had to
    be re-issued first because the processor would no longer collect it (``_charge``),
    ``refreshed`` names the one it replaced.

    ``collected`` is the fourth outcome and the one that is easy to miss: the charge went
    through, but on an invoice that is not the period the payer is behind on, so the
    subscription is NOT recovered and the account stays in dunning. Money in, episode
    open — see the recovery branch below.

    Safe to run repeatedly and at any cadence. ``should_attempt_now`` gates on the
    schedule rather than on when this last ran, so an hourly job and a daily one produce
    the same attempts — and a job that was down for a week does not fire the whole
    backlog at once, because the missed slots are simply past.
    """
    from billing.services import policy, store

    # Resolved ONCE for the cycle, not per account: every payer is on the same policy,
    # and re-reading it mid-run would let an edit land halfway through — some payers
    # given up on under the old window, some under the new.
    settings = policy.current()
    offsets = settings.retry_offsets_days
    window = settings.past_due_window_days

    retried: list[dict] = []
    recovered: list[dict] = []
    collected: list[dict] = []
    given_up: list[dict] = []

    groups = store.groups_in_dunning()
    if limit:
        groups = groups[:limit]

    for group in groups:
        _outcomes = _collect_one_group(group, now, offsets, window)
        # Mailed per CARD, once its outcome is recorded: a retry that succeeds is in both
        # ``retried`` and ``recovered`` for the same card, so it is still one notice, the
        # right one. Batched to the end of the cycle, anything that stopped the cycle - an
        # exception, a restart - lost every notice before it, and for good: the retry key
        # counts the attempt, and a recovered card leaves the dunning list.
        _notify_dunning(_outcomes["retried"], _outcomes["recovered"], _outcomes["given_up"])
        retried.extend(_outcomes["retried"])
        recovered.extend(_outcomes["recovered"])
        collected.extend(_outcomes["collected"])
        given_up.extend(_outcomes["given_up"])

    return {
        "retried": retried,
        "recovered": recovered,
        "collected": collected,
        "given_up": given_up,
    }


def _notify_dunning(retried: list[dict], recovered: list[dict],
                    given_up: list[dict]) -> None:
    """Mail the dunning outcomes. Never raises — see ``notify``.

    The retry notice is deliberately suppressed for an attempt that then SUCCEEDED: the
    same entry lands in both lists, and the customer cares about the outcome, not the
    mechanics. Only a retry that left the balance outstanding is worth an email.

    Dedupe keys are per-episode, and the retry key includes the attempt number — so each
    of the three or four scheduled attempts notifies once, and a job run twice in a day
    still only sends what actually happened once.
    """
    from billing.services import notify

    # Per ACCOUNT, not per payer: a payer's two cards are two episodes, and one card
    # recovering says nothing about the other's retry failing in the same pass. Keyed by
    # the payer alone, that second card's "your payment failed again" was dropped.
    def _account(entry) -> tuple:
        return entry["user_id"], str(entry.get("billing_group_id"))

    settled = {_account(entry) for entry in recovered}
    events = []
    for entry in retried:
        if _account(entry) in settled:
            continue
        # Charged successfully, but on an invoice that did not recover the subscription
        # (see the ``collected`` branch in ``collect_due``). Neither notice fits: "your
        # payment failed" is false to someone whose card was just debited, and "you're
        # all settled" is false to someone still past due. The processor's own receipt
        # covers the charge, and the past-due state keeps saying what is still owed, so
        # this sends nothing rather than something wrong.
        if entry.get("_collected"):
            continue
        # Paid - whatever became of recording it. "We still could not process your payment"
        # to a card that was just charged is the one notice worse than none.
        if entry.get("_paid"):
            continue
        events.append(
            (entry["user_id"], notify.DUNNING_RETRY_FAILED,
             f"{entry['_episode']}:{entry['attempts']}", entry)
        )
    for entry in recovered:
        events.append(
            (entry["user_id"], notify.PAYMENT_RECOVERED, entry["_episode"], entry)
        )
    # ``given_up`` sends nothing, and NOTHING ELSE PICKS IT UP EITHER.
    #
    # The account-closed notice was retired first, on the grounds that the access sweep
    # running immediately after this in the same pass (``daily.JOB_ORDER``) would mail
    # ``access_revoked`` per entity and say it better. That one was then retired too, so
    # the end of a dunning episode is now entirely silent: the customer's last word from
    # us is the retry-failed notice warning that suspension is coming, and no email
    # confirms it arrived. Deliberate — see the closing comment in
    # ``access_sweep.sweep_expired_module_access``.
    notify.notify_many(events)

    # ``_episode`` is scaffolding for the dedupe key, not part of what ``collect_due``
    # reports. Dropped here so the documented return shape stays what it was.
    for entry in (*retried, *recovered, *given_up):
        entry.pop("_episode", None)
        entry.pop("_collected", None)
        entry.pop("_paid", None)


def retry_now(user_id, entity_id=None, *, group_id=None, expect_invoice=None) -> dict:
    """Collect the outstanding invoice IMMEDIATELY, at the payer's own request.

    ``entity_id`` names the company the button was pressed from, and through it the CARD
    whose debt is settled. A payer with two cards can owe on one and be perfectly up to
    date on the other; charging the wrong one takes money and restores nothing on the page
    they are looking at. Omitting it takes the card that has been in collection longest —
    the one closest to being given up on.

    The same collection ``collect_due`` performs, minus one thing: the schedule gate.
    ``should_attempt_now`` paces AUTOMATIC retries so a cron job does not hammer a card
    every hour. It has no business standing between a customer who has just fixed their
    card and the debt they are trying to settle — without this they save a card and then
    wait up to two days for a slot, still locked out, with no way to say "try it now".

    And one thing more: WHICH invoice it charges. The scheduled run takes the oldest open
    one, because collections chase the oldest debt. This takes the CURRENT period's — the
    customer pressed a button to restore their service, and settling a bill for a period
    that ended months ago does not restore anything. If the only thing open is older debt
    it reports ``older_debt_only`` and charges nothing.

    Everything else is deliberately identical, and calls the same helpers, so a manual
    collection and a scheduled one cannot end in different states:

      * the GIVE-UP deadline still applies. Past it, retries stop for the same reason the
        cron stops — collection must never outlive access (see ``give_up_at``).
      * the attempt is counted against the same budget. A customer-initiated retry is a
        charge attempt like any other, and sharing the budget is what bounds the total
        number of times a card can be hit however the retry was triggered. The cost is
        that clicking twice spends two automatic slots; the account still rides out the
        window and is still recoverable, which ``should_give_up`` is explicit about.
      * success settles the period BEFORE clearing dunning, so the customer is entitled
        to what they just paid for rather than waiting for the next renewal run.

    Keyed on the DEBT, not on the dunning stamp. The open invoice is what the customer
    owes; ``dunning_started_at`` is bookkeeping for the retry schedule. Asking the stamp
    first meant a payer with a real unpaid invoice — but no stamp, because the renewal
    that should have set one never ran, or the row was written by hand — was told "no
    outstanding payment" while the page beside the button read "payment due". The stamp
    still governs the deadline and the attempt budget, which is all it is for.

    Returns ``{"status": ..., "attempts": int, "invoice": id|None, "reason": str|None}``
    where status is one of:

        paid            collected, subscription recovered
        failed          the card was declined again — ``reason`` says why
        no_card         nothing on file to charge; no attempt is spent on it
        gave_up         past the deadline; dunning closed rather than charged
        nothing_owed    no open invoice at all, so there is nothing to collect
        older_debt_only something is open, but nothing for the current period — no
                        attempt is spent, and ``invoice`` names the old debt
        not_this_invoice ``expect_invoice`` names an open invoice other than the
                        one this would charge — nothing is charged or counted
        not_collectable the invoice can no longer be paid and could not be re-issued
                        automatically (logged for a person to fix) — nothing was charged
        unavailable     the payment PROCESSOR failed, not the card - nothing was charged,
                        and the attempt is given back

    When the invoice had to be re-issued first - the processor had cancelled its payment for
    good (``_charge``) - ``invoice`` names the replacement that was charged, and ``refreshed``
    the one it replaced.

    ``group_id`` names the CARD outright instead of reaching it through a company - the
    payer portal's invoice row knows its billing account, not a company. It must be one
    of this payer's, or nothing is charged. ``expect_invoice`` (the processor's id) is the
    invoice that row shows: it is collected only if it is the one the rules below pick,
    so a button can never pay a different bill from the one it sits beside.
    """
    from billing.services import store
    from billing.services._log import logger
    from billing.services.stripe_client import (
        customer_default_payment_method,
    )

    _ctx, _refusal = _retry_context(user_id, entity_id, group_id=group_id)
    if _refusal is not None:
        return _refusal
    account = _ctx["account"]
    group = _ctx["group"]
    started = _ctx["started"]
    attempts = _ctx["attempts"]
    invoices = _ctx["invoices"]

    # THE CURRENT PERIOD, not the oldest debt. This is the one place the manual path
    # deliberately differs from the scheduled one, and the customer's intent is the
    # reason: they pressed a button to get their service back, and paying off a bill for
    # a period that ended months ago does not do that. The scheduled run may still chase
    # the oldest — that is a collections decision — but nobody chooses it by clicking.
    #
    # No key means the payer has never been billed, so there is no stale renewal to pick
    # up by mistake and the single open invoice is what they came to pay.
    key = _current_period_key(account, group)
    target = _manual_target(invoices, key)
    if expect_invoice and (target is None or (
        target["id"] != expect_invoice
        and (_replaced_by(invoices, target["id"]) or {}).get("id") != expect_invoice
    )):
        # Asked to collect ONE invoice and it is not the one these rules charge - a page
        # read before something else moved. Refused before a slot is spent: the row's
        # button is only offered on the invoice this picks, so it cannot pay another.
        # (The one exception is the replacement of the invoice they pick, mid-refresh - see
        # ``_replaced_by``: charging the pick collects on exactly that replacement.)
        return {"status": "not_this_invoice", "attempts": attempts,
                "invoice": expect_invoice, "reason": None}
    if target is None:
        # Something IS open, but nothing for the period they are behind on — an
        # abandoned renewal from a give-up, or a mid-period charge. Charging it would
        # take money and restore nothing, so it is reported rather than collected: the
        # decision to pursue old debt is not one a Pay-now button gets to make.
        logger.info(
            "dunning: payer {} pressed Pay now with only older debt open ({})",
            user_id, invoices[0]["id"],
        )
        return {"status": "older_debt_only", "attempts": attempts,
                "invoice": invoices[0]["id"], "reason": None}

    # No card => the charge CANNOT succeed, so it must not be attempted. Counting a slot
    # for it would spend the retry budget on a guaranteed decline, and every press would
    # spend another — the customer's actual next step is to add a card, which is the
    # button beside this one.
    if not customer_default_payment_method(account.stripe_customer_id):
        return {"status": "no_card", "attempts": attempts,
                "invoice": target["id"], "reason": None}

    # Counted BEFORE it runs, exactly as the scheduled path does: if this request dies
    # mid-retry the slot is spent rather than replayed. A double-charge is far worse
    # than a skipped retry.
    attempts = store.record_group_dunning_attempt(group.id)
    charged = _charge(group, target)
    invoice_id = charged.invoice["id"]
    refreshed = {"refreshed": charged.refreshed} if charged.refreshed else {}
    logger.info(
        "dunning: manual retry for payer {} invoice {} -> {} ({})",
        user_id, invoice_id,
        "paid" if charged.paid else "not collectable" if charged.stuck
        else "processor unavailable" if charged.unavailable else "failed",
        charged.reason,
    )
    if charged.unavailable:
        # Not a decline, and not the customer's to fix: the attempt is given back, and the
        # answer says so rather than "that card was declined".
        attempts = store.refund_group_dunning_attempt(group.id)
        return {"status": "unavailable", "attempts": attempts,
                "invoice": invoice_id, "reason": None}
    if charged.stuck:
        return {"status": "not_collectable", "attempts": attempts,
                "invoice": invoice_id, "reason": None}

    if charged.paid:
        # PAID - and answered as paid whatever becomes of recording it. A write failing here
        # used to turn a collected payment into a 500 the customer read as a failure.
        try:
            _settle_period(account, group, charged.invoice)
            _settle_handover(charged.invoice)
            # Only ends collection if it was running; a payer who paid an open invoice
            # without ever being dunned has nothing to clear - bar a SILENT grace, held while
            # the processor was failing (``store.hold_group_grace``), which ends here too.
            if started is not None:
                store.end_group_dunning(group.id, status="active")
            else:
                from billing.services import clock

                store.release_group_grace(group.id, clock.now())
        except Exception:
            logger.exception(
                "dunning: invoice {} for payer {} was PAID by Pay now, but recording it "
                "failed; the next pass settles it", invoice_id, user_id,
            )
        # Explicitly, and NOT only via ``end_group_dunning``. That call flips the module phase
        # back from past_due, but it is skipped entirely when there is no stamp — so a
        # payer with a real unpaid invoice and no dunning record (the case this function
        # goes out of its way to serve) paid, and was left switched off until the nightly
        # sweep. The scheduled path has always done this; the manual one only appeared to,
        # because dunning was normally running by the time anyone pressed the button.
        _restore_access(user_id)
        return {"status": "paid", "attempts": attempts,
                "invoice": invoice_id, "reason": charged.reason, **refreshed}

    return {"status": "failed", "attempts": attempts,
            "invoice": invoice_id, "reason": charged.reason, **refreshed}


def _collect_one_group(group, now, offsets, window) -> dict[str, list]:
    """Run one dunning cycle for ONE card. Returns the outcomes it produced.

    Four buckets rather than one because a single card can land in more than one --
    a charge that goes through on the wrong period is ``collected`` without being
    ``recovered``. They are append-only here and the caller extends its own lists
    with them, so the split cannot reorder or lose an outcome.

    Every ``continue`` in the loop this came from meant "nothing more to do for this
    card", which is a ``return`` once the body is a function.
    """
    from billing.services import billing_gateway, store
    from billing.services._log import logger

    out: dict[str, list] = {"retried": [], "recovered": [],
                            "collected": [], "given_up": []}
    account = _account_for(group)
    if account is None:
        logger.warning(
            "dunning: group {} has no billing account; skipping", group.id
        )
        return out
    user_id = account.user_id
    started = group.dunning_started_at
    attempts = int(group.dunning_attempts or 0)
    entry = {"user_id": user_id, "billing_group_id": group.id,
             "attempts": attempts}
    # The episode this entry belongs to, stamped now because ``end_group_dunning`` clears
    # ``dunning_started_at`` before the notification is composed. Without it a payer
    # who lapses, recovers, and lapses again months later would dedupe against the
    # first episode's email and hear nothing the second time.
    entry["_episode"] = (
        f"{user_id}:{group.id}:{started:%Y%m%dT%H%M%S}"
        if started else f"{user_id}:{group.id}"
    )
    # When THIS CARD's past-due access actually runs out. The SAME expression
    # ``access.access_end`` uses for a past-due module — ``paid_through`` plus the
    # window — so collection can never outlive entitlement however late the renewal
    # that started this ran. See ``give_up_at`` for the drift it closes.
    #
    # A card with no paid_through has never collected and cannot be past due on a
    # renewal; there is nothing to clamp against, so the unclamped deadline stands.
    access_ends_at = (
        group.paid_through + timedelta(days=window)
        if group.paid_through is not None
        else None
    )
    try:
        if should_give_up(now, started, window, access_ends_at):
            paid = _paid_at_the_deadline(account, group)
            if paid is not None or _paid_up(group, now):
                # Paid - elsewhere, or by a charge whose recovery was never recorded - just
                # as collection ran out. Closing it would cancel a card that has paid.
                _recover(out, account, group, entry, paid)
                return out
            store.end_group_dunning(group.id, status="closed")
            out["given_up"].append(entry)
            return out
        if not should_attempt_now(
            now, started, attempts, offsets, window, access_ends_at
        ):
            # Either not due yet, or the retries are spent and the account is
            # riding out the rest of the window — still recoverable if the customer
            # pays in the portal, so it stays in dunning until the deadline.
            return out

        invoices = _invoices_for_group(
            billing_gateway.open_invoices(account.stripe_customer_id),
            group,
            store.billing_groups_for_payer(user_id),
        )
        if not invoices:
            if _nothing_open_but_owed(account, group, now):
                # Not settled - there is no evidence it was (see there). No recovery, no
                # email, no attempt counted: the renewal pass raises or finishes what is
                # owed, and the deadline still closes the episode if nothing ever does.
                return out
            # Nothing outstanding on THIS card — it was settled elsewhere (a portal
            # payment, a manual charge). Collection has no reason to continue, but the
            # period it paid for still has to be recorded, or the customer has paid
            # and is locked out until the next renewal run notices.
            _recover(out, account, group, entry, None)
            return out

        # Which invoice would actually recover this card, decided BEFORE the charge:
        # afterwards the paid one is gone from the processor's open list and the
        # question cannot be asked again.
        current = _current_period_invoice(
            invoices, _current_period_key(account, group)
        )

        # The attempt is counted BEFORE it runs. If this process dies mid-retry the
        # slot is spent rather than replayed, which is the safe direction: a
        # double-charge is far worse than a skipped retry.
        store.record_group_dunning_attempt(group.id)
        # THE GROUP'S CURRENT CARD, not the one the invoice was raised against (see
        # ``_charge``). A payer whose card declined usually recovers by replacing it,
        # and the document still names the dead one.
        charged = _charge(group, invoices[0])
        if charged.unavailable:
            # The PROCESSOR failed, not the card (the user's rule, 2026-09-30): the attempt
            # is given back, nobody is told, and the next pass tries again. Counted, an
            # outage would spend the customer's retries - and give up on them - for us.
            store.refund_group_dunning_attempt(group.id)
            logger.error(
                "dunning: the payment processor failed for payer {} on group {}; no attempt "
                "counted, nothing sent", user_id, group.id,
            )
            return out
        target = charged.invoice
        if charged.refreshed and current is not None and current["id"] == charged.refreshed:
            # The invoice for the period they are behind on was re-issued: the
            # replacement IS that period's invoice now, so paying it recovers them.
            current = target
        paid = charged.paid
        entry["invoice"] = target["id"]
        entry["reason"] = charged.reason
        if charged.refreshed:
            entry["refreshed"] = charged.refreshed
        out["retried"].append(entry)

        if paid:
            # PAID, whatever becomes of recording it: it is never a failed retry, and never
            # mailed as one. Recording it is its own step - a write that fails now is
            # logged, and the next pass finds the card settled.
            entry["_paid"] = True
            try:
                # Advance BEFORE clearing dunning: this is what the customer just paid
                # for. Without it the money is collected and they stay unentitled until
                # the next monthly run adopts the invoice by its idempotency key — a
                # month of paying for nothing.
                _settle_period(account, group, target)
                # A handover's parked charge, collected here instead: finished as the
                # collection would have finished it.
                _settle_handover(target)
                # Collecting is not recovering. The oldest open invoice is what policy
                # charges, but the episode ends and access comes back only when the
                # CURRENT period is settled — otherwise a returning customer pays a
                # months-old bill, is told they are up to date, and is locked out again
                # by the next access sweep.
                #
                # ``current is None`` means nothing open belongs to the period they are
                # behind on: it was settled somewhere this code cannot see, so this IS
                # recovery. The alternative rule — recover only when NO invoice is open —
                # would let one uncollectable stale debt lock a paying customer out for
                # good.
                if current is None or current["id"] == target["id"]:
                    store.end_group_dunning(group.id, status="active")
                    _restore_access(user_id)
                    # Only once the episode has actually ended: the "Thank you" is sent
                    # for what is recorded, not for what was meant.
                    out["recovered"].append(entry)
                else:
                    entry["_collected"] = True
                    out["collected"].append(entry)
                    logger.info(
                        "dunning: payer {} paid stale invoice {}; current period {} "
                        "still open, staying in dunning",
                        user_id, target["id"], current["id"],
                    )
            except Exception:
                logger.exception(
                    "dunning: invoice {} for payer {} on group {} was PAID, but recording "
                    "it failed; the next pass settles it", target["id"], user_id, group.id,
                )
    except Exception:
        logger.exception(
            "dunning: cycle failed for payer {} on group {}", user_id, group.id
        )
    return out


def _recover(out, account, group, entry, invoice) -> None:
    """End THIS card's episode as recovered: the period it paid for recorded, collection
    cleared, access restored - and only then counted as ``recovered``, which is what mails
    the "Thank you". ``invoice`` is what paid, or None when it was settled somewhere this code
    cannot see (``_settle_period`` then leaves the period for the renewal pass to adopt)."""
    from billing.services import store

    _settle_period(account, group, invoice)
    store.end_group_dunning(group.id, status="active")
    _restore_access(account.user_id)
    out["recovered"].append(entry)


def _paid_up(group, now) -> bool:
    """Whether THIS card owes nothing any more: its period is paid past ``now`` - the renewal
    pass adopted a payment and moved it on - and no handover's first charge waits on it."""
    from billing.services import store

    return (group.paid_through is not None and group.paid_through > now
            and not store.handover_owed(group, now))


def _paid_at_the_deadline(account, group) -> dict | None:
    """The current period's invoice if it turns out PAID as collection runs out, else None.

    Asked before giving up, because giving up is the one outcome a late payment cannot undo:
    a card paid out of band on its last day - or by a charge whose recovery never got
    recorded - was closed, and the sweep then cancelled its companies. Raises when the
    processor cannot be read: nothing is closed on a guess, and the next pass asks again.
    """
    from billing.services import billing_gateway, store

    key = _current_period_key(account, group)
    record = store.invoice_for_key(key) if key else None
    if record is None or not record.external_id:
        return None
    found = billing_gateway.recheck(record)
    return found if found is not None and found.get("status") == "paid" else None


def _retry_context(user_id, entity_id, group_id=None):
    """Resolve WHICH card to collect on, or the reason there is nothing to collect.

    Returns ``(context, refusal)`` with exactly one of them set. The four refusals are
    the answers ``retry_now`` gives without charging anything -- no billing account, no
    card, the schedule already spent, or nothing owed -- and each is returned verbatim
    by the caller.

    It WRITES on two of those paths (closing a spent episode, and settling one that was
    paid somewhere this code cannot see). That is deliberate and stays here: both are
    part of deciding there is nothing to retry, not part of retrying.
    """
    from billing.services import billing_gateway, clock, policy, store

    account = store.customer_mapping_for_user(user_id)
    if account is None:
        return None, {"status": "nothing_owed", "attempts": 0,
                "invoice": None, "reason": None}

    # WHICH CARD'S DEBT. The button is pressed from one company's settings page, so the
    # debt to settle is the one on the card THAT company is billed to — not the payer's
    # oldest, which may belong to companies this person is not even looking at. Without an
    # entity the caller gets the card that has been in collection longest, which is the
    # one closest to being given up on.
    groups = store.billing_groups_for_payer(user_id)
    if group_id is not None:
        # The card named outright (the portal's invoice row). Only this payer's: an id
        # that is not one of theirs charges nothing rather than falling back to a card
        # nobody asked about.
        group = next((g for g in groups if str(g.id) == str(group_id)), None)
        if group is None:
            return None, {"status": "no_card", "attempts": 0,
                          "invoice": None, "reason": None}
    else:
        group = (
            store.billing_group_for_entity(entity_id, user_id) if entity_id else None
        )
    if group is None:
        in_dunning = [g for g in groups if g.dunning_started_at is not None]
        group = (
            min(in_dunning, key=lambda g: g.dunning_started_at)
            if in_dunning else (groups[0] if groups else None)
        )
    if group is None:
        # No card nominated for this company, so there is nothing to charge and nothing
        # to guess at — the customer's next step is to choose one, which is the control
        # beside this button.
        return None, {"status": "no_card", "attempts": 0, "invoice": None, "reason": None}

    now = clock.now()
    window = policy.current().past_due_window_days
    started = group.dunning_started_at
    attempts = int(group.dunning_attempts or 0)
    access_ends_at = (
        group.paid_through + timedelta(days=window)
        if group.paid_through is not None
        else None
    )

    # Only meaningful while collection is running: with no stamp there is no schedule to
    # have outrun, so there is no deadline to be past. A card that paid as it ran out is
    # recovered, not closed - the same re-check the scheduled run makes.
    if started is not None and should_give_up(now, started, window, access_ends_at):
        paid = _paid_at_the_deadline(account, group)
        if paid is not None or _paid_up(group, now):
            _settle_period(account, group, paid)
            _settle_handover(paid or {})
            store.end_group_dunning(group.id, status="active")
            _restore_access(user_id)
            return None, {"status": "nothing_owed", "attempts": attempts,
                          "invoice": None, "reason": None}
        store.end_group_dunning(group.id, status="closed")
        return None, {"status": "gave_up", "attempts": attempts,
                "invoice": None, "reason": None}

    invoices = _invoices_for_group(
        billing_gateway.open_invoices(account.stripe_customer_id), group, groups
    )
    if not invoices:
        # Nothing owed on this card. If collection was running it was settled somewhere
        # this code cannot see (a portal payment, a manual charge), so close it out and
        # record what it paid for — otherwise the customer has paid and stays locked out
        # until the next renewal run notices. Unless the period's invoice is a draft
        # nobody finalized: that is not settled, and the episode stays open for the
        # renewal pass to finish it (``_nothing_open_but_owed``).
        owed = _nothing_open_but_owed(account, group, now)
        if started is not None and not owed:
            _settle_period(account, group, None)
            store.end_group_dunning(group.id, status="active")
        return None, {"status": "nothing_owed", "attempts": attempts,
                "invoice": None, "reason": None}
    return {
        "account": account, "group": group, "started": started,
        "attempts": attempts, "invoices": invoices,
    }, None
