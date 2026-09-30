"""Single configured entry point to the Stripe SDK.

Import ``get_stripe()`` wherever Stripe API calls are needed rather than
importing ``stripe`` directly, so the API key is always applied from settings.
THIS IS THE ONLY MODULE IN THE SERVICE THAT IMPORTS ``stripe`` (cross-cutting rule 9;
``billing/tests/test_models_guard.py`` pins it).

This module also holds the thin Stripe helpers the billing layer builds on --
customers, saved cards, setup intents and checkout sessions -- so all Stripe
access goes through one place.

The Stripe-SUBSCRIPTION helpers that used to sit here went with the move to
in-house billing: ``list_products`` / ``list_active_recurring_prices`` (the
catalog is a table now, read by ``services.catalog``), ``create_paid_subscription``
and the ``SubscriptionItem`` writers, and the invoice-item readers the old
cancel-extension undo used. Invoices are built by ``services.billing`` and
collected by ``services.billing_gateway``; Stripe is the card vault and the
charge, nothing more. Git holds the removed helpers if a Stripe-side
subscription is ever wanted again.
"""
from __future__ import annotations

import stripe
from django.conf import settings

from billing.services import _context

# Per-request memo for ``customer_default_payment_method``. Same shape and lifetime as
# ``policy.current`` / ``money.decimal_places`` / ``clock.now`` - cached on the request
# scope (``_context``, what Flask's ``g`` became), so it is per-request and thread-safe
# (each scope has its own dict).
#
# It is here because "does this customer have a card" is a NETWORK round trip, and it was
# being made on every module-settings render, on the payer portal, and once per entity per
# login on the dashboard - synchronously, in front of the response. The answer cannot
# change while a request is in flight except by our own write, and that write invalidates
# it (see ``set_customer_default_payment_method``).
_G_DEFAULT_PM_KEY = "_stripe_default_payment_methods"

#: The Stripe API version every request is made at. PINNED rather than inherited: left
#: alone, ``stripe.api_version`` is whatever the installed stripe-python defaults to, so a
#: ``requirements.txt`` bump would change the API underneath the code. The one that bites is
#: ``2025-03-31.basil`` (stripe-python 12's default), which REMOVED Invoice ``charge`` and
#: ``payment_intent`` - what ``billing_gateway`` reads to record the card that paid and to
#: spot a payment Stripe has cancelled for good. Both would fail quietly. Moving off this
#: version means migrating those readers to ``invoice.payments`` first
#: (docs.stripe.com/changelog/basil/2025-03-31); ``billing/tests/test_stripe_api_version.py``
#: fails the build until then.
STRIPE_API_VERSION = "2024-12-18.acacia"


def _default_pm_cache() -> dict | None:
    """The request's memo, or None outside a scope (command, worker, import time)."""
    if not _context.active():
        return None
    cache = _context.get(_G_DEFAULT_PM_KEY)
    if cache is None:
        cache = {}
        _context.set(_G_DEFAULT_PM_KEY, cache)
    return cache


def forget_default_payment_method(customer_id: str | None) -> None:
    """Drop the memoized answer for one customer, after their card on file changed.

    Without this the memo is a correctness bug, not just a stale read: paid checkout
    captures a card and then asks whether one is on file to decide if it may subscribe
    directly (``checkout._has_payment_method``). Answering from a memo taken BEFORE the
    capture would send the payer back to capture a card they just saved.
    """
    cache = _default_pm_cache()
    if cache is not None and customer_id:
        cache.pop(str(customer_id), None)


def _config_value(key: str) -> str | None:
    """Read a Stripe setting. Settings ONLY - deliberately no fallback to the process
    environment, which Flask's version had: ``settings_test`` blanks the key so that an
    unstubbed test call raises in ``get_stripe``, and an environment fallback would quietly
    reach the developer's shell key instead."""
    value = getattr(settings, key, None)
    return value or None


class StripeNotConfigured(RuntimeError):
    """No Stripe key: nothing can be charged. OUR failure, never the customer's card - so it
    is retried (``is_transient``), not read as a decline."""


def get_stripe():
    """Return the ``stripe`` module with ``api_key`` and the pinned ``api_version`` set, or
    raise ``StripeNotConfigured``."""
    api_key = _config_value("STRIPE_SECRET_KEY")
    if not api_key:
        raise StripeNotConfigured(
            "STRIPE_SECRET_KEY is not configured; cannot make Stripe API calls."
        )
    stripe.api_key = api_key
    stripe.api_version = STRIPE_API_VERSION
    return stripe


#: Failures of the PROCESSOR, or of our access to it - never an answer about the card. The
#: network, a Stripe outage (5xx), a rate limit, our key refused, and a keyed request replayed
#: with other parameters. Each says nothing about whether the customer can pay, so each is
#: retried on the next pass (the user's rule, 2026-09-30: "Stripe itself fails -> retry next
#: hour") instead of expiring a trial or telling the customer their payment failed.
_TRANSIENT = (
    stripe.APIConnectionError,
    stripe.APIError,
    stripe.RateLimitError,
    stripe.AuthenticationError,
    stripe.PermissionError,
    stripe.IdempotencyError,
    StripeNotConfigured,
)


def is_transient(exc) -> bool:
    """Whether ``exc`` is the processor failing rather than the card being refused.

    A card error, an invalid request (no card, a detached card, an invoice that cannot be
    paid) and anything unrecognised are NOT transient - those keep today's handling, a
    decline. Unrecognised errors stay declines on purpose: the conservative reading, and the
    one every existing path was written for.
    """
    return isinstance(exc, _TRANSIENT)


def lacks_field(obj, field: str) -> bool:
    """Whether a REAL Stripe response has no ``field`` at all - not null, ABSENT.

    At the pinned ``STRIPE_API_VERSION`` every field the billing layer reads is always sent,
    null when empty, so an absent one means the API underneath has changed (see the pin).
    Only a genuine ``StripeObject`` is judged: a plain dict is a hand-built stand-in, whose
    missing keys say nothing about the API.
    """
    return isinstance(obj, stripe.StripeObject) and field not in obj


# --- Trusted-clock capture ---------------------------------------------------


def _record_server_time(stripe_result) -> None:
    """Stash the ``Date`` header off a Stripe response as the trusted 'now'.

    A SIDE EFFECT of a call being made for another reason, deliberately: it costs no
    extra round trip, and it is what lets ``clock.now()`` answer without one either.
    See ``services.clock`` for what the answer is used for and why the host's own wall
    clock is not good enough.

    Called from the READS that sit on the paths where access is decided --
    ``retrieve_customer`` (and through it ``customer_default_payment_method``, on every
    module-settings render), ``list_payment_methods`` and ``find_customer_by_user``. It
    used to hang off ``list_customer_subscriptions``, which went with the Stripe biller;
    when that happened the app silently fell back to the database clock, which is why
    the capture is now spread across three calls instead of resting on one.

    Best-effort by design: silently ignored if headers aren't available.
    """
    try:
        last_response = getattr(stripe_result, "last_response", None)
        headers = getattr(last_response, "headers", None) or {}
        date_header = headers.get("Date") or headers.get("date")
        if date_header:
            from billing.services import clock

            clock.record_http_date(date_header)
    except Exception:  # noqa: BLE001, S110 - clock capture must never break a Stripe read
        pass


# --- Write helpers (checkout / trials / billing) -----------------------------


# NOTE: no customer is created on any path that MIGHT save a card, deliberately. A
# customer must not exist until a card has actually been saved, so
# ``create_setup_checkout_session`` hands Stripe ``customer_creation="always"`` and lets
# it create one at session confirmation. ``checkout._adopt_session_customer`` then stamps
# and maps it. Creating one up front reintroduces an orphan customer for every abandoned
# checkout.
#
# ``create_customer_for_user`` below is the one direct create, and it does not weaken
# that: it is called from the in-app card form's CONFIRM step, i.e. after Stripe has
# already told us a payment method exists. Card first, customer second — the invariant is
# about ordering, not about which API call makes the customer.


def find_customer_by_user(user_id: str):
    """Find a payer's Stripe Customer via ``metadata.user_id`` and return the Customer
    dict (or None). The customer is owned by the paying USER, not the entity.

    NOTE: Stripe's Customer Search is eventually consistent (results can lag a write by
    up to ~a minute), so code running right after creating a customer must dedupe via the
    create idempotency key rather than relying on search.
    """
    if not user_id:
        return None
    stripe_client = get_stripe()
    result = stripe_client.Customer.search(
        query=f"metadata['user_id']:'{user_id}'",
        limit=1,
    )
    _record_server_time(result)
    data = result.get("data") or []
    return data[0] if data else None


def retrieve_customer(customer_id: str):
    """Retrieve a Stripe Customer by id (None if missing/blank)."""
    if not customer_id:
        return None
    stripe_client = get_stripe()
    customer = stripe_client.Customer.retrieve(customer_id)
    _record_server_time(customer)
    return customer


def create_setup_checkout_session(
    customer_id: str | None,
    success_url: str,
    cancel_url: str,
    currency: str,
    metadata: dict[str, str] | None = None,
):
    """Create a Checkout Session in ``setup`` mode to save a card (no charge).

    Used to capture a payment method before creating per-module paid subscriptions
    server-side. Setup mode requires a ``currency`` (the modules' billing currency).
    ``metadata`` (carrying the entity, payer and any module codes) rides on the session
    so the completion handler knows what to create; the saved card is set as the
    customer's default on completion.

    ``customer_id`` is None for a payer who has no Stripe customer yet. The session is
    then opened WITHOUT a customer and with ``customer_creation="always"``, so Stripe
    creates the Customer during session CONFIRMATION — i.e. only once the card is
    actually saved. Abandoning the session leaves no Customer behind. That is a
    deliberate invariant: NO CUSTOMER IS EVER CREATED WITHOUT A CARD. Do not "fix" this
    by resolving a customer up front — that reintroduces orphan customers for every
    abandoned checkout.

    It must be ``always``, not ``if_required``: with ``if_required`` the session can end
    up saving neither the customer nor the payment method, which defeats setup mode.

    The customer Stripe creates carries none of our bookkeeping (no ``metadata.user_id``,
    and it bypasses the ``user-customer-{user_id}`` idempotency key). The completion
    handler adopts it — see ``checkout._adopt_session_customer``.
    """
    payload: dict[str, object] = {
        "mode": "setup",
        "currency": (currency or "").lower(),
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": metadata or {},
    }
    if customer_id:
        payload["customer"] = customer_id
    else:
        payload["customer_creation"] = "always"
    return get_stripe().checkout.Session.create(**payload)


def retrieve_checkout_session(session_id: str):
    """Retrieve a Checkout Session with its ``setup_intent`` expanded (so the saved
    payment method is readable)."""
    return get_stripe().checkout.Session.retrieve(
        session_id, expand=["setup_intent"]
    )


def set_customer_default_payment_method(customer_id: str, payment_method_id: str):
    """Make ``payment_method_id`` the customer's default for invoices."""
    result = get_stripe().Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )
    # The memo below now holds a stale "no card" for this customer, and the very next
    # thing checkout does is ask whether one is on file.
    forget_default_payment_method(customer_id)
    return result


def set_customer_identity(
    customer_id: str,
    *,
    metadata: dict[str, str] | None = None,
    name: str | None = None,
    email: str | None = None,
    description: str | None = None,
):
    """Write who a customer IS: the ``user_id`` stamp plus the payer's display fields.

    One ``Customer.modify`` rather than several, so the identity of a customer Stripe
    just created during a setup Checkout lands atomically. ``metadata`` is the important
    half — it's what ``find_customer_by_user`` recovers from, and without it the customer
    is invisible to every lookup we have. (Stripe MERGES metadata keys; it doesn't
    replace the object.)

    Only non-None fields are sent, so a caller can leave Stripe's Checkout-collected
    value in place for anything it can't improve on (e.g. a payer whose local ``email``
    is null).
    """
    payload: dict[str, object] = {}
    if metadata:
        payload["metadata"] = metadata
    if name:
        payload["name"] = name
    if email:
        payload["email"] = email
    if description:
        payload["description"] = description
    if not payload:
        return None
    return get_stripe().Customer.modify(customer_id, **payload)


def attach_payment_method(payment_method_id: str, customer_id: str):
    """Attach a PaymentMethod to a customer.

    Only needed on the duplicate-customer reconciliation path: a card captured against
    a Stripe-created customer has to be moved onto the payer's pre-existing customer
    before it can be made the default there (see ``checkout._adopt_session_customer``).
    """
    return get_stripe().PaymentMethod.attach(payment_method_id, customer=customer_id)


def customer_default_payment_method(customer_id: str | None) -> str | None:
    """The customer's default invoice payment method id, or None.

    Used to decide whether paid checkout can create subscriptions directly (a card
    is on file) or must first capture one via a setup-mode Checkout.

    MEMOIZED FOR THE REQUEST. The Stripe round trip behind this was on the render path of
    every module-settings page — and ``get_module_cards`` is not the only caller in a
    request. ``None`` is cached like any other answer: "this payer has no card" is the
    common case on a trial and the one worth not asking twice.
    """
    if not customer_id:
        return None

    cache = _default_pm_cache()
    key = str(customer_id)
    if cache is not None and key in cache:
        return cache[key]

    customer = retrieve_customer(customer_id)
    pm = None
    if customer:
        pm = (customer.get("invoice_settings") or {}).get("default_payment_method")
        if isinstance(pm, dict):
            pm = pm.get("id")
    if cache is not None:
        cache[key] = pm
    return pm


def payment_method_display(payment_method_id: str | None) -> dict | None:
    """The card's brand + last4 for showing the payer what they're about to be charged
    on, or None if it can't be read.

    Display only — never a billing decision. Returns e.g. ``{"type": "card", "brand":
    "visa", "last4": "4242", "exp_month": 8, "exp_year": 2028, "expiry": "08/28",
    "label": "Visa •••• 4242"}``.

    The expiry is included because a card about to expire is the commonest reason a
    renewal fails, and the billing page is where someone would go to fix it. ``expiry`` is
    the pre-formatted MM/YY the UI prints; the raw parts are kept so a caller can compare
    against a date without parsing it back.

    NOT every payment method is a card. A payer who checked out through Stripe Link has a
    ``link`` method with no ``card`` object at all, and this used to answer None for
    them — so the one screen that says what will be charged said nothing. Those now come
    back with the type and a ``label`` and empty card fields, because "Link" is a true
    answer and blank is not.

    ``brand`` and ``last4`` keep their exact previous meaning — a card's, or None — so
    callers reading them are unaffected. ``label`` is the complete string to print.
    """
    if not payment_method_id:
        return None
    try:
        pm = get_stripe().PaymentMethod.retrieve(payment_method_id)
    except Exception:
        from billing.services._log import logger

        logger.exception(
            "subscription: could not read payment method {}", payment_method_id
        )
        return None
    card = (pm or {}).get("card") or {}
    kind = (pm or {}).get("type") or ""
    # Whoever the card is registered to. Stripe's own field, so the billing screen can
    # show a cardholder rather than guessing it is the payer — they are often different
    # people (a finance lead's card on a director's account).
    cardholder = ((pm or {}).get("billing_details") or {}).get("name")

    if card:
        month, year = card.get("exp_month"), card.get("exp_year")
        brand, last4 = card.get("brand"), card.get("last4")
        return {
            "type": "card",
            "brand": brand,
            "last4": last4,
            "cardholder": cardholder,
            "exp_month": month,
            "exp_year": year,
            "expiry": (
                f"{int(month):02d}/{int(year) % 100:02d}" if month and year else None
            ),
            "label": " ".join(
                p for p in [(brand or "card").title(), f"•••• {last4}" if last4 else ""]
                if p
            ),
        }

    if kind:
        # A wallet — Link today. Stripe exposes no card object for it (the funding source
        # is Link's business), so the honest answer is what it IS.
        return {
            "type": kind,
            "brand": None,
            "last4": None,
            "cardholder": cardholder,
            "exp_month": None,
            "exp_year": None,
            "expiry": None,
            "label": kind.replace("_", " ").title(),
        }

    return None


# --- Saved payment methods (the in-app wallet) -------------------------------
#
# Everything above reads or writes the ONE default card, which is all the billing engine
# ever needed: ``issue_invoice`` charges the customer and Stripe bills whatever
# ``invoice_settings.default_payment_method`` names. The billing account PAGE needs the
# rest of the shelf — every method saved against the customer, so one can be added,
# corrected, promoted or removed without leaving the app.
#
# Stripe remains the only place a card number exists. These helpers move ids and display
# fields; the PAN is entered into Stripe Elements in the browser and never reaches this
# process, which is what keeps the application out of PCI scope.


def create_customer_for_user(
    user_id,
    *,
    name: str | None = None,
    email: str | None = None,
    description: str | None = None,
):
    """Create the payer's Stripe Customer, stamped so every lookup can find it.

    THE ONLY DIRECT CUSTOMER CREATE IN THE APPLICATION, and it has exactly one caller:
    the in-app card form's confirm step, once Stripe has confirmed a SetupIntent and a
    payment method genuinely exists (see ``payment_methods.confirm_setup``). Do not call
    it earlier in a flow — "no customer without a card" is what stops an abandoned form
    leaving an orphan behind, and this is the first moment the card is a fact.

    ``metadata.user_id`` is not optional. It is what ``find_customer_by_user`` recovers
    from when the local mapping row is missing, and a customer without it is invisible to
    every lookup we have.

    The idempotency key is the payer, permanently: two tabs confirming two SetupIntents
    seconds apart must not produce two customers for one payer, and Customer Search is
    eventually consistent so it cannot be used to dedupe a write that just happened.
    Stripe replays the original response for 24h; beyond that the mapping row (written by
    the caller) is what prevents a second create.
    """
    payload: dict[str, object] = {"metadata": {"user_id": str(user_id)}}
    if name:
        payload["name"] = name
    if email:
        payload["email"] = email
    if description:
        payload["description"] = description
    return get_stripe().Customer.create(
        **payload, idempotency_key=f"user-customer-{user_id}"
    )


def list_payment_methods(customer_id: str | None) -> list:
    """Every payment method attached to a customer, newest first.

    No ``type`` filter — the customer's whole shelf. Filtering to ``card`` would silently
    hide a Stripe Link wallet that a payer checked out with, i.e. the very method their
    renewals are charged against (see ``payment_method_display``, which learned the same
    lesson).

    Returns ``[]`` for a payer with no customer rather than raising: "no billing account
    yet" is a real state the page has to render (an app-level trial never captured a
    card), not an error.
    """
    if not customer_id:
        return []
    result = get_stripe().PaymentMethod.list(customer=customer_id, limit=100)
    _record_server_time(result)
    return list(result.auto_paging_iter())


def retrieve_payment_method(payment_method_id: str):
    """Fetch one PaymentMethod (None if blank).

    The OWNERSHIP CHECK behind every mutation on this shelf: the id arrives from the
    browser, so ``pm.customer`` is compared against the caller's own customer before
    anything is promoted, edited or detached. Without it, a payer who guessed another
    payer's ``pm_…`` id could detach their card.
    """
    if not payment_method_id:
        return None
    return get_stripe().PaymentMethod.retrieve(payment_method_id)


def create_setup_intent(customer_id: str | None, *, user_id, metadata: dict | None = None):
    """A SetupIntent for the in-app card form to confirm against.

    ``usage="off_session"`` because of what the saved card is FOR: renewals and dunning
    retries charge it with nobody at the keyboard (``billing_gateway.issue_invoice``). Set
    up on-session, the card can be saved in a state the issuer later refuses for
    unattended charges — a failure that surfaces a month later on a renewal rather than
    now, in front of the person who could fix it.

    ``customer`` is optional and often absent: a payer adding their FIRST card has no
    customer, and creating one to open a form they may abandon is the orphan this app
    refuses to make. Stripe attaches the method to the customer on success when one is
    given; ``payment_methods.confirm_setup`` creates the customer and attaches the method
    by hand when one is not.

    ``metadata.user_id`` is what makes the confirm step safe. The SetupIntent id comes
    back from the browser, and for a customerless intent there is nothing else to check it
    against — an intent that does not carry the caller's own stamp is refused rather than
    attached.

    Card only. The Payment Element can offer redirect-based methods (iDEAL, Bancontact),
    and every one of them is a bank mandate this billing engine has no path for: it
    charges a saved method off-session on the anchor, which those cannot do.
    """
    payload: dict[str, object] = {
        "usage": "off_session",
        "payment_method_types": ["card"],
        "metadata": {"user_id": str(user_id), **(metadata or {})},
    }
    if customer_id:
        payload["customer"] = customer_id
    return get_stripe().SetupIntent.create(**payload)


def retrieve_setup_intent(setup_intent_id: str):
    """Fetch one SetupIntent (None if blank). Read to learn what the browser saved."""
    if not setup_intent_id:
        return None
    return get_stripe().SetupIntent.retrieve(setup_intent_id)


def update_payment_method(
    payment_method_id: str,
    *,
    exp_month: int | None = None,
    exp_year: int | None = None,
    billing_details: dict | None = None,
):
    """Edit what CAN be edited on a saved method: the expiry, and the billing details.

    Stripe does not let a card's number, CVC or brand be changed — those are the card, and
    a different card is a new PaymentMethod. So "Edit" here means the two things that
    legitimately change on the same plastic: a reissued expiry date, and the name/address
    the issuer checks against (a cardholder who moved is a real cause of declines).

    ``exp_month``/``exp_year`` are rejected outright by Stripe on a non-card method, so
    the caller filters them out for a wallet rather than sending them and reading back a
    Stripe error the customer cannot act on.
    """
    payload: dict[str, object] = {}
    if exp_month is not None or exp_year is not None:
        card: dict[str, object] = {}
        if exp_month is not None:
            card["exp_month"] = int(exp_month)
        if exp_year is not None:
            card["exp_year"] = int(exp_year)
        payload["card"] = card
    if billing_details:
        payload["billing_details"] = billing_details
    if not payload:
        return None
    return get_stripe().PaymentMethod.modify(payment_method_id, **payload)


def detach_payment_method(payment_method_id: str):
    """Remove a saved method from its customer.

    Detaching the customer's DEFAULT also clears ``invoice_settings.default_payment_method``
    at Stripe's end, which is why the caller refuses to detach a default while another
    method exists — the promotion has to happen first, or the account is briefly left with
    a shelf full of cards and nothing nominated to charge.
    """
    return get_stripe().PaymentMethod.detach(payment_method_id)


def create_billing_portal_session(
    customer_id: str,
    return_url: str,
    configuration: str,
    flow_data: dict | None = None,
):
    """Create a Stripe Customer Portal session.

    ``configuration`` is REQUIRED, and deliberately not optional: a session without one
    silently uses the account default, where Stripe's own cancel button is enabled — and
    cancelling there bypasses everything the in-app flow guarantees (no extension queued,
    no ``app_access_until``, no audit row), then the webhook revokes access on the spot,
    stripping the 30 days the customer paid for. Cancelling is IN-APP ONLY.

    Callers should resolve it via ``checkout._billing_portal_configuration``, which fails
    closed rather than falling back to the default. A route that opened the default portal
    existed and was deleted for exactly this reason.

    Pass ``flow_data`` to deep-link into a specific flow with an ``after_completion``
    redirect — but note the flow only decides where the session OPENS; ``configuration``
    is what bounds where the customer can go from there.
    """
    if not configuration:
        raise ValueError(
            "create_billing_portal_session requires a configuration — without one "
            "Stripe opens the default portal, which lets the customer cancel."
        )
    payload: dict[str, object] = {
        "customer": customer_id,
        "return_url": return_url,
        "configuration": configuration,
    }
    if flow_data:
        payload["flow_data"] = flow_data
    return get_stripe().billing_portal.Session.create(**payload)


# Tag for the portal configuration used to VIEW billing (invoices + payment method)
# without exposing Stripe's own cancellation flow (cancellation must go through the
# in-app prorated flow). Reused across sessions so we don't recreate it each time.
BILLING_MANAGEMENT_CONFIG_ROLE = "minty_billing_management"
_billing_management_config_id: str | None = None


def get_or_create_billing_management_configuration() -> str | None:
    """Return the id of the portal configuration that shows invoice history and lets
    the customer update their payment method, but does NOT allow cancelling or
    changing subscriptions. Finds the tagged config (by metadata) or creates it.
    Returns None if it can't be created (caller then falls back to the default
    portal). Memoized per process.
    """
    global _billing_management_config_id
    if _billing_management_config_id:
        return _billing_management_config_id

    s = get_stripe()
    try:
        for cfg in s.billing_portal.Configuration.list(limit=100).auto_paging_iter():
            meta = cfg.get("metadata") or {}
            if meta.get("minty_role") == BILLING_MANAGEMENT_CONFIG_ROLE and cfg.get("active", True):
                _billing_management_config_id = cfg["id"]
                return _billing_management_config_id

        cfg = s.billing_portal.Configuration.create(
            features={
                "invoice_history": {"enabled": True},
                "payment_method_update": {"enabled": True},
                "customer_update": {"enabled": False},
                "subscription_cancel": {"enabled": False},
                "subscription_update": {"enabled": False},
            },
            metadata={"minty_role": BILLING_MANAGEMENT_CONFIG_ROLE},
        )
        _billing_management_config_id = cfg["id"]
        return _billing_management_config_id
    except Exception:
        from billing.services._log import logger

        logger.exception(
            "stripe: could not get/create billing-management portal configuration; "
            "falling back to the default portal"
        )
        return None


# NOTE: there are deliberately no invoice-annotation helpers here.
#
# A payer has ONE subscription spanning every entity they own, so an invoice mixes
# entities: two entities on the bundle produce two identical "Super Minty 400.00" lines. The
# obvious fixes were both tried against live Stripe and both are dead ends.
#
# 1. Rewriting the LINE description. Refused on any subscription-typed line, by every
#    endpoint — the bulk one, the singular one, and the invoiceitems one:
#
#      POST /v1/invoices/{inv}/lines/{line}  ->  400 You may only update `tax_rates`,
#                                                `tax_amounts`, or `discounts` for a
#                                                subscription typed line item.
#      POST /v1/invoiceitems/{line}          ->  400 When passing an invoice's line item
#                                                id, you may only update `tax_rates` or
#                                                `discounts`.
#
#    A line's text is composed by Stripe from the PRODUCT name ("1 x {product}",
#    "Remaining time on {product} after {date}") and frozen at creation.
#
# 2. Writing the entity breakdown into the invoice MEMO (``Invoice.description``). This
#    works, but only on a DRAFT, and the draft window is not what it looks like:
#
#      renewal invoice                     created 13:00:00, finalized 14:00:00  (~1h)
#      proration from ``always_invoice``   created 13:00:00, finalized 13:00:00  (0s)
#
#    Every charge this app initiates — trial conversion, buy, plan change — uses
#    ``always_invoice`` and so finalizes in the same second, leaving no window at all.
#    Memos were therefore reachable only on multi-entity RENEWAL invoices, and a missed
#    webhook lost even those permanently ("Finalized invoices can't be updated in this
#    way"). Removed as more moving parts than coverage.
#
# The only route left is giving each entity its own Stripe Product, so the entity name
# is part of the line text Stripe generates.


def get_publishable_key() -> str | None:
    """Return the publishable key for use by the frontend (safe to expose)."""
    return _config_value("STRIPE_PUBLISHABLE_KEY")


