"""In-process scheduler for the subscription passes - the port of Minty's
``services/app_runtime/scheduler.py``, kept in-process by decision (2026-09-21: "use
in-process until Terraform arrives").

TWO JOBS, ONE FUNCTION. The FULL pass runs once a day and does everything, including the
unscoped access sweep. The LIGHT pass runs every OTHER hour and does only the two jobs a
customer feels the lateness of - closing trials and raising renewals - then sweeps just
the payers it touched. See ``billing/services/daily.py`` for the reasoning.

Everything below is about the ways an in-process timer gets this wrong.

**Two workers, one pass.** gunicorn runs several workers and each starts its own scheduler;
both fire at the same minute. The pass takes a Postgres advisory lock and the loser skips -
``daily.daily_lock``. The lock lives with the jobs rather than here because it is what makes
the pass safe from ANY caller, including a human running ``manage.py subscriptions
run-daily`` while the timer is mid-flight.

**A scope per pass.** The engine's per-request memos (``billing.services._context``: the
clock, the policy row, the catalog, the Stripe default-card answer) are what Flask's
``app.app_context()`` gave the timer; ``run_pass_now`` opens one around each pass, on the
job thread, and closes stale database connections either side of it.

**Off unless asked, twice.** ``SUBSCRIPTION_ENABLED`` outranks everything: dark, there is
nothing to convert, renew or retry, and a timer that charged anyway would be the one thing
the switch exists to make impossible. ``SUBSCRIPTION_SCHEDULER_ENABLED`` is the timer's
own switch, unset by default so importing the app in a test or a shell bills nobody.

**A missed run costs an hour, except for the full pass.** The job store is in memory, so a
process starting at 09:30 schedules its next run for 10:00. The FULL pass can lose a DAY: a
deploy after 05:00 HKT means no unscoped sweep and no dunning retries until 05:00 tomorrow.
Day-scale by nature (a 15-day window, whole-day retry offsets), so absorbed - but a paused
instance runs nothing at all. That is why ``manage.py subscriptions tick`` exists: Part 3's
Terraform moves the trigger to a Render Cron Job with no code change, and the pass does not
care who calls it.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger("billing-api")

FULL_JOB_ID = "subscriptions-full"
LIGHT_JOB_ID = "subscriptions-light"

#: The two pass modes; ``billing.services.daily`` spells the same words in step 2.
FULL = "full"
LIGHT = "light"


def run_pass_now(*, mode: str) -> dict | None:
    """One pass, under the lock. Returns None if another holder was already running one,
    or if subscriptions are dark.

    Both the full and the light job call this, and they share ONE lock rather than having
    one each - a light pass overlapping the full pass would put a narrowed sweep and an
    unscoped sweep on two different clocks.

    Never raises: this is what the timer calls, and an exception escaping a scheduled job
    is how a scheduler quietly stops being one. ``issue=True`` is not configurable - a
    scheduler that is switched on and silently not charging is indistinguishable from one
    that is working; stopping every charge means SUBSCRIPTION_SCHEDULER_ENABLED off.
    """
    if not settings.SUBSCRIPTION_ENABLED:
        logger.info("subscriptions: dark - the %s pass does nothing", mode)
        return None
    from django.db import close_old_connections

    from billing.services import _context, clock, daily

    # This runs on APScheduler's worker thread, which has its own ContextVar context (so
    # the request scope below is fresh, not a leftover) and its own thread-local database
    # connection - one that has sat idle since the last pass and may have been closed by
    # the server in the meantime. ``close_old_connections`` at entry drops a dead one
    # before the first query; at exit it releases what the pass opened rather than
    # holding a connection for the next hour.
    close_old_connections()
    try:
        with _context.scope(), daily.daily_lock() as acquired:
            if not acquired:
                logger.info(
                    "subscriptions: another pass is already running; skipping the %s pass", mode
                )
                return None
            # Read inside the lock: ``clock.now`` is the DATABASE's time, never this host's.
            return daily.run_daily(clock.now(), issue=True, mode=mode)
    except Exception:
        logger.exception("subscriptions: the %s pass raised", mode)
        return None
    finally:
        close_old_connections()


def start_scheduler():
    """Start the timer if this process is meant to have one. Returns it, or None.

    Called from ``billing.apps.BillingConfig.ready`` (which has already ruled out
    management commands and the autoreloader parent). Returning None is the normal case:
    only the deployed web service sets ``SUBSCRIPTION_SCHEDULER_ENABLED``.
    """
    if not settings.SUBSCRIPTION_ENABLED:
        logger.info("scheduler: not started - subscriptions are dark (SUBSCRIPTION_ENABLED)")
        return None
    if not settings.SUBSCRIPTION_SCHEDULER_ENABLED:
        logger.debug("scheduler: disabled (SUBSCRIPTION_SCHEDULER_ENABLED is not set)")
        return None

    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    full_hour = settings.SUBSCRIPTION_SCHEDULER_FULL_HOUR % 24
    timezone = pytz.timezone(settings.SUBSCRIPTION_SCHEDULER_TZ)

    scheduler = BackgroundScheduler(timezone=timezone, daemon=True)
    common = {
        # A pass still running when the next one is due must not start a second one. The
        # lock would refuse it anyway; this refuses it a step earlier, without a round trip.
        "max_instances": 1,
        # Suspended over several fire times: run ONCE on waking, not once per missed hour.
        "coalesce": True,
        # Late is still worth running: a pass that starts at 05:20 because the host was busy
        # does everything an 05:00 one would. Beyond that, wait for the next slot.
        "misfire_grace_time": 1800,
    }
    scheduler.add_job(
        run_pass_now,
        trigger=CronTrigger(hour=full_hour, minute=0, timezone=timezone),
        kwargs={"mode": FULL},
        id=FULL_JOB_ID,
        name="Full subscription pass",
        **common,
    )
    if settings.SUBSCRIPTION_SCHEDULER_LIGHT:
        # Every hour EXCEPT the full one, spelled out: two jobs firing in the same minute
        # would take the same lock and one would silently lose.
        light_hours = ",".join(str(h) for h in range(24) if h != full_hour)
        scheduler.add_job(
            run_pass_now,
            trigger=CronTrigger(hour=light_hours, minute=0, timezone=timezone),
            kwargs={"mode": LIGHT},
            id=LIGHT_JOB_ID,
            name="Light subscription pass",
            **common,
        )
    scheduler.start()
    logger.info(
        "scheduler: subscription passes %s %s - full pass at %02d:00, billing live",
        "hourly" if settings.SUBSCRIPTION_SCHEDULER_LIGHT else "daily only",
        timezone,
        full_hour,
    )
    return scheduler
