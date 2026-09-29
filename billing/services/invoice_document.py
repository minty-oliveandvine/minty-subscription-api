"""One invoice as the PDF prints it (Figma 09-A · Invoice PDF) — the content, as data.

``invoice_pdf`` draws this; nothing here knows about pages or fonts, so every rule about WHAT
the document says is tested without rendering anything.

WHY OUR OWN DOCUMENT, not Stripe's. Stripe's invoice PDF cannot be restyled to 09-A: its layout
is fixed (its own header and seller block, Qty / Unit price columns, two decimals), and its
"Bill to" is the Stripe CUSTOMER — one per payer — snapshotted when the invoice finalized. Ours
is the invoice's BILLING ACCOUNT (the user's rule, 2026-09-29), exactly what 08-B prints for it.

WHAT IT SAYS (the user's decisions, 2026-09-29):

* **Bill to** — the account's name (``portal.account_name``), its billing email (else the
  payer's) and its charged card's billing address, read LIVE, like 08-B: re-downloading an old
  invoice after a rename prints the new name. Freezing them at issue would need columns on
  ``subscription_invoice``; not done.
* **Lines** — 09-A's plan lines as headings, each with its total, and the companies listed
  under each. Every company row is ONE Stripe invoice item: the same amount, in Stripe's own
  words (``billing.line_description`` is the text the gateway sent) minus the plan its heading
  already names — "Company F Limited - Petty Cash (access after cancellation)" is printed as
  "Company F Limited (access after cancellation)" under "Petty cash module only".
* **Footer** — the Terms' address with the billing mailbox, and a note that says "bill date"
  because 09-A prints no due date.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from billing.services import money
from billing.services import store as sub_store
from billing.services._log import logger
from billing.services.billing import line_description

#: 09-A's words for the plans, in the order it prints them. Keyed by the catalogue's display
#: name (``billing_plan.display_name``) as the line snapshotted it, case-folded.
PLAN_LABELS = {
    "petty cash": "Petty cash module only",
    "payment request": "Payment request module only",
    "super minty": "SuperMinty",
}

#: Suffixed to a heading that carries on from the previous page.
CONTINUED = " (continued)"

SELLER_LINE = "DailyMinty Limited, Level 5, K11 Atelier, 728 King’s Road, Quarry Bay, Hong Kong."
CONTACT_EMAIL = "billing@dailyminty.com"
NOTE = (
    "The amount due will be debited from the payment details you have provided to us on or "
    "after the bill date stated above."
)


class NoDocument(Exception):
    """The invoice exists but is no document: never sent, a draft, or voided (409)."""


class BillToUnavailable(Exception):
    """The billing address could not be read from the processor (502) — never printed blank."""


class TotalMismatch(Exception):
    """The lines do not add up to the invoice's total (500). Never rendered: a document that
    disagrees with the charge is worse than no document."""


@dataclass(frozen=True)
class CompanyRow:
    """One Stripe invoice item, under its plan's heading."""

    text: str          # "Company F Limited (access after cancellation)"
    stripe_text: str   # the item as Stripe has it: "Company F Limited - Petty Cash (access …)"
    amount: str        # "HK$90.32", "-HK$180.65"
    amount_minor: int


@dataclass(frozen=True)
class PlanGroup:
    """A 09-A plan line: the heading, its total, and the companies it is made of."""

    label: str
    amount: str
    amount_minor: int
    rows: tuple[CompanyRow, ...]


@dataclass(frozen=True)
class InvoiceDocument:
    invoice_id: str
    reference: str                   # the Invoice # column's string (``portal._reference``)
    bill_date: str                   # "14 Sep 2026" — the list's date for this invoice
    bill_to_name: str
    bill_to_address: tuple[str, ...]
    bill_to_email: str
    amount_header: str               # "Amount (HK$)"
    groups: tuple[PlanGroup, ...]
    total: str
    total_minor: int
    note: str = NOTE
    seller_line: str = SELLER_LINE
    contact_email: str = CONTACT_EMAIL

    @property
    def filename(self) -> str:
        """``Inv-<ref>.pdf`` — the breakdown CSV's naming (``Inv-<ref> Breakdown by Entity``)."""
        return f"Inv-{self.reference.lstrip('#').strip()}.pdf"


def build_invoice_document(user_id, invoice_id) -> InvoiceDocument | None:
    """The document for one of this payer's invoices; None when it is not theirs.

    Raises ``NoDocument`` for an invoice that was never issued or was voided, and
    ``BillToUnavailable`` / ``TotalMismatch`` as documented on each.
    """
    from billing.services import portal

    invoice = portal._payers_invoice(user_id, invoice_id)
    if invoice is None:
        return None
    if not portal.has_document(invoice):
        raise NoDocument(f"invoice {invoice.id} is {invoice.status!r} with no document")

    currency = (invoice.currency or "").upper()
    symbol = money.symbol(currency)
    places = money.decimal_places(currency)

    def amount(minor: int) -> str:
        return _signed(minor, symbol, places)

    groups = _groups(invoice, amount)
    lines_total = sum(group.amount_minor for group in groups)
    if lines_total != int(invoice.total or 0):
        logger.error(
            "invoice pdf: invoice {} lines add up to {} but its total is {}; refusing to print "
            "a document that disagrees with the charge",
            invoice.id, lines_total, invoice.total,
        )
        raise TotalMismatch(f"invoice {invoice.id}: lines {lines_total} != total {invoice.total}")

    name, address, email = _bill_to(user_id, invoice)
    return InvoiceDocument(
        invoice_id=str(invoice.id),
        reference=portal._reference(invoice),
        bill_date=portal._fmt(portal._issued(invoice)) or "",
        bill_to_name=name,
        bill_to_address=address,
        bill_to_email=email,
        amount_header=f"Amount ({symbol})" if symbol else "Amount",
        groups=groups,
        total=amount(lines_total),
        total_minor=lines_total,
    )


def _signed(minor: int, symbol: str, places: int) -> str:
    """Trimmed like 09-A ("HK$5,872", "HK$90.32"), the sign IN FRONT ("-HK$180.65").
    ``money.format_trimmed`` alone writes a credit as "HK$-180.65"."""
    major = Decimal(abs(int(minor))) / (Decimal(10) ** places)
    text = money.format_trimmed(symbol, major, places)
    return f"-{text}" if minor < 0 else text


def _groups(invoice, amount) -> tuple[PlanGroup, ...]:
    """09-A's plan lines, each made of its company rows (see the module docstring)."""
    from billing.services import portal

    by_plan: dict[str, list[tuple[str, CompanyRow]]] = {}
    for line in invoice.lines.all():
        minor = int(line.amount or 0)
        if minor == 0:
            continue  # never sent: the gateway drops zero lines, so it is on no invoice
        product = line.product_name or ""
        plan = portal._plan_name(product)
        entity = line.entity_name or ""
        stripe_text = line_description(entity, product, kind=line.kind or "full", at=line.at)
        # Stripe's words minus the plan the heading names. Built from the text Stripe got, so
        # a row can only ever say what the item says.
        prefix = f"{entity} - {plan}"
        text = entity + stripe_text[len(prefix):] if stripe_text.startswith(prefix) else stripe_text
        by_plan.setdefault(plan, []).append(
            ((entity.casefold(), text), CompanyRow(text, stripe_text, amount(minor), minor))
        )

    known = [plan for plan in by_plan if plan.casefold() in PLAN_LABELS]
    known.sort(key=lambda plan: list(PLAN_LABELS).index(plan.casefold()))
    unknown = [plan for plan in by_plan if plan.casefold() not in PLAN_LABELS]
    for plan in unknown:
        logger.warning(
            "invoice pdf: invoice {} has a plan 09-A has no words for ({!r}); printing its name",
            invoice.id, plan,
        )

    groups = []
    for plan in known + unknown:
        # By company, then by the row's own words. Not "as issued": one invoice's lines share
        # a single ``created_at`` (``db_default=Now()`` in one insert), so the database may
        # return a company's two rows in either order and the PDF must not change between
        # downloads.
        rows = tuple(row for _key, row in sorted(by_plan[plan], key=lambda pair: pair[0]))
        subtotal = sum(row.amount_minor for row in rows)
        groups.append(
            PlanGroup(PLAN_LABELS.get(plan.casefold(), plan), amount(subtotal), subtotal, rows)
        )
    return tuple(groups)


def _bill_to(user_id, invoice) -> tuple[str, tuple[str, ...], str]:
    """``(name, address lines, email)`` for the invoice's billing account, as 08-B prints it.

    The account is the one that raised the invoice, else the payer's oldest
    (``portal.invoice_account_id``). A payer with no account at all is billed by their own
    name and email, as ``account_name`` already reads for an unnamed account.
    """
    from billing.services import payment_methods, portal
    from billing.services.store import _by_pk
    from shared_models.models import User

    groups = sub_store.billing_groups_for_payer(user_id)
    account_id = portal.invoice_account_id(invoice, [str(group.id) for group in groups])
    group = next((g for g in groups if str(g.id) == account_id), None)
    payer = portal._person(_by_pk(User, user_id), user_id)

    name = portal.account_name(group, payer)
    email = (getattr(group, "billing_email", None) or "").strip() or payer.get("email") or ""

    address: tuple[str, ...] = ()
    card_id = getattr(group, "stripe_payment_method_id", None)
    if card_id:
        try:
            wallet = payment_methods.list_for_user(user_id)
        except Exception as exc:
            logger.exception(
                "invoice pdf: could not read the billing address for invoice {}", invoice.id
            )
            raise BillToUnavailable(f"invoice {invoice.id}: wallet read failed") from exc
        # The CHARGED card's address, from the same wallet 08-B reads — and, like 08-B, none
        # when Stripe no longer holds that card.
        card = next((m for m in wallet["methods"] if m.get("id") == card_id), None)
        code = ((card or {}).get("address") or {}).get("country")
        names = portal._country_names({code}) if code else {}
        address = tuple(portal.address_lines(portal.card_address(card, names)))
    return name, address, email
