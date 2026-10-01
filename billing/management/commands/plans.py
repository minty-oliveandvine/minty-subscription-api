"""``manage.py plans list`` - the port of ``flask plans list``: the price catalog as the
database holds it. Read-only; the catalog is edited by hand in SQL and this is how you check
it. Writes nothing, so it is also the quickest proof that the service reaches the database
and the schema."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from shared_models.models import BillingPlan


class Command(BaseCommand):
    help = "List the billing plans (billing_plan)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("list",))
        parser.add_argument("--all", action="store_true", help="include inactive plans")

    def handle(self, *args, action: str, all: bool, **options):  # noqa: A002 - CLI word
        rows = BillingPlan.objects.order_by("code")
        if not all:
            rows = rows.filter(is_active=True)
        plans = list(rows)
        if not plans:
            self.stdout.write("no plans")
            return
        width = max(len(p.code) for p in plans)
        for p in plans:
            flag = "" if p.is_active else "  (inactive)"
            self.stdout.write(
                f"{p.code:<{width}}  {p.display_name:<24}  {p.amount:>8} {p.currency}"
                f"  every {p.interval_months} month(s){flag}"
            )
