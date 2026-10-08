"""The payer portal's billing accounts — the writes behind 08-A, 08-B and 08-C.

A BILLING ACCOUNT is a ``payer_billing_group`` row: the name it bills under ("Bill to"), a
billing email, the cards on it (``billing_account_payment_method``), the ONE card it
charges, the companies it pays for (``entity_billing_group``) and its own dunning clock.
Payers open them with a new card (``payment_methods.confirm_setup`` carrying a company and
an email — the "New billing account" form); here they switch the card one charges, rename
it, give it an address, and move a company from one account to another.

EVERY WRITE ANSWERS ``portal.build_billing_accounts``, so the page redraws from what the
server holds rather than patching itself — the rule the wallet routes already follow.

Three things are deliberately NOT here:

* the Stripe customer's identity. One customer per payer carries one name and email
  (``checkout._payer_identity`` — the oldest named account's), so renaming an account
  changes the portal, not the hosted invoice; ``01_schema_rebased.sql`` item 17 records
  that as owed;
* a column for the address. The account's address IS the billing address of the card it
  charges (the user's decision, 2026-09-25), so ``update`` writes it to that card at
  Stripe;
* any charge. Moving a company prices nothing and takes nothing: its paid days travel
  with it (``store._carry_paid_days``) and its next bill comes from the account it lands
  on.
"""

from __future__ import annotations

import re

from billing.services import store as sub_store
from billing.services._log import logger
from billing.services.payment_methods import (
    ADDRESS_KEYS,
    PaymentMethodError,
    _owned,
    _payer_of,
    account_of,
    write_billing_address,
)

#: One "@", something either side, a dot in the domain, PRINTABLE ASCII ONLY (English only,
#: the user's call 2026-10-01) — the frontends' ``EMAIL_RE`` (minty-web ``lib/emailInput.ts``
#: and its copies), deliberately shallow: these addresses authenticate nobody, and the only
#: real proof one works is sending to it. ``fullmatch`` — ``$`` alone would let a trailing
#: newline through.
EMAIL_RE = re.compile(r"[\x21-\x3F\x41-\x7E]+@[\x21-\x3F\x41-\x7E]+\.[\x21-\x3F\x41-\x7E]+")

#: ``payer_billing_group.billing_email`` / ``billing_company`` are VARCHAR(255).
FIELD_MAX = 255

# The copy is onboarding's 01-D form's, word for word: the same fields in two apps should
# not be explained two ways.
COMPANY_REQUIRED = "Enter the company name to invoice."
EMAIL_REQUIRED = "Enter the email address invoices should go to."
EMAIL_INVALID = "That email address doesn't look right."
#: The frontends' ``EMAIL_ASCII_HINT``, word for word. Its own sentence rather than
#: ``EMAIL_INVALID``: Korean in an address is a rule, not a typo, and the fix differs.
EMAIL_NOT_ENGLISH = "Email can only contain English letters, numbers and symbols."
TOO_LONG = "Keep that under 255 characters."


def email_refusal(email: str, invalid: str) -> str | None:
    """Why ``email`` (already trimmed, non-empty) is refused, or None. Non-ASCII first, in
    its own words; anything else ``EMAIL_RE`` rejects gets the caller's ``invalid``. Shared
    with ``portal.invite_admin_to_entity`` so the two email inputs here cannot drift."""
    if not email.isascii():
        return EMAIL_NOT_ENGLISH
    if not EMAIL_RE.fullmatch(email):
        return invalid
    return None


def validate_identity(email, company, *, require_both: bool) -> tuple[str | None, str | None]:
    """``(email, company)`` trimmed, or a 422 in the form's own words.

    ``require_both`` is the rule for OPENING an account (01-D's): both are its identity.
    Otherwise a field left out (None) is left alone, a blank company is refused — it is the
    account's name, and a blank one would print as an empty "Bill to" — and a blank email
    clears it.
    """
    company = None if company is None else str(company).strip()
    email = None if email is None else str(email).strip()

    if require_both or company is not None:
        if not company:
            raise PaymentMethodError(COMPANY_REQUIRED, status=422)
        if len(company) > FIELD_MAX:
            raise PaymentMethodError(TOO_LONG, status=422)
    if require_both and not email:
        raise PaymentMethodError(EMAIL_REQUIRED, status=422)
    if email:
        refusal = email_refusal(email, EMAIL_INVALID)
        if refusal:
            raise PaymentMethodError(refusal, status=422)
        if len(email) > FIELD_MAX:
            raise PaymentMethodError(TOO_LONG, status=422)
    return email, company


def _accounts(user_id) -> dict:
    from billing.services import portal

    return portal.build_billing_accounts(user_id)


def _payer(user_id) -> dict:
    from billing.services import portal
    from shared_models.models import User

    return portal._person(sub_store._by_pk(User, user_id), user_id)


def _name(group, payer: dict) -> str:
    from billing.services import portal

    return portal.account_name(group, payer)


def _company_name(entity_id) -> str:
    from shared_models.models import Entity

    entity = sub_store._by_pk(Entity, entity_id)
    return ((getattr(entity, "name", None) or "").strip()) or "That company"


# --- the card an account charges -------------------------------------------------------


def set_default_card(user_id, account_id, payment_method_id) -> dict:
    """Make one card on this account the card it CHARGES (08-B "Set as default", 08-N).

    WRITES MONEY FORWARD: from its next bill every company on the account is charged to
    this card, and a retry of a failed bill names it too (dunning passes the account's
    current card). Both halves of the pair move together (``store.set_group_default_card``).

    The Stripe customer's default is left alone. It decides what card pickers offer first,
    which is a payer-wide question this page is not asking; ``payment_methods.remove`` hands
    it over when the card holding it is removed from an account's page.
    """
    _owned(user_id, payment_method_id)
    account = account_of(user_id, account_id)
    if not sub_store.card_on_group(account.id, payment_method_id):
        raise PaymentMethodError("That card isn't on this billing account.", status=404)
    if account.stripe_payment_method_id != payment_method_id:
        sub_store.set_group_default_card(account.id, payment_method_id)
    return _accounts(user_id)


# --- the account's name, email and address (08-C) ----------------------------------------


def _valid_address(address) -> dict:
    """The address as Stripe will take it, or a 422. Line 1 and a registered country are
    required; every other key is optional and a blank one clears."""
    from shared_models.models import CountryInfo

    if not isinstance(address, dict):
        raise PaymentMethodError("Enter the address to bill.", status=422)
    cleaned = {
        key: str(address.get(key)).strip()
        for key in ADDRESS_KEYS
        if address.get(key) is not None
    }
    if not cleaned.get("line1"):
        raise PaymentMethodError("Enter the first line of the address.", status=422)
    country = (cleaned.get("country") or "").upper()
    if not country or not CountryInfo.objects.filter(country_code=country).exists():
        raise PaymentMethodError("Choose a country from the list.", status=422)
    cleaned["country"] = country
    return cleaned


def _valid_cardholder(name) -> str:
    """The name on the charged card, trimmed, or a 422. A blank clears it, as Stripe's ""
    does; the form never sends one (Stripe's address form requires the name)."""
    name = str(name).strip()
    if len(name) > FIELD_MAX:
        raise PaymentMethodError(TOO_LONG, status=422)
    return name


def update(
    user_id,
    account_id,
    *,
    billing_company=None,
    billing_email=None,
    address=None,
    cardholder=None,
) -> dict:
    """Rename an account and/or change its address (08-C "Save billing account").

    ``cardholder`` is the name on the card it charges: 08-C's address form is Stripe's own
    (the AddressElement), which asks for the name with the address, so both go on that card
    in one write.

    EVERYTHING IS VALIDATED BEFORE ANYTHING IS WRITTEN, then Stripe first. The address
    lives at Stripe (on the card the account charges) and Stripe is the write that fails —
    a network error, a refused field — so failing there leaves nothing changed. The local
    rename almost never fails; when it does after Stripe succeeded, the payer is told
    exactly that, and both halves are safe to repeat.
    """
    account = account_of(user_id, account_id)
    email, company = validate_identity(billing_email, billing_company, require_both=False)
    cleaned = _valid_address(address) if address is not None else None
    holder = _valid_cardholder(cardholder) if cardholder is not None else None
    if company is None and email is None and cleaned is None and holder is None:
        raise PaymentMethodError("There was nothing to change.", status=422)

    if cleaned is not None or holder is not None:
        try:
            _owned(user_id, account.stripe_payment_method_id)
        except PaymentMethodError as exc:
            raise PaymentMethodError(
                "This billing account has no card to keep its address on. "
                "Add a card to it first.",
                status=409,
            ) from exc
        write_billing_address(user_id, account.stripe_payment_method_id, cleaned, name=holder)

    if company is not None or email is not None:
        try:
            sub_store.set_account_identity(
                account.id, billing_email=email, billing_company=company
            )
        except Exception as exc:
            logger.exception("billing accounts: could not rename account {}", account.id)
            if cleaned is not None or holder is not None:
                raise PaymentMethodError(
                    "Your address was saved, but the company name and email weren't. "
                    "Please try again.",
                    status=500,
                ) from exc
            raise
    return _accounts(user_id)


# --- moving a company between accounts ("Change billing account") ------------------------


def move_company(user_id, entity_id, account_id) -> dict:
    """Put one company on one of the payer's accounts. Returns the accounts, plus ``moved`` —
    what went where (``from_account`` null for a first placement), or None when it was
    already there.

    A company on NO account yet — a trial started without a card — is PLACED rather than
    moved (source ``chosen``): Manage Subscriptions asks which account bills a change before
    it applies it, and a card-free trial is exactly the company that has none. Placing it
    charges nothing and writes no consent — "you may bill me for this company" stays the
    confirm's own statement (``authorize-billing`` and the rest), made right after; a placed
    company with no consent is billed nothing, and its trial still only ends. There are no
    paid days to carry.

    THAT COMPANY ALSO HAS NO SUBSCRIBER, since a trial establishes none, so the placement
    is made with ``establish_payer=True`` — without it ``_payer_of`` would refuse 409 and
    the promise above would be unreachable. It widens who may place a company, to any
    member of one that nobody pays for, which is the same rule the rest of the
    subscription obeys (``store.may_manage_subscription``); ``_payer_of`` still answers
    404 the moment somebody else IS the payer, so this is never a way past one that
    exists. Placing nominates for a payer the module rows do not yet carry: the caller
    confirms billing in the SAME request (``modules._activate_subscription``), which
    stamps that same user, so the two cannot be left disagreeing.

    Refused, each in words that name the fix:

    * a PAST-DUE company. Its debt is an invoice the account it is on raised: the retries,
      "Pay now" (``dunning.retry_now`` settles the account the company is on NOW) and the
      recovery that restores its access (``end_group_dunning`` → the account's companies)
      all follow the account. Moved, they would chase the wrong one and the company would
      stay past due with nothing able to clear it;
    * a target account IN DUNNING — the company's access would be measured against a date
      that has stopped, and it would join a collection already failing;
    * a target account whose card Stripe no longer holds — its next bill could not be paid.
    """
    payer = _payer_of(user_id, entity_id, establish_payer=True)
    target = account_of(user_id, account_id)
    nomination = sub_store.nomination_for_entity(entity_id, payer)
    company = _company_name(entity_id)
    if nomination is not None and str(nomination.billing_group_id) == str(target.id):
        return {**_accounts(user_id), "moved": None}

    person = _payer(user_id)
    leaving = sub_store.billing_group(nomination.billing_group_id) if nomination else None
    if sub_store.entity_is_past_due(entity_id, payer):
        raise PaymentMethodError(
            f"{company}'s last payment on {_name(leaving, person)} didn't go through. "
            "Settle it there first, then move the company."
            if leaving is not None
            else f"{company}'s last payment didn't go through. "
            "Settle it first, then choose its billing account.",
            status=409,
        )
    if target.dunning_started_at is not None:
        raise PaymentMethodError(
            f"A payment on {_name(target, person)} didn't go through. Settle it before "
            "moving a company onto it.",
            status=409,
        )
    try:
        _owned(user_id, target.stripe_payment_method_id)
    except PaymentMethodError as exc:
        raise PaymentMethodError(
            f"{_name(target, person)} has no card it can charge. Add a card to it first.",
            status=409,
        ) from exc

    placing = nomination is None
    sub_store.nominate_group_for_entity(
        entity_id, payer, target.id, source="chosen" if placing else "moved"
    )
    if placing:
        logger.info(
            "billing accounts: payer {} placed {} on account {}", user_id, entity_id, target.id
        )
    else:
        logger.info(
            "billing accounts: payer {} moved {} from account {} to {}",
            user_id, entity_id, getattr(leaving, "id", None), target.id,
        )
    payload = _accounts(user_id)
    payload["moved"] = {
        "entity_id": str(entity_id),
        "entity_name": company,
        "from_account": (
            {"id": str(leaving.id), "name": _name(leaving, person)} if leaving else None
        ),
        "to_account": {"id": str(target.id), "name": _name(target, person)},
    }
    return payload


# --- retrying a failed invoice (08-B's invoice row) --------------------------------------

NOT_WAITING = "That invoice isn't waiting for a payment."


def retry_invoice(user_id, invoice_id) -> dict | None:
    """Collect ONE failed invoice now, on the card of the account it belongs to (08-B's
    *Retry payment*). None when it is not the caller's, or not an invoice id at all.

    The engine's manual collection, unchanged (``dunning.retry_now``): the same attempt
    budget and give-up deadline as the scheduled retries, the period settled and access
    switched back on when it is paid. Two things are pinned from the row: the CARD - the
    invoice's account, or the payer's oldest for one raised before accounts (the attribution
    dunning collects by) - and the INVOICE, charged only if it is the one those rules pick;
    otherwise ``not_this_invoice``, with nothing charged and no attempt spent.

    And only one the page OFFERS it on (``portal.retryable_invoice_ids``): a page read before
    something moved, or a direct call, cannot reach a bill the business does not chase - the
    abandoned renewal of an account whose access has run out.
    """
    import uuid

    from billing.services.dunning import retry_now
    from billing.services.portal import FAILED_INVOICE_STATUSES, retryable_invoice_ids
    from shared_models.models import SubscriptionInvoice

    try:
        wanted = str(uuid.UUID(str(invoice_id)))
    except ValueError:
        return None
    invoice = SubscriptionInvoice.objects.filter(id=wanted, payer_user_id=str(user_id)).first()
    if invoice is None:
        return None
    if (invoice.status or "").lower() not in FAILED_INVOICE_STATUSES or not invoice.external_id:
        # Re-issued since the page was drawn (the processor would no longer collect it, so
        # dunning voided it and raised an identical replacement): the debt is still there,
        # on another row - not "nothing to pay" but "look again".
        if sub_store.replacement_of(invoice) is not None:
            return {"status": "not_this_invoice", "attempts": 0,
                    "invoice": invoice.external_id, "reason": None}
        raise PaymentMethodError(NOT_WAITING, status=409)
    if str(invoice.id) not in retryable_invoice_ids(user_id):
        return {"status": "not_this_invoice", "attempts": 0,
                "invoice": invoice.external_id, "reason": None}

    groups = sub_store.billing_groups_for_payer(user_id)
    group_id = invoice.billing_group_id or (groups[0].id if groups else None)
    return retry_now(
        user_id,
        group_id=str(group_id) if group_id else None,
        expect_invoice=invoice.external_id,
    )
