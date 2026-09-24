"""Billing email — the subscription engine's only outbound notification.

Before this module the engine was SILENT. Every lifecycle event was computed, stored and
acted on, and the customer learned about it either by happening to open the Module &
Subscription settings page or by being bounced to an "Access Denied" screen after their
module had already been switched off. The most consequential event of all — a free trial
that will not convert because no card is on file — was knowable days in advance and
announced nowhere.

THREE RULES, all of them load-bearing:

1. **Never raise.** Every entry point returns a bool and swallows its own exceptions.
   These are called from inside scheduled jobs that move money; a dead SMTP host must not
   be the thing that aborts a renewal run halfway through a batch of payers.

2. **Never send twice.** The jobs that call this are documented as safe to re-run at any
   cadence, and that property is only true of state reconciliation — a send is not
   idempotent. So the send is CLAIMED in ``subscription_email_log`` first, and a claim
   that already succeeded short-circuits. See that model for why the constraint lives in
   the database rather than here.

3. **Send after the money, never during.** Callers collect events and flush them once
   their loop has finished and ``store`` has committed. Mailing mid-loop risks telling a
   customer about a charge that then rolls back, and puts network latency inside a
   billing transaction.

The copy for all eleven events lives in ``_COPY`` below rather than in eleven templates, so
the entire customer-facing vocabulary of the billing system is reviewable on one screen —
which matters more here than template purity, because these are the only words Minty ever
says to a customer about their money.

Recipient is the PAYER, with ONE exception. Every action these emails ask for (add a card,
settle an invoice, confirm billing) is one only the payer can take; co-admins on an entity
would receive amounts they cannot act on. The consequence is a known gap — a co-admin
still watches modules go dark with no explanation — and closing it needs a separate,
redacted template set rather than a wider recipient list on these.

The exception is ``subscriber_transfer_requested``, which is addressed to someone who is
NOT yet the payer and is being asked to become one. It belongs here rather than in a
separate system because it is a message about money with an amount in it, and the whole
point of keeping this vocabulary on one screen is that no such message escapes review.

PORTED FROM FLASK (Part 2 step 2). The copy, the builders and the three rules are Flask's
``blueprints/subscription/services/notify.py`` verbatim; what changed is the delivery
layer underneath them - Flask-Mail became Django's mail framework (``InlineImageMessage``
keeps the ``multipart/related`` wire shape), ``render_template`` became the Jinja2
template backend rendering the SAME two templates (``templates/email/``), ``PUBLIC_URL``
became ``MINTY_PUBLIC_URL``, and the links a person receives go to minty-web through
Flask's re-handoff (``settings_url`` / ``portal_url``) instead of to Flask's own pages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.mail.message import SafeMIMEMultipart
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string

from billing.services import clock, display
from billing.services._log import logger
from billing.services.money import format_minor

#: ``billing/static/`` - the email images live at ``billing/static/email/<name>.png``.
STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"

# The email log's two states (Flask: ``subscription_email_log.STATUS_*``). A row is
# ``failed`` from the claim until the send succeeds, so a crash between the two reads as
# a failure to retry rather than a delivery.
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

# --- Events -------------------------------------------------------------------
# Stable strings: they are persisted as dedupe rows, so renaming one silently
# re-sends every email of that kind to every customer who already had it.
TRIAL_ENDING = "trial_ending"
TRIAL_EXPIRED = "trial_expired"
RENEWAL_PAID = "renewal_paid"
RENEWAL_FAILED = "renewal_failed"
DUNNING_RETRY_FAILED = "dunning_retry_failed"
PAYMENT_RECOVERED = "payment_recovered"
# The handover family. ``requested`` is the one email in this module sent to a non-payer.
SUBSCRIBER_TRANSFER_REQUESTED = "subscriber_transfer_requested"
SUBSCRIBER_TRANSFER_ACCEPTED = "subscriber_transfer_accepted"
SUBSCRIBER_TRANSFER_DECLINED = "subscriber_transfer_declined"
SUBSCRIBER_TRANSFER_EXPIRED = "subscriber_transfer_expired"

# Retired 2026-09. The strings stay documented because historical ``subscription_email_log``
# rows still carry them, and anyone reading a row for "trial_converted" needs to find out
# here that it was deliberately stopped rather than assume the send is broken:
#
#   trial_converted            a converting trial is now silent; the first charge is
#                              receipted by ``renewal_paid`` in the same daily pass
#   account_closed             superseded, then access_revoked went too
#   subscriber_transfer_failed the accepting user sees the decline in the browser
#   access_revoked             nothing now announces a revocation at all -- see the
#                              closing comment in access_sweep.sweep_expired_module_access
#
# Do NOT reuse these strings for a different meaning — a dedupe row written years ago
# would suppress the new email for anyone who received the old one.

EVENTS = (
    TRIAL_ENDING,
    TRIAL_EXPIRED,
    RENEWAL_PAID,
    RENEWAL_FAILED,
    DUNNING_RETRY_FAILED,
    PAYMENT_RECOVERED,
    SUBSCRIBER_TRANSFER_REQUESTED,
    SUBSCRIBER_TRANSFER_ACCEPTED,
    SUBSCRIBER_TRANSFER_DECLINED,
    SUBSCRIBER_TRANSFER_EXPIRED,
)

# One template for everything except the receipt. ``renewal_paid`` is a document rather
# than a notice — it has to itemise what was charged — and the redesigned notice template
# is prose-only with nowhere to put a line-item table. Temporary: once the invoice-styled
# mockup exists, that design absorbs the receipt and this mapping collapses back to one.
NOTICE_TEMPLATE = "email/subscription_notice.html"
RECEIPT_TEMPLATE = "email/subscription_receipt.html"
TEMPLATES = {RENEWAL_PAID: RECEIPT_TEMPLATE}


# --- Formatting helpers -------------------------------------------------------


def money(amount_minor, currency: str | None) -> str:
    """``28000, 'hkd'`` -> ``'HKD 280.00'``.

    Currency is stated alongside the number rather than as a symbol: these emails reach
    customers in several currencies and a bare ``$`` is ambiguous across most of them.
    """
    code = (currency or "").strip().upper()
    return f"{code} {format_minor(amount_minor, currency)}".strip()


def day(value) -> str:
    """A date a human reads without parsing: ``12 Mar 2026``. Empty when unknown.

    The FORMAT is ``display.day`` -- email is prose, so the day is unpadded. What stays
    here is the empty-string contract: these values go straight into email templates,
    where ``None`` would render the word "None" into a sentence, so a missing date has to
    come back as "" and the isinstance guard has to stay in front of it.
    """
    if not isinstance(value, datetime):
        return ""
    return display.day(value) or ""


def modules_phrase(codes) -> str:
    """``['PETTY_CASH', 'PAYMENT_REQUEST']`` -> ``'Petty Cash and Payment Request'``."""
    names = [_module_name(code) for code in codes if code]
    if not names:
        return "your modules"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _module_name(code: str) -> str:
    # BILL is the plan word (billing_plan.code); PAYMENT_REQUEST the module code.
    pretty = {"PETTY_CASH": "Petty Cash", "PAYMENT_REQUEST": "Payment Request", "BILL": "Payment Request"}
    key = str(code or "").strip().upper()
    return pretty.get(key, key.title().replace("_", " "))


def base_url() -> str:
    """Public origin for links, WITHOUT a request to derive it from.

    ``MINTY_PUBLIC_URL`` — Flask's ``PUBLIC_URL``, the address a PERSON reaches Minty at.
    Distinct from ``FLASK_APP_URL``, which in the docker stack is the internal service
    name (``http://minty:5001``) and would ship a button nobody outside the network can
    press. One setting for every link a customer receives, so a domain change has one
    place to land.

    Set explicitly rather than derived: these are sent from scheduled jobs, where there
    is no request to build an absolute URL from. An unset value drops the button rather
    than shipping a dead one.
    """
    value = (getattr(settings, "MINTY_PUBLIC_URL", "") or "").rstrip("/")
    _warn_once_if_unreachable(value)
    return value


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", ".local", ".test")
_warned_unreachable = False


def _warn_once_if_unreachable(value: str) -> None:
    """Say something when the buttons in these emails cannot possibly work.

    A developer's ``MINTY_PUBLIC_URL`` reaching production mail is a silent failure otherwise:
    every message goes out looking perfect and every button lands on a host only the
    sender can resolve. Cheap to detect, and worth one loud line per process — the alert
    emails are the ones whose whole purpose is getting somebody to click through.

    Once per process, not per send: a nightly run mailing forty payers should not print
    forty copies of the same configuration problem.
    """
    global _warned_unreachable
    if _warned_unreachable:
        return
    lowered = value.lower()
    if not value:
        _warned_unreachable = True
        logger.warning(
            "notify: MINTY_PUBLIC_URL is unset — billing emails will go out with no "
            "action buttons at all."
        )
    elif any(host in lowered for host in _LOCAL_HOSTS):
        _warned_unreachable = True
        logger.warning(
            "notify: MINTY_PUBLIC_URL is {!r}, which no recipient can reach. Billing emails "
            "will ship buttons that go nowhere.", value,
        )


def billing_sender() -> str | None:
    """The From address for billing mail.

    Its own address rather than the account-wide sender (Flask's ``BREVO_EMAIL``, here
    ``DEFAULT_FROM_EMAIL``): an invitation comes from a colleague, a dunning notice comes
    from the company about to switch your access off. Recipients filter and search on the
    sender, so those should not share one.

    Falls back to ``DEFAULT_FROM_EMAIL`` when unset. An environment that has not yet
    verified the dedicated sender with the relay keeps sending rather than silently
    failing — an unverified From is rejected or spam-filed, and ``notify`` swallows SMTP
    errors by design, so the failure would be invisible.
    """
    return (getattr(settings, "SUBSCRIPTION_EMAIL", None)
            or getattr(settings, "DEFAULT_FROM_EMAIL", None))


def settings_url(entity_id) -> str:
    """The Module page for an entity — where every action actually is.

    The page is minty-web's (Part 2 step 4), reached through Flask's login-gated
    re-handoff so a cold recipient with no session is signed in first: Flask stays the
    only identity and the only minter of tokens, and this service puts no JWT in a URL.
    Flask's copy of this function linked to its own template
    (``/entity/settings/module/{id}``); that route goes in step 5 and this link lands with
    it — until then it is dead by design (documented in docs/features/subscriptions-api.md).
    """
    root = base_url()
    if not root or not entity_id:
        return root
    return handoff_url(f"/subscription/entities/{entity_id}/modules", entity_id=entity_id)


def handoff_url(next_path: str, *, entity_id=None) -> str:
    """``{MINTY_PUBLIC_URL}/handoff/minty-web?next=...[&entity_id=...]`` — a link into
    minty-web that Flask authenticates on the way through. Empty when there is no public
    origin, like every other link here."""
    root = base_url()
    if not root:
        return root
    params = {"next": "/" + str(next_path or "").lstrip("/")}
    if entity_id:
        params["entity_id"] = str(entity_id)
    return f"{root}/handoff/minty-web?{urlencode(params)}"


def portal_url(next_path: str = "/subscription/subscriptions") -> str:
    """The payer portal in minty-web (``/subscription/subscriptions``, or ``/incoming`` for
    the recipient of a handover), through the same re-handoff. Replaces the JWT-minting
    ``billing_app_profile_unscoped_url`` Flask's transfer notices used - see
    ``settings_url`` for why no token travels in a link from here."""
    return handoff_url(next_path)


# --- Inline images ------------------------------------------------------------
# Embedded in the message rather than linked. A remote <img> in an email fails in two
# ordinary situations, and both produce a broken-image box, which reads worse than no
# logo at all:
#
#   1. most clients — Gmail and Outlook included — block remote images by default until
#      the reader clicks "show images", so the masthead is a grey box on first open;
#   2. the host has to be publicly reachable. ``MINTY_PUBLIC_URL`` is a developer's
#      ``https://localhost:5001`` far more often than anyone intends, and mail sent that
#      way carries a logo nobody outside that machine can load. That is exactly what the
#      first live send did.
#
# A CID attachment is part of the message, so it renders offline, behind image blocking,
# and whatever ``MINTY_PUBLIC_URL`` says. The cost is ~14KB per email, which is nothing next to
# a masthead that is broken by default.

LOGO_CID = "minty-logo"
#: Built by Minty's ``scripts/subscription/build_email_assets.py`` from ``static/img/new_logo.png``,
#: not that file itself: the source is a padded 571x379 canvas weighing 98KB, and this one
#: is attached to EVERY message the billing system sends. Trimmed and downsized it is ~19KB.
#: Copied here from ``Minty/static/img/email/`` (step 2); regenerate there, copy here.
LOGO_PATH = ("email", "logo.png")

#: The hero illustration, one per event, resolved by event key. An event whose art has
#: not been drawn yet simply resolves to nothing and the template omits the row — which
#: is why a new event can ship before its illustration exists.
ILLUSTRATION_CID = "minty-art"


def illustration_path(event: str) -> tuple[str, ...]:
    """Where ``event``'s illustration lives under ``static/``.

    Built by Minty's ``scripts/subscription/build_email_assets.py``, which trims and downsizes
    the source art; the files here are ~10-40KB, not the ~1MB originals in Minty's
    ``static/img``.
    """
    return ("email", f"{event}.png")


_image_cache: dict[tuple[str, ...], bytes | None] = {}


def image_bytes(*parts: str) -> bytes | None:
    """An inline image file, read once per process. None if it cannot be read.

    Resolved from ``billing/static/`` (``STATIC_ROOT`` below), the copies of Minty's
    ``static/img/email/`` files that travel with this package - nothing here is served
    over HTTP, so no staticfiles machinery is involved.

    Cached either way — including the failure — so a missing asset costs one warning
    rather than a disk hit on every message of every nightly run.
    """
    if parts in _image_cache:
        return _image_cache[parts]

    path = str(STATIC_ROOT.joinpath(*parts))
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except Exception as exc:
        # Not an error: the email is entirely readable without it, and every word of it
        # still renders. Worth saying once so a lost asset does not go unnoticed forever.
        logger.warning("notify: could not read email image at {}: {}", path, exc)
        data = None

    _image_cache[parts] = data
    return data


def logo_bytes() -> bytes | None:
    """The masthead logo. Kept as its own name because every message attaches it."""
    return image_bytes(*LOGO_PATH)


# --- Copy ---------------------------------------------------------------------
# Each builder takes the event context and returns the rendered content. Keeping them
# as functions rather than format strings is what lets one event vary its wording on a
# fact — TRIAL_ENDING says something materially different depending on whether a card is
# on file, and that difference is the entire value of the email.


def _days_until(value) -> int | None:
    """Whole days from today to ``value``, or None if it isn't a date.

    Calendar days, not elapsed hours: a trial ending tomorrow afternoon is "1 day", not
    "0 days" because it is 20 hours away. The heading is the first thing the customer
    reads and it has to agree with how they would count it themselves.
    """
    if not isinstance(value, (datetime, date)):
        return None
    ends = value.date() if isinstance(value, datetime) else value
    # ``clock.now``, not ``date.today``. Everything else in the billing engine dates
    # itself from this clock, and the replay harness moves it — reading the process clock
    # here would make a scenario run months in the past render "Trial Ending today" on a
    # trial with three weeks left, and the email would disagree with the state that
    # produced it.
    return (ends - clock.now().date()).days


def _in_days(count: int | None) -> str:
    """``7`` -> ``'in 7 days'``. Degrades to a phrase that is true whatever the number is.

    Never renders "in 0 days" or a negative: the warning job can run late, and a heading
    reading "Trial Ending in -1 days" is the kind of thing customers screenshot.
    """
    if count is None:
        return "soon"
    if count <= 0:
        return "today"
    if count == 1:
        return "in 1 day"
    return f"in {count} days"


def _trial_ending(ctx: dict) -> dict:
    """The trial is about to lapse AND the customer has to do something about it.

    Sent only when a card or billing consent is missing - a trial that will convert
    cleanly is not mailed at all, because there is nothing to act on. That filter lives in
    ``checkout.notify_trials_ending``; by the time it reaches here, action IS needed.

    The two blocked states used to read differently. They no longer do:

      no card:  "The free trial for {mods} on {entity} ends on {ends}. There's no payment
                 method saved for this company yet, so access will stop on that date
                 rather than continuing."
                "Adding a card before then keeps everything running with no interruption
                 - you won't be charged until the trial actually ends."
      consent:  "The free trial for {mods} on {entity} ends on {ends}. Your saved card is
                 shared with your other companies, so it won't be charged for this one
                 until you confirm - and access will stop on that date instead."
                "Confirming takes a moment and charges nothing today - the first payment
                 is taken when the trial actually ends."
      convert:  "The free trial for {mods} on {entity} ends on {ends}. Your saved card
                 will be charged then and access continues without interruption - there's
                 nothing you need to do."
                "If you'd rather not continue, you can cancel any time before that date."

    Kept because the consent wording named the one thing this email no longer explains:
    WHY a card the customer can see on their own billing page will not be charged. That
    explanation now lives only in the in-app banner (``notices.py``), which distinguishes
    "add a payment method" from "confirm billing for this company". If a payer ever asks
    why they were told to add a card they already have, this is the paragraph they needed.
    """
    entity = ctx.get("entity_name") or "your company"
    ends = day(ctx.get("trial_end"))
    left = _days_until(ctx.get("trial_end"))
    return {
        # The subject echoes the heading. A payer with several companies loses the
        # company name from their inbox list, which the amber line inside the email
        # carries instead.
        "subject": f"Your Minty trial ends {_in_days(left)}",
        "heading": f"Trial Ending {_in_days(left)}",
        "entity_name": entity,
        "body": [
            f"Your Minty trial will end {_in_days(left)}.",
            f"To continue using your current modules after the trial ends, please "
            f"choose a subscription plan before {ends}.",
        ],
        "cta_label": "Go to Manage Subscription",
        "cta_url": settings_url(ctx.get("entity_id")),
    }


def _trial_expired(ctx: dict) -> dict:
    entity = ctx.get("entity_name") or "your company"
    mods = modules_phrase(ctx.get("codes") or [])
    return {
        "subject": "Your trial has ended",
        "heading": "Your trial has ended",
        "entity_name": entity,
        "body": [
            f"The free trial for {mods} has ended, and access has been switched off.",
            "Your data is safe and unchanged. Subscribing restores access to everything "
            "exactly as you left it.",
        ],
        "cta_label": "Go to Manage Subscription",
        "cta_url": settings_url(ctx.get("entity_id")),
    }


def _renewal_paid(ctx: dict) -> dict:
    """The receipt. The ONE email still rendering the pre-redesign template.

    It is a document rather than a notice: the line items and the period are the point of
    it, and the redesigned notice template is prose-only with nowhere to itemise. Keeps
    ``tone``, ``facts`` and ``lines``, which every other builder has dropped - see
    ``TEMPLATES``.
    """
    total = money(ctx.get("total"), ctx.get("currency"))
    start, end = day(ctx.get("period_start")), day(ctx.get("period_end"))
    return {
        "tone": "neutral",
        "subject": f"Your Minty receipt — {total}",
        "heading": "Payment received",
        "lede": f"Thanks — we've charged {total} for your Minty subscription.",
        "facts": [("Amount", total), ("Period", f"{start} – {end}" if start else "")],
        "lines": ctx.get("lines") or [],
        "body": [],
        "cta_label": "View billing",
        "cta_url": base_url(),
    }


def _payment_failed_body(deadline: str) -> list[str]:
    """Shared body for the two payment-failure emails, which share a design and art.

    Only the first line differs between them, so it is passed in rather than duplicated:
    a first decline and a fourth are the same message with a different count in front.
    """
    body = [
        "If you have resolved the issue with your payment method, you can retry the "
        "payment at any time.",
    ]
    if deadline:
        body.append(
            f"Access to the affected entities will be suspended if the payment is not "
            f"received by {deadline}."
        )
    else:
        # No deadline in context. Still say suspension is coming - the sentence exists to
        # convey that retries are finite, and dropping it entirely would leave the email
        # reading as though nothing happens if it is ignored.
        body.append(
            "Access to the affected entities will be suspended if the payment is not "
            "received."
        )
    return body


def _renewal_failed(ctx: dict) -> dict:
    return {
        "subject": "We couldn't process your payment",
        "heading": "We couldn't process your payment",
        # No entity line: a card belongs to the payer and may cover several companies, so
        # naming one of them would be arbitrary rather than merely redundant.
        "body": ["We could not process your latest subscription payment."]
        + _payment_failed_body(day(ctx.get("deadline"))),
        "cta_label": "Go to Manage Subscription",
        "cta_url": base_url(),
    }


def _dunning_retry_failed(ctx: dict) -> dict:
    return {
        "subject": "We couldn't process your payment",
        "heading": "We couldn't process your payment",
        "body": ["We still could not process your subscription payment."]
        + _payment_failed_body(day(ctx.get("deadline"))),
        "cta_label": "Go to Manage Subscription",
        "cta_url": base_url(),
    }


def _payment_recovered(ctx: dict) -> dict:
    return {
        "subject": "Thank you for your payment",
        "heading": "Payment issue resolved",
        "body": [
            "Payment issue has been resolved.",
            "We have successfully received your payment.",
        ],
        "cta_label": "Go to Minty",
        "cta_url": base_url(),
    }


def _subscriber_transfer_requested(ctx: dict) -> dict:
    """Sent to the person being ASKED to take the bill on - not to the payer.

    States NO amount. The figure used to be in the facts table this email had before the
    redesign, and briefly survived as a sentence:

        "Accepting charges {amount} today, covering the period already paid for up to
         {billed_through}."

    Removed by decision. Worth knowing what that costs, because the original reason was
    not decorative: accepting is a purchase, so this asks someone to take on a recurring
    charge without showing them the figure. The amount IS on the review screen the button
    leads to — ``portal_url`` — which is now the only place it appears before they commit.
    """
    entity = ctx.get("entity_name") or "a company"
    who = ctx.get("from_name") or "The current subscriber"
    body = [
        f"{who} would like to transfer the subscription ownership of the above entity "
        f"to you.",
        "To complete the transfer, please review and accept the request in Minty.",
        "If accepted, you will become responsible for managing the subscription and "
        "future billing of this entity.",
    ]
    return {
        "subject": "Subscription transfer request received",
        "heading": "Subscription transfer request received",
        "entity_name": entity,
        "body": body,
        "cta_label": "Review Transfer Request",
        "cta_url": ctx.get("portal_url") or base_url(),
    }


def _subscriber_transfer_accepted(ctx: dict) -> dict:
    """Sent to the OUTGOING payer: their bill just got smaller and they should know why."""
    entity = ctx.get("entity_name") or "a company"
    who = ctx.get("to_name") or "Another admin"
    return {
        "subject": "Subscription transfer completed",
        "heading": "Subscription transfer completed",
        "entity_name": entity,
        "body": [
            f"{who} has accepted the subscription transfer request for the above entity.",
            "The subscription has now been transferred successfully.",
            "You will no longer be able to manage the subscription or billing "
            "information for this entity.",
            "Your final invoice will include subscription charges up to the transfer "
            "date.",
        ],
        "cta_label": "Go to Minty",
        "cta_url": ctx.get("portal_url") or base_url(),
    }


def _subscriber_transfer_declined(ctx: dict) -> dict:
    """Sent to the outgoing payer when the recipient says no.

    Until this existed, declining was silent: the status flipped and an audit row was
    written, but the person who asked was never told, so a request that had actually been
    answered looked identical to one nobody had opened yet.
    """
    entity = ctx.get("entity_name") or "a company"
    who = ctx.get("to_name") or "The recipient"
    return {
        "subject": "Transfer request declined",
        "heading": "Transfer request declined",
        "entity_name": entity,
        "body": [
            f"{who} declined the transfer request.",
            "Your subscription remains unchanged.",
        ],
        "cta_label": "Go to Manage Subscription",
        "cta_url": ctx.get("portal_url") or base_url(),
    }


def _subscriber_transfer_expired(ctx: dict) -> dict:
    """Sent to the payer who asked, when nobody ever answered.

    Distinct from ``declined``: no one said no, the request simply ran out. The
    distinction matters because the follow-up differs - a decline is an answer, an expiry
    usually means the email was missed and re-sending is reasonable.
    """
    entity = ctx.get("entity_name") or "a company"
    who = ctx.get("to_name") or "the recipient"
    return {
        "subject": "Transfer request expired",
        "heading": "Transfer request expired",
        "entity_name": entity,
        "body": [
            f"The transfer request expired before {who} responded.",
            "No changes have been made to your subscription.",
        ],
        "cta_label": "Go to Manage Subscription",
        "cta_url": ctx.get("portal_url") or base_url(),
    }


_COPY = {
    TRIAL_ENDING: _trial_ending,
    TRIAL_EXPIRED: _trial_expired,
    RENEWAL_PAID: _renewal_paid,
    RENEWAL_FAILED: _renewal_failed,
    DUNNING_RETRY_FAILED: _dunning_retry_failed,
    PAYMENT_RECOVERED: _payment_recovered,
    SUBSCRIBER_TRANSFER_REQUESTED: _subscriber_transfer_requested,
    SUBSCRIBER_TRANSFER_ACCEPTED: _subscriber_transfer_accepted,
    SUBSCRIBER_TRANSFER_DECLINED: _subscriber_transfer_declined,
    SUBSCRIBER_TRANSFER_EXPIRED: _subscriber_transfer_expired,
}


# --- Delivery -----------------------------------------------------------------


@dataclass
class Attachment:
    """One attached part, in the shape Flask-Mail's ``Attachment`` had: the callers and
    the tests read ``filename`` / ``content_type`` / ``data`` / ``disposition`` /
    ``headers`` off the message before it is ever serialised."""

    filename: str
    content_type: str
    data: bytes
    disposition: str = "attachment"
    headers: dict = field(default_factory=dict)

    def as_mime(self) -> MIMEBase:
        maintype, _, subtype = (self.content_type or "application/octet-stream").partition("/")
        if maintype == "image":
            part: MIMEBase = MIMEImage(self.data, _subtype=subtype or None)
        else:
            part = MIMEBase(maintype, subtype or "octet-stream")
            part.set_payload(self.data)
            encoders.encode_base64(part)
        part.add_header("Content-Disposition", self.disposition, filename=self.filename)
        for name, value in (self.headers or {}).items():
            part[name] = value
        return part


class InlineImageMessage(EmailMultiAlternatives):
    """A message whose inline parts are RELATED to the body, not merely attached.

    Django, like Flask-Mail, builds every message with attachments as ``multipart/mixed``,
    which says "here is a body, and separately here are some files". That is the wrong
    statement for an image the body references by ``cid:`` — the correct container is
    ``multipart/related`` (RFC 2387), which says the parts belong to one document.

    It matters in practice, not just on paper: under ``mixed`` several Outlook builds
    render the logo inline AND list it as a paperclip attachment, so a billing notice
    arrives looking like it has a file enclosed. Under ``related`` it is unambiguously
    part of the message.

    Only flipped when EVERY attachment carries a ``Content-ID``. A message that ever
    gains a genuine enclosure — a PDF invoice, say — is left as ``mixed``, which is then
    the correct answer again.

    Constructed with Flask-Mail's keywords (``subject``, ``sender``, ``recipients``,
    ``html``) and readable through ``sender`` / ``html`` / ``attachments``, so the ported
    callers and tests did not have to learn Django's (``from_email``, ``alternatives``);
    the recipient list is Django's ``to``.
    """

    def __init__(self, *, subject: str, sender: str | None, recipients: list[str], html: str):
        super().__init__(subject=subject, body="", from_email=sender, to=list(recipients))
        self.html = html
        self.attach_alternative(html, "text/html")
        # Shadows Django's list on purpose: ours holds ``Attachment`` records, and
        # ``_create_attachments`` below is the only reader.
        self.attachments: list[Attachment] = []

    @property
    def sender(self) -> str | None:
        return self.from_email

    # No ``recipients`` property: Django's ``EmailMessage.recipients()`` is a METHOD the
    # backends call, and shadowing it with Flask-Mail's list attribute breaks every send.
    # Readers use ``.to``.

    def attach(self, filename, content_type=None, data=None, disposition="attachment",
               headers=None):  # noqa: D417 - Flask-Mail's signature, kept for the callers
        self.attachments.append(Attachment(
            filename=filename, content_type=content_type or "application/octet-stream",
            data=data or b"", disposition=disposition or "attachment", headers=dict(headers or {}),
        ))

    def _create_attachments(self, msg):
        if not self.attachments:
            return msg
        encoding = self.encoding or settings.DEFAULT_CHARSET
        body_msg = msg
        msg = SafeMIMEMultipart(_subtype=self.mixed_subtype, encoding=encoding)
        if self.body or body_msg.is_multipart():
            msg.attach(body_msg)
        for attachment in self.attachments:
            msg.attach(attachment.as_mime())
        if all(part.headers.get("Content-ID") for part in self.attachments):
            msg.set_type("multipart/related")
            # Names which part is the root document. Without it a strict client has to
            # guess which of the related parts to actually display.
            msg.set_param("type", "multipart/alternative")
        return msg

    def as_bytes(self) -> bytes:
        return self.message().as_bytes()


_UNCONFIGURED_BACKENDS = (
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
)


def mail_configured() -> bool:
    """Whether a send from here can reach anybody.

    False for the console and dummy backends and for an SMTP backend with no host - the
    settings' own default when ``EMAIL_HOST`` is unset. Checked BEFORE the claim, so an
    unconfigured host writes no dedupe row and the notice is not spent: the next run with
    mail configured still sends it. (Flask's ``mail`` extension was always registered, so
    its unconfigured case CLAIMED and then failed to connect; the row stayed ``failed`` and
    the effect - retried next run - was the same.) The in-memory backend the tests run
    under counts as configured.
    """
    backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    if backend in _UNCONFIGURED_BACKENDS:
        return False
    if backend.endswith("smtp.EmailBackend") and not getattr(settings, "EMAIL_HOST", ""):
        return False
    return True


def recipient_for(user_id) -> tuple[str | None, str]:
    """``(email, first_name)`` for a payer.

    Falls back to ``xero_email``: a user who signed up through Xero may have no personal
    ``email`` at all, and they are just as capable of owing money as anyone else.
    """
    from billing.services.store import _by_pk
    from shared_models.models import User

    user = _by_pk(User, user_id)
    if user is None:
        return None, ""
    address = (user.email or user.xero_email or "").strip()
    return (address or None), (user.first_name or "").strip()


def _claim(user_id, event: str, dedupe_key: str):
    """Reserve this send, or return None if it has already gone out.

    Insert-then-send rather than send-then-record: a process that dies between the two
    leaves a claim with no email, which costs one missed notice. The other order leaves
    an email with no claim, which mails the customer again on every subsequent run.

    The insert is a savepoint so a lost race (the unique index refusing it) can be caught
    without poisoning an enclosing transaction - pytest-django wraps each test in one.
    """
    from shared_models.models import SubscriptionEmailLog

    existing = SubscriptionEmailLog.objects.filter(
        event=event, dedupe_key=str(dedupe_key)
    ).first()
    if existing is not None:
        # A previous attempt that failed to deliver is retried; one that succeeded is
        # never touched again.
        return None if existing.status != STATUS_FAILED else existing

    row = SubscriptionEmailLog(
        user_id=str(user_id),
        event=event,
        dedupe_key=str(dedupe_key),
        status=STATUS_FAILED,
    )
    try:
        with transaction.atomic():
            row.save(force_insert=True)
    except IntegrityError:
        # Almost certainly the unique constraint: a concurrent run claimed it first.
        # Either way somebody else owns this send.
        return None
    return row


def notify(user_id, event: str, *, dedupe_key: str, context: dict | None = None) -> bool:
    """Send one billing email to a payer. Returns whether it went out. Never raises.

    ``False`` covers every non-delivery equally — already sent, no address, mail not
    configured, SMTP refused — because no caller can act differently on the difference.
    The distinctions are in the log and in ``subscription_email_log.error``.
    """
    try:
        builder = _COPY.get(event)
        if builder is None:
            logger.error("notify: unknown billing email event {}", event)
            return False

        address, first_name = recipient_for(user_id)
        if not address:
            logger.warning(
                "notify: payer {} has no email address; skipping {}", user_id, event
            )
            return False

        if not mail_configured():
            logger.error("notify: mail is not configured; skipping {}", event)
            return False

        row = _claim(user_id, event, dedupe_key)
        if row is None:
            logger.debug("notify: {} / {} already sent", event, dedupe_key)
            return False

        content = builder(context or {})
        logo = logo_bytes()
        # Resolved from the EVENT, not from anything the builder returns, so copy and art
        # cannot drift apart and no builder has to know a filename.
        art = image_bytes(*illustration_path(event))
        html = render_to_string(
            TEMPLATES.get(event, NOTICE_TEMPLATE),
            {
                "first_name": first_name,
                "base_url": base_url(),
                # Only offered to the template when the bytes are actually going to be
                # attached, so the markup can never reference a part that isn't there.
                "logo_src": f"cid:{LOGO_CID}" if logo else None,
                "illustration_src": f"cid:{ILLUSTRATION_CID}" if art else None,
                **content,
            },
        )
        message = InlineImageMessage(
            subject=content["subject"],
            sender=billing_sender(),
            recipients=[address],
            html=html,
        )
        if logo:
            message.attach(
                "logo.png",
                "image/png",
                logo,
                "inline",
                # Angle brackets are required by RFC 2392 for the header; the ``src``
                # references it WITHOUT them (``cid:minty-logo``). Getting that pair
                # wrong is the usual reason an inline image silently fails to resolve.
                headers={"Content-ID": f"<{LOGO_CID}>",
                         "X-Attachment-Id": LOGO_CID},
            )
        if art:
            message.attach(
                "illustration.png",
                "image/png",
                art,
                "inline",
                headers={"Content-ID": f"<{ILLUSTRATION_CID}>",
                         "X-Attachment-Id": ILLUSTRATION_CID},
            )
        row.recipient = address
        try:
            message.send()
        except Exception as exc:
            # The claim row stays at ``failed`` on purpose, so the next run of the job
            # retries this send rather than treating it as delivered.
            row.error = str(exc)[:500]
            row.save(update_fields=["recipient", "error"])
            logger.error("notify: failed to send {} to {}: {}", event, address, exc)
            return False

        row.status = STATUS_SENT
        row.save(update_fields=["recipient", "status"])
        logger.info("notify: sent {} to {} ({})", event, address, dedupe_key)
        return True
    except Exception:
        # The outermost guard. Nothing about a notification may propagate into a caller
        # that is in the middle of billing somebody. (Flask rolled its session back here;
        # under autocommit there is nothing staged to roll back.)
        logger.exception("notify: unexpected failure sending {}", event)
        return False


def notify_many(events) -> int:
    """Flush a batch of prepared notifications; returns how many were delivered.

    ``events`` are ``(user_id, event, dedupe_key, context)`` tuples. Callers accumulate
    these during their run and flush ONCE at the end — see rule 3 in the module
    docstring. Nothing here raises, so a batch always drains completely.
    """
    sent = 0
    for user_id, event, dedupe_key, context in events:
        if notify(user_id, event, dedupe_key=dedupe_key, context=context):
            sent += 1
    return sent
