"""The payer portal's billing accounts — the read model and the three writes behind 08-A/B/C.

A billing account is a ``payer_billing_group``: a name, an email, the cards on it, the ONE
card it charges, the companies it pays for and its own dunning clock. What is worth pinning:

* **Each account says what IT charges.** The card views are shared between accounts and
  mark the Stripe CUSTOMER's default; an account page that showed that as "Default" would
  point at a card the account never charges.
* **The date is the one ahead, not the anchor.** Every account renews on the payer's
  anchor, and the anchor is the FIRST charge — printed as "Next Billing Date" it is a date
  in the past from the second month on.
* **Moving a company is money-safe.** Its paid days travel with it, an emptied account
  takes the cycle back rather than stranding the company's access on a stale date, and
  the four refusals each name their fix.
* **The silent failures this work found stay fixed** — the removal guard that stopped at
  the first account on a shared card, the card-keyed nomination that raised on one, and
  the confirm retry that opened a second account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from billing.services.payment_methods import _companies_billing_on as REAL_COMPANIES_BILLING_ON
from billing.tests.engine.test_billing_accounts import _entity, _user
from billing.tests.engine.test_billing_payment_methods import NOW, _card, wallet  # noqa: F401

pytestmark = pytest.mark.django_db

PAID_TO = NOW + timedelta(days=14)


# --- building blocks ----------------------------------------------------------------


def _payer(email="payer@accounts.test"):
    return _user(None, email)


def _open(payer, pm_id, *, company=None, email=None, age_days=0):
    """An account, optionally aged — two rows opened in one test share a clock tick, and
    "oldest first" must not fall to the uuid tie-break."""
    from billing.services import store

    account = store.create_billing_account(
        payer.id, pm_id, billing_email=email, billing_company=company
    )
    if age_days:
        account.created_at = datetime.now(UTC) - timedelta(days=age_days)
        account.save(update_fields=["created_at"])
    return account


def _billed(entity, payer, account, phase="active"):
    """A company ``payer`` pays for, on ``account``."""
    from billing.services import store

    store.upsert_module_row(entity.id, "PETTY_CASH", payer.id, phase=phase)
    store.nominate_group_for_entity(entity.id, payer.id, account.id, source="chosen")


def _paid_to(account, when):
    from billing.services import store

    store.set_group_paid_through(account.id, when)


def _account(result, account_id):
    return next(a for a in result["accounts"] if a["id"] == str(account_id))


def _read(payer, **kwargs):
    from billing.services import portal

    return portal.build_billing_accounts(payer.id, **kwargs)


# --- the read model -----------------------------------------------------------------


def test_accounts_come_oldest_first_named_or_as_the_payer(app, wallet):  # noqa: F811
    """The first is the one 08-A shows by default. An account nobody named reads as the
    payer — what every account opened before names existed renders as."""
    payer = _payer()
    named = _open(payer, "pm_a", company="Acme Ltd", age_days=2)
    unnamed = _open(payer, "pm_b", age_days=1)
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]

    result = _read(payer)

    assert [a["id"] for a in result["accounts"]] == [str(named.id), str(unnamed.id)]
    assert [a["name"] for a in result["accounts"]] == ["Acme Ltd", "Pat Payer"]
    assert result["accounts"][1]["billing_company"] is None
    assert result["payer"]["name"] == "Pat Payer"


def test_each_account_marks_the_card_it_charges_not_the_customers_default(
    app, wallet  # noqa: F811
):
    """The shared card views mark Stripe's CUSTOMER default. An account page reading that
    as "Default" points at a card the account never charges."""
    from billing.services import store

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    store.add_card_to_group(account.id, "pm_b")
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]
    wallet["default"] = "pm_b"

    view = _account(_read(payer), account.id)

    assert [(c["id"], c["is_default"]) for c in view["cards"]] == [
        ("pm_a", True), ("pm_b", False),
    ]
    assert view["card"]["id"] == "pm_a"
    # The flat wallet is untouched: the customer default still answers pickers.
    assert _read(payer)["default_id"] == "pm_b"


def test_the_address_is_the_charged_cards_billing_address(app, wallet, countries):  # noqa: F811
    """No column holds an account's address (the user's decision): it IS the billing
    address of the card the account charges, named the way the portal names countries."""
    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    card = _card("pm_a")
    card["billing_details"]["address"] = {
        "line1": "Unit 10, 1/F", "line2": "ABC Building", "city": "Hong Kong",
        "state": "Quarry Bay", "postal_code": None, "country": "HK",
    }
    wallet["methods"] = [card]

    address = _account(_read(payer), account.id)["address"]

    assert address["line1"] == "Unit 10, 1/F"
    assert address["state"] == "Quarry Bay"
    assert address["country_name"] == "Hong Kong"


def test_bill_to_email_is_the_billing_email_then_the_shared_business_email_then_the_payer(
    app, wallet  # noqa: F811
):
    """What 08-B prints under "Bill to" - and where the account's money emails go, and what
    its invoices name (``store.account_email``, the user's order, 2026-09-30)."""
    payer = _payer()
    billed = _open(payer, "pm_a", email="ap@billing.test", age_days=3)
    shared = _open(payer, "pm_b", age_days=2)
    split = _open(payer, "pm_c", age_days=1)
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111"), _card("pm_c", last4="2222")]
    companies = (
        (billed, "ap@acme.test"),
        (shared, "ap@group.test"), (shared, "ap@group.test"),
        (split, "ap@one.test"), (split, "ap@two.test"),
    )
    for index, (account, email) in enumerate(companies):
        entity = _entity(None, f"Company {index}")
        entity.business_email = email
        entity.save(update_fields=["business_email"])
        _billed(entity, payer, account)

    result = _read(payer)

    assert [_account(result, a.id)["bill_to_email"] for a in (billed, shared, split)] == [
        "ap@billing.test", "ap@group.test", "payer@accounts.test",
    ]
    # The column itself stays raw: 08-C's form edits what was typed, not the fallback.
    assert _account(result, shared.id)["billing_email"] is None


def test_a_charged_card_stripe_no_longer_holds_reads_as_no_card(app, wallet):  # noqa: F811
    """Detached at Stripe, the account cannot pay. A blank row would hide that; null says it."""
    payer = _payer()
    account = _open(payer, "pm_gone", company="Acme Ltd")
    wallet["methods"] = []

    view = _account(_read(payer), account.id)

    assert view["card"] is None
    assert view["cards"] == []
    assert view["address"] is None


def test_companies_are_listed_with_what_is_owed(app, wallet, monkeypatch):  # noqa: F811
    from billing.services import clock, store

    monkeypatch.setattr(clock, "now", lambda: NOW)
    payer = _payer()
    first = _open(payer, "pm_a", company="Acme Ltd", age_days=2)
    second = _open(payer, "pm_b", company="Beta Ltd", age_days=1)
    # Past due with no dunning running: a collection given up on long ago. The customer was
    # told (``dunning.customer_told``), so it shows.
    store.set_group_paid_through(first.id, NOW - timedelta(days=40))
    _billed(_entity(None, "Zeta Co"), payer, first, phase="active")
    _billed(_entity(None, "Alpha Co"), payer, first, phase="past_due")
    _billed(_entity(None, "Trial Co"), payer, second, phase="trial")
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]

    result = _read(payer)
    one, two = _account(result, first.id), _account(result, second.id)

    assert [(c["entity_name"], c["past_due"]) for c in one["companies"]] == [
        ("Alpha Co", True), ("Zeta Co", False),
    ]
    assert (one["past_due"], one["in_dunning"]) == (True, False)
    assert (two["past_due"], two["in_dunning"]) == (False, False)

    group = store.billing_group(second.id)
    group.dunning_started_at = NOW
    group.save(update_fields=["dunning_started_at"])

    two = _account(_read(payer), second.id)
    assert (two["past_due"], two["in_dunning"]) == (True, True)


def test_a_company_held_past_due_while_the_processor_failed_is_not_shown_failed(
    app, wallet, monkeypatch  # noqa: F811
):
    """Held in its grace while the PROCESSOR was failing - nobody was told, because the card
    was never asked. The account must not say otherwise (the user's rule, 2026-09-30)."""
    from billing.services import clock, store

    monkeypatch.setattr(clock, "now", lambda: NOW)
    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    store.set_group_paid_through(account.id, NOW - timedelta(days=2))   # inside the grace
    _billed(_entity(None, "Held Co"), payer, account, phase="past_due")
    wallet["methods"] = [_card("pm_a")]

    one = _account(_read(payer), account.id)

    assert [(c["entity_name"], c["past_due"]) for c in one["companies"]] == [("Held Co", False)]
    assert (one["past_due"], one["in_dunning"]) == (False, False)


def test_a_company_handed_away_is_history_not_a_listing(app, wallet):  # noqa: F811
    """A nomination outlives a handover as history. The account must not claim a company
    somebody else pays for now."""
    from billing.services import store

    payer = _payer()
    other = _payer("new-payer@accounts.test")
    account = _open(payer, "pm_a", company="Acme Ltd")
    gone = _entity(None, "Handed Over Ltd")
    store.upsert_module_row(gone.id, "PETTY_CASH", other.id, phase="active")
    store.nominate_group_for_entity(gone.id, payer.id, account.id)
    wallet["methods"] = [_card("pm_a")]

    assert _account(_read(payer), account.id)["companies"] == []


def test_the_country_list_comes_only_when_asked(app, wallet, countries):  # noqa: F811
    payer = _payer()
    _open(payer, "pm_a")
    wallet["methods"] = [_card("pm_a")]

    assert "countries" not in _read(payer)
    assert "publishable_key" not in _read(payer)
    asked = _read(payer, countries=True)
    assert {"code": "HK", "name": "Hong Kong"} in asked["countries"]
    # 08-C's address form is Stripe's own: the key that mounts it comes with the registry.
    assert asked["publishable_key"] == "pk_test_123"


def test_no_stripe_here_is_no_key_rather_than_a_failed_read(
    app, wallet, countries, monkeypatch  # noqa: F811
):
    """The form then says the address cannot be changed here; the name and email still can."""
    payer = _payer()
    _open(payer, "pm_a")
    wallet["methods"] = [_card("pm_a")]
    monkeypatch.setattr(wallet["module"], "get_publishable_key", lambda: None)

    assert _read(payer, countries=True)["publishable_key"] is None


def test_the_next_billing_date_is_the_boundary_ahead_not_the_anchor(
    app, wallet, monkeypatch  # noqa: F811
):
    """THE BUG 08-A SHIPPED WITH. Anchored 28 Jul, read on 13 Aug: the page printed
    "28 Jul 2026" as the next billing date. One date, shared by every account."""
    from billing.services import clock, store

    payer = _payer()
    _open(payer, "pm_a")
    wallet["methods"] = [_card("pm_a")]
    monkeypatch.setattr(
        store, "billing_cycle_for_user",
        lambda _u: (datetime(2026, 7, 28, 13, tzinfo=UTC), "HKD"),
    )
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 8, 13, 9, tzinfo=UTC))

    result = _read(payer)

    assert result["next_billing"] == "28 Aug 2026"
    assert result["next_billing_iso"].startswith("2026-08-28")


def test_no_cycle_means_no_date(app, wallet):  # noqa: F811
    """Nothing has ever been charged: there is no cycle to project."""
    payer = _payer()
    _open(payer, "pm_a")
    wallet["methods"] = [_card("pm_a")]

    assert _read(payer)["next_billing"] is None


# --- the next bill, estimated (08-B "Amount (estimated)") ------------------------------

ANCHOR = datetime(2026, 7, 28, 13, tzinfo=UTC)
NEXT = datetime(2026, 8, 28, 13, tzinfo=UTC)


def _cycle(payer, monkeypatch):
    """Anchored 28 Jul, read on 13 Aug: the next renewal is 28 Aug."""
    import uuid

    from billing.services import clock, store
    from billing.tests.engine.conftest import seed_plans
    from shared_models.models import UserStripeCustomer

    seed_plans()
    # The row itself: ``wallet`` stubs ``store.upsert_customer_mapping``.
    UserStripeCustomer(
        id=str(uuid.uuid4()), user_id=str(payer.id), stripe_customer_id="cus_1"
    ).save(force_insert=True)
    store.start_billing_cycle(payer.id, ANCHOR, "hkd")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 8, 13, 9, tzinfo=UTC))


def _module(entity, payer, code, phase, trial_end=None):
    from billing.services import store

    store.upsert_module_row(entity.id, code, payer.id, phase=phase, trial_end=trial_end)


def _nominate(entity, payer, account):
    from billing.services import store

    store.nominate_group_for_entity(entity.id, payer.id, account.id, source="chosen")


def test_the_next_bill_is_what_the_renewal_runner_would_charge_that_account(
    app, wallet, monkeypatch  # noqa: F811
):
    """Priced by the runner's own ``build_renewal`` for the period starting on the next
    billing date - so the estimate cannot quote a price the invoice will not charge - and
    per ACCOUNT: another account's company is on its own bill, not this one's."""
    from billing.services import portal

    payer = _payer()
    first = _open(payer, "pm_a", company="First", age_days=2)
    second = _open(payer, "pm_b", company="Second", age_days=1)
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]
    _cycle(payer, monkeypatch)
    _billed(_entity(None, "Alpha Ltd"), payer, first)
    _billed(_entity(None, "Beta Ltd"), payer, first)
    _billed(_entity(None, "Gamma Ltd"), payer, second)

    result = _read(payer)

    assert _account(result, first.id)["next_bill"] == {
        "amount": portal._money(56000, "HKD"),
        "amount_minor": 56000,
        "currency": "HKD",
    }
    assert _account(result, second.id)["next_bill"]["amount_minor"] == 28000


def test_a_trial_converting_before_the_date_is_on_the_bill_one_that_lapses_is_not(
    app, wallet, monkeypatch  # noqa: F811
):
    """A trial ending before the renewal with a card and consent is active by then, so the
    renewal bills it - and its company goes onto the bundle. The trial-end job's own rule:
    no consent, and it lapses instead; ending after the date, it is not on THIS bill."""
    from billing.services import renewals, store
    from billing.services.billing import period_containing

    payer = _payer()
    account = _open(payer, "pm_a")
    wallet["methods"] = [_card("pm_a")]
    _cycle(payer, monkeypatch)
    converting = datetime(2026, 8, 20, tzinfo=UTC)

    both = _entity(None, "Both Ltd")  # paid Petty Cash + a Payment Request trial converting
    _module(both, payer, "PETTY_CASH", "active")
    _module(both, payer, "PAYMENT_REQUEST", "trial", trial_end=converting)
    _nominate(both, payer, account)
    store.record_billing_consent(both.id, payer.id, "card")

    lapsing = _entity(None, "Lapsing Ltd")  # a trial nobody agreed to be billed for
    _module(lapsing, payer, "PETTY_CASH", "trial", trial_end=converting)
    _nominate(lapsing, payer, account)

    later = _entity(None, "Later Ltd")  # converts, but after the renewal
    _module(later, payer, "PETTY_CASH", "trial", trial_end=datetime(2026, 9, 5, tzinfo=UTC))
    _nominate(later, payer, account)
    store.record_billing_consent(later.id, payer.id, "card")

    assert _account(_read(payer), account.id)["next_bill"]["amount_minor"] == 40000  # the bundle

    # The runner is untouched: it bills the phases as they are when it fires.
    period = period_containing(ANCHOR, NEXT)
    assert renewals.build_renewal(payer.id, period, group_id=account.id).total == 28000


def test_no_cycle_nothing_billing_or_a_failure_means_no_estimate(
    app, wallet, monkeypatch  # noqa: F811
):
    """No cycle yet: no date, so no bill. Only trials that will not convert: nothing to
    bill. A forecast that cannot be priced leaves the figure out - never the page."""
    from billing.services import renewals

    payer = _payer()
    account = _open(payer, "pm_a")
    wallet["methods"] = [_card("pm_a")]
    trial = _entity(None, "Trial Ltd")
    _module(trial, payer, "PETTY_CASH", "trial", trial_end=datetime(2026, 8, 20, tzinfo=UTC))
    _nominate(trial, payer, account)

    assert _account(_read(payer), account.id)["next_bill"] is None  # no cycle

    _cycle(payer, monkeypatch)
    assert _account(_read(payer), account.id)["next_bill"] is None  # no consent: it lapses

    _billed(_entity(None, "Paid Ltd"), payer, account)

    def boom(*_a, **_k):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(renewals, "build_renewal", boom)
    result = _read(payer)
    assert _account(result, account.id)["next_bill"] is None
    assert result["next_billing"] == "28 Aug 2026"  # the page still answers


# --- the card an account charges ----------------------------------------------------


def test_switching_the_charged_card_moves_both_halves(app, wallet):  # noqa: F811
    """08-B's "Set as default": renewals read the account column, the page reads the
    shelf — both move, and the customer default does not."""
    from billing.services import billing_accounts, store

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    store.add_card_to_group(account.id, "pm_b")
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]
    wallet["default"] = "pm_a"

    result = billing_accounts.set_default_card(payer.id, account.id, "pm_b")

    assert store.billing_group(account.id).stripe_payment_method_id == "pm_b"
    assert {r.stripe_payment_method_id: r.is_default for r in store.cards_in_group(account.id)} == {
        "pm_a": False, "pm_b": True,
    }
    assert _account(result, account.id)["default_id"] == "pm_b"
    assert wallet["default"] == "pm_a"


@pytest.mark.parametrize("case", ["not_on_the_account", "someone_elses_account", "someone_elses_card"])
def test_the_charged_card_only_moves_within_the_callers_own(app, wallet, case):  # noqa: F811
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a"), _card("pm_c", last4="3333")]
    target_account, card = account.id, "pm_c"
    if case == "someone_elses_account":
        target_account = _open(_payer("other@accounts.test"), "pm_x").id
        store.add_card_to_group(target_account, "pm_c")
    if case == "someone_elses_card":
        wallet["methods"].append(_card("pm_theirs", customer="cus_else"))
        store.add_card_to_group(account.id, "pm_theirs")
        card = "pm_theirs"

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.set_default_card(payer.id, target_account, card)

    assert caught.value.status == 404
    assert store.billing_group(account.id).stripe_payment_method_id == "pm_a"


# --- 08-C: the name, the email and the address ---------------------------------------


def _address(**overrides):
    base = {"line1": "2 ABC Street", "line2": "", "city": "Hong Kong", "state": "",
            "country": "hk"}
    base.update(overrides)
    return base


def test_nothing_is_written_when_anything_is_invalid(app, wallet, countries):  # noqa: F811
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a")]

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.update(
            payer.id, account.id, billing_company="New Name", address=_address(line1="  ")
        )

    assert caught.value.status == 422
    assert wallet["updated"] == []
    assert store.billing_group(account.id).billing_company == "Acme Ltd"


def test_the_address_goes_on_the_charged_card_and_a_blank_clears(
    app, wallet, countries  # noqa: F811
):
    """``""`` is Stripe's "unset". ``None`` is dropped by the SDK before the request, which
    is how clearing a field used to report success and change nothing."""
    from billing.services import billing_accounts

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a")]

    billing_accounts.update(payer.id, account.id, address=_address())

    assert wallet["updated"] == [
        ("pm_a", {"billing_details": {"address": {
            "line1": "2 ABC Street", "line2": "", "city": "Hong Kong", "state": "",
            "country": "HK",
        }}}),
    ]


def test_the_cardholder_travels_with_the_address_in_one_write(app, wallet, countries):  # noqa: F811
    """Stripe's address form asks for the name with the address; both are the charged card's
    billing details, so they go in ONE write - never an address saved and a name refused."""
    from billing.services import billing_accounts

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a")]

    billing_accounts.update(
        payer.id, account.id, address=_address(postal_code="999077"),
        cardholder="  Rebecca Park ",
    )

    assert wallet["updated"] == [
        ("pm_a", {"billing_details": {
            "address": {"line1": "2 ABC Street", "line2": "", "city": "Hong Kong",
                        "state": "", "postal_code": "999077", "country": "HK"},
            "name": "Rebecca Park",
        }}),
    ]


def test_a_cardholder_alone_goes_to_the_charged_card(app, wallet, countries):  # noqa: F811
    from billing.services import billing_accounts

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a")]

    billing_accounts.update(payer.id, account.id, cardholder="Rebecca Park")

    assert wallet["updated"] == [("pm_a", {"billing_details": {"name": "Rebecca Park"}})]


def test_a_refused_address_leaves_the_name_alone(app, wallet, countries, monkeypatch):  # noqa: F811
    """Stripe first: it is the write that fails, and failing there changes nothing."""
    from billing.services import billing_accounts, store

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a")]

    def _refuse(*_a, **_k):
        raise RuntimeError("stripe said no")

    monkeypatch.setattr(wallet["module"], "update_payment_method", _refuse)

    with pytest.raises(RuntimeError):
        billing_accounts.update(
            payer.id, account.id, billing_company="New Name", address=_address()
        )

    assert store.billing_group(account.id).billing_company == "Acme Ltd"


@pytest.mark.parametrize(
    ("kwargs", "status", "words"),
    [
        ({"address": _address(country="ZZ")}, 422, "country"),
        ({}, 422, "nothing to change"),
        ({"billing_company": "   "}, 422, "company name"),
        ({"billing_email": "not-an-address"}, 422, "doesn't look right"),
        ({"billing_email": "ap@acme"}, 422, "doesn't look right"),
        # English only (2026-10-01): Korean either side of the "@" is its own sentence.
        ({"billing_email": "김철수@acme.test"}, 422, "English letters, numbers and symbols"),
        ({"billing_email": "ap@회사.한국"}, 422, "English letters, numbers and symbols"),
        # VARCHAR(255) - and Stripe's name, held to the same limit.
        ({"billing_company": "x" * 256}, 422, "under 255 characters"),
        ({"billing_email": "a" * 250 + "@acme.test"}, 422, "under 255 characters"),
        ({"cardholder": "x" * 256}, 422, "under 255 characters"),
    ],
)
def test_each_refusal_names_its_field(app, wallet, countries, kwargs, status, words):  # noqa: F811
    from billing.services import billing_accounts
    from billing.services.payment_methods import PaymentMethodError

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a")]

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.update(payer.id, account.id, **kwargs)

    assert caught.value.status == status
    assert words in caught.value.message
    assert wallet["updated"] == []


@pytest.mark.parametrize("email", ["a+b@sub.domain.museum", "o'neil_1@x-y.co.uk"])
def test_the_email_rule_is_english_only_but_still_shallow(email):
    """Printable ASCII is the whole tightening: an odd but English address still passes."""
    from billing.services import billing_accounts

    assert billing_accounts.validate_identity(email, None, require_both=False) == (email, None)


def test_an_account_whose_card_is_gone_cannot_hold_an_address(app, wallet, countries):  # noqa: F811
    from billing.services import billing_accounts
    from billing.services.payment_methods import PaymentMethodError

    payer = _payer()
    account = _open(payer, "pm_gone", company="Acme Ltd")
    wallet["methods"] = []

    for kwargs in ({"address": _address()}, {"cardholder": "Rebecca Park"}):
        with pytest.raises(PaymentMethodError) as caught:
            billing_accounts.update(payer.id, account.id, **kwargs)

        assert caught.value.status == 409
        assert "no card to keep its address on" in caught.value.message


def test_renaming_touches_no_card_and_a_blank_email_clears(app, wallet):  # noqa: F811
    from billing.services import billing_accounts, store

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd", email="ap@acme.test")
    wallet["methods"] = [_card("pm_a")]

    result = billing_accounts.update(
        payer.id, account.id, billing_company="  Vine Consulting Limited ", billing_email=""
    )

    group = store.billing_group(account.id)
    assert (group.billing_company, group.billing_email) == ("Vine Consulting Limited", None)
    assert _account(result, account.id)["name"] == "Vine Consulting Limited"
    assert wallet["updated"] == []


# --- moving a company between accounts ----------------------------------------------


def _two_accounts(wallet):  # noqa: F811
    """Acme (charging pm_a, paid to PAID_TO) holding a company to move plus one that stays,
    and Beta (charging pm_b) with no cycle yet."""
    payer = _payer()
    acme = _open(payer, "pm_a", company="Acme Ltd", age_days=2)
    beta = _open(payer, "pm_b", company="Beta Ltd", age_days=1)
    moving, staying = _entity(None, "Moving Co"), _entity(None, "Staying Co")
    _billed(moving, payer, acme)
    _billed(staying, payer, acme)
    _paid_to(acme, PAID_TO)
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]
    return payer, acme, beta, moving


def test_moving_repoints_the_company_and_charges_nothing(app, wallet):  # noqa: F811
    from billing.services import billing_accounts, store
    from shared_models.models import SubscriptionInvoice

    payer, acme, beta, moving = _two_accounts(wallet)

    result = billing_accounts.move_company(payer.id, moving.id, beta.id)

    nomination = store.nomination_for_entity(moving.id, payer.id)
    assert str(nomination.billing_group_id) == str(beta.id)
    assert nomination.source == "moved"
    # Beta had never collected: it takes the cycle over where Acme's money reached.
    assert store.billing_group(beta.id).paid_through == PAID_TO
    assert not SubscriptionInvoice.objects.exists()
    assert result["moved"] == {
        "entity_id": str(moving.id),
        "entity_name": "Moving Co",
        "from_account": {"id": str(acme.id), "name": "Acme Ltd"},
        "to_account": {"id": str(beta.id), "name": "Beta Ltd"},
    }
    assert [c["entity_name"] for c in _account(result, beta.id)["companies"]] == ["Moving Co"]


def test_an_emptied_account_takes_the_cycle_back(app, wallet):  # noqa: F811
    """THE ACCESS HOLE. Beta was emptied a month ago, so renewals stopped advancing it; the
    company's access is measured against Beta's date, not against a claim, and would lapse
    at the next sweep for days it had paid for."""
    from billing.services import billing_accounts, store

    payer, _acme, beta, moving = _two_accounts(wallet)
    _paid_to(beta, PAID_TO - timedelta(days=31))

    billing_accounts.move_company(payer.id, moving.id, beta.id)

    assert store.billing_group(beta.id).paid_through == PAID_TO
    assert store.paid_through_for_entity(moving.id) == PAID_TO
    assert store.module_row(moving.id, "PETTY_CASH").billed_through is None


def test_a_busy_account_behind_keeps_its_date_and_the_company_carries_a_claim(
    app, wallet  # noqa: F811
):
    """Advancing an account other companies still pay on would hand them free days; the
    claim suppresses the re-bill of days the company already paid for instead."""
    from billing.services import billing_accounts, store

    payer, _acme, beta, moving = _two_accounts(wallet)
    behind = PAID_TO - timedelta(days=31)
    _paid_to(beta, behind)
    _billed(_entity(None, "Beta Resident Co"), payer, beta)

    billing_accounts.move_company(payer.id, moving.id, beta.id)

    assert store.billing_group(beta.id).paid_through == behind
    assert store.module_row(moving.id, "PETTY_CASH").billed_through == PAID_TO


def test_a_past_due_company_is_settled_before_it_moves(app, wallet):  # noqa: F811
    """Its debt is an invoice Acme raised; the retries and "Pay now" follow the account it
    is on. Moved, they would chase Beta, find nothing owed, and nothing could clear it."""
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer, acme, beta, moving = _two_accounts(wallet)
    store.upsert_module_row(moving.id, "PETTY_CASH", payer.id, phase="past_due")

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.move_company(payer.id, moving.id, beta.id)

    assert caught.value.status == 409
    assert caught.value.message == (
        "Moving Co's last payment on Acme Ltd didn't go through. "
        "Settle it there first, then move the company."
    )
    assert str(store.nomination_for_entity(moving.id, payer.id).billing_group_id) == str(acme.id)


def test_a_target_in_dunning_is_refused(app, wallet):  # noqa: F811
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer, _acme, beta, moving = _two_accounts(wallet)
    group = store.billing_group(beta.id)
    group.dunning_started_at = NOW
    group.save(update_fields=["dunning_started_at"])

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.move_company(payer.id, moving.id, beta.id)

    assert caught.value.status == 409
    assert "A payment on Beta Ltd didn't go through" in caught.value.message


def test_a_target_whose_card_is_gone_is_refused(app, wallet):  # noqa: F811
    from billing.services import billing_accounts
    from billing.services.payment_methods import PaymentMethodError

    payer, _acme, beta, moving = _two_accounts(wallet)
    wallet["methods"] = [_card("pm_a")]  # pm_b detached at Stripe

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.move_company(payer.id, moving.id, beta.id)

    assert caught.value.status == 409
    assert caught.value.message == "Beta Ltd has no card it can charge. Add a card to it first."


def _loose_trial(payer):
    """A company the payer pays for with a trial started card-free: on no account at all."""
    from billing.services import store

    loose = _entity(None, "Loose Co")
    store.upsert_module_row(loose.id, "PETTY_CASH", payer.id, phase="trial")
    return loose


def test_a_company_on_no_account_is_placed_and_nothing_else_is_written(app, wallet):  # noqa: F811
    """Manage Subscriptions asks which account bills a change before applying it, and a
    card-free trial has none: it is PLACED (it used to be refused as "nothing to move").
    Nothing is charged, no consent is written - that is the confirm's own statement, made
    right after - and there are no paid days to carry onto the account."""
    from billing.services import billing_accounts, store
    from shared_models.models import EntityBillingConsent, SubscriptionInvoice

    payer, _acme, beta, _moving = _two_accounts(wallet)
    loose = _loose_trial(payer)

    result = billing_accounts.move_company(payer.id, loose.id, beta.id)

    nomination = store.nomination_for_entity(loose.id, payer.id)
    assert str(nomination.billing_group_id) == str(beta.id)
    assert nomination.source == "chosen"
    assert result["moved"] == {
        "entity_id": str(loose.id),
        "entity_name": "Loose Co",
        "from_account": None,
        "to_account": {"id": str(beta.id), "name": "Beta Ltd"},
    }
    assert store.billing_group(beta.id).paid_through is None
    assert not EntityBillingConsent.objects.filter(entity_id=str(loose.id)).exists()
    assert not SubscriptionInvoice.objects.exists()


def test_a_company_with_no_subscriber_at_all_is_placed_too(app, wallet):  # noqa: F811
    """THE REAL card-free trial, since 2026-10-08: it has no payer either, because starting a
    trial establishes none. ``_payer_of`` would answer 409 "no subscription to bill yet" and
    the placement above would be unreachable, so ``move_company`` asks with
    ``establish_payer=True`` - it only answers when the answer is "nobody", and still 404s the
    moment somebody else pays.

    It writes the NOMINATION only. The module rows are stamped by the confirm that follows in
    the same request (``modules._activate_subscription``), which is why nothing here claims the
    company has a subscriber yet."""
    from billing.services import billing_accounts, store

    payer, _acme, beta, _moving = _two_accounts(wallet)
    loose = _entity(None, "Nobody's Co")
    store.upsert_module_row(loose.id, "PETTY_CASH", phase="trial")
    assert store.payer_for_entity(loose.id) is None

    result = billing_accounts.move_company(payer.id, loose.id, beta.id)

    assert result["moved"]["from_account"] is None
    nomination = store.nomination_for_entity(loose.id, payer.id)
    assert str(nomination.billing_group_id) == str(beta.id)
    # Still no subscriber: placing is not confirming.
    assert store.payer_for_entity(loose.id) is None


def test_placing_a_company_somebody_else_pays_for_is_still_refused(app, wallet):  # noqa: F811
    """``establish_payer`` is not a way past the check and cannot become one."""
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer, _acme, beta, _moving = _two_accounts(wallet)
    stranger = _user(None, "stranger@accounts.test")
    theirs = _entity(None, "Theirs Co")
    store.upsert_module_row(theirs.id, "PETTY_CASH", stranger.id, phase="active")

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.move_company(payer.id, theirs.id, beta.id)

    assert caught.value.status == 404


@pytest.mark.parametrize("target_state", ["in_dunning", "card_gone"])
def test_a_first_placement_meets_the_same_target_refusals(app, wallet, target_state):  # noqa: F811
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer, _acme, beta, _moving = _two_accounts(wallet)
    loose = _loose_trial(payer)
    if target_state == "in_dunning":
        group = store.billing_group(beta.id)
        group.dunning_started_at = NOW
        group.save(update_fields=["dunning_started_at"])
    else:
        wallet["methods"] = [_card("pm_a")]  # pm_b detached at Stripe

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.move_company(payer.id, loose.id, beta.id)

    assert caught.value.status == 409
    assert store.nomination_for_entity(loose.id, payer.id) is None


def test_moving_onto_the_account_it_is_on_changes_nothing(app, wallet):  # noqa: F811
    from billing.services import billing_accounts

    payer, acme, _beta, moving = _two_accounts(wallet)

    assert billing_accounts.move_company(payer.id, moving.id, acme.id)["moved"] is None


@pytest.mark.parametrize("case", ["not_the_payer", "someone_elses_account"])
def test_only_the_payer_moves_only_onto_their_own(app, wallet, case):  # noqa: F811
    from billing.services import billing_accounts, store
    from billing.services.payment_methods import PaymentMethodError

    payer, acme, beta, moving = _two_accounts(wallet)
    stranger = _payer("stranger@accounts.test")
    caller, target = payer.id, beta.id
    if case == "not_the_payer":
        caller = stranger.id
    else:
        target = _open(stranger, "pm_z").id

    with pytest.raises(PaymentMethodError) as caught:
        billing_accounts.move_company(caller, moving.id, target)

    assert caught.value.status == 404
    assert str(store.nomination_for_entity(moving.id, payer.id).billing_group_id) == str(acme.id)


# --- the silent failures, pinned -----------------------------------------------------


def test_the_removal_guard_checks_every_account_on_a_shared_card(app, monkeypatch):
    """It stopped at the FIRST account charging the card. Two accounts may share one, so
    a card still renewing the second account's companies could be detached."""
    from billing.services import payment_methods

    monkeypatch.setattr(payment_methods, "_companies_billing_on", REAL_COMPANIES_BILLING_ON)
    payer = _payer()
    _open(payer, "pm_shared", company="Empty Ltd", age_days=2)
    busy = _open(payer, "pm_shared", company="Busy Ltd", age_days=1)
    _billed(_entity(None, "Renewing Co"), payer, busy)

    assert payment_methods._companies_billing_on(payer.id, "pm_shared") == ["Renewing Co"]


def test_a_card_nomination_on_a_shared_card_joins_the_oldest_account(app):
    """``_one_or_none`` raised here, so every card-keyed nomination for the payer — the
    settings picker, consent, a handover's accept — answered 500."""
    from billing.services import store

    payer = _payer()
    oldest = _open(payer, "pm_shared", company="First Ltd", age_days=2)
    _open(payer, "pm_shared", company="Second Ltd", age_days=1)

    group = store.nominate_card_for_entity(_entity(None, "Joining Co").id, payer.id, "pm_shared")

    assert str(group.id) == str(oldest.id)


def test_a_retried_confirm_answers_the_account_it_opened(app, wallet, monkeypatch):  # noqa: F811
    """The answer to "New billing account" can be lost on the way back; the retry must not
    open a second account on the same card (which is the shared-card state above)."""
    import billing.services.checkout as checkout
    from billing.services import store

    payer = _payer()
    pm = wallet["module"]
    monkeypatch.setattr(pm, "retrieve_setup_intent", lambda _i: {
        "id": "seti_1", "status": "succeeded", "payment_method": "pm_new",
        "customer": "cus_1", "metadata": {"user_id": str(payer.id)},
    })
    monkeypatch.setattr(checkout, "_resolve_customer_id", lambda _u: "cus_1")

    first = pm.confirm_setup(
        payer.id, "seti_1", billing_company="Acme Ltd", billing_email="ap@acme.test"
    )
    again = pm.confirm_setup(
        payer.id, "seti_1", billing_company="Acme Trading Ltd", billing_email="ap@acme.test"
    )

    assert first["account"]["id"] == again["account"]["id"]
    assert [g.billing_company for g in store.billing_groups_for_payer(payer.id)] == [
        "Acme Trading Ltd"
    ]


def test_removing_a_spare_from_an_accounts_page_hands_the_customer_default_over(
    app, wallet  # noqa: F811
):
    """"Make another one the default first" named the Stripe customer default, which the
    account page has no button for — its "Set as default" switches what the ACCOUNT
    charges. The customer default goes to this account's card instead."""
    from billing.services import store

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    store.add_card_to_group(account.id, "pm_b")
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]
    wallet["default"] = "pm_b"

    wallet["module"].remove(payer.id, "pm_b", account_id=account.id)

    assert wallet["default"] == "pm_a"
    assert wallet["detached"] == ["pm_b"]


def test_an_accounts_own_card_is_refused_in_its_words(app, wallet):  # noqa: F811
    from billing.services.payment_methods import PaymentMethodError

    payer = _payer()
    account = _open(payer, "pm_a", company="Acme Ltd")
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]

    with pytest.raises(PaymentMethodError) as caught:
        wallet["module"].remove(payer.id, "pm_a", account_id=account.id)

    assert caught.value.status == 409
    assert "this billing account's default card" in caught.value.message
    assert wallet["detached"] == []
