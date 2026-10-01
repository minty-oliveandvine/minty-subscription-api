"""The in-app wallet: the saved payment methods behind the billing account page.

Three things are worth pinning down here, and "does the list come back" is none of them.

* **The id in the request is checked, not trusted.** Four of the five endpoints take a
  ``pm_…`` from the browser — the first thing in the payer portal that can be pointed
  somewhere — so the tests assert that a method belonging to another customer answers
  "not found" rather than being promoted, edited or detached.

* **The two removal refusals.** Both exist because the account charges itself while
  nobody is watching: the default cannot go while another method could take its place,
  and the last method cannot go at all while something is still billing forward. They
  are the difference between a wallet and a billing account.

* **No customer is created before a card exists.** ``confirm_setup`` is the only path in
  the application that creates a Stripe customer directly, and it may only do so once
  Stripe has said the SetupIntent succeeded. A declined or abandoned card must leave
  nothing behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import jwt
import pytest

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _card(pm_id="pm_1", *, last4="4242", exp_month=4, exp_year=2029, brand="visa",
          customer="cus_1", created=1_700_000_000, name="A Payer"):
    """A Stripe PaymentMethod as the SDK hands it back — a dict, subscript and ``get``."""
    return {
        "id": pm_id,
        "type": "card",
        "customer": customer,
        "created": created,
        "billing_details": {"name": name, "email": None, "address": {"line1": "1 Main St"}},
        "card": {
            "brand": brand,
            "last4": last4,
            "exp_month": exp_month,
            "exp_year": exp_year,
            "funding": "credit",
            "country": "HK",
        },
    }


def _link(pm_id="pm_link", *, customer="cus_1"):
    """A Stripe Link wallet: no ``card`` object at all, which is a state, not a failure."""
    return {
        "id": pm_id,
        "type": "link",
        "customer": customer,
        "created": 1_700_000_100,
        "billing_details": {"name": None, "address": {}},
    }


@pytest.fixture
def wallet(app, monkeypatch):
    """``payment_methods`` with Stripe and the store stubbed, and a frozen clock.

    Patches the names bound INSIDE the service, not on ``stripe_client``: the module
    imports them directly, so patching the source module would leave the service holding
    the originals (and reaching real Stripe).
    """
    from billing.services import payment_methods as pm

    state = {
        "customer_id": "cus_1",
        "methods": [],
        "default": None,
        "detached": [],
        "updated": [],
        "created_customers": [],
        "attached": [],
        "bills_forward": False,
        # Companies nominated onto a card, by ``pm_...``. Empty is the ordinary case: a
        # card nothing is billed to can be removed freely.
        "billing_on": {},
    }

    monkeypatch.setattr(pm.clock, "now", lambda: NOW)
    monkeypatch.setattr(pm.sub_store, "customer_id_for_user", lambda _u: state["customer_id"])
    monkeypatch.setattr(
        pm.sub_store, "upsert_customer_mapping",
        lambda user_id, customer_id: state.update(customer_id=customer_id),
    )
    monkeypatch.setattr(pm, "list_payment_methods", lambda _c: list(state["methods"]))
    monkeypatch.setattr(pm, "customer_default_payment_method", lambda _c: state["default"])
    monkeypatch.setattr(
        pm, "retrieve_payment_method",
        lambda pm_id: next(
            (m for m in state["methods"] if m["id"] == pm_id), None
        ),
    )
    monkeypatch.setattr(
        pm, "set_customer_default_payment_method",
        lambda _c, pm_id: state.update(default=pm_id),
    )
    monkeypatch.setattr(
        pm, "detach_payment_method",
        lambda pm_id: state["detached"].append(pm_id),
    )
    monkeypatch.setattr(
        pm, "update_payment_method",
        lambda pm_id, **kwargs: state["updated"].append((pm_id, kwargs)),
    )
    monkeypatch.setattr(pm, "attach_payment_method", lambda p, c: state["attached"].append((p, c)))
    monkeypatch.setattr(pm, "forget_default_payment_method", lambda _c: None)
    monkeypatch.setattr(pm, "get_publishable_key", lambda: "pk_test_123")
    monkeypatch.setattr(pm, "_bills_forward", lambda _u: state["bills_forward"])
    monkeypatch.setattr(
        pm, "_companies_billing_on",
        lambda _u, pm_id: list(state["billing_on"].get(pm_id, [])),
    )

    def _create_customer(user_id, **fields):
        state["created_customers"].append((str(user_id), fields))
        return {"id": "cus_new"}

    monkeypatch.setattr(pm, "create_customer_for_user", _create_customer)

    state["accounts"] = []
    state["module"] = pm
    return state


@pytest.fixture
def stubbed_accounts(wallet, monkeypatch):
    """The billing-account step of a confirm (local rows; covered by the API tests and
    ``test_portal_billing_accounts``), stubbed so the Stripe-facing half can be pinned on its
    own. Records what it was asked in ``wallet["accounts"]``. NOT part of ``wallet``: other
    modules import that fixture and need the real step."""
    pm = wallet["module"]

    def _account(user_id, payment_method, group_id, email, company):
        wallet["accounts"].append((str(user_id), payment_method, group_id, email, company))
        return SimpleNamespace(
            id=group_id or "g_new", billing_email=email, billing_company=company,
            stripe_payment_method_id=payment_method,
        )

    monkeypatch.setattr(pm, "_account_for_confirm", _account)
    return wallet


# --- Reading -----------------------------------------------------------------


def test_no_customer_is_a_state_the_page_can_show(app, wallet):
    """An account whose trials never captured a card has no Stripe customer at all. That
    is not an empty wallet and not an error — it is where "Add payment method" is the
    only thing on screen."""
    wallet["customer_id"] = None

    result = wallet["module"].list_for_user("u1")

    assert result == {"has_account": False, "default_id": None, "methods": [], "total": 0}


def test_the_default_leads_the_list_whenever_it_was_added(app, wallet):
    """The default is the only method with billing meaning, so a payer scanning for
    "what will actually be charged" should not have to hunt for it."""
    wallet["methods"] = [
        _card("pm_old", created=1_600_000_000),
        _card("pm_default", created=1_500_000_000),
    ]
    wallet["default"] = "pm_default"

    methods = wallet["module"].list_for_user("u1")["methods"]

    assert [m["id"] for m in methods] == ["pm_default", "pm_old"]
    assert methods[0]["is_default"] is True
    assert methods[1]["is_default"] is False


def test_a_card_is_good_until_the_end_of_its_expiry_month(app, wallet):
    """08/26 during August 2026 is live, not expired — the frozen clock is mid-month."""
    wallet["methods"] = [_card(exp_month=8, exp_year=2026)]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["expired"] is False
    assert view["expires_soon"] is True
    assert view["expiry"] == "08/26"


def test_a_month_that_has_passed_reads_expired(app, wallet):
    wallet["methods"] = [_card(exp_month=7, exp_year=2026)]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["expired"] is True
    assert view["expires_soon"] is False


def test_a_distant_expiry_is_not_flagged(app, wallet):
    wallet["methods"] = [_card(exp_month=4, exp_year=2029)]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert (view["expired"], view["expires_soon"]) == (False, False)


def test_a_tokenised_card_says_which_wallet_it_came_through(app, wallet):
    """The card is what gets charged, but the payer added it through Apple Pay and will
    not recognise a row that only names the plastic underneath."""
    method = _card()
    method["card"]["wallet"] = {"type": "apple_pay"}
    wallet["methods"] = [method]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["wallet"] == "apple_pay"
    assert view["wallet_label"] == "Apple Pay"


def test_a_wallet_stripe_has_not_documented_still_gets_a_name(app, wallet):
    method = _card()
    method["card"]["wallet"] = {"type": "some_new_wallet"}
    wallet["methods"] = [method]

    assert wallet["module"].list_for_user("u1")["methods"][0]["wallet_label"] == (
        "Some New Wallet"
    )


def test_a_typed_in_card_has_no_wallet(app, wallet):
    wallet["methods"] = [_card()]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["wallet"] is None
    assert view["wallet_label"] is None


def test_the_country_is_the_issuers_and_outranks_the_billing_address(
    app, wallet, monkeypatch
):
    """DELIBERATE, and it looks like a bug from the inside: a payer who picks the
    Philippines in the card form still sees the country their card was ISSUED in. That is
    the column working — the issuer drives cross-border fees and declines and is the half
    visible nowhere else, while the billing address is in the row's own Edit dialog."""
    from billing.services import portal

    monkeypatch.setattr(
        portal,
        "_country_names",
        lambda _codes: {"PH": "Philippines", "US": "United States"},
    )
    method = _card()
    method["card"]["country"] = "US"
    method["billing_details"]["address"] = {"line1": "1 Main St", "country": "PH"}
    wallet["methods"] = [method]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["country"] == "US"
    assert view["country_name"] == "United States"


def test_the_country_is_named_not_coded(app, wallet, monkeypatch):
    """Resolved against the country registry so this column reads the way the portal's
    other country columns do."""
    from billing.services import portal

    monkeypatch.setattr(portal, "_country_names", lambda _codes: {"HK": "Hong Kong SAR China"})
    wallet["methods"] = [_card()]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["country"] == "HK"
    assert view["country_name"] == "Hong Kong SAR China"


def test_an_unknown_country_falls_back_to_its_code(app, wallet, monkeypatch):
    """A two-letter cell is a worse answer than a name and a better one than a blank."""
    from billing.services import portal

    monkeypatch.setattr(portal, "_country_names", lambda _codes: {})
    wallet["methods"] = [_card()]

    assert wallet["module"].list_for_user("u1")["methods"][0]["country_name"] == "HK"


def test_a_wallet_with_no_issuer_falls_back_to_the_billing_address(app, wallet, monkeypatch):
    """A Link method has no card object and so no issuing country. The address country is
    a better answer than a blank."""
    from billing.services import portal

    monkeypatch.setattr(portal, "_country_names", lambda _codes: {"GB": "United Kingdom"})
    method = _link()
    method["billing_details"] = {"name": None, "address": {"country": "GB"}}
    wallet["methods"] = [method]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["country"] == "GB"
    assert view["country_name"] == "United Kingdom"


def test_a_wallet_with_no_card_object_still_describes_itself(app, wallet):
    """A Stripe Link method exposes no card. "Link" is a truer answer than four rows of
    dots, and blank is no answer at all."""
    wallet["methods"] = [_link()]

    view = wallet["module"].list_for_user("u1")["methods"][0]

    assert view["type"] == "link"
    assert view["label"] == "Link"
    assert view["last4"] is None
    assert view["expired"] is False


# --- The ownership check -----------------------------------------------------


def test_another_payers_method_cannot_be_promoted(app, wallet):
    """The id comes from the browser. A ``pm_…`` on somebody else's customer answers the
    same as one that does not exist — telling the two apart confirms an id."""
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_card("pm_theirs", customer="cus_someone_else")]

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].set_default("u1", "pm_theirs")

    assert excinfo.value.status == 404
    assert wallet["default"] is None


def test_another_payers_method_cannot_be_detached(app, wallet):
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_card("pm_theirs", customer="cus_someone_else")]

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].remove("u1", "pm_theirs")

    assert excinfo.value.status == 404
    assert wallet["detached"] == []


def test_promoting_a_method_makes_it_the_one_that_charges(app, wallet):
    wallet["methods"] = [_card("pm_1"), _card("pm_2", last4="1111")]
    wallet["default"] = "pm_1"

    result = wallet["module"].set_default("u1", "pm_2")

    assert wallet["default"] == "pm_2"
    assert result["default_id"] == "pm_2"


# --- Removing ----------------------------------------------------------------


def test_the_default_cannot_be_removed_while_another_could_take_over(app, wallet):
    """Stripe clears ``default_payment_method`` on detach, so removing it first leaves an
    account holding cards with none nominated. The message names the fix."""
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_card("pm_1"), _card("pm_2", last4="1111")]
    wallet["default"] = "pm_1"

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].remove("u1", "pm_1")

    assert excinfo.value.status == 409
    assert "default" in excinfo.value.message
    assert wallet["detached"] == []


def test_the_last_method_cannot_be_removed_while_something_still_bills(app, wallet):
    """The next renewal would decline into dunning by design rather than by accident."""
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_card("pm_1")]
    wallet["default"] = "pm_1"
    wallet["bills_forward"] = True

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].remove("u1", "pm_1")

    assert excinfo.value.status == 409
    assert wallet["detached"] == []


def test_the_last_method_can_go_when_nothing_is_being_billed(app, wallet):
    """A payer who cancelled everything is entitled to take their card off the account."""
    wallet["methods"] = [_card("pm_1")]
    wallet["default"] = "pm_1"
    wallet["bills_forward"] = False

    wallet["module"].remove("u1", "pm_1")

    assert wallet["detached"] == ["pm_1"]


def test_a_spare_method_can_always_be_removed(app, wallet):
    wallet["methods"] = [_card("pm_1"), _card("pm_2", last4="1111")]
    wallet["default"] = "pm_1"
    wallet["bills_forward"] = True

    wallet["module"].remove("u1", "pm_2")

    assert wallet["detached"] == ["pm_2"]


def test_removing_a_spare_never_promotes_a_survivor(app, wallet):
    """Which card an account pays with is the payer's decision. Choosing one for them
    silently is how a company gets charged on a card it did not nominate."""
    wallet["methods"] = [_card("pm_1"), _card("pm_2", last4="1111")]
    wallet["default"] = "pm_1"

    wallet["module"].remove("u1", "pm_2")

    assert wallet["default"] == "pm_1"


# --- Editing -----------------------------------------------------------------


def test_an_expiry_in_the_past_is_refused_in_our_own_words(app, wallet):
    """Stripe's refusal arrives as an API error the form cannot attach to a field, and a
    past date is the mistake worth catching before it becomes an uncharg[e]able card."""
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_card("pm_1")]

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].update("u1", "pm_1", exp_month=7, exp_year=2026)

    assert excinfo.value.status == 422
    assert wallet["updated"] == []


def test_the_current_month_is_still_valid(app, wallet):
    wallet["methods"] = [_card("pm_1")]

    wallet["module"].update("u1", "pm_1", exp_month=8, exp_year=2026)

    assert wallet["updated"] == [("pm_1", {"exp_month": 8, "exp_year": 2026})]


def test_a_two_digit_year_is_read_as_this_century(app, wallet):
    """"29" is what a customer types into a two-box expiry field."""
    wallet["methods"] = [_card("pm_1")]

    wallet["module"].update("u1", "pm_1", exp_month=4, exp_year=29)

    assert wallet["updated"][-1][1]["exp_year"] == 2029


def test_a_wallet_has_no_expiry_to_change(app, wallet):
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_link("pm_link")]

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].update("u1", "pm_link", exp_month=4, exp_year=2029)

    assert excinfo.value.status == 422


def test_only_the_address_keys_that_were_supplied_are_sent(app, wallet):
    """Sending the whole shape with blanks would erase an address the payer never
    touched."""
    wallet["methods"] = [_card("pm_1")]

    wallet["module"].update(
        "u1", "pm_1", name="New Name", address={"line1": "2 New St", "city": None}
    )

    _pm_id, payload = wallet["updated"][-1]
    assert payload["billing_details"]["name"] == "New Name"
    assert payload["billing_details"]["address"] == {"line1": "2 New St"}
    assert "exp_month" not in payload


def test_a_blanked_field_is_cleared_not_ignored(app, wallet):
    """``""`` is Stripe's "unset". A blank used to become ``None``, which the SDK drops from
    the request (``stripe/_encode.py``) — the edit reported success and changed nothing."""
    wallet["methods"] = [_card("pm_1")]

    wallet["module"].update("u1", "pm_1", name="  ", address={"line2": "  ", "city": None})

    _pm_id, payload = wallet["updated"][-1]
    assert payload["billing_details"] == {"name": "", "address": {"line2": ""}}


def test_an_empty_edit_is_refused_rather_than_sent(app, wallet):
    from billing.services.payment_methods import PaymentMethodError

    wallet["methods"] = [_card("pm_1")]

    with pytest.raises(PaymentMethodError) as excinfo:
        wallet["module"].update("u1", "pm_1")

    assert excinfo.value.status == 422
    assert wallet["updated"] == []


# --- Adding ------------------------------------------------------------------


def _intent(**fields):
    base = {
        "id": "seti_1",
        "status": "succeeded",
        "payment_method": "pm_new",
        "customer": None,
        "metadata": {"user_id": "u1"},
    }
    base.update(fields)
    return base


def test_the_setup_intent_carries_what_elements_needs(app, wallet, monkeypatch):
    pm = wallet["module"]
    monkeypatch.setattr(
        pm, "create_setup_intent",
        lambda customer_id, **_kw: {"id": "seti_1", "client_secret": "seti_1_secret"},
    )

    result = pm.start_setup("u1")

    assert result["client_secret"] == "seti_1_secret"
    assert result["publishable_key"] == "pk_test_123"


def test_an_unconfigured_environment_says_so_rather_than_rendering_a_dead_form(
    app, wallet, monkeypatch
):
    from billing.services.payment_methods import PaymentMethodError

    pm = wallet["module"]
    monkeypatch.setattr(pm, "get_publishable_key", lambda: None)
    monkeypatch.setattr(
        pm, "create_setup_intent", lambda customer_id, **_kw: {"id": "seti_1"}
    )

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.start_setup("u1")

    assert excinfo.value.status == 503


def test_a_setup_intent_stamped_for_someone_else_is_not_adopted(app, wallet, stubbed_accounts, monkeypatch):
    """A customerless SetupIntent has nothing else tying it to anybody, which is exactly
    why the stamp is there."""
    from billing.services.payment_methods import PaymentMethodError

    pm = wallet["module"]
    monkeypatch.setattr(
        pm, "retrieve_setup_intent", lambda _i: _intent(metadata={"user_id": "u2"})
    )

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.confirm_setup("u1", "seti_1", billing_group_id="g1")

    assert excinfo.value.status == 404
    assert wallet["attached"] == []


def test_a_card_that_was_not_confirmed_creates_nothing(app, wallet, stubbed_accounts, monkeypatch):
    """Declined, abandoned, or still needing authentication — no customer, no attach."""
    from billing.services.payment_methods import PaymentMethodError

    pm = wallet["module"]
    wallet["customer_id"] = None
    monkeypatch.setattr(
        pm, "retrieve_setup_intent",
        lambda _i: _intent(status="requires_payment_method", payment_method=None),
    )

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.confirm_setup("u1", "seti_1", billing_group_id="g1")

    assert excinfo.value.status == 409
    assert wallet["created_customers"] == []
    assert wallet["attached"] == []


def test_the_first_card_opens_the_billing_account(app, wallet, stubbed_accounts, monkeypatch):
    """A SetupIntent needs no customer, so the first card can be saved from the billing
    page (into a billing account) — and the customer is made only now, once Stripe has said
    the card is real."""
    pm = wallet["module"]
    wallet["customer_id"] = None
    monkeypatch.setattr(pm, "retrieve_setup_intent", lambda _i: _intent())
    # No existing customer anywhere. The resolve helper is the checkout one, stubbed here
    # because it would otherwise search live Stripe.
    import billing.services.checkout as checkout

    monkeypatch.setattr(checkout, "_resolve_customer_id", lambda _u: None)
    monkeypatch.setattr(checkout, "_payer_identity", lambda _u: {"name": "A Payer"})
    monkeypatch.setattr(checkout, "_seed_user_customer_mapping", lambda *_a: None)

    pm.confirm_setup("u1", "seti_1", billing_group_id="g1")

    assert wallet["created_customers"] == [("u1", {"name": "A Payer"})]
    assert wallet["attached"] == [("pm_new", "cus_new")]
    # The first card is always the default: an account whose only method is not nominated
    # has nothing to charge.
    assert wallet["default"] == "pm_new"


def test_an_existing_customer_wins_over_the_one_the_intent_named(app, wallet, stubbed_accounts, monkeypatch):
    """Two customers for one payer make ``find_customer_by_user`` ambiguous, which is
    worse than the duplicate itself."""
    pm = wallet["module"]
    monkeypatch.setattr(
        pm, "retrieve_setup_intent", lambda _i: _intent(customer="cus_duplicate")
    )
    import billing.services.checkout as checkout

    monkeypatch.setattr(checkout, "_resolve_customer_id", lambda _u: "cus_1")

    pm.confirm_setup("u1", "seti_1", billing_group_id="g1")

    assert wallet["attached"] == [("pm_new", "cus_1")]
    assert wallet["created_customers"] == []


def test_a_further_card_does_not_steal_the_default_unless_asked(app, wallet, stubbed_accounts, monkeypatch):
    pm = wallet["module"]
    wallet["default"] = "pm_1"
    wallet["methods"] = [_card("pm_1")]
    monkeypatch.setattr(pm, "retrieve_setup_intent", lambda _i: _intent(customer="cus_1"))
    import billing.services.checkout as checkout

    monkeypatch.setattr(checkout, "_resolve_customer_id", lambda _u: "cus_1")

    pm.confirm_setup("u1", "seti_1", billing_group_id="g1")
    assert wallet["default"] == "pm_1"

    pm.confirm_setup("u1", "seti_1", make_default=True, billing_group_id="g1")
    assert wallet["default"] == "pm_new"


# --- A card is only ever added through a billing account (2026-10-01) ---------


def _no_stripe_reads(monkeypatch, pm):
    def _boom(*_a, **_k):
        raise AssertionError("a confirm naming no account must not reach Stripe")

    monkeypatch.setattr(pm, "retrieve_setup_intent", _boom)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"billing_email": "a@b.co"},
        {"billing_company": "Acme"},
        {"billing_email": "   ", "billing_company": "Acme"},
        {"billing_group_id": "  "},
    ],
    ids=["nothing", "email only", "company only", "blank email", "blank group"],
)
def test_a_confirm_naming_no_account_is_refused_before_stripe(app, wallet, stubbed_accounts, monkeypatch, fields):
    """THE RULE: no card saved unattached to a billing account. Refused 422 before the
    SetupIntent is even read — so nothing is attached, no customer is created, no default
    is set and no account is opened."""
    from billing.services.payment_methods import ACCOUNT_REQUIRED, PaymentMethodError

    pm = wallet["module"]
    wallet["customer_id"] = None
    _no_stripe_reads(monkeypatch, pm)

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.confirm_setup("u1", "seti_1", **fields)

    assert excinfo.value.status == 422
    assert excinfo.value.message == ACCOUNT_REQUIRED == "Choose a billing account for this card."
    assert wallet["attached"] == []
    assert wallet["created_customers"] == []
    assert wallet["default"] is None
    assert wallet["accounts"] == []


def test_the_routes_front_half_refuses_an_empty_confirm_the_same_way(app, wallet, stubbed_accounts, monkeypatch):
    """``confirm_into_account`` (both routes) answers the same sentence, before Stripe."""
    from billing.services.payment_methods import ACCOUNT_REQUIRED, PaymentMethodError

    pm = wallet["module"]
    _no_stripe_reads(monkeypatch, pm)

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.confirm_into_account("u1", "seti_1")

    assert (excinfo.value.status, excinfo.value.message) == (422, ACCOUNT_REQUIRED)
    assert wallet["attached"] == [] and wallet["accounts"] == []


def test_a_half_filled_new_account_is_refused_in_the_forms_words(app, wallet, stubbed_accounts, monkeypatch):
    """An email with no company is an attempt at "New billing account": the form's own
    field sentence, 422, still before Stripe."""
    from billing.services.billing_accounts import COMPANY_REQUIRED
    from billing.services.payment_methods import PaymentMethodError

    pm = wallet["module"]
    _no_stripe_reads(monkeypatch, pm)

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.confirm_into_account("u1", "seti_1", billing_email="a@b.co")

    assert (excinfo.value.status, excinfo.value.message) == (422, COMPANY_REQUIRED)
    assert wallet["attached"] == []


def test_someone_elses_account_is_refused_before_stripe(app, wallet, stubbed_accounts, monkeypatch):
    from billing.services.payment_methods import PaymentMethodError

    pm = wallet["module"]
    _no_stripe_reads(monkeypatch, pm)
    monkeypatch.setattr(
        pm.sub_store, "billing_group",
        lambda gid: SimpleNamespace(id=gid, payer_user_id="someone_else"),
    )

    with pytest.raises(PaymentMethodError) as excinfo:
        pm.confirm_into_account("u1", "seti_1", billing_group_id="g_theirs")

    assert excinfo.value.status == 404
    assert wallet["attached"] == []


def test_a_new_account_with_both_fields_opens_one(app, wallet, stubbed_accounts, monkeypatch):
    pm = wallet["module"]
    monkeypatch.setattr(pm, "retrieve_setup_intent", lambda _i: _intent(customer="cus_1"))
    import billing.services.checkout as checkout

    monkeypatch.setattr(checkout, "_resolve_customer_id", lambda _u: "cus_1")

    result = pm.confirm_into_account(
        "u1", "seti_1", billing_email=" pay@acme.co ", billing_company=" Acme "
    )

    assert wallet["accounts"] == [("u1", "pm_new", None, "pay@acme.co", "Acme")]
    assert result["account"]["id"] == "g_new"


# --- The endpoints -----------------------------------------------------------


def _token(app, **claims):
    return jwt.encode(claims, app.config["SECRET_KEY"], algorithm="HS256")


@pytest.fixture
def signed_in(monkeypatch):
    """A resolvable user for the routes' ``User.query.get`` check."""
    import shared_models.models as models_db

    monkeypatch.setattr(
        models_db,
        "User",
        SimpleNamespace(query=SimpleNamespace(get=lambda _id: SimpleNamespace(id="u1"))),
    )


