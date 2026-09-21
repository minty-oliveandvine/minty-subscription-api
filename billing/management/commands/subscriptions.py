"""``manage.py subscriptions <job>`` - the port of ``flask subscriptions``
(Minty's ``cli/subscription_access.py``, one job per verb, ASCII output).

    tick                  what a cron job calls: the full pass at the full hour, the light
                          pass otherwise (Part 3 moves the trigger to a Render Cron Job
                          running exactly this; nothing else changes)
    run-daily             the pass, --mode full|light, the RENEWAL step dry unless --issue
                          (--days-before N for the trial warning window)
    close-trials          end app-level trials whose term is up (--limit N)
    run-renewals          bill payers whose period has ended; DRY unless --issue
                          (--user ID, repeatable; --limit N)
    retry-dunning         retry declined renewals on schedule (--limit N)
    notify-trial-ending   warn payers whose trial ends in --days-before days (default 3)
    sweep-access          reconcile module access with the subscriptions
    reconcile-customers   report (or --repair) payers whose Stripe customer has no mapping
    revoke-ungranted      launch day: take access from modules no subscription backs,
                          dry unless --apply. REFUSES while dark.

EVERY JOB EXITS 0 AND DOES NOTHING WHILE ``SUBSCRIPTION_ENABLED`` IS OFF. That is the dark
contract for the command line: a cron job or a runbook step that fires against a dark
deployment must be a no-op, not a failure that pages somebody, and never a write. The one
exception is spelled out - ``revoke-ungranted`` says so and exits 1, because a launch-day
command that silently did nothing would leave the operator believing access was revoked.

Every job runs inside a request scope (``billing.services._context.scope()``): the engine's
per-request memos - the clock, the policy row, the price catalog, the Stripe default-card
answer - are what Flask's ``app.app_context()`` gave these commands, and without a scope
each job would fall back to the host clock and re-read the catalog per row.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from billing.scheduler import FULL, LIGHT

JOBS = (
    "tick",
    "run-daily",
    "close-trials",
    "run-renewals",
    "retry-dunning",
    "notify-trial-ending",
    "sweep-access",
    "reconcile-customers",
    "revoke-ungranted",
)


def _count(value) -> str:
    return str(len(value) if isinstance(value, (list, tuple, set, dict)) else value)


class Command(BaseCommand):
    help = "Run one of the subscription jobs (see the module docstring)."

    def add_arguments(self, parser):
        parser.add_argument("job", choices=JOBS)
        parser.add_argument("--mode", choices=(FULL, LIGHT), default=FULL,
                            help="run-daily only: which pass (default full)")
        parser.add_argument("--issue", action="store_true",
                            help="run-daily / run-renewals: really charge (dry by default)")
        parser.add_argument("--apply", action="store_true",
                            help="revoke-ungranted: really revoke (dry by default)")
        parser.add_argument("--repair", action="store_true",
                            help="reconcile-customers: write the missing mapping rows")
        parser.add_argument("--limit", type=int, default=None,
                            help="close-trials / run-renewals / retry-dunning / notify-trial-ending: at most N")
        parser.add_argument("--days-before", type=int, default=None, dest="days_before",
                            help="notify-trial-ending (default 3) / run-daily: the trial warning window")
        parser.add_argument("--user", action="append", dest="users", default=[],
                            help="run-renewals: bill only these payer ids (repeatable)")

    def handle(self, *args, job: str, mode: str, issue: bool, apply: bool, repair: bool,
               limit, days_before, users, **options):
        if job == "revoke-ungranted" and not settings.SUBSCRIPTION_ENABLED:
            # Not a no-op: the operator must know it did not run.
            raise CommandError(
                "revoke-ungranted refuses while SUBSCRIPTION_ENABLED is off - switch the "
                "API on first (launch day, step 8b), then run it dry, read, then --apply."
            )
        if not settings.SUBSCRIPTION_ENABLED:
            self.stdout.write(f"subscriptions {job}: dark (SUBSCRIPTION_ENABLED off) - nothing to do")
            return

        from billing.services import _context

        runner = getattr(self, "_job_" + job.replace("-", "_"))
        with _context.scope():
            runner(mode=mode, issue=issue, apply=apply, repair=repair, limit=limit,
                   days_before=days_before, users=users)

    # ---- the pass -------------------------------------------------------------------------

    def _job_tick(self, *, mode, **_):
        """What a cron job calls. Picks the pass the scheduler would have run at this hour
        (the full pass at SUBSCRIPTION_SCHEDULER_FULL_HOUR, the light one otherwise) and runs
        it under the lock, issuing. Exits 0 whether or not there was anything to do."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from billing import scheduler

        hour = datetime.now(ZoneInfo(settings.SUBSCRIPTION_SCHEDULER_TZ)).hour
        chosen = FULL if hour == settings.SUBSCRIPTION_SCHEDULER_FULL_HOUR % 24 else LIGHT
        result = scheduler.run_pass_now(mode=chosen)
        if result is None:
            self.stdout.write(f"tick: {chosen} pass not run (another holder, or it raised)")
            return
        self._report_pass(result, issue=True)

    def _job_run_daily(self, *, mode, issue, days_before, **_):
        """The pass the scheduler runs, runnable by hand.

        THIS IS NOT A DRY RUN WITHOUT ``--issue``: the flag gates ONE of the jobs (the
        renewal). The other jobs always do their real work, and one of them spends money -
        ``close-trials`` converts a due trial to paid, which cuts an invoice and charges the
        card on file. To see what tonight will bill without touching anything, run
        ``run-renewals`` on its own. Safe to run while the scheduler is mid-pass: same lock.
        """
        from billing.services import clock, daily

        kwargs = {} if days_before is None else {"days_before": days_before}
        with daily.daily_lock() as acquired:
            if not acquired:
                self.stdout.write("A pass is already running elsewhere. Nothing done.")
                return
            result = daily.run_daily(clock.now(), issue=issue, mode=mode, **kwargs)
        self._report_pass(result, issue=issue)

    def _report_pass(self, result: dict, *, issue: bool) -> None:
        if not issue:
            self.stdout.write(
                "No --issue: the renewal step only reported. The other jobs ran for real, "
                "and close-trials charges converting trials."
            )
        for entry in result["jobs"]:
            if entry.get("skipped"):
                self.stdout.write(f"  skipped {entry['job']}: {entry['reason']}")
            elif entry["ok"]:
                counts = ", ".join(
                    f"{key} {_count(value)}" for key, value in sorted((entry["summary"] or {}).items())
                )
                self.stdout.write(f"  ok      {entry['job']}: {counts or 'nothing to do'}")
            else:
                self.stdout.write(f"  FAILED  {entry['job']}: {entry['error']}")
        if result.get("behind"):
            # The one thing a summary of counts cannot show: these payers were billed for a
            # period and are STILL due, so tomorrow's pass charges them again.
            self.stdout.write(
                f"  {len(result['behind'])} payer(s) more than one period behind; they will "
                f"be billed again tomorrow: {', '.join(result['behind'])}"
            )
        self.stdout.write("Daily pass complete." if result["ok"] else "Daily pass FINISHED WITH ERRORS.")

    # ---- the jobs, one at a time ------------------------------------------------------

    def _job_close_trials(self, *, limit, **_):
        from billing.services.checkout import convert_or_expire_due_trials

        summary = convert_or_expire_due_trials(limit=limit)
        converted, expired = summary["converted"], summary["expired"]
        self.stdout.write(f"Converted {len(converted)} trial(s) to paid; expired {len(expired)}.")
        for item in converted:
            self.stdout.write(f"  converted: {item['code']} (entity {item['entity_id']})")
        for item in expired:
            self.stdout.write(f"  expired:   {item['code']} (entity {item['entity_id']})")

    def _job_run_renewals(self, *, issue, users, limit, **_):
        """The monthly charge, which nothing else performs. DRY BY DEFAULT: ``--issue`` is
        the only thing that moves money. ``scope`` has no default in ``run_renewals``:
        billing everyone has to be TYPED, so "every payer" is an explicit request here."""
        from billing.services import clock
        from billing.services.renewals import ALL_PAYERS, run_renewals

        scope = list(users) if users else ALL_PAYERS
        result = run_renewals(clock.now(), scope=scope, issue=issue, limit=limit)
        if not issue:
            # ASCII only: these run under cron on a Windows host whose console is cp1252.
            self.stdout.write(f"DRY RUN - nothing charged. {len(result['planned'])} payer(s) due:")
            for item in result["planned"]:
                self.stdout.write(
                    f"  would bill {item['total'] / 100:,.2f} to {item['user_id']} "
                    f"({item['period_start']:%d %b} - {item['period_end']:%d %b %Y})"
                )
        else:
            self.stdout.write(
                f"Billed {len(result['issued'])}; {len(result['failed'])} failed, "
                f"{len(result['skipped'])} skipped."
            )
            for item in result["issued"]:
                self.stdout.write(f"  paid    {item['total'] / 100:,.2f}  {item['user_id']}  {item['invoice']}")
        # Failures are the ones that need eyes: each has started dunning and will be retried
        # by ``retry-dunning``, but a run where everything fails is a broken card processor,
        # not fifteen broken cards.
        for item in result["failed"]:
            self.stdout.write(f"  FAILED  {item['total'] / 100:,.2f}  {item['user_id']}  {item.get('status')}")
        for item in result["skipped"]:
            self.stdout.write(f"  skipped {item['user_id']}: {item['reason']}")

    def _job_retry_dunning(self, *, limit, **_):
        """No dry mode: it retries invoices ALREADY issued and owed, so it cannot bill
        anything new. Safe at any cadence - the schedule is keyed off the first failure."""
        from billing.services import clock
        from billing.services.dunning import collect_due

        result = collect_due(clock.now(), limit=limit)
        self.stdout.write(
            f"Retried {len(result['retried'])}; recovered {len(result['recovered'])}; "
            f"gave up on {len(result['given_up'])}."
        )
        for item in result["recovered"]:
            self.stdout.write(f"  recovered: {item['user_id']}")
        for item in result["given_up"]:
            self.stdout.write(f"  gave up:   {item['user_id']} after {item['attempts']} attempt(s)")

    def _job_notify_trial_ending(self, *, days_before, limit, **_):
        from billing.services.checkout import notify_trials_ending

        result = notify_trials_ending(days_before=3 if days_before is None else days_before, limit=limit)
        warned = result["warned"]
        self.stdout.write(f"Warned {len(warned)} payer(s) about a trial ending.")
        for item in warned:
            self.stdout.write(f"  warned: {item['entity_id']} ({', '.join(item.get('codes') or [])})")

    def _job_sweep_access(self, **_):
        from billing.services.access_sweep import sweep_expired_module_access

        summary = sweep_expired_module_access()
        disabled = summary["disabled"]
        self.stdout.write(f"Disabled {len(disabled)} module(s) past their access grace.")
        for item in disabled:
            self.stdout.write(f"  - {item['code']} (entity {item['entity_id']})")
        # Restorations are printed even when there are none. A silent zero and a job that
        # cannot restore at all look identical in a cron log, and telling those apart is the
        # whole point of the line.
        restored = summary.get("restored", [])
        self.stdout.write(f"Restored {len(restored)} module(s) whose entitlement returned.")
        for item in restored:
            self.stdout.write(f"  + {item['code']} (entity {item['entity_id']})")

    def _job_reconcile_customers(self, *, repair, **_):
        """Detect (and optionally repair) lost payer->customer mappings. Also reports the
        case repair cannot fix: TWO Stripe customers stamped with the same ``user_id``."""
        from collections import defaultdict

        from billing.services import store
        from billing.services.stripe_client import get_stripe
        from shared_models.models import UserStripeCustomer

        mapped = {row.stripe_customer_id: row.user_id for row in UserStripeCustomer.objects.all()}
        try:
            stripe = get_stripe()
        except RuntimeError as exc:  # no STRIPE_SECRET_KEY: say so, exit 1
            raise CommandError(str(exc)) from exc
        by_user: dict[str, list[str]] = defaultdict(list)
        for customer in stripe.Customer.list(limit=100).auto_paging_iter():
            user_id = (customer.get("metadata") or {}).get("user_id")
            if user_id:
                by_user[user_id].append(customer["id"])

        orphans = [
            (user_id, ids[0]) for user_id, ids in by_user.items() if len(ids) == 1 and ids[0] not in mapped
        ]
        ambiguous = {u: ids for u, ids in by_user.items() if len(ids) > 1}

        self.stdout.write(
            f"{len(mapped)} mapping row(s); {sum(len(v) for v in by_user.values())} "
            f"stamped Stripe customer(s); {len(orphans)} unmapped."
        )
        for user_id, customer_id in orphans:
            self.stdout.write(f"  unmapped: {user_id} -> {customer_id}")
        for user_id, ids in ambiguous.items():
            self.stdout.write(f"  AMBIGUOUS: {user_id} is stamped on {len(ids)}: {', '.join(ids)}")

        if not orphans:
            self.stdout.write("Nothing to repair." if not ambiguous else "No repairable orphans.")
        elif not repair:
            self.stdout.write("Reported only - pass --repair to write these rows.")
        else:
            for user_id, customer_id in orphans:
                store.upsert_customer_mapping(user_id, customer_id)
            self.stdout.write(f"Repaired {len(orphans)} mapping row(s).")

        if ambiguous:
            # Not repairable here on purpose: picking one would silently strand whatever
            # card and history sit on the other.
            self.stdout.write(
                f"{len(ambiguous)} payer(s) have more than one Stripe customer; merge and "
                "delete the duplicates in Stripe by hand."
            )

    def _job_revoke_ungranted(self, *, apply, **_):
        """LAUNCH DAY: the m1a01 step the cutover skipped while subscriptions were dark.
        Dry by default; --apply writes. Grants nothing, starts nothing."""
        from billing.services.access_sweep import revoke_ungranted_module_access

        hits = revoke_ungranted_module_access(dry_run=not apply)
        verb = "Switched off" if apply else "Would switch off"
        self.stdout.write(f"{verb} {len(hits)} module grant(s) with no subscription row.")
        for item in hits:
            self.stdout.write(f"  - {item['code']} (entity {item['entity_id']})")
