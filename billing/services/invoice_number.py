"""The invoice number: ``INV-<ACCOUNT>-YYYYMM-NNNN`` (owner, 2026-10-23).

``ACCOUNT`` is the billing account's name as it stood when the invoice was issued, cut to
A-Z and 0-9 and at most eight characters; a name with none of those (empty, or Chinese
only) falls back to the first six characters of the account's id - or the payer's, on an
invoice raised before billing accounts. ``YYYYMM`` is the month it was issued, in UTC like
the Date column beside it. ``NNNN`` is ONE counter per month for the whole of Minty
(``subscription_invoice_counter``), so the number is unique on the counter alone: two
accounts both called "Acme" never collide.

A number is given ONCE, the first time the local row is an issued document - open, paid,
uncollectible or void, which Stripe only reaches by finalizing. A draft or a reservation
never sent uses none up, a voided invoice keeps its own, and renaming the account later
changes no issued number. ``issued_at`` alone cannot be the trigger: a draft's settle
already carries Stripe's ``created`` there (``billing_gateway._record_of``).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from django.db import transaction
from django.db.models import F

#: The local statuses that mean Stripe finalized the invoice. ``void`` is here because a
#: draft cannot be voided at Stripe - only deleted - so a voided row was issued first.
ISSUED_STATUSES = frozenset({"open", "paid", "uncollectible", "void"})

_CODE_LENGTH = 8
_FALLBACK_LENGTH = 6


def account_code(company: str | None, fallback_id) -> str:
    """``"Acme Trading Ltd"`` -> ``"ACMETRAD"``; no usable letter -> ``fallback_id``'s head."""
    ascii_only = unicodedata.normalize("NFKD", company or "").encode("ascii", "ignore").decode()
    code = re.sub(r"[^A-Z0-9]", "", ascii_only.upper())[:_CODE_LENGTH]
    if code:
        return code
    return re.sub(r"[^A-Z0-9]", "", str(fallback_id).upper())[:_FALLBACK_LENGTH]


def format_number(code: str, period: str, value: int) -> str:
    """``INV-ACME-202610-0042``; past 9,999 in a month the counter simply grows a digit."""
    return f"INV-{code}-{period}-{value:04d}"


def _period(moment: datetime | None) -> str:
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m")


def _next_value(period: str) -> int:
    """Take the month's next number. Call inside a transaction: the UPDATE holds the
    counter's row lock until it commits, which is what keeps two workers apart."""
    from minty_db.models import SubscriptionInvoiceCounter as Counter

    Counter.objects.bulk_create([Counter(period=period, last_value=0)], ignore_conflicts=True)
    Counter.objects.filter(period=period).update(last_value=F("last_value") + 1)
    return Counter.objects.get(period=period).last_value


def assign(invoice_id) -> str | None:
    """Give the invoice its number if it is issued and has none; return its number.

    Idempotent: a row that already has one keeps it and nothing is counted. The invoice row
    is locked first, so two settles of the same invoice at once give it one number, and the
    counter moves in the same transaction, so a failure here leaves no gap.
    """
    from minty_db.models import SubscriptionInvoice

    with transaction.atomic():
        invoice = (
            # ``of=self``: Postgres refuses FOR UPDATE on the nullable side of the join.
            SubscriptionInvoice.objects.select_for_update(of=("self",))
            .select_related("billing_group")
            .get(pk=str(invoice_id))
        )
        if invoice.invoice_number or invoice.status not in ISSUED_STATUSES:
            return invoice.invoice_number
        group = invoice.billing_group
        code = account_code(
            group.billing_company if group else None,
            group.id if group else invoice.payer_user_id,
        )
        period = _period(invoice.issued_at)
        number = format_number(code, period, _next_value(period))
        SubscriptionInvoice.objects.filter(pk=invoice.pk).update(invoice_number=number)
        return number
