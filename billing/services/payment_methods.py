"""The payer's saved payment methods — the read model and the writes behind them.

ONE SHELF, SEVERAL COMPANIES ON IT. The methods here belong to the PAYER: they hang off
one Stripe customer, and that has not changed. What has changed is which of them gets
charged for what. Each company is nominated onto one saved method — the pair is a
``payer_billing_group``, which owns that card's ``paid_through`` and its own dunning
clock — and ``renewals`` raises one invoice per group rather than one per payer.

So there are two different writes on this screen, and the difference matters:

* ``set_for_entity`` puts ONE company on a card. This is the write with billing
  consequences now, and they are confined to that company.
* ``set_default`` still exists but means less than it used to. It nominates nothing: it
  is the card offered first when a company is being put on one, and the account's answer
  to "which of these is the main one". Renewals do not read it.

NO SILENT FALLBACK. A company with no nomination is not billed on the default — it is not
billed at all, and the charge paths report it (see ``store.card_for_entity``). Inheriting
a card quietly is exactly how one card came to pay for every company here in the first
place.

WHAT IS ACTUALLY STORED WHERE. Stripe holds the card; this application holds an id. The
number is typed into Stripe Elements in the browser and confirmed straight against a
SetupIntent — it never touches this process, this database or these logs, which is what
keeps the application out of PCI scope. Moving the management UI in-app does not move the
card in-app, and nothing here should ever be extended to accept a PAN.

WHY THE SHELF EXISTS AT ALL. The billing engine once consulted only
``invoice_settings.default_payment_method``: one card, set at capture, replaced by
sending the payer to Stripe's hosted form. The shelf outlived that, and now holds the
cards companies are nominated onto — several of them in use at once, plus the ones a
payer saves ahead of an expiry so they can switch on their own date rather than on a
failed renewal.

THE TWO REFUSALS. Both exist because the account is live and unattended money depends on
it:

* the DEFAULT cannot be detached while another method exists — promote first, or the
  account is left holding cards with none nominated, and Stripe clears the default on
  detach;
* the LAST method cannot be detached at all while something is billing forward — the next
  renewal would decline into dunning by design rather than by accident.

Nothing here starts, stops or prices a subscription. Cancelling stays in the in-app
prorated flow; this module changes what gets charged, never what is owed.
"""

from __future__ import annotations

from datetime import UTC

from billing.services import clock, display
from billing.services import store as sub_store
from billing.services._log import logger
from billing.services.stripe_client import (
    attach_payment_method,
    create_customer_for_user,
    create_setup_intent,
    customer_default_payment_method,
    detach_payment_method,
    forget_default_payment_method,
    get_publishable_key,
    list_payment_methods,
    retrieve_payment_method,
    retrieve_setup_intent,
    set_customer_default_payment_method,
    update_payment_method,
)

# How near an expiry has to be before the page flags it. Two months, because the warning
# is only worth printing while it can still be acted on: a card expiring at the end of
# next month has at most one renewal left on it, and the payer needs the replacement
# saved before that renewal, not after it declines.
EXPIRING_SOON_MONTHS = 2

#: The refusal for a card confirmed into NO billing account (the user's rule, 2026-10-01:
#: a card is only ever added through a billing account). The web matches on it.
ACCOUNT_REQUIRED = "Choose a billing account for this card."


class PaymentMethodError(Exception):
    """Raised with a message the page can show verbatim, and the status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def run(handler, user_id) -> tuple[dict, int]:
    """Run one payment-method action for ``user_id``. Answers ``(payload, status)``.

    THE THREE FAILURE MODES every transport shares, written once. ``PaymentMethodError``
    carries a message written for the customer and the status to say it with (409 for the
    two removal refusals, 422 for a bad expiry); anything else is a bug or Stripe being
    down, and says so without leaking what broke.

    AUTH AND CORS ARE NOT HERE. They are the only things the transports actually differ
    in — the bearer routes in ``routes.portal`` resolve a token and wrap the answer in
    CORS headers, the onboarding twins do the same with a different origin, and the
    session routes on the settings page do neither. Everything between was three copies
    of this function waiting to drift.
    """
    try:
        return handler(user_id), 200
    except PaymentMethodError as exc:
        return {"error": exc.message}, exc.status
    except Exception:
        logger.exception("payment methods: action failed for user {}", user_id)
        return {"error": "Something got stuck on our end. Let's try again?"}, 500


# --- Reading -----------------------------------------------------------------


def _fmt(moment) -> str | None:
    """'15 Aug 2026' — the same zero-padded form the rest of the portal prints, and now
    literally the same function: see ``display.day_padded``."""
    return display.day_padded(moment)


def _months_until(exp_year, exp_month, now) -> int | None:
    """Whole months from ``now`` to the END of the card's expiry month.

    A card is good through the last day of its expiry month, so 04/29 during April 2029 is
    still live and answers 0 — not expired, not yet next month's problem. Negative means
    the month has passed.
    """
    if not exp_year or not exp_month:
        return None
    return (int(exp_year) - now.year) * 12 + (int(exp_month) - now.month)


def _brand_label(brand: str | None) -> str:
    """'visa' -> 'Visa', 'amex' -> 'Amex', 'mastercard' -> 'Mastercard'."""
    return (brand or "card").replace("_", " ").title()


# How a tokenised card was presented. ``card.wallet.type`` is set when the number Stripe
# holds came through a wallet rather than off the plastic — the underlying card is still
# the one that gets charged, but the payer thinks of it as "my Apple Pay", and a row that
# does not say so is a card they will not recognise.
#
# Title-casing the raw value gets most of these right and mangles the ones that matter
# ("Apple Pay" survives, "Amex Express Checkout" does not), so the names Stripe documents
# are spelled out and anything new falls back to the generic rule.
_WALLET_LABELS = {
    "apple_pay": "Apple Pay",
    "google_pay": "Google Pay",
    "samsung_pay": "Samsung Pay",
    "link": "Link",
    "visa_checkout": "Visa Checkout",
    "amex_express_checkout": "Amex Express Checkout",
    "masterpass": "Masterpass",
}


def _wallet_label(wallet_type: str | None) -> str | None:
    if not wallet_type:
        return None
    return _WALLET_LABELS.get(wallet_type, wallet_type.replace("_", " ").title())


def _view(pm, default_id: str | None, now, country_names: dict | None = None) -> dict:
    """One saved method, in the shape the page renders.

    Built from the PaymentMethod object already in hand rather than by calling
    ``payment_method_display`` per row — that helper retrieves by id, which would be one
    network round trip per card on a page whose whole content is the list.

    A method with no ``card`` object is not a failure to describe: a Stripe Link wallet
    exposes none, and "Link" is the true answer. Its card fields stay null and the UI has
    to cope, exactly as ``payment_method_display`` decided.
    """
    pm_id = pm.get("id")
    card = pm.get("card") or {}
    kind = pm.get("type") or ""
    details = pm.get("billing_details") or {}
    address = details.get("address") or {}

    brand, last4 = card.get("brand"), card.get("last4")
    exp_month, exp_year = card.get("exp_month"), card.get("exp_year")
    months_left = _months_until(exp_year, exp_month, now)
    wallet_type = ((card.get("wallet") or {}) or {}).get("type")

    # THE ISSUING COUNTRY, falling back to the billing address.
    #
    # A DELIBERATE CHOICE between two facts that disagree constantly, and it looks like a
    # bug from the inside: pick the Philippines in the card form and the row still reads
    # "United States", because `card.country` is where the card was ISSUED and Stripe's
    # 4242 test card is issued in the US. That is the column working.
    #
    # The issuer is the fact worth printing. It is a property of the card, which is what
    # these rows are; it is what drives cross-border fees and the declines that come with
    # them; and it is the half the payer cannot see anywhere else. The billing address is
    # already in the row's own Edit dialog, and it is whatever the payer last typed.
    #
    # The address is the FALLBACK rather than nothing, because a wallet has no card object
    # and therefore no issuer to name, and an address country beats a blank cell.
    country_code = card.get("country") or address.get("country")

    created = pm.get("created")
    added = None
    if created:
        try:
            from datetime import datetime

            added = datetime.fromtimestamp(int(created), tz=UTC)
        except (TypeError, ValueError, OSError):
            added = None

    return {
        "id": pm_id,
        "type": kind or ("card" if card else ""),
        "brand": brand,
        "brand_label": _brand_label(brand) if card else kind.replace("_", " ").title(),
        "last4": last4,
        # The complete string to print when a row has to be described in one line.
        "label": (
            f"{_brand_label(brand)} •••• {last4}"
            if last4
            else (kind.replace("_", " ").title() or "Saved method")
        ),
        # Stripe's own field. Often NOT the payer — a finance lead's card on a director's
        # account is normal — so it is shown rather than assumed.
        "cardholder": details.get("name"),
        "email": details.get("email"),
        "address": {
            "line1": address.get("line1"),
            "line2": address.get("line2"),
            "city": address.get("city"),
            "state": address.get("state"),
            "postal_code": address.get("postal_code"),
            "country": address.get("country"),
        },
        "exp_month": exp_month,
        "exp_year": exp_year,
        "expiry": (
            f"{int(exp_month):02d}/{int(exp_year) % 100:02d}"
            if exp_month and exp_year
            else None
        ),
        # "credit" / "debit" / "prepaid". Stripe's own classification, not a guess from
        # the brand — a debit Visa and a credit Visa are the same brand and behave
        # differently at the issuer.
        "funding": card.get("funding"),
        # How it was presented: "apple_pay", "google_pay", "link"… None for a card
        # entered as a number.
        "wallet": wallet_type,
        "wallet_label": _wallet_label(wallet_type),
        "country": country_code,
        # Resolved against the country registry, so this column reads the way the rest of
        # the portal's country columns do. Falls back to the code — a two-letter cell is a
        # worse answer than a name and a better one than a blank.
        "country_name": (country_names or {}).get(country_code or "", country_code),
        "is_default": bool(default_id) and pm_id == default_id,
        "expired": months_left is not None and months_left < 0,
        "expires_soon": (
            months_left is not None and 0 <= months_left <= EXPIRING_SOON_MONTHS
        ),
        "added": _fmt(added),
        "added_iso": added.isoformat() if added else None,
    }


def _sorted(views: list[dict]) -> list[dict]:
    """Default first, then newest saved first.

    The default is the only row with billing meaning, so it leads regardless of when it
    was added — a payer scanning for "what will actually be charged" should not have to
    find it.
    """
    return sorted(
        views,
        key=lambda v: (0 if v["is_default"] else 1, -(_epoch(v))),
    )


def _epoch(view: dict) -> int:
    from datetime import datetime

    iso = view.get("added_iso")
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return 0


def _bills_forward(user_id) -> bool:
    """Is anything on this account due to be charged again.

    Asked before the last saved method may be removed. ``is_billing_forward`` is the same
    question the renewal runner asks when it decides what to put on the next invoice — see
    ``access.is_billing_forward``, which distinguishes it from "do they have it" and "is
    money involved" — so the refusal cannot disagree with what would actually be charged.

    Fails CLOSED. If the rows cannot be read we answer True and refuse the removal: a
    refusal the payer can retry is a smaller harm than a silently emptied wallet on an
    account that renews next week.
    """
    from billing.services import access

    try:
        return any(
            access.is_billing_forward(phase=getattr(row, "phase", None) or "")
            for row in sub_store.module_rows_for_payer(user_id)
        )
    except Exception:
        logger.exception(
            "payment methods: could not read the module rows for payer {}", user_id
        )
        return True


def _companies_billing_on(user_id, payment_method_id: str) -> list[str]:
    """Names of the companies this card is due to be charged for. Empty is the safe case.

    Only ones still BILLING FORWARD count — ``access.is_billing_forward``, the same
    question the renewal runner asks about what goes on the next invoice, so the refusal
    and the charge cannot disagree. A cancelled company keeps its nomination as history
    and must not block the payer from tidying up a card.

    Fails CLOSED like ``_bills_forward``: if this cannot be read it claims the card is in
    use. A refusal the payer can retry is a far smaller harm than detaching the card three
    companies renew on.

    EVERY ACCOUNT CHARGING THE CARD, not the first. Two accounts may charge one card since
    ``v1a01_billing_account``, and stopping at the first let a card be detached while the
    second account's companies still renewed on it.
    """
    from billing.services import access

    try:
        entity_ids: set[str] = set()
        for candidate in sub_store.billing_groups_for_payer(user_id):
            if candidate.stripe_payment_method_id == payment_method_id:
                entity_ids |= sub_store.entity_ids_in_group(candidate.id)
        if not entity_ids:
            return []
        live = {
            str(row.entity_id)
            for row in sub_store.module_rows_for_payer(user_id)
            if str(row.entity_id) in entity_ids
            and access.is_billing_forward(phase=getattr(row, "phase", None) or "")
        }
        if not live:
            return []
        from shared_models.models import Entity

        rows = Entity.objects.filter(id__in=sorted(live))
        return sorted((e.name or "").strip() or str(e.id) for e in rows)
    except Exception:
        logger.exception(
            "payment methods: could not read what {} is billing for payer {}",
            payment_method_id, user_id,
        )
        return ["your subscriptions"]


def _still_billing_message(names: list[str]) -> str:
    """The refusal, naming the companies — because the fix is per company.

    Listed rather than counted: the payer has to go and move each one onto another card,
    and "3 companies" does not tell them which. Capped so a payer with thirty does not get
    a paragraph.
    """
    shown = names[:3]
    rest = len(names) - len(shown)
    listed = ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")
    return (
        f"{listed} {'is' if len(names) == 1 else 'are'} billed to this payment method. "
        "Move them to another card first, then remove it."
    )


def list_for_user(user_id) -> dict:
    """Every method saved on the payer's account, with the default marked.

    ``has_account`` is the distinction the page has to be able to draw: a payer whose
    trial never captured a card has no Stripe customer at all, which is not an empty
    wallet and not an error — it is the state where "Add payment method" is the only thing
    on the screen.

    Stripe failures PROPAGATE. An empty list rendered after a failed read tells a payer
    their cards are gone; the page can retry an error.
    """
    customer_id = sub_store.customer_id_for_user(user_id)
    if not customer_id:
        return {"has_account": False, "default_id": None, "methods": [], "total": 0}

    default_id = customer_default_payment_method(customer_id)
    now = clock.now()
    raw = list_payment_methods(customer_id)

    # ONE registry query for the whole list, not one per row. Reuses the portal's own
    # resolver so this column and the entity ones name a country the same way.
    #
    # NEVER FAILS THE PAGE OVER A LABEL — the same posture the resolver itself takes, and
    # the wrapper is here because its own handler cannot always run: it rolls back on
    # error, and the rollback needs the app context the failure may have been about. Every
    # cell falls back to the two-letter code, which is a worse answer than a name and a far
    # better one than an error where the payer's cards should be.
    from billing.services.portal import _country_names

    codes = {
        (pm.get("card") or {}).get("country")
        or ((pm.get("billing_details") or {}).get("address") or {}).get("country")
        for pm in raw
    }
    try:
        names = _country_names(codes)
    except Exception:
        logger.exception("payment methods: could not resolve country names")
        names = {}

    methods = _sorted([_view(pm, default_id, now, names) for pm in raw])
    return {
        "has_account": True,
        "default_id": default_id,
        "methods": methods,
        "total": len(methods),
    }


def accounts_for_user(user_id) -> dict:
    """The payer's billing accounts, each with the cards on it.

    What the card picker renders. An ACCOUNT is a name, a set of cards, and the one card
    it charges; the payer chooses between accounts, not between loose cards, which is the
    distinction ``payer_billing_group`` has carried since it stopped being "the account
    default".

    ONE STRIPE READ FOR THE WHOLE PAGE. The card descriptions come from
    ``list_for_user`` and are looked up by id, rather than each account fetching its own —
    a payer with three accounts on two cards would otherwise read the same method twice
    and could render it two different ways.

    A card on the shelf that Stripe no longer knows about is DROPPED from the account
    rather than shown as a blank row: it was detached at Stripe, and the shelf is a local
    copy of a fact Stripe owns.
    """
    wallet, groups = _accounts_with_cards(user_id)

    accounts = [
        {
            "id": group.id,
            "billing_email": group.billing_email,
            "billing_company": group.billing_company,
            "default_id": group.stripe_payment_method_id,
            "cards": cards,
            "total": len(cards),
        }
        for group, cards in groups
    ]

    return {
        "has_account": wallet["has_account"],
        "accounts": accounts,
        "total": len(accounts),
        # The flat wallet as well, because "add a card to an account" and "the payer's
        # cards" are different questions and the dialog asks both.
        "methods": wallet["methods"],
        "default_id": wallet["default_id"],
    }


def _accounts_with_cards(user_id) -> tuple[dict, list[tuple]]:
    """``(wallet, [(account, cards)])`` — the payer's accounts, oldest first, each with the
    live card views on its shelf (default first).

    The one Stripe read (``list_for_user``) behind both readers of accounts: the onboarding
    picker (``accounts_for_user``) and the payer portal (``portal.build_billing_accounts``).
    The card dicts are SHARED between accounts holding the same card, and their
    ``is_default`` is the Stripe customer's default — a reader that marks a card per
    account copies it first.
    """
    wallet = list_for_user(user_id)
    by_id = {m["id"]: m for m in wallet["methods"]}
    groups = [
        (
            group,
            [
                by_id[row.stripe_payment_method_id]
                for row in sub_store.cards_in_group(group.id)
                if row.stripe_payment_method_id in by_id
            ],
        )
        for group in sub_store.billing_groups_for_payer(user_id)
    ]
    return wallet, groups


# --- Adding ------------------------------------------------------------------


def start_setup(user_id) -> dict:
    """Open a SetupIntent for the in-app card form, and hand back what Elements needs.

    ``client_secret`` authorises the browser to confirm THIS intent and nothing else, and
    the publishable key is safe to publish by definition. Neither is a credential for this
    application.

    Works for a payer with NO customer, which is the point: a SetupIntent needs none, so
    the first card can be saved from the billing page — and the customer is created in
    ``confirm_setup`` once Stripe says the card is real. This is the ONLY way a card is
    added (the Stripe-hosted Checkout and Billing Portal routes were deleted 2026-10-01).

    Adding a card AUTHORISES NOTHING. Billing an entity needs that entity's own consent
    (``store.has_billing_consent``), which is granted on its settings page and is exactly
    what stops a card saved here from silently converting some other company's trial.
    """
    customer_id = sub_store.customer_id_for_user(user_id)
    intent = create_setup_intent(customer_id, user_id=user_id)
    key = get_publishable_key()
    if not key:
        # Elements cannot be mounted without it, and failing here names the cause. The
        # alternative is a card form that renders and then silently refuses to confirm.
        logger.error("payment methods: STRIPE_PUBLISHABLE_KEY is not configured")
        raise PaymentMethodError(
            "Card payments aren't configured on this environment.", status=503
        )
    return {
        "client_secret": intent.get("client_secret"),
        "publishable_key": key,
        "setup_intent": intent.get("id"),
    }


def _payer_customer_for_confirm(user_id, intent_customer: str | None) -> tuple[str, bool]:
    """The customer the confirmed card belongs on. Returns ``(customer_id, created)``.

    Three cases, and the ordering matters:

    * the payer already has a customer — it WINS, even if the intent named another. Two
      customers for one payer make ``find_customer_by_user`` ambiguous, which is worse
      than the duplicate itself.
    * the intent carried one and the payer has none — adopt it.
    * neither — create one now. This is the first moment the card is a fact, which is the
      only moment a customer may be made.
    """
    # Resolved rather than read off the mapping: a payer whose row went missing has a real
    # card-bearing customer in Stripe, and creating a second one here would strand it.
    from billing.services.checkout import (
        _payer_identity,
        _resolve_customer_id,
        _seed_user_customer_mapping,
    )

    existing = _resolve_customer_id(user_id)
    if existing:
        if intent_customer and intent_customer != existing:
            logger.warning(
                "payment methods: setup intent for payer {} named customer {} but the "
                "payer already has {} — attaching to the existing one",
                user_id, intent_customer, existing,
            )
        return existing, False

    if intent_customer:
        _seed_user_customer_mapping(user_id, intent_customer)
        return intent_customer, False

    customer = create_customer_for_user(user_id, **_payer_identity(user_id))
    customer_id = customer.get("id")
    if not customer_id:
        raise PaymentMethodError(
            "We couldn't open a billing account for that card. Let's try again?",
            status=502,
        )
    # NOT swallowed the way ``_seed_user_customer_mapping`` swallows its own failures: the
    # anchor and paid-through live on this row, so without it the next charge cannot be
    # billed at all. The ``metadata.user_id`` stamp above keeps the customer findable, so
    # a raise here loses nothing but the request.
    sub_store.upsert_customer_mapping(user_id, customer_id)
    return customer_id, True


def confirm_setup(
    user_id,
    setup_intent_id: str,
    *,
    make_default: bool = False,
    billing_group_id=None,
    billing_email=None,
    billing_company=None,
) -> dict:
    """Take ownership of a card the browser just confirmed. Returns the fresh list.

    The browser confirms the SetupIntent directly with Stripe, so this is the app finding
    out what happened — the id comes back from the client and every fact is re-read from
    Stripe rather than trusted.

    ``metadata.user_id`` is the gate. A SetupIntent with no customer has nothing else
    tying it to anybody, so one that does not carry the caller's own stamp is refused: it
    is either another payer's or not ours at all.

    Idempotent. Re-running on the same intent re-attaches an already-attached method
    (Stripe accepts it), re-sets the same default, and returns the same list — a
    double-click or a retried request cannot produce two cards or two customers.

    THE BILLING ACCOUNT IS REQUIRED, AND IS NEVER CREATED BY ACCIDENT.

    * ``billing_group_id`` — put the card on an account that already exists. The account
      is checked to be the caller's first; a group id from the browser naming someone
      else's account would otherwise move a card onto it.
    * ``billing_email`` AND ``billing_company`` with no group — OPEN a new account on this
      card, named ("New billing account").
    * neither — REFUSED, 422 ``ACCOUNT_REQUIRED``, before Stripe is asked anything. A card
      is only ever added through a billing account (the user's rule, 2026-10-01); the old
      "save the card and nothing else" path is gone. Routes should come in through
      ``confirm_into_account``, which also validates the account fields first.

    A RETRY DOES NOT OPEN A SECOND ACCOUNT. Two accounts on one card are legal, but never
    on a card this call has only just confirmed: a SetupIntent always makes a fresh
    ``pm_...``, so an account of this payer already charging it can only be the one an
    earlier attempt at this same request opened — whose answer was lost on the way back.
    That account is renamed with what this attempt carried and answered again.
    """
    # FIRST, before any Stripe call: refusing after the attach below would leave a card
    # saved against no account, which is exactly what this rule forbids.
    _require_account(billing_group_id, billing_email, billing_company)

    intent = retrieve_setup_intent(setup_intent_id)
    if not intent:
        raise PaymentMethodError("That card setup couldn't be found.", status=404)

    stamped = str((intent.get("metadata") or {}).get("user_id") or "")
    if stamped != str(user_id):
        # Same answer as "not found": telling the two apart confirms an id to someone who
        # should not be asking.
        raise PaymentMethodError("That card setup couldn't be found.", status=404)

    status = intent.get("status")
    payment_method = intent.get("payment_method")
    if isinstance(payment_method, dict):
        payment_method = payment_method.get("id")
    if status != "succeeded" or not payment_method:
        # The card was declined, abandoned, or still needs the customer to authenticate.
        # Nothing is saved and nothing is created — notably no customer.
        raise PaymentMethodError(
            "That card wasn't saved. Please check the details and try again.", status=409
        )

    intent_customer = intent.get("customer")
    if isinstance(intent_customer, dict):
        intent_customer = intent_customer.get("id")

    customer_id, created_customer = _payer_customer_for_confirm(user_id, intent_customer)

    # Stripe attaches automatically when the intent named a customer. It cannot have when
    # the payer had none, and the customer we just made is not the one the intent knew
    # about — so attach explicitly. Attaching an already-attached method to the same
    # customer is a no-op at Stripe, which is what keeps the retry above harmless.
    try:
        attach_payment_method(payment_method, customer_id)
    except Exception:
        # Only a genuine failure matters here; the common "already attached" case does not
        # raise. Re-read below decides whether anything actually landed.
        logger.exception(
            "payment methods: could not attach {} to {}", payment_method, customer_id
        )

    forget_default_payment_method(customer_id)
    current_default = customer_default_payment_method(customer_id)
    # The FIRST card is always the default — an account whose only saved method is not
    # nominated has nothing to charge, which is the state dunning exists to shout about.
    if make_default or created_customer or not current_default:
        set_customer_default_payment_method(customer_id, payment_method)

    account = _account_for_confirm(
        user_id, payment_method, billing_group_id, billing_email, billing_company
    )

    payload = list_for_user(user_id)
    # Additive: every existing caller reads ``methods`` and is untouched.
    payload["account"] = {
        "id": account.id,
        "billing_email": account.billing_email,
        "billing_company": account.billing_company,
        "default_id": account.stripe_payment_method_id,
    }
    return payload


def _require_account(billing_group_id, billing_email, billing_company) -> None:
    """Refuse a confirm that names no billing account: neither an existing account id nor
    BOTH the fields that open a new one. 422 ``ACCOUNT_REQUIRED``."""
    if str(billing_group_id or "").strip():
        return
    if str(billing_email or "").strip() and str(billing_company or "").strip():
        return
    raise PaymentMethodError(ACCOUNT_REQUIRED, status=422)


def confirm_into_account(
    user_id,
    setup_intent_id: str,
    *,
    make_default: bool = False,
    billing_group_id=None,
    billing_email=None,
    billing_company=None,
) -> dict:
    """The two confirm routes' shared front half (the payer portal's and the onboarding
    twin's), then ``confirm_setup``.

    Every check that can refuse runs HERE, before the service attaches anything at Stripe —
    a refusal after the attach would leave a card saved against no account:

    * a ``billing_group_id`` must be the caller's own account (``account_of``, 404); an
      email sent with it renames the account and is held to the email rule;
    * otherwise a company or an email means "open a new account", and BOTH are required
      (``validate_identity``, 422 in the form's own words);
    * otherwise nothing names an account: 422 ``ACCOUNT_REQUIRED``.
    """
    from billing.services.billing_accounts import validate_identity

    group_id = str(billing_group_id or "").strip() or None
    email, company = billing_email, billing_company
    if group_id:
        account_of(user_id, group_id)
        email, _ = validate_identity(email, None, require_both=False)
    elif email is not None or company is not None:
        email, company = validate_identity(email, company, require_both=True)
    else:
        raise PaymentMethodError(ACCOUNT_REQUIRED, status=422)
    return confirm_setup(
        user_id, setup_intent_id, make_default=make_default,
        billing_group_id=group_id, billing_email=email, billing_company=company,
    )


def _account_for_confirm(
    user_id, payment_method, billing_group_id, billing_email, billing_company
):
    """The billing account this confirmed card belongs to — never None: ``confirm_setup``
    has already refused a confirm naming no account (``_require_account``).

    Split out because it is the only part of ``confirm_setup`` that touches local state,
    and it must run AFTER Stripe has confirmed the card exists — an account opened for a
    card that was declined is an account that can never be charged.
    """
    if billing_group_id:
        # THE OWNERSHIP CHECK. The id came from the browser; without this, naming
        # another payer's account moves a card onto it.
        account = account_of(user_id, billing_group_id)
        sub_store.add_card_to_group(account.id, payment_method)
        # Commits the shelf row too — ``add_card_to_group`` deliberately leaves the unit
        # of work open so the two land together. With both fields None this only commits.
        return sub_store.set_account_identity(
            account.id, billing_email=billing_email, billing_company=billing_company
        )

    retried = next(
        (
            group
            for group in sub_store.billing_groups_for_payer(user_id)
            if group.stripe_payment_method_id == payment_method
        ),
        None,
    )
    if retried is not None:
        logger.info(
            "payment methods: payer {} re-confirmed {}; answering account {} again",
            user_id, payment_method, retried.id,
        )
        return sub_store.set_account_identity(
            retried.id, billing_email=billing_email, billing_company=billing_company
        )
    return sub_store.create_billing_account(
        user_id, payment_method,
        billing_email=billing_email, billing_company=billing_company,
    )


def account_of(user_id, account_id):
    """The caller's billing account ``account_id``, or a refusal the page can show.

    THE OWNERSHIP CHECK for every account id that arrives from the browser — the same rule
    ``_owned`` applies to a ``pm_...``: somebody else's account answers exactly like one
    that does not exist, so a guessed id confirms nothing.
    """
    wanted = str(account_id or "").strip()
    if not wanted:
        raise PaymentMethodError("No billing account was given.", status=400)
    account = sub_store.billing_group(wanted)
    if account is None or str(account.payer_user_id) != str(user_id):
        raise PaymentMethodError("That billing account couldn't be found.", status=404)
    return account


# --- Editing, promoting, removing --------------------------------------------


def _owned(user_id, payment_method_id: str) -> tuple[str, dict]:
    """``(customer_id, payment_method)`` once the method is proven to be the caller's.

    THE SECURITY CHECK for every mutation below. The id arrives from the browser, so
    ``pm.customer`` is compared against the customer resolved from the TOKEN's user — a
    ``pm_…`` id belonging to somebody else answers "not found" rather than being acted on.
    """
    if not payment_method_id:
        raise PaymentMethodError("No payment method was given.", status=400)

    customer_id = sub_store.customer_id_for_user(user_id)
    if not customer_id:
        raise PaymentMethodError("You don't have a billing account yet.", status=409)

    try:
        pm = retrieve_payment_method(payment_method_id)
    except Exception as exc:
        logger.exception(
            "payment methods: could not read {} for payer {}", payment_method_id, user_id
        )
        raise PaymentMethodError(
            "That payment method couldn't be found.", status=404
        ) from exc

    pm_customer = (pm or {}).get("customer")
    if isinstance(pm_customer, dict):
        pm_customer = pm_customer.get("id")
    if not pm or str(pm_customer or "") != str(customer_id):
        raise PaymentMethodError("That payment method couldn't be found.", status=404)
    return customer_id, pm


def set_default(user_id, payment_method_id: str) -> dict:
    """Make one method the account's main card. Returns the list.

    NOMINATES NOTHING. Renewals read the card each company was put on
    (``store.card_for_entity``), never this, so promoting here changes what is charged for
    exactly nothing that is already running. What it does change is what every card picker
    offers FIRST, and therefore what the next company nominated is likely to end up on.

    It used to be the one write on this screen with billing consequences, and they were
    account-wide. ``set_for_entity`` is that write now, and its consequences stop at one
    company.
    """
    customer_id, _pm = _owned(user_id, payment_method_id)
    set_customer_default_payment_method(customer_id, payment_method_id)
    return list_for_user(user_id)


# --- The card ONE company is billed on ---------------------------------------


def _payer_of(user_id, entity_id, *, establish_payer: bool = False) -> str:
    """Refuse unless ``user_id`` is the payer for ``entity_id``. Returns the payer id.

    THE AUTHORISATION for the per-entity write, and it is deliberately the same test that
    gates every other change to a subscription (``store.may_manage_subscription`` asks it
    too): the person whose card is about to be spent on a company is the person who pays
    for it. Being an admin of the company is not enough — an admin who does not pay could
    otherwise move someone else's billing onto a card of their choosing.

    ``establish_payer`` IS FOR THE ACT THAT CREATES THE RELATIONSHIP, WHEREVER IT IS MADE.

    The payer is read from ``entity_module_subscription``, and that column is NULL until
    the company has a subscriber: a free trial is started by any admin and commits nobody
    (``checkout.start_module_trial``), and in the wizard the rows do not exist at all
    until finalize. Either way the caller is asking to nominate a card, or place a
    company, for one that has no payer — and that request is precisely the one that
    ESTABLISHES the payer, which is the same reasoning ``store.may_manage_subscription``
    already sets out: until a payer exists nobody is being billed, so the first act may
    create the relationship.

    IT IS NOT A WAY ROUND THE CHECK, and it cannot become one — it only applies when the
    answer is "nobody". A company that already HAS a payer still 404s for anyone else, flag
    or no flag. The caller passing it is expected to have proved membership itself: the
    onboarding route does via ``_entity_for_member``, the module actions via ``_gate``.

    It does not itself WRITE the payer, either — it only answers who it will be. The write
    is ``store.establish_entity_payer``, made in the same request by
    ``checkout.activate_entity_billing``, so a nomination keyed on this answer and the
    module rows cannot be left disagreeing.
    """
    if not entity_id:
        raise PaymentMethodError("No company was given.", status=400)
    payer = sub_store.payer_for_entity(entity_id)
    if payer is None:
        if establish_payer:
            return str(user_id)
        raise PaymentMethodError(
            "That company has no subscription to bill yet.", status=409
        )
    if str(payer) != str(user_id):
        # Same wording as an unknown company on purpose: whether somebody else pays for a
        # given company is not this endpoint's to disclose.
        raise PaymentMethodError("That company couldn't be found.", status=404)
    return str(payer)


def for_entity(user_id, entity_id, *, establish_payer: bool = False) -> dict:
    """The saved methods, plus which one THIS company is billed on.

    ``nominated_id`` is the answer the picker needs and ``default_id`` is the fallback it
    preselects when there is no nomination yet — the account's main card, offered rather
    than assumed. They are returned separately because the difference is the whole point:
    one is what will be charged for this company, the other is only a suggestion.

    ``establish_payer`` as in ``_payer_of``: it is threaded here because ``set_for_entity``
    returns through this function, and a nomination that succeeded would otherwise raise on
    the way back out.
    """
    _payer_of(user_id, entity_id, establish_payer=establish_payer)
    payload = list_for_user(user_id)
    group = sub_store.billing_group_for_entity(entity_id, user_id)
    payload["entity_id"] = str(entity_id)
    payload["nominated_id"] = group.stripe_payment_method_id if group else None
    return payload


def set_for_entity(user_id, entity_id, payment_method_id: str,
                   *, source: str = "chosen", establish_payer: bool = False) -> dict:
    """Put one company on one saved card. Returns the list, with the new nomination.

    THE write with billing consequences, and they are confined: from here on this
    company's renewals, purchases and trial conversion are charged to this card, and
    nothing else the payer owns moves.

    Ownership is proven BEFORE the entity is looked at — ``_owned`` compares the method's
    customer against the one resolved from the caller, so another payer's ``pm_...``
    answers "not found" rather than being nominated onto anything.

    Consent is NOT written here. "You may bill me for this company" and "on this card" are
    two different statements: a payer changing their own card is not re-authorising the
    relationship, and rewriting the consent record would lose when they actually agreed to
    it.
    """
    _owned(user_id, payment_method_id)
    payer = _payer_of(user_id, entity_id, establish_payer=establish_payer)
    # THE NOMINATION NAMES THE PAYER IT WAS WRITTEN FOR, and when it established one that
    # is the caller. The module rows carry no payer until billing is CONFIRMED, and the
    # same caller's confirm is what stamps them (``store.establish_entity_payer``, via
    # ``checkout.activate_entity_billing`` or onboarding's ``/billing/authorize``), so
    # the two agree. Were they ever different, ``card_for_entity`` — which resolves the
    # payer from the module rows — would not find this nomination, and the entity would
    # hold a card nobody could see. A nomination made and then abandoned without a
    # confirm is exactly that: harmless, because nothing bills a subscriber-less company,
    # but it is why the route does both halves in ONE request.
    sub_store.nominate_card_for_entity(entity_id, payer, payment_method_id, source)
    # Threaded, or the read on the way out raises the 409 the write just stepped past.
    return for_entity(user_id, entity_id, establish_payer=establish_payer)


def _valid_expiry(exp_month, exp_year) -> tuple[int, int]:
    """Parse and sanity-check an edited expiry, in the app's own words.

    Checked here rather than left to Stripe because Stripe's refusal ("Your card's
    expiration year is invalid.") arrives as an API error the form cannot attach to a
    field, and a past date is the mistake worth catching before it becomes a saved card
    that cannot be charged.
    """
    try:
        month, year = int(exp_month), int(exp_year)
    except (TypeError, ValueError) as exc:
        raise PaymentMethodError(
            "Enter the expiry as a month and a year.", status=422
        ) from exc

    # 12 and 100 are calendar facts, not tunables: naming them MONTHS_IN_YEAR /
    # CENTURY would read worse at the point of use than the numbers do.
    if not 1 <= month <= 12:  # noqa: PLR2004
        raise PaymentMethodError("That expiry month doesn't exist.", status=422)
    if year < 100:  # noqa: PLR2004
        # "29" for 2029 — what a customer types into a two-box expiry field.
        year += 2000
    now = clock.now()
    if (year, month) < (now.year, now.month):
        raise PaymentMethodError("That expiry date has already passed.", status=422)
    return month, year


def update(
    user_id,
    payment_method_id: str,
    *,
    exp_month=None,
    exp_year=None,
    name: str | None = None,
    address: dict | None = None,
) -> dict:
    """Correct a saved method's expiry or billing details. Returns the list.

    Deliberately NOT a way to change the card. Stripe does not allow a number, brand or
    CVC to be edited — a different card is a different PaymentMethod — so this covers the
    two things that legitimately change on the same plastic: a reissued expiry, and the
    name and address the issuer checks. Everything else is "Add payment method".
    """
    _customer_id, pm = _owned(user_id, payment_method_id)

    payload: dict = {}
    if exp_month is not None or exp_year is not None:
        if not (pm.get("card") or {}):
            raise PaymentMethodError(
                "That payment method has no expiry date to change.", status=422
            )
        month, year = _valid_expiry(
            exp_month if exp_month is not None else (pm.get("card") or {}).get("exp_month"),
            exp_year if exp_year is not None else (pm.get("card") or {}).get("exp_year"),
        )
        payload["exp_month"], payload["exp_year"] = month, year

    details: dict = {}
    if name is not None:
        # A blank is "", not None: see ``_clean_address``.
        details["name"] = str(name).strip()
    if address is not None:
        cleaned = _clean_address(address)
        if cleaned:
            details["address"] = cleaned
    if details:
        payload["billing_details"] = details

    if not payload:
        raise PaymentMethodError("There was nothing to change.", status=422)

    update_payment_method(payment_method_id, **payload)
    return list_for_user(user_id)


#: The address keys Stripe knows. The billing account's address is the address of the card
#: it charges (no column holds one), so this is the whole shape 08-C edits.
ADDRESS_KEYS = ("line1", "line2", "city", "state", "postal_code", "country")


def _clean_address(address: dict) -> dict:
    """Only the keys Stripe knows, and only the ones SUPPLIED — sending the whole shape
    with blanks would erase an address the payer did not touch.

    A supplied BLANK is sent as ``""``, which is Stripe's "unset". It used to become
    ``None``, and the SDK drops ``None`` from the request entirely (``stripe/_encode.py``),
    so clearing a field reported success and changed nothing.
    """
    return {
        key: str(address.get(key)).strip()
        for key in ADDRESS_KEYS
        if address.get(key) is not None
    }


def write_billing_address(
    user_id, payment_method_id: str, address: dict | None, *, name: str | None = None
) -> None:
    """Put ``address`` - and the cardholder's ``name``, when given - on one of the caller's
    cards at Stripe, in ONE write.

    What a billing account's address IS: the billing address of the card it charges. The
    account table holds no address (a user decision, 2026-09-25), so 08-C writes here and
    08-B reads the card back. 08-C's address form is Stripe's own, which asks for the name
    with the address, so the two travel together. Ownership is proven first, as for every
    card write.
    """
    _owned(user_id, payment_method_id)
    details: dict = {}
    cleaned = _clean_address(address or {})
    if cleaned:
        details["address"] = cleaned
    if name is not None:
        # A blank is "", not None: see ``_clean_address``.
        details["name"] = str(name).strip()
    if not details:
        raise PaymentMethodError("There was nothing to change.", status=422)
    update_payment_method(payment_method_id, billing_details=details)


def remove(user_id, payment_method_id: str, *, account_id=None) -> dict:
    """Detach a saved method. Returns the list.

    Two refusals, both about leaving the account unable to pay itself — see the module
    docstring. Neither is a permission check: they are guards on a live billing
    relationship, and each one names the fix.

    ``account_id`` is the billing account whose page the payer is on (08-B). It changes
    two things. The account's OWN charged card is refused in the account's words. And the
    Stripe customer's default — which that page cannot set, since "Set as default" there
    switches what the ACCOUNT charges — is handed to this account's card instead of
    refusing with a fix the payer has no button for. The customer default still matters
    (what a company with no nomination is put on at consent, what a handover nominates),
    so it is moved rather than left to Stripe to clear. Without ``account_id`` nothing
    here has changed.
    """
    customer_id, pm = _owned(user_id, payment_method_id)
    account = account_of(user_id, account_id) if account_id else None
    if account is not None and account.stripe_payment_method_id == payment_method_id:
        raise PaymentMethodError(
            "That's this billing account's default card. Make another card its default "
            "first, then remove it.",
            status=409,
        )

    default_id = customer_default_payment_method(customer_id)
    others = [
        m for m in list_payment_methods(customer_id) if m.get("id") != payment_method_id
    ]

    # FIRST, because it is the one that costs real money. A card companies are nominated
    # onto is the card their renewals are charged to; detaching it leaves them with a
    # ``pm_...`` Stripe no longer holds, and every one of their renewals fails into
    # dunning on a date nobody is watching. The default rule below is bookkeeping by
    # comparison — nothing is billed to the default.
    billing_on_it = _companies_billing_on(user_id, payment_method_id)
    if billing_on_it:
        raise PaymentMethodError(
            _still_billing_message(billing_on_it), status=409
        )
    if payment_method_id == default_id and others:
        successor = account.stripe_payment_method_id if account is not None else None
        if not successor or not any(m.get("id") == successor for m in others):
            raise PaymentMethodError(
                "That's the account's main payment method. Make another one the default "
                "first, then remove it.",
                status=409,
            )
        set_customer_default_payment_method(customer_id, successor)
        logger.info(
            "payment methods: payer {} removing default {}; customer default handed to "
            "account {}'s card {}",
            user_id, payment_method_id, account.id, successor,
        )
    if not others and _bills_forward(user_id):
        raise PaymentMethodError(
            "This is the only payment method on the account, and there are "
            "subscriptions still being billed to it. Add another one first.",
            status=409,
        )

    detach_payment_method(payment_method_id)
    # Detaching the default clears it at Stripe's end. Nothing here promotes a survivor in
    # its place: which card an account pays with is the payer's decision, and choosing one
    # for them silently is how a company gets charged on a card it did not nominate. The
    # page shows "no default" and asks — the only case this can arise in is a non-default
    # detach, since the branch above refuses the other one.
    forget_default_payment_method(customer_id)
    logger.info(
        "payment methods: payer {} removed {} ({})",
        user_id, payment_method_id, (pm.get("card") or {}).get("last4") or pm.get("type"),
    )
    return list_for_user(user_id)
