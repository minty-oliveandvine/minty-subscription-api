"""The subscription pass: the five maintenance jobs, in the one correct order.

Until this existed there was no scheduler anywhere in Minty — the jobs in
``cli/subscription_access.py`` ran only when a human typed them, which meant a trial past
``trial_end`` kept granting access indefinitely and ``paid_through`` never advanced, so
nobody was billed after their first period. This module is the thing that runs.

TWO PASSES, ONE PIECE OF CODE.

* the FULL pass, once a day, runs all five jobs;
* the LIGHT pass, every other hour, runs only the two whose lateness a customer feels —
  ``close-trials`` and ``run-renewals`` — and then sweeps ONLY the payers those two just
  touched.

The split exists because exactly one job is expensive. Four of them do work proportional
to what is DUE, which is usually nothing: a couple of indexed queries and out.
``sweep-access`` is the exception — it rebuilds the candidate set from every entity
holding an enabled or billed module and syncs each one, whether or not anything happened
to it. Running THAT hourly would pay for the whole customer base 24 times a day to enforce
windows measured in 15 and 30 days.

So the unscoped sweep stays daily, matching the scale of the boundaries only it can catch
(a past-due window expiring, a cancelled module's extension running out — dates that
elapse with no event behind them). The light pass instead calls
``sweep_expired_module_access(payer_user_id=...)`` for each payer it changed, which is the
pattern dunning already uses when a payment clears an episode. It is not optional: every
argument that the light pass's own steps handle their own access correctly is an argument
from "the other code paths get it right", and the sweep exists precisely to backstop that
assumption — its docstring lists three bugs where the assumption failed.

It is deliberately NOT a sixth job. It calls the same five services the CLI commands call,
and the order is the whole point:

    notify-trial-ending -> close-trials -> run-renewals -> retry-dunning -> sweep-access

* ``close-trials`` before ``run-renewals``, so a trial that converts today is billed by
  today's pass rather than waiting a month;
* ``retry-dunning`` after ``run-renewals``, so a renewal that fails this morning is
  already in dunning by the time the retry pass looks at it;
* **``sweep-access`` LAST**, which is the one place this differs from the order the CLI
  used to list, and it is not cosmetic. See below.

WHY THE SWEEP GOES LAST. It reconciles the access map against dates, and the date it
judges a paid module by is the payer's ``paid_through`` — which the renewal it used to run
BEFORE is the only thing that advances. Sharing one clock across the pass made that
concrete and monthly: a payer whose period elapsed since yesterday's pass is, at 02:00,
still ``active`` with a ``paid_through`` in the past, so ``access.grants_access`` says no
and the sweep revokes their modules — seconds before the renewal step bills them
successfully for the new period. Nothing re-syncs the map after a successful renewal (only
dunning does, on recovery), so access came back at the NEXT pass, a day later. Every payer,
every renewal: charged and locked out for twenty-four hours.

Running it last fixes that at the source rather than special-casing it. The sweep then
judges settled state: renewed payers have ``paid_through`` advanced so nothing is revoked,
genuinely failed ones are ``past_due`` and covered by the grace window, and anything
dunning recovered is restored in this pass instead of tomorrow's.

ONE JOB'S FAILURE DOES NOT STOP THE PASS. A broken sweep must not be the reason nobody
gets billed, and a mail outage must not be the reason a trial never ends. Each step is
caught, logged and recorded in the summary, and the pass continues.

With ONE exception, and it is the same rule as the ordering: ``sweep-access`` is SKIPPED
when a job that would have granted entitlement failed — ``close-trials`` or
``run-renewals``. A trial that did not convert is still sitting past its end date, and a
payer who was not billed still has a stale ``paid_through``; the sweep cannot tell either
from a customer who has genuinely lapsed, and would revoke both for a failure that is
simply going to be retried tomorrow. Skipping it for a day costs nothing — the access map
was already stale and stays stale one more day. Revoking a paying customer's access does
not cost nothing.

RUNNING IT TWICE IN A DAY IS SAFE, and that is a property of the jobs rather than of this
module: invoices are claimed under a unique ``idempotency_key`` before the charge, every
email is deduped in ``subscription_email_log``, dunning gates on the schedule rather than
on when it last ran, and the sweep re-derives the whole access map every pass. The
advisory lock below is therefore belt-and-braces, not the thing keeping the money right.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime

from billing.services._log import logger

# Every job in the pass, in call order. Names match the CLI commands so a line in the log
# and a command a human can type are the same string.
NOTIFY_TRIAL_ENDING = "notify-trial-ending"
CLOSE_TRIALS = "close-trials"
SWEEP_ACCESS = "sweep-access"
RUN_RENEWALS = "run-renewals"
RETRY_DUNNING = "retry-dunning"

# Not one of the five: the light pass's narrowed sweep, over just the payers this pass
# changed. Named distinctly so a log line never claims the whole access map was
# reconciled when only a handful of accounts were.
SWEEP_TOUCHED = "sweep-touched"

# Finish subscriber handovers whose accept got part-way and stopped. Normally a no-op:
# it reads a partial index that an accept which completed leaves empty. It exists for the
# one window the accept's ordering cannot remove — money collected, payer pointer not yet
# moved — because unlike a renewal, which recomputes its key on the next pass anyway, an
# accept is a one-shot user action that nothing would otherwise revisit.
REPAIR_TRANSFERS = "repair-transfers"

FULL = "full"
LIGHT = "light"

JOB_ORDER = (
    NOTIFY_TRIAL_ENDING,
    CLOSE_TRIALS,
    # BEFORE the renewal. A stranded handover has already been paid for, and completing
    # it writes the claim that keeps the entity off this run's invoice — left until
    # afterwards, the renewal would bill days the new payer has already settled.
    REPAIR_TRANSFERS,
    RUN_RENEWALS,
    RETRY_DUNNING,
    SWEEP_ACCESS,
)

# The hourly pass. Same two money jobs in the same relative order, then the scoped sweep —
# so the ordering rule that makes the full pass correct (a sweep only ever runs AFTER the
# renewal that advances ``paid_through``, on the same clock) holds here too, just narrowed
# to the payers involved.
LIGHT_ORDER = (
    CLOSE_TRIALS,
    REPAIR_TRANSFERS,
    RUN_RENEWALS,
    SWEEP_TOUCHED,
)

STEP_ORDER = {FULL: JOB_ORDER, LIGHT: LIGHT_ORDER}

# Both sweeps answer to the same skip rule.
SWEEP_STEPS = (SWEEP_ACCESS, SWEEP_TOUCHED)

# Jobs whose failure makes a sweep unsafe to run: both GRANT entitlement, and the sweep
# reads the state they would have written. Dunning is not one of them — a recovery it
# missed leaves the payer ``past_due``, which the grace window already covers, so the worst
# case there is access restored a day late rather than revoked a day early.
SWEEP_BLOCKERS = (CLOSE_TRIALS, RUN_RENEWALS)

# How many days ahead of ``trial_end`` the warning email goes out. Matches the CLI
# default; it is a notification window, not a money rule, so it does not live in
# ``billing_policy`` with the ones that are.
DEFAULT_DAYS_BEFORE = 3

# A stable 64-bit key for ``pg_try_advisory_lock``. Derived from a name rather than
# hard-coded so it is obvious what it belongs to, and from ``sha256`` rather than
# ``hash()`` because the built-in is salted per process — two gunicorn workers would
# compute two different keys and neither would ever block the other.
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"minty:subscriptions:run-daily").digest()[:8],
    "big",
    signed=True,
)


@contextmanager
def daily_lock():
    """Hold the cluster-wide "a daily pass is running" lock. Yields True if we got it.

    The Procfile runs gunicorn with two workers, and each one starts its own scheduler.
    Without this both fire at 02:00 and run the whole pass concurrently. The jobs survive
    that — see the module docstring — but two passes interleaving produce a log nobody can
    read, and they double every Stripe call for no benefit.

    ``pg_try_advisory_lock`` and not ``pg_advisory_lock``: the loser must SKIP, not queue.
    A second pass that waits and then runs the moment the first finishes is exactly the
    concurrent double-run this is meant to prevent, just sequenced.

    Taken on its OWN connection, not on ``db.session``. A session-level advisory lock is
    released when its connection returns to the pool, and the jobs commit repeatedly while
    they work — so a lock taken on the ORM session would quietly evaporate part-way
    through the pass, which is worse than no lock at all because it looks like one.

    On anything that is not Postgres (the SQLite test database) there is no advisory lock
    and no second worker either, so this yields True and gets out of the way.

    Under Django the "own connection" is a raw psycopg connection opened from the default
    alias's parameters (``connection.get_new_connection``), not the ORM's connection -
    the ORM's is what autocommit uses for every write in the pass, and a session-level
    lock on it would follow the connection back to the pool the same way.
    """
    from django.db import connection as orm_connection

    if orm_connection.vendor != "postgresql":
        logger.debug("subscriptions: no advisory lock on {}", orm_connection.vendor)
        yield True
        return

    raw = orm_connection.get_new_connection(orm_connection.get_connection_params())
    acquired = False
    try:
        with raw.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
            acquired = bool(cursor.fetchone()[0])
        yield acquired
    finally:
        try:
            if acquired:
                with raw.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        except Exception:
            # Closing the connection releases it anyway. Failing to unlock cleanly must
            # not turn a completed pass into a raised exception.
            logger.exception("subscriptions: could not release the daily lock")
        raw.close()


def _notify_trial_ending(now: datetime, *, days_before: int, issue: bool) -> dict:
    from billing.services.checkout import notify_trials_ending

    return notify_trials_ending(days_before=days_before)


def _close_trials(now: datetime, *, days_before: int, issue: bool) -> dict:
    from billing.services.checkout import convert_or_expire_due_trials

    return convert_or_expire_due_trials()


def _sweep_access(now: datetime, *, days_before: int, issue: bool) -> dict:
    from billing.services.access_sweep import sweep_expired_module_access

    return sweep_expired_module_access()


def _run_renewals(now: datetime, *, days_before: int, issue: bool) -> dict:
    from billing.services.renewals import ALL_PAYERS, run_renewals

    # ``ALL_PAYERS`` is typed here, once, on purpose. ``run_renewals`` has no default
    # scope precisely so that billing the entire customer base cannot be reached by
    # leaving an argument out — a harness driving an injected clock once charged a live
    # payer for catch-up periods that way. A scheduled pass genuinely does mean everybody,
    # so it says so.
    return run_renewals(now, scope=ALL_PAYERS, issue=issue)


def _repair_transfers(now: datetime, *, days_before: int, issue: bool) -> dict:
    from billing.services.transfers import repair_stranded

    # Honours no ``issue`` gate because it CHARGES NOTHING. It finishes a handover from
    # money already collected, or asks the processor about a reservation and releases it;
    # gating that behind the billing switch would leave a paid-for company stranded on
    # every shadow run.
    return repair_stranded(now)


def _retry_dunning(now: datetime, *, days_before: int, issue: bool) -> dict:
    from billing.services.dunning import collect_due

    return collect_due(now)


_RUNNERS = {
    NOTIFY_TRIAL_ENDING: _notify_trial_ending,
    CLOSE_TRIALS: _close_trials,
    SWEEP_ACCESS: _sweep_access,
    RUN_RENEWALS: _run_renewals,
    RETRY_DUNNING: _retry_dunning,
    REPAIR_TRANSFERS: _repair_transfers,
}


def _payers_for_entities(entity_ids: set[str]) -> set[str]:
    """Which payers own these entities. One lookup each — the light pass touches few.

    ``convert_or_expire_due_trials`` reports what it did by ENTITY, because that is the
    unit a trial converts in. The sweep narrows by PAYER, because that is the unit a
    billing cycle belongs to. This is the join between them, and it is the only reason the
    scoped sweep needs any plumbing at all.
    """
    from billing.services import store

    payers: set[str] = set()
    for entity_id in entity_ids:
        try:
            for row in store.module_rows_for_entity(entity_id):
                payer = getattr(row, "payer_user_id", None)
                if payer:
                    payers.add(str(payer))
                    break
        except Exception:
            # A payer we fail to resolve is simply swept by the next full pass instead.
            # Losing the narrowed sweep for one entity must not fail the whole pass.
            logger.exception("subscriptions: could not resolve the payer for {}", entity_id)
    return payers


def _touched_payers(summaries: dict) -> set[str]:
    """Every payer whose entitlement this pass may have moved.

    Renewals report by payer already. Trials report by entity, so those are resolved.
    Deliberately WIDE: a payer listed here is swept, and sweeping a payer nothing happened
    to costs one narrowed reconciliation, while missing one leaves their access map stale
    until the next full pass.
    """
    payers: set[str] = set()

    renewals = summaries.get(RUN_RENEWALS) or {}
    for bucket in ("issued", "failed", "skipped"):
        for entry in renewals.get(bucket, []) or []:
            user_id = entry.get("user_id")
            if user_id:
                payers.add(str(user_id))

    trials = summaries.get(CLOSE_TRIALS) or {}
    entity_ids = {
        str(entry["entity_id"])
        for bucket in ("converted", "expired")
        for entry in trials.get(bucket, []) or []
        if entry.get("entity_id")
    }
    return payers | _payers_for_entities(entity_ids)


def _sweep_touched(payer_ids: set[str]) -> dict:
    """Reconcile access for just these payers. Same shape as the unscoped sweep.

    One payer failing does not stop the rest: their map is repaired by the next full pass,
    which is exactly the guarantee the full pass exists to provide.
    """
    from billing.services.access_sweep import sweep_expired_module_access

    disabled: list[dict] = []
    restored: list[dict] = []
    for payer_id in sorted(payer_ids):
        try:
            summary = sweep_expired_module_access(payer_user_id=payer_id) or {}
        except Exception:
            logger.exception("subscriptions: scoped sweep failed for payer {}", payer_id)
            continue
        disabled.extend(summary.get("disabled", []))
        restored.extend(summary.get("restored", []))
    return {"payers": sorted(payer_ids), "disabled": disabled, "restored": restored}


def _counts(summary: dict) -> dict:
    """``{"issued": [...], "failed": []}`` -> ``{"issued": 3, "failed": 0}``.

    The pass logs SHAPES, not payloads. A renewal run returns every invoice line it built;
    printing that once a day buries the one number anybody scans the log for.
    """
    return {
        key: len(value) if isinstance(value, (list, tuple, set, dict)) else value
        for key, value in (summary or {}).items()
    }


def _log_renewal_backlog(now: datetime, result: dict) -> list[str]:
    """Warn about payers who are STILL due after being billed — i.e. more than a period
    behind — and return their ids.

    ``run_renewals`` bills exactly ONE period per payer per run: ``next_period`` is derived
    from the anchor and whatever ``paid_through`` currently says. That is the right
    behaviour, but it means an account whose ``paid_through`` is three months stale is
    caught up over three consecutive daily passes, each one a real charge to a real card.
    Nothing else in the system says so out loud, and "why did this customer get billed
    three times in three days" is not a question to answer from first principles at the
    time it is asked.

    Payers whose charge FAILED are excluded: they are still due for the obvious reason,
    they are already reported as failures, and they are dunning's problem now.
    """
    from billing.services.renewals import due_renewals

    failed = {str(item["user_id"]) for item in result.get("failed", [])}
    behind = [
        str(account.user_id)
        for account, _paid_through in due_renewals(now)
        if str(account.user_id) not in failed
    ]
    if behind:
        logger.warning(
            "subscriptions: {} payer(s) are still due after this pass and will be billed "
            "again tomorrow - they are more than one period behind: {}",
            len(behind),
            ", ".join(behind),
        )
    return behind


def run_daily(
    now: datetime,
    *,
    issue: bool,
    mode: str = FULL,
    days_before: int = DEFAULT_DAYS_BEFORE,
) -> dict:
    """Run a pass. Returns a summary; never raises for a job that failed.

    ``mode`` is ``FULL`` (all five, ending in the unscoped sweep) or ``LIGHT`` (the two
    money jobs, then a sweep narrowed to the payers they touched). See the module
    docstring for why the expensive sweep is the one that stays daily.

    ``issue`` has NO default — the same rule ``run_renewals`` applies to ``scope``. Every
    caller states what it wants, so a scheduler that is meant to bill and a hand-run that
    is meant to look cannot be confused for one another by omission.

    It gates the RENEWAL step and nothing else, which is narrower than it sounds: with it
    off the pass still converts due trials (cutting an invoice and charging the card),
    still expires the ones with no card, still revokes and restores access, and still
    lets dunning retry invoices already owed. ``issue=False`` is a smaller pass, not a
    read-only one.

    ``now`` is passed in rather than read here so the pass is testable against a fixed
    clock, and so every step in one pass agrees about what time it is — which is not a
    nicety: the sweep and the renewal MUST judge ``paid_through`` against the same instant
    or the sweep revokes a payer the renewal is about to bill.
    """
    steps = STEP_ORDER.get(mode)
    if steps is None:
        raise ValueError(f"unknown pass mode {mode!r}; expected {FULL!r} or {LIGHT!r}")

    results: list[dict] = []
    summaries: dict[str, dict] = {}
    failed_jobs: set[str] = set()
    backlog: list[str] = []

    for name in steps:
        blockers = sorted(failed_jobs.intersection(SWEEP_BLOCKERS))
        if name in SWEEP_STEPS and blockers:
            # The one ordering rule with teeth — see the module docstring.
            logger.warning(
                "subscriptions: skipping {} because {} failed; entitlement those jobs "
                "would have granted is missing, and the sweep would read it as lapsed",
                name,
                " and ".join(blockers),
            )
            results.append({"job": name, "ok": True, "skipped": True,
                            "reason": f"{' and '.join(blockers)} failed"})
            continue

        try:
            if name == SWEEP_TOUCHED:
                summary = _sweep_touched(_touched_payers(summaries))
            else:
                summary = _RUNNERS[name](now, days_before=days_before, issue=issue)
        except Exception as exc:
            # (Flask rolled its session back here so the next job started clean; under
            # Django's autocommit a failed job leaves nothing staged.)
            failed_jobs.add(name)
            logger.exception("subscriptions: {} failed", name)
            results.append({"job": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue

        results.append({"job": name, "ok": True, "summary": summary})
        summaries[name] = summary
        logger.info("subscriptions: {} {}", name, _counts(summary))
        if name == RUN_RENEWALS and issue:
            backlog = _log_renewal_backlog(now, summary)

    ok = not failed_jobs
    log = logger.info if ok else logger.error
    log(
        "subscriptions: {} pass finished - {}/{} step(s) ok, issue={}{}",
        mode,
        len(steps) - len(failed_jobs),
        len(steps),
        issue,
        f", failed: {', '.join(sorted(failed_jobs))}" if failed_jobs else "",
    )
    return {"ok": ok, "mode": mode, "issue": issue, "jobs": results,
            "failed": sorted(failed_jobs), "behind": backlog}
