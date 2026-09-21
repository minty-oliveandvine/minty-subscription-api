"""``manage.py subscriptions <job>`` - the port of ``flask subscriptions``.

    tick                  what a cron job calls: the full pass at the full hour, the light
                          pass otherwise (Part 3 moves the trigger to a Render Cron Job
                          running exactly this; nothing else changes)
    run-daily             the pass, --mode full|light, dry unless --issue
    close-trials, run-renewals, retry-dunning, notify-trial-ending, sweep-access,
    reconcile-customers   the five jobs and the two utilities, one at a time
    revoke-ungranted      launch day: take access from modules no subscription backs,
                          dry unless --apply. REFUSES while dark.

EVERY JOB EXITS 0 AND DOES NOTHING WHILE ``SUBSCRIPTION_ENABLED`` IS OFF. That is the dark
contract for the command line: a cron job or a runbook step that fires against a dark
deployment must be a no-op, not a failure that pages somebody, and never a write. The one
exception is spelled out - ``revoke-ungranted`` says so and exits 1, because a launch-day
command that silently did nothing would leave the operator believing access was revoked.

Part 2 step 2 fills the job bodies from ``billing/services``; until then each says so and
exits 0 (or 1 for ``revoke-ungranted``, dark or not, since it cannot yet do its job).
"""

from __future__ import annotations

import sys

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

    def handle(self, *args, job: str, mode: str, issue: bool, apply: bool, **options):
        if job == "revoke-ungranted" and not settings.SUBSCRIPTION_ENABLED:
            # Not a no-op: the operator must know it did not run.
            raise CommandError(
                "revoke-ungranted refuses while SUBSCRIPTION_ENABLED is off - switch the "
                "API on first (launch day, step 8b), then run it dry, read, then --apply."
            )
        if not settings.SUBSCRIPTION_ENABLED:
            self.stdout.write(f"subscriptions {job}: dark (SUBSCRIPTION_ENABLED off) - nothing to do")
            return

        try:
            from billing.services import daily  # noqa: F401  (Part 2 step 2)
        except ImportError:
            self.stdout.write(
                f"subscriptions {job}: the engine is not ported yet (Part 2 step 2); nothing run"
            )
            if job == "revoke-ungranted":
                sys.exit(1)
            return

        # ---- Part 2 step 2 replaces this block with the dispatch table onto the jobs ----
        raise CommandError(f"subscriptions {job}: not wired yet (Part 2 step 2)")
