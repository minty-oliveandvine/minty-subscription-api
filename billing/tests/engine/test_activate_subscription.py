"""A TRIAL HAS NO SUBSCRIBER UNTIL BILLING IS CONFIRMED (the user's rule, 2026-10-08).

The rule, and the two halves that enforce it:

* ``store.establish_entity_payer`` - one UPDATE with ``payer_user_id IS NULL`` in the WHERE, so
  it establishes a first subscriber and can never move or clear an existing one, and two admins
  racing cannot open two billing relationships;
* ``checkout.activate_entity_billing`` - the one in-app act that creates the relationship:
  payer, account and consent together or not at all, charging nothing.

``test_one_payer_per_entity`` covers the OTHER invariant (a second module joins the entity's
payer rather than opening a second one). This file is about the state before there is one.
"""

from __future__ import annotations

import pytest

from .conftest import make_entity, make_user, seed_country, seed_currency, seed_module, seed_plan

pytestmark = pytest.mark.django_db

MODULE = "PETTY_CASH"


@pytest.fixture
def shop():
    """An admin and a company with one module and a sellable plan."""
    currency = seed_currency("HKD")
    country = seed_country(currency)
    owner = make_user("owner@test.com")
    entity = make_entity(owner, currency=currency, country=country, modules=(MODULE,))
    seed_module(MODULE, "Petty Cash")
    seed_plan(MODULE, 28000, display_name="Petty Cash")
    return owner, entity


def _trial(entity, codes=(MODULE,)):
    """Module rows in trial with NO payer, written the way ``start_module_trial`` writes them."""
    from billing.services import store

    return [store.upsert_module_row(entity.id, code, phase="trial") for code in codes]


def _db_row(entity, code=MODULE):
    from shared_models.models import EntityModuleSubscription

    return EntityModuleSubscription.objects.get(entity_id=str(entity.id), function_code=code)


def _db_user(user_id):
    """The live User row. ``make_user`` answers a snapshot; the services want the model."""
    from shared_models.models import User

    return User.objects.get(id=str(user_id))


# --- the store primitive --------------------------------------------------------------------


def test_a_trial_row_is_written_with_no_payer(shop):
    owner, entity = shop
    _trial(entity)

    from billing.services import store

    assert _db_row(entity).payer_user_id is None
    assert store.payer_for_entity(entity.id) is None
    # And so ANY admin may act, which is the whole point of not establishing one.
    assert store.may_manage_subscription(entity.id, owner.id) is True
    assert store.may_manage_subscription(entity.id, make_user("other@test.com").id) is True


def test_establishing_stamps_every_row_of_the_entity(shop):
    owner, entity = shop
    seed_module("PAYMENT_REQUEST", "Payment Request")
    _trial(entity, (MODULE, "PAYMENT_REQUEST"))

    from billing.services import store

    assert store.establish_entity_payer(entity.id, owner.id) == str(owner.id)

    assert str(_db_row(entity).payer_user_id) == str(owner.id)
    assert str(_db_row(entity, "PAYMENT_REQUEST").payer_user_id) == str(owner.id)
    assert str(store.payer_for_entity(entity.id)) == str(owner.id)


def test_establishing_again_is_the_same_answer_and_changes_nothing(shop):
    """The idempotent re-press: a double click, or a second tab."""
    owner, entity = shop
    _trial(entity)

    from billing.services import store

    first = store.establish_entity_payer(entity.id, owner.id)
    second = store.establish_entity_payer(entity.id, owner.id)

    assert first == second == str(owner.id)


def test_the_loser_of_a_race_is_told_who_won_and_stamps_nothing(shop):
    """Two admins pressing Activate at the same moment. ``payer_user_id IS NULL`` in the WHERE
    is a compare-and-set, so the second matches no rows - and must NOT be told it succeeded."""
    owner, entity = shop
    rival = make_user("rival@test.com")
    _trial(entity)

    from billing.services import store

    store.establish_entity_payer(entity.id, owner.id)
    answer = store.establish_entity_payer(entity.id, rival.id)

    assert answer == str(owner.id), "the loser must be handed the winner, not its own id"
    assert str(_db_row(entity).payer_user_id) == str(owner.id)


def test_establishing_never_moves_an_existing_payer(shop):
    """Moving one is ``transfer_entity_payer``, a different act with a different name."""
    owner, entity = shop
    rival = make_user("rival@test.com")
    _trial(entity)

    from billing.services import store

    store.establish_entity_payer(entity.id, owner.id)
    store.establish_entity_payer(entity.id, rival.id)

    assert str(store.payer_for_entity(entity.id)) == str(owner.id)


def test_establishing_refuses_without_an_entity_or_a_payer(shop):
    _owner, entity = shop
    from billing.services import store

    with pytest.raises(ValueError):
        store.establish_entity_payer(entity.id, None)
    with pytest.raises(ValueError):
        store.establish_entity_payer(None, "u1")


def test_the_payer_is_read_from_a_row_that_has_one_not_the_first_row(shop):
    """A company with one module activated and another trialled afterwards holds BOTH shapes.
    An unordered ``.first()`` would let the subscriber-less row answer, and every money read
    downstream would then say "nobody" for a company that is being billed."""
    owner, entity = shop
    seed_module("PAYMENT_REQUEST", "Payment Request")
    _trial(entity)

    from billing.services import store
    from shared_models.models import EntityModuleSubscription

    store.establish_entity_payer(entity.id, owner.id)
    # A second module row with no payer, written straight so no code path can stamp it.
    EntityModuleSubscription.objects.create(
        id=store._uuid(),
        entity_id=str(entity.id),
        function_code="PAYMENT_REQUEST",
        payer_user_id=None,
        phase="trial",
    )

    assert str(store.payer_for_entity(entity.id)) == str(owner.id)


def test_a_module_row_without_a_payer_makes_nobody_a_payer(shop):
    """``module_rows_for_payer`` filters by equality, so a NULL row belongs to no payer -
    which is why Manage Subscriptions has to ask for them separately."""
    owner, entity = shop
    _trial(entity)

    from billing.services import store

    assert store.module_rows_for_payer(owner.id) == []
    assert [str(r.entity_id) for r in store.module_rows_without_payer([entity.id])] == [
        str(entity.id)
    ]


def test_manage_subscriptions_lists_a_company_nobody_pays_for_to_its_admin(shop):
    """Otherwise a company you just trialled VANISHES from the one screen that answers "what
    am I running?": the list is built from ``module_rows_for_payer``, an equality filter no
    NULL row matches. It is listed with no subscriber, and the row offers Activate."""
    owner, entity = shop
    _trial(entity)

    from billing.services import portal

    body = portal.build_payer_subscriptions(owner.id)

    (item,) = body["entities"]
    assert item["entity_id"] == str(entity.id)
    assert item["has_subscriber"] is False
    assert item["subscriber"] is None


def test_the_list_does_not_leak_a_company_the_viewer_is_no_admin_of(shop):
    """Scoped twice: the viewer's own approved memberships bound the query, then
    MODULE_MANAGE is asked per company. A cashier is not offered its bill."""
    owner, entity = shop
    _trial(entity)
    cashier = make_user("cashier@test.com")

    from billing.services import portal
    from shared_models.models import UserEntity

    UserEntity.objects.create(
        user_id=cashier.id, entity_id=entity.id, role="cashier", approved=True
    )
    stranger = make_user("stranger@test.com")

    assert portal.build_payer_subscriptions(cashier.id)["entities"] == []
    assert portal.build_payer_subscriptions(stranger.id)["entities"] == []
    assert len(portal.build_payer_subscriptions(owner.id)["entities"]) == 1


def test_once_activated_the_company_is_listed_as_the_payers_own(shop):
    owner, entity = shop
    _trial(entity)

    from billing.services import portal, store

    store.establish_entity_payer(entity.id, owner.id)
    (item,) = portal.build_payer_subscriptions(owner.id)["entities"]

    assert item["has_subscriber"] is True
    assert item["subscriber"]["id"] == str(owner.id)


# --- the wizard's confirm, which happens before there is anything to stamp -------------------


def _confirmed_in_the_wizard(owner, entity):
    """What step 2's billing sheet writes: a nominated account and this company's consent,
    while ``entity_module_subscription`` is still empty for it."""
    from billing.services import store

    store.upsert_customer_mapping(owner.id, "cus_test")
    store.nominate_card_for_entity(entity.id, owner.id, "pm_card", source="capture")
    store.record_billing_consent(entity.id, owner.id, "confirmed")


def test_finalize_stamps_the_person_who_confirmed_billing_in_the_wizard(shop):
    """The billing sheet is step 2 and the rows are created at finalize, so the act that
    establishes the subscriber happens BEFORE there is anything to write it on. Without the
    late stamp a company that DID add a card would hold one nobody could see - every money read
    resolves the payer from these rows - and its trial would expire having been told it would
    convert."""
    owner, entity = shop
    _confirmed_in_the_wizard(owner, entity)

    from billing.services import checkout, store

    checkout.start_trials_for_enabled_modules(entity, _db_user(owner.id))

    assert str(store.payer_for_entity(entity.id)) == str(owner.id)


def test_finalize_stamps_nobody_when_the_billing_sheet_was_skipped(shop):
    """The whole point: a company that skipped the sheet goes live with trials nobody is
    liable for, and any of its admins may confirm later."""
    owner, entity = shop

    from billing.services import checkout, store

    created = checkout.start_trials_for_enabled_modules(entity, _db_user(owner.id))

    assert created, "the trials still start"
    assert store.payer_for_entity(entity.id) is None


def test_a_company_merely_placed_on_an_account_has_not_confirmed(shop):
    """BOTH halves are required. ``billing/accounts/move`` writes no consent, so a placed
    company is not a confirmed one and finalize must not stamp anybody for it."""
    owner, entity = shop

    from billing.services import checkout, store

    store.upsert_customer_mapping(owner.id, "cus_test")
    store.nominate_card_for_entity(entity.id, owner.id, "pm_card", source="chosen")

    assert store.confirmed_payer_for_entity(entity.id) is None
    checkout.start_trials_for_enabled_modules(entity, _db_user(owner.id))
    assert store.payer_for_entity(entity.id) is None


def test_finalize_credits_whoever_confirmed_not_whoever_finished(shop):
    """``_entity_for_member`` only checks MEMBERSHIP, so another member can finish the wizard.
    Keying the stamp on the acting user would then leave the authoriser's nomination orphaned -
    the old step-2/step-9 mismatch. It is asked by entity instead."""
    owner, entity = shop
    finisher = make_user("finisher@test.com")
    _confirmed_in_the_wizard(owner, entity)

    from billing.services import checkout, store
    from shared_models.models import UserEntity

    UserEntity.objects.create(
        user_id=finisher.id, entity_id=entity.id, role="admin", approved=True
    )

    checkout.start_trials_for_enabled_modules(entity, _db_user(finisher.id))

    assert str(store.payer_for_entity(entity.id)) == str(owner.id)


# --- the service act -----------------------------------------------------------------------


def _account_with_a_card(owner, entity):
    """The payer's customer, a billing account on a card, and this company placed on it."""
    from billing.services import store

    store.upsert_customer_mapping(owner.id, "cus_test")
    store.nominate_card_for_entity(entity.id, owner.id, "pm_card", source="chosen")


def test_activating_writes_the_payer_the_account_and_the_consent(shop):
    owner, entity = shop
    _trial(entity)
    _account_with_a_card(owner, entity)

    from billing.services import checkout, store

    answer = checkout.activate_entity_billing(entity, _db_user(owner.id))

    assert answer == {"ok": True, "payer_user_id": str(owner.id)}
    assert str(store.payer_for_entity(entity.id)) == str(owner.id)
    assert store.has_billing_consent(entity.id, owner.id) is True
    assert store.billing_group_for_entity(entity.id, owner.id) is not None


def test_activating_a_company_on_no_account_leaves_no_payer_behind(shop):
    """THE PICKER'S LOOP depends on this. The account cannot be read before a payer exists
    (a nomination is keyed on the pair), so the stamp happens first and the 402 rolls it
    back - a refusal leaves the company exactly as subscriber-less as it was, and pressing
    again with a different account is safe."""
    owner, entity = shop
    _trial(entity)

    from billing.services import checkout, store

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.activate_entity_billing(entity, _db_user(owner.id))

    assert exc.value.status == 402
    assert exc.value.message == checkout.NO_ACCOUNT_FOR_COMPANY
    assert store.payer_for_entity(entity.id) is None, "the stamp must have rolled back"
    assert store.has_billing_consent(entity.id, owner.id) is False


def test_activating_a_company_with_no_subscription_is_refused(shop):
    """Nothing to activate: no trial has been started, so there are no rows to stamp."""
    owner, entity = shop
    _account_with_a_card(owner, entity)

    from billing.services import checkout, store

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.activate_entity_billing(entity, _db_user(owner.id))

    assert exc.value.status == 409


def test_activating_a_company_somebody_else_just_took_is_refused(shop):
    """Never a silent success: both would walk away believing they are being charged."""
    owner, entity = shop
    rival = make_user("rival@test.com")
    _trial(entity)
    _account_with_a_card(rival, entity)

    from billing.services import checkout, store

    store.establish_entity_payer(entity.id, owner.id)

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.activate_entity_billing(entity, _db_user(rival.id))

    assert exc.value.status == 409
    assert exc.value.message == checkout.SOMEONE_ELSE_ACTIVATED
    assert str(store.payer_for_entity(entity.id)) == str(owner.id)
