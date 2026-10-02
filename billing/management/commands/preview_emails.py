"""``manage.py preview_emails`` - render every email, to files for review or to a real inbox.

    python manage.py preview_emails                          # HTML files in _email_preview/
    python manage.py preview_emails --send you@work.com      # real messages
    python manage.py preview_emails --only onboarding_reminder --send you@work.com

The port of Minty's ``scripts/subscription/preview_billing_emails.py``, and for the same
reason it never calls ``notify.notify``: that CLAIMS a row in ``subscription_email_log``
before sending, and a review send against a real id would spend the claim the customer's
real email needed. This builds the same message (``notify.build_message``) from FIXTURES
and writes nothing to the database.

Files get their images as ``data:`` URIs so a browser shows them; a send attaches them as
CID parts, which is what has to survive a real mail client.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ENTITY = "Aetheria Capital Limited"
#: No such company: the links in a preview open Minty's "not found" page, never a real one.
DEMO_ENTITY_ID = "00000000-0000-4000-8000-000000000001"


def contexts() -> dict[str, dict]:
    """One representative context per event, in the order a customer might meet them."""
    from billing.services import clock, notify

    now = clock.now()
    zone = notify.zone_named(None)
    portal = notify.portal_url()
    company = {"entity_id": DEMO_ENTITY_ID, "entity_name": ENTITY, "zone": zone}
    transfer = {**company, "from_name": "Rebecca Park", "to_name": "John Doe",
                "portal_url": portal}
    return {
        notify.ONBOARDING_REMINDER: {**company, "saved_step": 2},
        notify.TRIAL_ENDING: {
            **company, "codes": ["PETTY_CASH", "PAYMENT_REQUEST"],
            "trial_end": now + timedelta(days=7), "amount": 28000, "currency": "HKD",
            "needs_card": True, "needs_consent": False,
        },
        notify.RENEWAL_FAILED: {"total": 68000, "currency": "HKD", "zone": zone,
                                "deadline": now + timedelta(days=14)},
        notify.DUNNING_RETRY_FAILED: {"attempts": 2, "zone": zone,
                                      "deadline": now + timedelta(days=9)},
        notify.PAYMENT_RECOVERED: {"zone": zone},
        notify.SUBSCRIBER_TRANSFER_REQUESTED: {
            **transfer, "amount": 68000, "currency": "HKD",
            "billed_through": now + timedelta(days=21), "expires_at": now + timedelta(days=7),
        },
        notify.SUBSCRIBER_TRANSFER_ACCEPTED: transfer,
        notify.SUBSCRIBER_TRANSFER_DECLINED: transfer,
        notify.SUBSCRIBER_TRANSFER_EXPIRED: transfer,
    }


class Command(BaseCommand):
    help = "Render every email to HTML files, or send one of each to an address."

    def add_arguments(self, parser):
        parser.add_argument("--send", metavar="ADDRESS",
                            help="send real mail to this address instead of writing files")
        parser.add_argument("--out", default="_email_preview", type=Path,
                            help="directory for the HTML files (default: _email_preview)")
        parser.add_argument("--only", metavar="EVENTS",
                            help="comma-separated event keys (default: all)")

    def handle(self, *args, send, out, only, **options):
        from billing.services import _context, notify

        with _context.scope():
            everything = contexts()
            missing = set(notify.EVENTS) - set(everything)
            if missing:  # a new event needs a fixture here before it can be reviewed
                raise CommandError(f"No preview fixture for: {', '.join(sorted(missing))}")
            wanted = [e.strip() for e in (only or "").split(",") if e.strip()] or list(everything)
            unknown = [e for e in wanted if e not in everything]
            if unknown:
                raise CommandError(f"Unknown event(s): {', '.join(unknown)}")
            if send:
                self._send(send, wanted, everything)
            else:
                self._write(out, wanted, everything)

    def _write(self, out: Path, wanted, everything) -> None:
        from billing.services import notify

        out.mkdir(parents=True, exist_ok=True)
        for index, event in enumerate(wanted, start=1):
            message = notify.build_message(event, everything[event], address="preview@example.com",
                                           first_name="Angelika", inline=True)
            path = out / f"{index:02d}_{event}.html"
            path.write_text(message.html, encoding="utf-8")
            self.stdout.write(f"  {path.name:44s} {message.subject}")
        self.stdout.write(f"\nWrote {len(wanted)} files to {out}")

    def _send(self, address: str, wanted, everything) -> None:
        from billing.services import notify

        if not notify.mail_configured():
            raise CommandError("Mail is not configured (SMTP_URL unset) - nothing was sent.")
        if "locmem" in (getattr(settings, "EMAIL_BACKEND", "") or ""):
            raise CommandError("EMAIL_BACKEND is the in-memory one - nothing would leave.")
        failures = 0
        for event in wanted:
            message = notify.build_message(event, everything[event], address=address,
                                           first_name="Angelika")
            try:
                message.send()
                self.stdout.write(f"  sent  {event:34s} {message.subject}  (from {message.sender})")
            except Exception as exc:  # noqa: BLE001 - report every one, stop for none
                failures += 1
                self.stderr.write(f"  FAIL  {event:34s} {exc}")
        self.stdout.write(f"\n{len(wanted) - failures}/{len(wanted)} sent to {address}")
        if failures:
            raise CommandError(f"{failures} send(s) failed")
