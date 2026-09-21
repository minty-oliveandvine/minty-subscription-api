"""Billing accounts — the account is the identity, and it holds the cards.

``v1a01_billing_account`` turned ``payer_billing_group`` from "one card and its cycle"
into the account a payer creates and names: ``billing_email`` / ``billing_company``, plus
``billing_account_payment_method`` listing every card on it.

THE ONE THING THESE TESTS ARE REALLY FOR is the duplicated default. The card an account
charges is recorded twice on purpose — as ``payer_billing_group.stripe_payment_method_id``
so the charge path is a single row read, and as ``is_default`` on the shelf so the picker
can render it. Nothing raises when those two disagree; the account simply bills a card the
payer was never shown. So every path that writes either one is pinned here to write both.
"""
from __future__ import annotations

import uuid
from datetime import UTC

import pytest


@pytest.fixture
def db_session(db):
    """Flask's per-test database (rows DELETEd afterwards) is pytest-django's ``db``
    (a transaction rolled back afterwards). The helpers below take it for parity."""
    return _DbShim()


class _DbShim:
    """What the ported helpers still reach for: ``db.session.refresh(row)``."""

    class session:  # noqa: N801
        @staticmethod
        def refresh(row):
            row.refresh_from_db()

        @staticmethod
        def commit():
            pass


def _user(db, email):
    from shared_models.models import User

    row = User(
        id=str(uuid.uuid4()),
        email=email,
        username=email,
        first_name="Pat",
        last_name="Payer",
        password="x",
        system_role="normal",
        approved=True,
    )
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _entity(db, name="Acme"):
    from shared_models.models import Entity

    row = Entity(id=str(uuid.uuid4()), name=name, status="disconnected")
    row.save(force_insert=True)
    pass  # commit: autocommit under Django
    return row


def _cards(store, group_id):
    return {
        row.stripe_payment_method_id: row.is_default
        for row in store.cards_in_group(group_id)
    }


# --- opening an account ---------------------------------------------------------


def test_a_new_account_opens_with_its_card_already_on_the_shelf(app, db_session):
    """The shelf is not filled in later. An account charging a card its own list does
    not contain is the divergence the whole table exists to prevent."""
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "opens@test.com")
        account = store.create_billing_account(
            payer.id, "pm_first",
            billing_email="ap@acme.test", billing_company="Acme Ltd",
        )

        assert account.billing_email == "ap@acme.test"
        assert account.billing_company == "Acme Ltd"
        assert account.stripe_payment_method_id == "pm_first"
        assert _cards(store, account.id) == {"pm_first": True}


def test_an_account_can_be_opened_unnamed(app, db_session):
    """Identity is optional and stays optional — an unnamed account renders as the
    payer's own details, which is what every account did before identities existed."""
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "unnamed@test.com")
        account = store.create_billing_account(payer.id, "pm_bare")

        assert account.billing_email is None
        assert account.billing_company is None
        assert _cards(store, account.id) == {"pm_bare": True}


def test_blank_identity_fields_are_stored_as_absent_not_as_empty_text(app, db_session):
    """"" and NULL must not be two ways of saying "unnamed" — one of them would print
    as an empty line on an invoice while the other falls back to the payer."""
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "blank@test.com")
        account = store.create_billing_account(
            payer.id, "pm_blank", billing_email="   ", billing_company="",
        )

        assert account.billing_email is None
        assert account.billing_company is None


def test_a_payer_may_hold_the_same_card_on_two_accounts(app, db_session):
    """What ``uq_payer_billing_group_payer_card`` used to forbid. One company each,
    separate invoices, one card behind both — an ordinary arrangement."""
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "twoaccounts@test.com")
        first = store.create_billing_account(
            payer.id, "pm_shared", billing_company="Acme Ltd"
        )
        second = store.create_billing_account(
            payer.id, "pm_shared", billing_company="Acme Trading Ltd"
        )

        assert first.id != second.id
        assert {a.id for a in store.billing_groups_for_payer(payer.id)} == {
            first.id, second.id
        }


# --- the shelf ------------------------------------------------------------------


def test_adding_a_card_twice_updates_one_row_rather_than_making_two(app, db_session):
    """Find-or-create. Two rows for one card on one account leaves "which of these is
    it" with no answer."""
    pass  # (db handle not needed under Django)
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "dupe@test.com")
        account = store.create_billing_account(payer.id, "pm_one")

        store.add_card_to_group(account.id, "pm_two")
        store.add_card_to_group(account.id, "pm_two")
        pass  # commit: autocommit under Django

        assert _cards(store, account.id) == {"pm_one": True, "pm_two": False}


def test_adding_a_second_card_does_not_change_what_is_charged(app, db_session):
    """"Add a card" is not "switch card". A payer keeping a spare on the account must
    not discover the spare was billed."""
    pass  # (db handle not needed under Django)
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "spare@test.com")
        account = store.create_billing_account(payer.id, "pm_main")

        store.add_card_to_group(account.id, "pm_spare")
        pass  # commit: autocommit under Django

        assert account.stripe_payment_method_id == "pm_main"
        assert _cards(store, account.id) == {"pm_main": True, "pm_spare": False}


# --- the duplicated default -----------------------------------------------------


def test_switching_the_default_moves_both_halves_of_the_pair(app, db_session):
    """THE TEST THIS MODULE EXISTS FOR. ``renewals`` reads the account column and the
    picker reads ``is_default``; a switch that moves one and not the other bills a card
    the payer is not being shown, and nothing raises when it happens."""
    pass  # (db handle not needed under Django)
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "switch@test.com")
        account = store.create_billing_account(payer.id, "pm_old")
        store.add_card_to_group(account.id, "pm_new")
        pass  # commit: autocommit under Django

        store.set_group_default_card(account.id, "pm_new")

        assert store.billing_group(account.id).stripe_payment_method_id == "pm_new"
        assert _cards(store, account.id) == {"pm_old": False, "pm_new": True}


def test_switching_to_an_unknown_card_puts_it_on_the_shelf_first(app, db_session):
    """"Charge this instead" must not leave the account charging something its own
    list does not contain."""
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "unknown@test.com")
        account = store.create_billing_account(payer.id, "pm_old")

        store.set_group_default_card(account.id, "pm_never_seen")

        assert store.billing_group(account.id).stripe_payment_method_id == "pm_never_seen"
        assert _cards(store, account.id) == {"pm_old": False, "pm_never_seen": True}


def test_exactly_one_card_is_ever_the_default(app, db_session):
    """Demote-then-promote, in that order. Postgres holds this with a partial unique
    index; the ordering is what makes the service agree with it on every dialect."""
    pass  # (db handle not needed under Django)
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "onedefault@test.com")
        account = store.create_billing_account(payer.id, "pm_a")
        store.add_card_to_group(account.id, "pm_b")
        store.add_card_to_group(account.id, "pm_c")
        pass  # commit: autocommit under Django

        for card in ("pm_b", "pm_c", "pm_a"):
            store.set_group_default_card(account.id, card)
            defaults = [k for k, v in _cards(store, account.id).items() if v]
            assert defaults == [card], f"expected only {card} to be default"


def test_nominating_an_entity_onto_a_new_card_opens_the_shelf_too(app, db_session):
    """``nominate_card_for_entity`` is the one path that creates an account without
    going through ``create_billing_account``, so it has to open the shelf itself."""
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "nominate@test.com")
        entity = _entity(db_session)

        account = store.nominate_card_for_entity(entity.id, payer.id, "pm_nominated")

        assert _cards(store, account.id) == {"pm_nominated": True}


# --- the identity on the Stripe customer ----------------------------------------


def test_the_named_account_outranks_the_user_record(app, db_session):
    """The reversal. The user row still says who the payer IS; the account says what
    their invoices should SAY, and a finance lead does not want their own name there."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "named@test.com")
        store.create_billing_account(
            payer.id, "pm_x",
            billing_email="invoices@acme.test", billing_company="Acme Ltd",
        )

        assert checkout._payer_identity(payer.id) == {
            "name": "Acme Ltd",
            # NOT the company: description is what tells two payers sharing a name apart
            # in the Stripe dashboard, and two payers may bill for the same company.
            "description": f"@{payer.username}",
            "email": "invoices@acme.test",
        }


def test_an_unnamed_account_leaves_the_user_record_in_charge(app, db_session):
    """An account nobody named must behave exactly as it did before identities."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from billing.services import store

    with app.app_context():
        payer = _user(db_session, "plain@test.com")
        store.create_billing_account(payer.id, "pm_y")

        assert checkout._payer_identity(payer.id) == {
            "name": "Pat Payer",
            "description": f"@{payer.username}",
            "email": "plain@test.com",
        }


def test_the_oldest_named_account_names_the_customer(app, db_session):
    """There is ONE Stripe customer per payer and it holds one name. Taking the most
    recent account would rename the customer every time the payer opened another."""
    checkout = pytest.importorskip("billing.services.checkout")  # slice C
    from datetime import datetime, timedelta

    from billing.services import store

    pass  # (db handle not needed under Django)

    with app.app_context():
        payer = _user(db_session, "oldest@test.com")
        first = store.create_billing_account(
            payer.id, "pm_1", billing_company="First Ltd"
        )
        store.create_billing_account(payer.id, "pm_2", billing_company="Second Ltd")

        # Aged by hand. Both rows take their ``created_at`` from the same server clock
        # and land in the same tick, and the tie then falls to the uuid — stable, but
        # not insertion order, so a test that relied on it would be asserting the
        # tie-break rather than the rule.
        first.created_at = datetime.now(UTC) - timedelta(days=1)
        first.save(update_fields=["created_at"])

        assert checkout._payer_identity(payer.id)["name"] == "First Ltd"


def test_an_unreadable_account_never_breaks_naming_the_customer(app, monkeypatch):
    """A display name is never worth failing a card save for. Asked outside an app
    context — or with the table missing — this falls back to the user record."""
    import shared_models.models as models_db
    checkout = pytest.importorskip("billing.services.checkout")  # slice C

    class _Payer:
        first_name = "Pat"
        last_name = "Payer"
        username = "patpayer"
        email = "pat@example.com"

    class _UserModel:
        # ``store._by_pk`` asks ``Model.objects.filter(pk=...).first()`` (Flask's test faked
        # ``Model.query.get``).
        objects = type(
            "_M", (), {"filter": staticmethod(lambda **kw: type("_Q", (), {"first": staticmethod(lambda: _Payer())})())}
        )()

    monkeypatch.setattr(models_db, "User", _UserModel)

    # No app context here at all, which is exactly what the store call needs.
    assert checkout._payer_identity("u1") == {
        "name": "Pat Payer",
        "description": "@patpayer",
        "email": "pat@example.com",
    }


# --- nominating before a subscription exists ------------------------------------
#
# Onboarding asks for a card at step 2, but the module rows that say who the payer IS are
# not written until finalize, because the trial clock starts at All Set. So the billing
# sheet's Confirm nominates a card for a company that has no payer yet, and used to be
# refused with "That company has no subscription to bill yet." These pin the exception
# that allows it, and its limits.


def test_a_company_with_no_payer_still_refuses_a_nomination_by_default(app, db_session):
    """The 409 is the right answer everywhere except onboarding, and stays the default.

    The in-app billing screens and the payer portal share this code path. For them a
    company with no subscription really is an error, and silently letting the caller
    become its payer would be a different bug than the one being fixed.
    """
    payment_methods = pytest.importorskip("billing.services.payment_methods")  # slice C

    user = _user(db_session, "nobody-pays@example.com")
    entity = _entity(db_session, "Unpaid Ltd")

    with pytest.raises(payment_methods.PaymentMethodError) as caught:
        payment_methods._payer_of(user.id, entity.id)
    assert caught.value.status == 409


def test_onboarding_may_establish_the_payer_the_company_has_none_of(app, db_session):
    """The request IS what makes them the payer, so it cannot require them to be one."""
    payment_methods = pytest.importorskip("billing.services.payment_methods")  # slice C

    user = _user(db_session, "first-payer@example.com")
    entity = _entity(db_session, "Fresh Ltd")

    assert payment_methods._payer_of(
        user.id, entity.id, establish_payer=True
    ) == str(user.id)


def test_establishing_a_payer_is_not_a_way_past_one_that_exists(app, db_session):
    """THE TEST THAT MATTERS. The flag must only answer "nobody", never "somebody else".

    Were it ever read as "skip the check", any member of a company could move another
    payer's billing onto a card of their own choosing — which is the exact attack
    ``_payer_of`` was written to stop.
    """
    from billing.services.constants import PHASE_TRIAL
    payment_methods = pytest.importorskip("billing.services.payment_methods")  # slice C
    from billing.services import store

    payer = _user(db_session, "pays@example.com")
    other = _user(db_session, "does-not-pay@example.com")
    entity = _entity(db_session, "Taken Ltd")
    store.upsert_module_row(entity.id, "PETTY_CASH", payer.id, phase=PHASE_TRIAL)

    with pytest.raises(payment_methods.PaymentMethodError) as caught:
        payment_methods._payer_of(other.id, entity.id, establish_payer=True)
    assert caught.value.status == 404


def test_a_card_can_be_nominated_before_the_subscription_exists(app, db_session, monkeypatch):
    """End to end through ``set_for_entity``, which is what the route actually calls.

    ``_owned`` is stubbed because it reads the card from Stripe; what is under test is the
    payer resolution either side of it, not the ownership proof. The RETURN TRIP is half
    the point: ``set_for_entity`` answers through ``for_entity``, which asks ``_payer_of``
    a second time, so a flag threaded into the write but not the read would raise after
    the nomination had already been written.
    """
    payment_methods = pytest.importorskip("billing.services.payment_methods")  # slice C
    from billing.services import store

    user = _user(db_session, "onboarder@example.com")
    entity = _entity(db_session, "Wizard Ltd")

    monkeypatch.setattr(payment_methods, "_owned", lambda *a, **k: ("cus_x", {}))
    monkeypatch.setattr(
        payment_methods, "list_for_user", lambda _uid: {"methods": [], "default_id": None}
    )

    payload = payment_methods.set_for_entity(
        user.id, entity.id, "pm_wizard", establish_payer=True
    )

    assert payload["nominated_id"] == "pm_wizard"
    assert store.card_for_entity(entity.id, user.id) == "pm_wizard"
    # The account opened for it carries the card on its shelf, like every other account.
    group = store.billing_group_for_entity(entity.id, user.id)
    assert _cards(store, group.id) == {"pm_wizard": True}
