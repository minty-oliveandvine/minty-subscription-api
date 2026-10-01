"""Handing a subscription to a new payer — the refusals, and the three failure windows.

The accept cannot be one transaction: by the time a card declines the invoice row is
already on disk (reserved before the processor is called). So the guarantee is ORDER —
charge first, flip second — and the offer row is the journal that closes the gap between
the two. (The FLIP itself - payer, consent, nomination, ``accepted`` - IS one transaction
in this port; ``test_the_flip_is_all_or_nothing`` below pins that.)

These run against the real model rather than a mocked session, because the things worth
proving are the journal's state transitions and the partial unique index (declared on the
mirror, so SQLite enforces it too), and a stubbed session proves neither. Only the CHARGE
is mocked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2027, 8, 20, tzinfo=UTC)
PAID_THROUGH = datetime(2027, 9, 12, tzinfo=UTC)
PERIOD_END = datetime(2027, 10, 1, tzinfo=UTC)
# The usual case is a FUTURE window - the outgoing payer has bought days nobody has used -
# and an accept takes no money for those: it parks the instant on ``collect_at`` and the
# daily pass charges on the day. So a test about the CHARGE has to put the window in the
# past, which is the other case: the money ran out before anybody got round to accepting,
# the days are being used now, and they are owed at once.
LAPSED = datetime(2027, 8, 10, tzinfo=UTC)
# The incoming payer's cycle anchor, as the charge reports it back. Distinct from every
# other date here so a test that finds it on the offer row cannot be reading something
# else that happens to match.
ANCHOR = datetime(2027, 9, 1, tzinfo=UTC)

# user.id is a uuid (C1); SQLite refuses a non-hex literal outright.
OLD, NEW = "0a7d0e6e-0000-4000-8000-00000000001d", "0a7d0e6e-0000-4000-8000-00000000002e"
# entities.id is a uuid too (C2), and since C7 every subscription-table FK to it as well
ENTITY = "0a7d0e6e-0000-4000-8000-0000000000e1"
# subscription_transfer.id is uuid as of u1a01_subscription_types.
#
# IT ALSO MUST CONTAIN HEX LETTERS. SQLAlchemy renders a uuid column on SQLite as
# the declared type "UUID", which matches none of SQLite's affinity keywords and
# so gets NUMERIC affinity. A uuid whose hex is all digits - 1111...1111 - is then
# stored as a REAL and comes back as 1.1111111111141117e+31, which blows up on the
# read with a bare AttributeError. Postgres does not care; the test database does.
OFFER = "7a17ffe4-0000-4000-8000-00000000000a"

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


def _user(User, uid, email, approved):
    """A minimally valid ``user`` row — password/first/last are NOT NULL."""
    return User(id=uid, username=email, email=email, password="x",
                first_name="A", last_name="B", approved=approved)


class _Row:
    """An ``entity_module_subscription`` stand-in — only the fields the rules read."""

    def __init__(self, code="PAYMENT_REQUEST", phase="active", ext_state=None, ext_amount=None):
        self.entity_id = ENTITY
        self.function_code = code
        self.phase = phase
        self.payer_user_id = OLD
        self.extension_state = ext_state
        self.extension_amount = ext_amount
        self.billed_through = None


def _trial_row(code="PETTY_CASH", phase="trial", days=14):
    """A module on a free trial, with an end date the handover must carry across."""
    row = _Row(code=code, phase=phase)
    row.trial_end = NOW + timedelta(days=days)
    return row


def _wire(monkeypatch, db, *, rows=None, payer=OLD, dunning=(), admin=True,
          approved=True, card=True, charge=None, paid_through=PAID_THROUGH):
    """Mock what sits either side of the service; return (transfers, calls)."""
    from billing.services import checkout, clock, store, transfers
    from shared_models.models import User, UserEntity

    calls = {"charges": [], "consent": [], "audit": [], "flips": [], "swept": []}

    monkeypatch.setattr(clock, "now", lambda: NOW)
    monkeypatch.setattr(transfers.clock, "now", lambda: NOW)
    monkeypatch.setattr(store, "rows_for_entity",
                        lambda eid: list(rows if rows is not None else [_Row()]))
    monkeypatch.setattr(store, "payer_for_entity", lambda eid: payer)
    monkeypatch.setattr(store, "payer_is_dunning", lambda uid: uid in dunning)
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_new")
    monkeypatch.setattr(
        store, "transfer_entity_payer",
        lambda eid, new, **kw: calls["flips"].append((eid, new, kw.get("billed_through"))),
    )
    monkeypatch.setattr(
        store, "record_billing_consent",
        lambda eid, uid, src: calls["consent"].append((eid, uid, src)),
    )
    monkeypatch.setattr(
        store, "record_action",
        lambda **kw: calls["audit"].append(kw),
    )
    monkeypatch.setattr(
        "billing.services.stripe_client.customer_default_payment_method",
        lambda cid: "pm_1" if card else None,
    )
    monkeypatch.setattr(
        "billing.services.access_sweep.sweep_expired_module_access",
        lambda payer_user_id=None: calls["swept"].append(payer_user_id),
    )

    def _charge(entity_id, payer_user_id, customer_id, codes, *, at, idempotency_key,
                keep_open=False):
        calls["charges"].append({"at": at, "key": idempotency_key, "keep_open": keep_open})
        if callable(charge):
            return charge(len(calls["charges"]))
        return charge or {"paid": True, "period_end": PERIOD_END, "invoice_id": "in_1",
                          "amount": 19000, "currency": "HKD", "anchor": ANCHOR,
                          "reason": None}

    monkeypatch.setattr(checkout, "_bill_transfer_in_house", _charge)

    # A real, approved admin membership for the nominee unless a test says otherwise -
    # on a real company row: user_entity.entity_id is an FK.
    from shared_models.models import Entity

    Entity.objects.get_or_create(id=ENTITY, defaults={"name": "Handover Co", "status": "disconnected"})
    _user(User, NEW, "new@test.com", approved).save(force_insert=True)
    _user(User, OLD, "old@test.com", True).save(force_insert=True)
    if admin:
        UserEntity.objects.create(user_id=NEW, entity_id=ENTITY, role="admin", approved=True)
    if card:
        # The incoming payer has already put this company on one of their billing accounts.
        # The accept no longer falls back to the Stripe customer's default card (2026-10-01:
        # a card is only ever chosen through a billing account), so a test about what the
        # accept DOES needs a nomination in place; the ones about choosing an account pass
        # ``card=False`` and a ``billing_group_id``.
        group = store.create_billing_account(NEW, "pm_1")
        store.nominate_group_for_entity(ENTITY, NEW, group.id, "chosen")
        calls["group"] = group.id
    return transfers, calls


def _offer(db, transfers, *, status="pending", attempt=0, key=None, expires=None):
    from shared_models.models import SubscriptionTransfer

    row = SubscriptionTransfer(
        id=OFFER, entity_id=ENTITY, from_user_id=OLD, to_user_id=NEW,
        status=status, charge_attempt=attempt, charge_key=key,
        expires_at=expires or (NOW + timedelta(days=7)),
        accepted_billed_through=PAID_THROUGH if status != "pending" else None,
    )
    row.save(force_insert=True)
    return row


# --- the refusals ------------------------------------------------------------------


def test_only_the_current_payer_may_hand_the_company_over(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session)
    reasons = transfers.transfer_blockers(ENTITY, from_user_id="someone", to_user_id=NEW)
    assert any("Only the person currently being billed" in r for r in reasons)


def test_an_entity_nobody_pays_for_has_nothing_to_hand_over(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, payer=None)
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("Nobody is being billed" in r for r in reasons)


def test_a_payer_mid_dunning_cannot_hand_the_debt_off(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, dunning={OLD})
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("still being collected on this account" in r for r in reasons)


def test_a_recipient_mid_dunning_cannot_take_one_on(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, dunning={NEW})
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("can't take on" in r for r in reasons)


def test_an_unbilled_cancellation_charge_blocks_it(db_session, monkeypatch):
    """The debt rides the row, so moving the row moves it onto the new payer's invoice
    and makes it uncollectable from whoever actually incurred it."""
    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_Row(ext_state="pending", ext_amount=2746)],
    )
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("Un-cancel it" in r for r in reasons)


def test_a_module_on_trial_no_longer_blocks_it(db_session, monkeypatch):
    """This used to be a refusal, for three reasons that have each since been dealt with:
    the conversion charged the new card on the OLD payer's consent (fixed when consent
    became per-payer), the amount was never disclosed (now quoted on the accept screen),
    and the warning email could never reach them (the trial dedupe key now carries the
    payer). What makes it coherent rather than merely allowed is that the free days
    genuinely travel — ``trial_end`` has one writer and no path moves it."""
    transfers, _ = _wire(monkeypatch, db_session, rows=[_trial_row()])
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert not any("trial" in r.lower() for r in reasons)


def test_a_non_admin_cannot_be_handed_the_bill(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, admin=False)
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("needs to be an admin" in r for r in reasons)


def test_a_deactivated_account_cannot_be_handed_the_bill(db_session, monkeypatch):
    """``_admin_candidates`` checks the membership flags but not ``User.approved``, so a
    deactivated admin is still on the list this validates against."""
    transfers, _ = _wire(monkeypatch, db_session, approved=False)
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("isn't active" in r for r in reasons)


def test_no_saved_card_does_not_block_the_offer(db_session, monkeypatch):
    """Being ASKED is not being charged, so a card-less admin may be offered the company.

    This was a blocker, and it was wrong in three ways: it refused someone for a thing
    they could fix in seconds, it refused them before they had been asked whether they
    even wanted the company, and the offering screen never showed it anyway — the portal
    drops every "That person" reason, so a card-less admin rendered as selectable and the
    POST then refused. The requirement lives at the accept now, which is where the charge
    is (see the two tests below)."""
    transfers, _ = _wire(monkeypatch, db_session, card=False)
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert reasons == []


def test_a_card_less_recipient_can_be_sent_an_offer(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session, card=False)

    ok, _msg, _ = transfers.offer_transfer(OLD, ENTITY, NEW)

    assert ok is True
    assert calls["charges"] == []


def test_accepting_without_a_billing_account_is_refused_and_charges_nothing(
    db_session, monkeypatch
):
    """The requirement did not go away, it moved to the moment it is actually true. No
    account named and none nominated — and the Stripe customer's default card is NOT a
    fallback any more, even though the recipient has one (``_wire`` stubs it as pm_1). The
    accept refuses BEFORE the journal write, leaving the offer pending for them to retry
    once they have chosen an account."""
    transfers, calls = _wire(monkeypatch, db_session, card=False)
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)

    assert ok is False
    assert msg == "Choose a billing account before taking over the billing."
    assert calls["charges"] == []
    assert calls["flips"] == []
    offer.refresh_from_db()
    assert offer.status == "pending"


def test_accepting_with_someone_elses_account_is_refused(db_session, monkeypatch):
    """The account id comes from the browser. One belonging to anyone but the INCOMING
    payer - here the outgoing payer's own - answers exactly like one that does not exist,
    and nothing moves."""
    from billing.services import store
    from shared_models.models import EntityBillingGroup

    transfers, calls = _wire(monkeypatch, db_session, card=False)
    theirs = store.create_billing_account(OLD, "pm_old")
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, billing_group_id=theirs.id
    )

    assert ok is False
    assert msg == "That billing account couldn't be found."
    assert calls["charges"] == [] and calls["flips"] == []
    assert not EntityBillingGroup.objects.filter(entity_id=ENTITY, payer_user_id=NEW).exists()
    offer.refresh_from_db()
    assert offer.status == "pending"


def test_accepting_with_an_unknown_account_is_refused(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session, card=False)
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, billing_group_id="7a17ffe4-0000-4000-8000-0000000000ff"
    )

    assert ok is False and msg == "That billing account couldn't be found."
    assert calls["flips"] == []


def test_accepting_with_your_own_account_nominates_that_exact_account(db_session, monkeypatch):
    """The chosen ACCOUNT, not "an account holding its card": the recipient has two
    accounts charging the same card, and the newer one - the one they picked - is what the
    company lands on (a card-keyed nomination would take the oldest)."""
    from billing.services import store
    from shared_models.models import EntityBillingGroup

    transfers, calls = _wire(monkeypatch, db_session, card=False)
    store.create_billing_account(NEW, "pm_shared")  # older, same card
    chosen = store.create_billing_account(NEW, "pm_shared")
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, billing_group_id=chosen.id
    )

    assert ok is True
    link = EntityBillingGroup.objects.get(entity_id=ENTITY, payer_user_id=NEW)
    assert str(link.billing_group_id) == str(chosen.id)
    assert link.source == "transfer"
    assert calls["flips"], "the handover completed"


def test_a_chosen_account_replaces_an_existing_nomination(db_session, monkeypatch):
    from billing.services import store
    from shared_models.models import EntityBillingGroup

    transfers, calls = _wire(monkeypatch, db_session)  # already nominated onto calls["group"]
    other = store.create_billing_account(NEW, "pm_2")
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, billing_group_id=other.id
    )

    assert ok is True
    link = EntityBillingGroup.objects.get(entity_id=ENTITY, payer_user_id=NEW)
    assert str(link.billing_group_id) == str(other.id)


def test_a_trial_only_handover_still_records_the_chosen_account(db_session, monkeypatch):
    """Nothing is charged for a trial, but the trial converts on the chosen account at term
    end - dropping the choice would leave the company on no card."""
    from billing.services import store
    from shared_models.models import EntityBillingGroup

    transfers, calls = _wire(monkeypatch, db_session, card=False, rows=[_trial_row()])
    chosen = store.create_billing_account(NEW, "pm_trial")
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, billing_group_id=chosen.id
    )

    assert ok is True
    assert calls["charges"] == []
    link = EntityBillingGroup.objects.get(entity_id=ENTITY, payer_user_id=NEW)
    assert str(link.billing_group_id) == str(chosen.id)


def test_a_clean_handover_has_no_blockers(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session)
    assert transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW) == []


# --- the offer ----------------------------------------------------------------------


def test_only_one_offer_may_be_open_per_company(db_session, monkeypatch):
    """The partial unique index is the real guard; the pre-check only makes the message
    better. Both are exercised here."""
    transfers, _ = _wire(monkeypatch, db_session)

    ok, _msg, _ = transfers.offer_transfer(OLD, ENTITY, NEW)
    assert ok is True
    ok2, msg2, _ = transfers.offer_transfer(OLD, ENTITY, NEW)
    assert ok2 is False and "already a handover waiting" in msg2


def test_you_cannot_hand_a_company_to_yourself(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session)
    ok, msg, _ = transfers.offer_transfer(OLD, ENTITY, OLD)
    assert ok is False and "already the one being billed" in msg


# --- accept: the money and the ordering ----------------------------------------------


def test_accept_takes_no_money_and_parks_the_charge(db_session, monkeypatch):
    """The window starts 12 Sept; it is 20 Aug. Nothing is owed for days nobody has used.

    The handover still COMPLETES — the payer flips, consent is recorded, the old payer
    stops being liable — because who owns the company and who has paid for it were never
    the same question."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    ok, _msg, _result = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is True
    assert calls["charges"] == [], "a handover takes no money"
    assert transfers._aware(offer.collect_at) == PAID_THROUGH
    assert offer.status == "accepted"
    assert calls["consent"] == [(ENTITY, NEW, "transfer")]


def test_a_parked_handover_claims_nothing(db_session, monkeypatch):
    """The load-bearing half. ``billed_through`` says "someone's money covers these days";
    until the collection it is nobody's. Written at accept, the company would run free AND
    be marked paid for - ``entities_covered_into`` would suppress the renewal - which is an
    absence with no invoice to notice it by."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert calls["flips"] == [(ENTITY, NEW, None)], "the payer moves, the claim does not"
    assert offer.accepted_billed_through is None


def test_a_lapsed_window_is_charged_at_accept(db_session, monkeypatch):
    """The other case: the old payer's money ran out on 10 Aug and it is 20 Aug, so these
    are days being used right now. There is no future date to defer to."""
    transfers, calls = _wire(monkeypatch, db_session, paid_through=LAPSED)
    offer = _offer(db_session, transfers)

    ok, _msg, result = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is True
    assert len(calls["charges"]) == 1
    assert calls["charges"][0]["at"] == LAPSED
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]
    assert offer.status == "accepted"
    assert offer.collect_at is None, "nothing left to collect"
    assert result["invoice_id"] == "in_1"


def test_accept_records_the_cycle_the_entity_landed_on(db_session, monkeypatch):
    """The anchor is per PAYER, so a handover moves the entity onto a different cycle.

    Recorded alongside ``accepted_billed_through`` because it cannot be re-derived later:
    the incoming payer's anchor is immutable, but a repair pass reading it back has no way
    to tell an anchor that was established BY this accept from one that was already there.
    """
    transfers, _calls = _wire(monkeypatch, db_session, paid_through=LAPSED)
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    # Through ``_aware``, as every production read of these columns is: SQLite hands back
    # a naive value for a ``timezone=True`` column, and the billing layer refuses those.
    assert transfers._aware(offer.accepted_anchor_at) == ANCHOR
    assert transfers._aware(offer.accepted_billed_through) == PERIOD_END, "unchanged"


def test_a_decline_records_no_anchor(db_session, monkeypatch):
    """Nothing was charged, so there is no cycle to claim the entity landed on."""
    transfers, _calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        charge={"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                "currency": None, "anchor": None, "reason": "declined"},
    )
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert offer.accepted_anchor_at is None


def test_the_handover_instant_is_read_at_accept_not_quoted_at_offer(db_session, monkeypatch):
    """``paid_through`` advances on every successful renewal, so an offer that outlives a
    cycle would otherwise bill a window the old payer has since paid for.

    Read at accept even though nothing is charged there - the instant parked on
    ``collect_at`` IS the window's start, and a stale one would collect early."""
    transfers, _calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    moved = datetime(2027, 10, 12, tzinfo=UTC)
    from billing.services import store

    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: moved)

    # Same value per company: these cases describe an account with one card.

    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: moved)
    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert transfers._aware(offer.collect_at) == moved


def test_a_decline_leaves_the_company_where_it_was(db_session, monkeypatch):
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        charge={"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                "currency": None, "anchor": None,
                "reason": "That payment didn't go through."},
    )
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is False and "didn't go through" in msg
    assert calls["flips"] == [], "the pointer must not move"
    assert calls["consent"] == []
    assert offer.status == "pending", "back to pending so it can be retried"
    assert offer.charge_attempt == 1


def test_a_retry_after_a_decline_uses_a_fresh_key(db_session, monkeypatch):
    """THE trap. Voiding an invoice deliberately keeps its row and its key claimed, so a
    key that did not change between attempts would be refused as "already claimed" and
    the customer could never retry after fixing their card."""
    outcomes = {1: {"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                    "currency": None, "anchor": None, "reason": "declined"},
                2: {"paid": True, "period_end": PERIOD_END, "invoice_id": "in_2",
                    "amount": 19000, "currency": "HKD", "anchor": ANCHOR,
                    "reason": None}}
    transfers, calls = _wire(monkeypatch, db_session, paid_through=LAPSED,
                             charge=lambda n: outcomes[n])
    offer = _offer(db_session, transfers)

    assert transfers.respond_to_transfer(NEW, offer.id, accept=True)[0] is False
    offer.refresh_from_db()
    assert transfers.respond_to_transfer(NEW, offer.id, accept=True)[0] is True
    offer.refresh_from_db()

    keys = [c["key"] for c in calls["charges"]]
    assert keys == [f"transfer-{OFFER}-1", f"transfer-{OFFER}-2"], keys
    assert len(set(keys)) == 2, "a retry must not reuse the claimed key"


def test_an_accept_that_died_after_the_charge_is_finished_not_recharged(db_session, monkeypatch):
    """The crash window. The row says ``charged``, so the money is in and only the flip
    is missing — a retried accept must complete it and charge nothing."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers, status="charged", attempt=1,
                   key=f"transfer-{OFFER}-1")

    ok, _msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is True
    assert calls["charges"] == [], "the money was already taken"
    assert calls["flips"] == [(ENTITY, NEW, PAID_THROUGH)]
    assert offer.status == "accepted"


# --- choosing what to take on (07-D) --------------------------------------------------


def _upserts(monkeypatch):
    """Capture ``upsert_module_row``, which is how a declined module is ended."""
    from billing.services import store

    written: list[dict] = []
    monkeypatch.setattr(
        store, "upsert_module_row",
        lambda eid, code, payer, **fields: written.append({"code": code, **fields}),
    )
    return written


def test_taking_the_whole_company_is_the_default(db_session, monkeypatch):
    """No ``codes`` at all - every caller written before the screen offered a choice."""
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        rows=[_Row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST")],
    )
    written = _upserts(monkeypatch)
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)

    assert ok is True
    assert written == [], "nothing declined, so nothing ended"
    assert len(calls["charges"]) == 1


def test_a_declined_module_ends_where_the_money_runs_out(db_session, monkeypatch):
    """The date is the whole design. ``checkout.cancel_module`` would end it at
    ``max(paid_through, now + paid_cancel_access_days)`` - LATER than paid-through - and
    book an extension the OUTGOING payer would owe for days the recipient declined. It
    would also make the accept refuse itself: the pending-extension blocker is
    re-evaluated inside the accept."""
    transfers, _calls = _wire(
        monkeypatch, db_session,
        rows=[_Row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST")],
    )
    written = _upserts(monkeypatch)
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, codes=["PETTY_CASH"]
    )

    assert ok is True
    assert written == [{
        "code": "PAYMENT_REQUEST",
        "phase": "scheduled_cancel",
        "app_access_until": PAID_THROUGH,
    }]
    # NO extension fields at all - not a zero, absent. That is what keeps the blocker
    # quiet and leaves nobody owing anything for the wind-down.
    assert "extension_amount" not in written[0]
    assert "extension_state" not in written[0]


def test_only_the_kept_modules_are_priced(db_session, monkeypatch):
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        rows=[_Row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST")],
    )
    _upserts(monkeypatch)
    offer = _offer(db_session, transfers)

    priced: list[set] = []
    from billing.services import checkout

    monkeypatch.setattr(
        checkout, "_bill_transfer_in_house",
        lambda eid, payer, cid, c, *, at, idempotency_key: (
            priced.append(set(c)) or {"paid": True, "period_end": PERIOD_END,
                                      "invoice_id": "in_1", "amount": 19000,
                                      "currency": "HKD", "anchor": ANCHOR, "reason": None}
        ),
    )
    transfers.respond_to_transfer(NEW, offer.id, accept=True, codes=["PETTY_CASH"])

    assert priced == [{"PETTY_CASH"}]
    assert calls["charges"] == [], "the stub above replaced the wired one"


def test_declining_everything_is_refused(db_session, monkeypatch):
    """Taking on no modules is not a handover. Read generously as "all of them" it would
    hand over a company nobody agreed to take."""
    transfers, calls = _wire(monkeypatch, db_session)
    written = _upserts(monkeypatch)
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True, codes=[])

    assert ok is False
    assert "at least one module" in msg
    assert written == [] and calls["flips"] == [] and calls["charges"] == []


def test_codes_naming_nothing_this_company_has_are_refused(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, codes=["NOT_A_MODULE"]
    )

    assert ok is False
    assert "at least one module" in msg
    assert calls["flips"] == []


def test_a_declined_module_is_not_ended_when_the_accept_fails(db_session, monkeypatch):
    """Ordering. Cancelled first, a declined card would leave the company stripped of a
    module by a handover that never happened - rows changed, payer unmoved, nobody told."""
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        rows=[_Row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST")],
        charge={"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                "currency": None, "anchor": None, "reason": "declined"},
    )
    written = _upserts(monkeypatch)
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, codes=["PETTY_CASH"]
    )

    assert ok is False
    assert written == [], "the declined module still belongs to the company"
    assert calls["flips"] == []


def test_a_declined_trial_goes_through_the_trial_path(db_session, monkeypatch):
    """A trial costs nobody anything, so there is nothing to end early: its free days run
    to ``trial_end`` and it then expires instead of converting. That branch already exists
    in ``checkout.cancel_module``, and ending a trial at the paid-through date instead
    would cut short days that were never anybody's to charge for."""
    transfers, _calls = _wire(
        monkeypatch, db_session,
        rows=[_Row(code="PETTY_CASH"), _trial_row(code="PAYMENT_REQUEST")],
    )
    written = _upserts(monkeypatch)
    cancelled: list[str] = []
    from billing.services import checkout

    monkeypatch.setattr(
        checkout, "cancel_module",
        lambda entity, user, code, **kw: cancelled.append(code),
    )
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(
        NEW, offer.id, accept=True, codes=["PETTY_CASH"]
    )

    assert ok is True
    assert cancelled == ["PAYMENT_REQUEST"]
    assert written == [], "the trial path writes its own row, not this one"


def test_a_row_already_winding_down_is_left_alone(db_session, monkeypatch):
    """Not declining it - it is already going. Writing it again would move its access
    date onto the paid-through and discard an extension somebody may already owe."""
    transfers, _calls = _wire(
        monkeypatch, db_session,
        rows=[_Row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST", phase="scheduled_cancel")],
    )
    written = _upserts(monkeypatch)
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True, codes=["PETTY_CASH"])

    assert written == []


# --- the deferred charge, collected --------------------------------------------------


def _parked(db, transfers, *, collect_at=PAID_THROUGH):
    """An accepted handover whose first charge is parked on ``collect_at``."""
    from shared_models.models import SubscriptionTransfer

    row = SubscriptionTransfer(
        id=OFFER, entity_id=ENTITY, from_user_id=OLD, to_user_id=NEW,
        status="accepted", charge_attempt=0, charge_key=None,
        expires_at=NOW + timedelta(days=7), collect_at=collect_at,
    )
    row.save(force_insert=True)
    return row


def _group(monkeypatch, *, dunning_at=None):
    """The card the incoming payer put the company on, as ``store`` answers for it."""
    from billing.services import store

    class _Group:
        id = "g_new"
        payer_user_id = NEW
        stripe_payment_method_id = "pm_1"
        paid_through = None
        dunning_started_at = dunning_at

    group = _Group()
    monkeypatch.setattr(store, "billing_group_for_entity", lambda eid, uid=None: group)
    return group


def test_the_parked_charge_is_taken_on_the_day_and_not_before(db_session, monkeypatch):
    """THE guard this whole change needs. Deferring the charge must move it, not lose it:
    a window that is never collected is a company running for nothing, and nothing else
    in the engine would notice - its card's ``paid_through`` is NULL, so ``due_renewals``
    skips it outright.

    ``payer=NEW`` throughout this section: the accept already flipped the company, which
    is the state every collection runs against."""
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    offer = _parked(db_session, transfers)

    # The day before. Not due, not touched.
    early = transfers.collect_due(PAID_THROUGH - timedelta(days=1))
    offer.refresh_from_db()
    assert early == {"collected": [], "failed": [], "abandoned": []}
    assert calls["charges"] == []
    assert transfers._aware(offer.collect_at) == PAID_THROUGH

    # The day itself.
    result = transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()

    assert len(result["collected"]) == 1
    assert len(calls["charges"]) == 1
    assert calls["charges"][0]["at"] == PAID_THROUGH, "the window it was parked for"
    assert offer.collect_at is None, "cleared IS the record of settled"
    assert offer.charge_invoice_id == "in_1"


def test_collecting_writes_the_claim_that_the_accept_did_not(db_session, monkeypatch):
    """The claim says "these days are covered". Until the money is in, they are not - so
    it goes on here and at no earlier moment."""
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    _parked(db_session, transfers)

    transfers.collect_due(PAID_THROUGH)

    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]


def test_a_second_pass_does_not_charge_again(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    _parked(db_session, transfers)

    transfers.collect_due(PAID_THROUGH)
    transfers.collect_due(PAID_THROUGH + timedelta(days=1))

    assert len(calls["charges"]) == 1


def test_a_declined_collection_stays_owed_and_retries_with_a_fresh_key(db_session, monkeypatch):
    """The same trap as the accept's retry: the failed attempt's invoice was voided and
    its key stays claimed, so a stable key would jam this forever. ``collect_at`` is NOT
    cleared - a fixed card settles it on the next pass with nobody re-accepting."""
    outcomes = {1: {"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                    "currency": None, "anchor": None, "reason": "declined"},
                2: {"paid": True, "period_end": PERIOD_END, "invoice_id": "in_2",
                    "amount": 19000, "currency": "HKD", "anchor": ANCHOR, "reason": None}}
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=lambda n: outcomes[n])
    _group(monkeypatch)
    offer = _parked(db_session, transfers)

    first = transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()
    assert len(first["failed"]) == 1
    assert transfers._aware(offer.collect_at) == PAID_THROUGH, "still owed"
    assert calls["flips"] == [], "no claim for money that was not taken"

    second = transfers.collect_due(PAID_THROUGH + timedelta(days=1))
    offer.refresh_from_db()
    assert len(second["collected"]) == 1
    assert offer.collect_at is None

    keys = [c["key"] for c in calls["charges"]]
    assert keys == [f"transfer-{OFFER}-1", f"transfer-{OFFER}-2"], keys


def test_a_declined_collection_puts_the_company_past_due(db_session, monkeypatch):
    """A company nobody has paid for is past due, which is what starts the grace window
    and eventually revokes access - exactly as a declined renewal does (the user's call,
    2026-09-30). Its invoice stays open for dunning to chase."""
    from billing.services import store

    transfers, _calls = _wire(
        monkeypatch, db_session, payer=NEW,
        charge={"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                "currency": None, "anchor": None, "reason": "declined"},
    )
    group = _group(monkeypatch)
    started: list = []
    monkeypatch.setattr(store, "begin_group_dunning",
                        lambda gid, at: started.append((gid, at)))
    _parked(db_session, transfers)

    transfers.collect_due(PAID_THROUGH)

    assert started == [(group.id, PAID_THROUGH)]


def test_a_company_handed_on_again_is_not_charged_to_the_wrong_person(db_session, monkeypatch):
    """A handover can be handed on before its charge falls due. The second one reads the
    paid-through as it stands and parks its own window; billing the first recipient for
    days a third party now owns would charge somebody for a company they do not have."""
    transfers, calls = _wire(monkeypatch, db_session, payer="0a7d0e6e-0000-4000-8000-00000000003f")
    _group(monkeypatch)
    offer = _parked(db_session, transfers)

    result = transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()

    assert calls["charges"] == []
    assert len(result["abandoned"]) == 1
    assert offer.collect_at is None, "stop asking - it is not collectable"


def test_nothing_billing_forward_is_not_a_debt(db_session, monkeypatch):
    """Weeks pass between the accept and the collection. Every module cancelled in that
    time leaves a company that owes nothing, which is not a failure to collect."""
    transfers, calls = _wire(
        monkeypatch, db_session, payer=NEW, rows=[_Row(code="PETTY_CASH", phase="expired")],
    )
    _group(monkeypatch)
    offer = _parked(db_session, transfers)

    result = transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()

    assert calls["charges"] == []
    assert len(result["abandoned"]) == 1
    assert offer.collect_at is None


def test_the_module_set_is_re_read_at_collection(db_session, monkeypatch):
    """Priced on what the company holds ON THE DAY, never on the set quoted at accept."""
    transfers, calls = _wire(
        monkeypatch, db_session, payer=NEW,
        rows=[_Row(code="PETTY_CASH", phase="active"), _Row(code="PAYMENT_REQUEST", phase="expired")],
    )
    _group(monkeypatch)
    _parked(db_session, transfers)

    codes: list = []
    from billing.services import checkout

    def _charge(entity_id, payer, customer_id, c, *, at, idempotency_key, keep_open=False):
        codes.append(set(c))
        return {"paid": True, "period_end": PERIOD_END, "invoice_id": "in_1",
                "amount": 19000, "currency": "HKD", "anchor": ANCHOR, "reason": None}

    monkeypatch.setattr(checkout, "_bill_transfer_in_house", _charge)
    transfers.collect_due(PAID_THROUGH)

    assert codes == [{"PETTY_CASH"}], "the expired module is not priced"
    assert calls["charges"] == [], "the stub above replaced the wired one"


def test_the_collection_runs_in_the_daily_pass_before_the_renewal():
    """Ordering, not decoration. The collection establishes the card's ``paid_through``;
    run after the renewal, that card would look like one that has never collected, be
    skipped, and leave the company a day unbilled on every pass."""
    from billing.services import daily

    assert daily.COLLECT_TRANSFERS in daily._RUNNERS
    for order in (daily.JOB_ORDER, daily.LIGHT_ORDER):
        assert daily.COLLECT_TRANSFERS in order
        assert order.index(daily.COLLECT_TRANSFERS) < order.index(daily.RUN_RENEWALS)


# --- telling the payer how it ended (07-I / A-07 / A-08) -----------------------------


def _ended(db, transfers, status, *, seen=None, offer_id=OFFER):
    """A finished offer THIS payer made, seen or not."""
    from shared_models.models import SubscriptionTransfer

    row = SubscriptionTransfer(
        id=offer_id, entity_id=ENTITY, from_user_id=OLD, to_user_id=NEW,
        status=status, expires_at=NOW + timedelta(days=7),
        responded_at=NOW, outcome_seen_at=seen,
    )
    row.save(force_insert=True)
    return row


def test_an_unseen_decline_is_reported_to_the_payer_who_asked(db_session, monkeypatch):
    """The gap this closes: every other read filters on the three OPEN statuses, so a
    declined offer was invisible and the offering screen fell back to the picker exactly as
    though nothing had ever been asked."""
    transfers, _calls = _wire(monkeypatch, db_session)
    _ended(db_session, transfers, "declined")

    out = transfers.unseen_outcomes(OLD)

    assert len(out) == 1
    assert out[0]["id"] == OFFER
    assert out[0]["status"] == "declined"
    assert out[0]["entity_id"] == ENTITY
    # Named, because the modal's title is "<Name> declined the transfer".
    assert out[0]["who"]


def test_a_seen_outcome_is_never_reported_again(db_session, monkeypatch):
    transfers, _calls = _wire(monkeypatch, db_session)
    _ended(db_session, transfers, "declined", seen=NOW)

    assert transfers.unseen_outcomes(OLD) == []


def test_expired_and_accepted_are_reported_too_but_cancelled_is_not(db_session, monkeypatch):
    """A cancellation is the payer's OWN withdrawal, answered by 07-K as they did it.
    Telling somebody their own click happened is not news."""
    transfers, _calls = _wire(monkeypatch, db_session)
    ids = {
        "expired": "7a17ffe4-0000-4000-8000-0000000000b1",
        "accepted": "7a17ffe4-0000-4000-8000-0000000000b2",
        "cancelled": "7a17ffe4-0000-4000-8000-0000000000b3",
        "pending": "7a17ffe4-0000-4000-8000-0000000000b4",
    }
    for status, oid in ids.items():
        _ended(db_session, transfers, status, offer_id=oid)

    reported = {o["status"] for o in transfers.unseen_outcomes(OLD)}

    assert reported == {"expired", "accepted"}


def test_only_the_payer_who_asked_is_told(db_session, monkeypatch):
    """Scoped on ``from_user_id``. The recipient already knows - they answered it."""
    transfers, _calls = _wire(monkeypatch, db_session)
    _ended(db_session, transfers, "declined")

    assert transfers.unseen_outcomes(NEW) == []
    assert transfers.unseen_outcomes(None) == []


def test_done_stamps_it_seen_once_and_is_idempotent(db_session, monkeypatch):
    transfers, _calls = _wire(monkeypatch, db_session)
    offer = _ended(db_session, transfers, "declined")

    ok, _msg = transfers.mark_outcome_seen(OLD, OFFER)
    offer.refresh_from_db()
    first = offer.outcome_seen_at

    assert ok is True
    assert first is not None
    assert transfers.unseen_outcomes(OLD) == []

    # Again: a double-click must not move the stamp or raise.
    ok2, _msg2 = transfers.mark_outcome_seen(OLD, OFFER)
    offer.refresh_from_db()
    assert ok2 is True
    assert offer.outcome_seen_at == first


def test_marking_somebody_elses_handover_seen_is_refused(db_session, monkeypatch):
    transfers, _calls = _wire(monkeypatch, db_session)
    offer = _ended(db_session, transfers, "declined")

    ok, msg = transfers.mark_outcome_seen(NEW, OFFER)
    offer.refresh_from_db()

    assert ok is False
    assert "isn't yours" in msg
    assert offer.outcome_seen_at is None
    # The same answer for one that does not exist, so an id cannot be probed for existence.
    assert transfers.mark_outcome_seen(OLD, "7a17ffe4-0000-4000-8000-00000000dead")[0] is False


def test_the_repair_step_finishes_a_stranded_handover(db_session, monkeypatch):
    """The push half of the same recovery, for when nobody ever clicks again."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers, status="charged", attempt=1,
                   key=f"transfer-{OFFER}-1")

    result = transfers.repair_stranded(NOW)
    offer.refresh_from_db()

    assert result["completed"] == [offer.id]
    assert calls["charges"] == []
    assert calls["flips"] == [(ENTITY, NEW, PAID_THROUGH)]
    assert offer.status == "accepted"


def test_the_repair_step_does_nothing_when_nothing_is_stranded(db_session, monkeypatch):
    """It reads a partial index that is empty in the normal case, so the nightly pass
    costs nothing."""
    transfers, calls = _wire(monkeypatch, db_session)
    _offer(db_session, transfers, status="pending")

    assert transfers.repair_stranded(NOW) == {
        "completed": [], "released": [], "waiting": [], "expired": []
    }
    assert calls["flips"] == []


def test_a_lapsed_request_is_retired_and_the_payer_who_asked_is_told(db_session,
                                                                     monkeypatch):
    """Expiry used to be LAZY: ``respond_to_transfer`` flipped a stale offer only when
    somebody happened to touch it, so a request nobody ever opened sat ``pending`` for
    ever and the person who sent it was never told it had run out.

    The sweep is what gives that moment somewhere to happen.
    """
    transfers, _ = _wire(monkeypatch, db_session)
    _offer(db_session, transfers, status="pending", expires=NOW - timedelta(days=1))
    sent = []
    monkeypatch.setattr(transfers, "_notify",
                        lambda offer, kind: sent.append((offer.id, kind)))

    from shared_models.models import SubscriptionTransfer

    assert transfers.repair_stranded(NOW)["expired"] == [OFFER]
    assert sent == [(OFFER, "expired")]
    assert SubscriptionTransfer.objects.get(pk=OFFER).status == "expired"


def test_a_request_still_inside_its_window_is_left_alone(db_session, monkeypatch):
    """The sweep must not shorten the deadline it is enforcing."""
    transfers, _ = _wire(monkeypatch, db_session)
    _offer(db_session, transfers, status="pending")
    sent = []
    monkeypatch.setattr(transfers, "_notify", lambda offer, kind: sent.append(kind))

    assert transfers.repair_stranded(NOW)["expired"] == []
    assert sent == []


def test_declining_tells_the_payer_who_asked(db_session, monkeypatch):
    """Until this send existed, declining was silent to the sender: the status flipped and
    an audit row was written, so a request that had actually been answered looked exactly
    like one nobody had opened yet."""
    transfers, _ = _wire(monkeypatch, db_session)
    _offer(db_session, transfers, status="pending")
    sent = []
    monkeypatch.setattr(transfers, "_notify",
                        lambda offer, kind: sent.append((offer.id, kind)))

    ok, _message, _ = transfers.respond_to_transfer(NEW, OFFER, accept=False)

    assert ok
    assert sent == [(OFFER, "declined")]


def test_a_charging_row_the_processor_never_saw_is_released(db_session, monkeypatch):
    """Reserved, never confirmed, and the processor has no record — so the key is free
    again and the offer goes back to pending for a clean retry."""
    from billing.services import renewals

    transfers, calls = _wire(monkeypatch, db_session)
    monkeypatch.setattr(renewals, "_already_invoiced", lambda cid, key, **kw: None)
    offer = _offer(db_session, transfers, status="charging", attempt=1,
                   key=f"transfer-{OFFER}-1")

    result = transfers.repair_stranded(NOW)
    offer.refresh_from_db()

    assert result["released"] == [offer.id]
    assert offer.status == "pending"
    assert calls["flips"] == []


def test_an_unpaid_raised_invoice_is_left_alone(db_session, monkeypatch):
    """Forcing it either way here would either bill twice or give the company away."""
    from billing.services import renewals

    transfers, calls = _wire(monkeypatch, db_session)
    monkeypatch.setattr(renewals, "_already_invoiced", lambda cid, key, **kw: "open")
    offer = _offer(db_session, transfers, status="charging", attempt=1,
                   key=f"transfer-{OFFER}-1")

    result = transfers.repair_stranded(NOW)
    offer.refresh_from_db()

    assert result["waiting"] == [offer.id]
    assert offer.status == "charging"
    assert calls["flips"] == []


def test_a_handover_charge_left_a_draft_is_reported_loudly(db_session, monkeypatch, caplog):
    """"The customer can still settle it" is false of a DRAFT: never finalized, it is in no
    list anybody pays from, and nothing retries it. Waiting quietly for ever was the old
    answer; it still waits - forcing it either way would bill twice or give the company
    away - but says so at ERROR, naming the draft, every pass."""
    from billing.services import billing_gateway, renewals, store

    transfers, calls = _wire(monkeypatch, db_session)
    monkeypatch.setattr(renewals, "_already_invoiced", lambda cid, key, **kw: "draft")

    class _Row:
        external_id = "in_handover_draft"

    monkeypatch.setattr(store, "invoice_for_key", lambda key: _Row())
    offer = _offer(db_session, transfers, status="charging", attempt=1,
                   key=f"transfer-{OFFER}-1")

    result = transfers.repair_stranded(NOW)

    assert result["waiting"] == [offer.id]
    stranded = [r.getMessage() for r in caplog.records
                if r.levelname == "ERROR" and "STRANDED DRAFT in_handover_draft" in r.getMessage()]
    assert len(stranded) == 1
    assert billing_gateway.NOT_RETRIED in stranded[0]
    assert f"transfer-{OFFER}-1" in stranded[0]


# --- expiry, decline, cancel ---------------------------------------------------------


def test_an_expired_offer_cannot_be_accepted(db_session, monkeypatch):
    """Checked HERE, not only by a sweep — otherwise "expires in 7 days" quietly means
    "expires whenever something next looks at it"."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers, expires=NOW - timedelta(days=1))

    ok, msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is False and "expired" in msg
    assert offer.status == "expired"
    assert calls["charges"] == []


def test_an_offer_sent_to_someone_else_cannot_be_accepted(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer("interloper", offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is False and "sent to someone else" in msg


def test_declining_closes_the_offer_and_moves_nothing(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=False)
    offer.refresh_from_db()

    assert ok is True and offer.status == "declined"
    assert calls["charges"] == [] and calls["flips"] == []


def test_the_initiator_can_withdraw_a_pending_offer(db_session, monkeypatch):
    """Without this their own exit depends indefinitely on someone else answering."""
    transfers, _ = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    ok, _msg = transfers.cancel_transfer(OLD, offer.id)
    offer.refresh_from_db()

    assert ok is True and offer.status == "cancelled"


def test_a_handover_being_charged_cannot_be_withdrawn(db_session, monkeypatch):
    """Cancelling mid-charge would strand money already being collected."""
    transfers, _ = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers, status="charging", attempt=1)

    ok, msg = transfers.cancel_transfer(OLD, offer.id)
    offer.refresh_from_db()

    assert ok is False and "already being processed" in msg


def test_a_nominee_demoted_since_the_offer_invalidates_it(db_session, monkeypatch):
    """Re-checked at ACCEPT, not only when the list was drawn. Otherwise the pointer
    could be aimed at someone who is no longer a member at all."""
    transfers, calls = _wire(monkeypatch, db_session, admin=False)
    offer = _offer(db_session, transfers)

    ok, msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is False and "needs to be an admin" in msg
    assert offer.status == "cancelled", "permanently invalid, not merely deferred"
    assert calls["charges"] == []


def test_a_temporary_blocker_leaves_the_offer_standing(db_session, monkeypatch):
    """A debt clears on its own; the offer should still be there when it does."""
    transfers, _ = _wire(monkeypatch, db_session, dunning={OLD})
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is False
    assert offer.status == "pending"


# --- the audit trail -------------------------------------------------------------------


def test_the_handover_is_recorded_with_both_parties(db_session, monkeypatch):
    """A transfer is the first action here with two of them, and a row naming one loses
    the only question anyone asks afterwards."""
    transfers, calls = _wire(monkeypatch, db_session,
                             rows=[_Row("PAYMENT_REQUEST"), _Row("PETTY_CASH")])
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    accepted = [a for a in calls["audit"] if a["action"] == "transfer_accepted"]
    assert {a["function_code"] for a in accepted} == {"PAYMENT_REQUEST", "PETTY_CASH"}
    assert all(a["payer_before"] == OLD and a["payer_after"] == NEW for a in accepted)


def test_module_access_is_resynced_after_the_flip(db_session, monkeypatch):
    """Nothing else does it at a handover — dunning re-syncs on recovery, the light pass
    only for payers it touched."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert calls["swept"] == [NEW]


# --- an entity with nothing left to pay for ------------------------------------
#
# An expired trial, or a cancellation that has run its course, leaves a ROW behind — and
# that row still names a payer. So every other refusal passes and the offer looks fine,
# while ``_billable_codes`` comes back empty and there is nothing to charge for.
#
# Refused at OFFER, not at accept. Left to the accept the request goes out, the email
# lands, and the recipient is the one told it cannot happen — for a reason that was
# already true when it was sent.


def test_an_entity_whose_only_module_expired_cannot_be_handed_over(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, rows=[_Row(phase="expired")])

    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)

    assert any("nothing active on this company" in r for r in reasons)


def test_an_entity_winding_down_cannot_be_handed_over(db_session, monkeypatch):
    """``scheduled_cancel`` is not billing forward either: it may still have access, but
    nothing renews, so the incoming payer would be taking on nothing."""
    transfers, _ = _wire(monkeypatch, db_session, rows=[_Row(phase="scheduled_cancel")])

    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)

    assert any("nothing active on this company" in r for r in reasons)


def test_the_offer_is_refused_rather_than_the_accept(db_session, monkeypatch):
    """The whole point of catching it early — no request, and therefore no email."""
    transfers, calls = _wire(monkeypatch, db_session, rows=[_Row(phase="expired")])

    ok, msg, _ = transfers.offer_transfer(OLD, ENTITY, NEW)

    assert ok is False
    assert "nothing active on this company" in msg
    assert calls["charges"] == []


def test_a_dead_row_beside_a_live_one_does_not_block_it(db_session, monkeypatch):
    """The common shape: one module expired its trial, another is paid and running. The
    live one is what makes the entity transferable, and the dead one must not veto it."""
    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_Row(code="PETTY_CASH", phase="expired"), _Row(code="PAYMENT_REQUEST", phase="active")],
    )

    assert transfers.transfer_blockers(
        ENTITY, from_user_id=OLD, to_user_id=NEW
    ) == []


def test_only_the_live_module_is_charged_for(db_session, monkeypatch):
    """The expired row moves with the entity but is not priced — you do not bill someone
    for a module nobody holds."""
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        rows=[_Row(code="PETTY_CASH", phase="expired"), _Row(code="PAYMENT_REQUEST", phase="active")],
    )
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    # _billable_codes is what the charge is priced on, and it is active-only.
    assert calls["charges"], "the handover should have gone through"
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]


def test_the_dead_row_still_moves_to_the_new_payer(db_session, monkeypatch):
    """It is a row about the ENTITY, so it follows the entity. That also carries the spent
    trial with it: ``start_module_trial`` refuses any module whose row already exists, so
    the incoming payer inherits "this module has had its free trial" rather than getting a
    fresh one. The trial belongs to the company, not to whoever is paying."""
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        rows=[_Row(code="PETTY_CASH", phase="expired"), _Row(code="PAYMENT_REQUEST", phase="active")],
    )
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    # One UPDATE over the entity — every row of it, whatever its phase.
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]
    # And the audit records both modules, dead one included.
    accepted = [a for a in calls["audit"] if a["action"] == "transfer_accepted"]
    assert {a["function_code"] for a in accepted} == {"PETTY_CASH", "PAYMENT_REQUEST"}


# --- the claim must not land on a row that was never billed -----------------------
#
# ``billed_through`` means "someone else's money already covers these days". That is only
# true of a row that was being billed. A trial was free.
#
# Stamping a trial row LOOKS inert, because the renewal filter skips anything not billing
# forward — right up until the trial converts, ``_finish_conversion`` writes phase=active,
# and the claim silently goes live. The new payer's renewals for that entity are then
# suppressed for every period it covers. Free months, and unfixable: the column is outside
# ``_MODULE_MUTABLE_FIELDS``, so no ordinary writer can clear it.
#
# These run the REAL ``transfer_entity_payer`` against the database, because the thing
# being tested is the WHERE clause on its second UPDATE — a mocked store proves nothing.


# The real-row tests below write genuine subscription rows, whose entity_id / payer_user_id
# are uuid FKs to ``entities`` / ``user`` since C7 - so the parties must exist, as uuids.
REAL_ENTITY = ENTITY


def _real_rows(db, specs):
    """Insert the company, both payers and genuine entity_module_subscription rows; return
    the model."""
    from shared_models.models import Entity, EntityModuleSubscription, User

    Entity.objects.get_or_create(id=REAL_ENTITY, defaults={"name": "Handover Co", "status": "disconnected"})
    for uid, email in ((OLD, "old@test.com"), (NEW, "new@test.com")):
        if not User.objects.filter(pk=uid).exists():
            _user(User, uid, email, True).save(force_insert=True)
    for code, phase in specs:
        # No explicit id: the model's own default supplies a valid uuid. Nothing here
        # reads it - ``_claims`` keys by function_code.
        EntityModuleSubscription.objects.create(
            entity_id=REAL_ENTITY, function_code=code,
            payer_user_id=OLD, phase=phase,
        )
    return EntityModuleSubscription


def _claims(model):
    # SQLite hands back naive datetimes for a timezone=True column, so the read is
    # normalised here rather than in every assertion. Production re-attaches UTC the same
    # way in ``transfers._aware`` — for a sharper reason there: the billing layer REFUSES
    # a naive datetime, and that refusal would land after the money had moved.
    def aware(value):
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

    return {
        r.function_code: (r.payer_user_id, aware(r.billed_through))
        for r in model.objects.filter(entity_id=REAL_ENTITY)
    }


def test_the_claim_skips_a_trial_row_but_the_payer_does_not(db_session, monkeypatch):
    from billing.services import store

    model = _real_rows(db_session, [("PAYMENT_REQUEST", "active"), ("PETTY_CASH", "trial")])

    store.transfer_entity_payer(REAL_ENTITY, NEW, billed_through=PERIOD_END)

    claims = _claims(model)
    assert claims["PAYMENT_REQUEST"] == (NEW, PERIOD_END), "a billed row carries the claim"
    assert claims["PETTY_CASH"][0] == NEW, "every row follows the entity to the new payer"
    assert claims["PETTY_CASH"][1] is None, (
        "a trial was free — a claim here becomes free months once it converts"
    )


@pytest.mark.parametrize("unbilled_phase", ["expired", "scheduled_cancel"])
def test_the_claim_skips_expired_and_cancelled_rows_too(db_session, monkeypatch, unbilled_phase):
    """Same rule, same reason: nothing was being billed, so nothing is covered.

    One unbilled phase per run: ``function_code`` is the closed ``module_code`` enum (two
    modules), so a third fictional module cannot stand in for the second phase any more.
    """
    from billing.services import store

    model = _real_rows(db_session, [
        ("PAYMENT_REQUEST", "active"),
        ("PETTY_CASH", unbilled_phase),
    ])

    store.transfer_entity_payer(REAL_ENTITY, NEW, billed_through=PERIOD_END)

    claims = _claims(model)
    assert claims["PAYMENT_REQUEST"][1] == PERIOD_END
    assert claims["PETTY_CASH"][1] is None
    assert all(payer == NEW for payer, _ in claims.values())


def test_a_past_due_row_does_carry_the_claim(db_session, monkeypatch):
    """``past_due`` IS billing forward — the subscription has not ended and the money is
    still owed — so its days really were covered and the claim belongs on it."""
    from billing.services import store

    model = _real_rows(db_session, [("PAYMENT_REQUEST", "past_due")])

    store.transfer_entity_payer(REAL_ENTITY, NEW, billed_through=PERIOD_END)

    assert _claims(model)["PAYMENT_REQUEST"] == (NEW, PERIOD_END)


def test_a_converted_trial_is_billable_rather_than_suppressed(db_session, monkeypatch):
    """The end of the story the first test guards. Transfer while on trial, let it
    convert, and the entity must appear on the new payer's renewal — not be skipped as
    though somebody had already paid for it."""
    from billing.services import renewals, store
    from billing.services.billing import Period

    model = _real_rows(db_session, [("PETTY_CASH", "trial")])
    store.transfer_entity_payer(ENTITY, NEW, billed_through=PERIOD_END)

    # The conversion: phase goes active, and nothing clears a claim.
    model.objects.filter(entity_id=ENTITY).update(phase="active")

    rows = list(model.objects.filter(entity_id=ENTITY))
    monkeypatch.setattr(store, "module_rows_for_payer", lambda uid: rows)

    period = Period(
        datetime(2027, 9, 1, tzinfo=UTC), datetime(2027, 10, 1, tzinfo=UTC)
    )
    assert renewals.entities_covered_into(NEW, period) == set(), (
        "a converted trial carries no claim, so nothing suppresses its renewal"
    )


# --- handing over an entity that is on trial ---------------------------------------
#
# The free days travel with the row: `trial_end` has one writer and no path moves it, so
# the incoming payer inherits the remaining term and the trial converts on THEIR card at
# its original date. It stays once-per-entity, so they inherit a spent trial rather than
# minting a fresh one.
#
# Nothing is charged at accept for the trial itself — those days are free. What they are
# charged for is any module that was actually being billed.


def test_a_trial_no_longer_blocks_a_handover(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, rows=[_trial_row()])

    assert transfers.transfer_blockers(
        ENTITY, from_user_id=OLD, to_user_id=NEW
    ) == []


def test_a_trial_only_entity_is_handed_over_without_a_charge(db_session, monkeypatch):
    """Nothing has been paid for, so there is no window to buy. The handover is the flip."""
    transfers, calls = _wire(monkeypatch, db_session, rows=[_trial_row()])
    offer = _offer(db_session, transfers)

    ok, _msg, _ = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is True
    assert calls["charges"] == [], "free days are not billable"
    assert calls["flips"], "the payer still moves"
    assert calls["consent"] == [(ENTITY, NEW, "transfer")], (
        "their consent is what authorises the conversion later"
    )
    assert offer.status == "accepted"


def test_a_charge_free_handover_raises_no_invoice_and_claims_no_key(db_session, monkeypatch):
    """It skips the journal entirely, which is safe only because no money moves — the
    journal exists to close the gap between taking money and moving the pointer."""
    transfers, _ = _wire(monkeypatch, db_session, rows=[_trial_row()])
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert offer.charge_attempt == 0
    assert offer.charge_key is None
    assert offer.charge_invoice_id is None


def test_a_mixed_entity_charges_only_for_the_paid_module(db_session, monkeypatch):
    transfers, calls = _wire(
        monkeypatch, db_session, paid_through=LAPSED,
        rows=[_trial_row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST", phase="active")],
    )
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert len(calls["charges"]) == 1
    # Both rows move; only the billed one was priced.
    accepted = [a for a in calls["audit"] if a["action"] == "transfer_accepted"]
    assert {a["function_code"] for a in accepted} == {"PETTY_CASH", "PAYMENT_REQUEST"}


def test_an_entity_with_nothing_at_all_is_still_refused(db_session, monkeypatch):
    """The narrowed rule has to keep catching the case it was written for — a genuinely
    dead entity, where the dead rows still name a payer and every other check passes."""
    transfers, _ = _wire(monkeypatch, db_session, rows=[_Row(phase="expired")])

    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)

    assert any("nothing active on this company" in r for r in reasons)


def test_an_expired_trial_is_not_mistaken_for_a_running_one(db_session, monkeypatch):
    """``_trial_rows`` keys on the PHASE, not on the presence of a ``trial_end`` — an
    expired row keeps its date as history, and reading that as a live trial would make
    every dead entity look transferable."""
    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_trial_row(phase="expired")],
    )

    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)

    assert any("nothing active on this company" in r for r in reasons)


# --- disclosing the inherited trial ------------------------------------------------
#
# Accepting now commits someone to a charge weeks away, so the screen has to name it.
# The grouping is the part worth pinning: pricing is per module SET, so modules ending on
# the same day are one bundled charge and must be quoted together. Quoting them
# separately would disclose amounts that sum to more than the customer is charged.


def _priced(monkeypatch, by_codes):
    """Stand in for the real pricer, recording the code sets it was asked about."""
    from billing.services import checkout

    asked = []

    def _quote(entity_id, payer_user_id, codes, *, at, before=None):
        asked.append((frozenset(codes), at))
        return {"amount": by_codes[frozenset(codes)], "currency": "HKD",
                "anchor_is_new": False}

    monkeypatch.setattr(checkout, "quote_transfer_charge", _quote)
    return asked


def _named(monkeypatch, names):
    """Stand in for the catalog. ``billing_plan_for_codes`` is what turns a module SET
    into the product name the customer sees on their invoice."""
    from types import SimpleNamespace

    from billing.services import store

    def _plan(codes):
        display = names if isinstance(names, str) else names.get(frozenset(codes))
        return SimpleNamespace(display_name=display) if display else None

    monkeypatch.setattr(store, "billing_plan_for_codes", _plan)


def test_two_trials_ending_together_are_quoted_as_one_bundle(db_session, monkeypatch):
    """The bundle IS the discount. Pricing each module alone and adding them up overstates
    what the customer will actually pay — the same class of error as quoting the wrong
    window, and on the same screen."""
    end = NOW + timedelta(days=14)
    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_trial_row(code="PAYMENT_REQUEST"), _trial_row(code="PETTY_CASH")],
    )
    asked = _priced(monkeypatch, {frozenset({"PAYMENT_REQUEST", "PETTY_CASH"}): 30097})
    _named(monkeypatch, "Super Minty")

    disclosure = transfers.trial_disclosure(ENTITY, NEW)

    assert len(disclosure) == 1, "one conversion date, one charge, one line"
    assert disclosure[0]["codes"] == ["PAYMENT_REQUEST", "PETTY_CASH"]
    assert disclosure[0]["amount"] == 30097
    # NAMED by the set too, not just priced by it — the bundle is a product, and
    # "Super Minty" is what the customer will see on the invoice.
    assert disclosure[0]["label"] == "Super Minty"
    assert asked == [(frozenset({"PAYMENT_REQUEST", "PETTY_CASH"}), end)], (
        "priced once, as a set, at the conversion date"
    )


def test_trials_ending_on_different_days_are_separate_charges(db_session, monkeypatch):
    """They convert on different days, so they really are two charges — and collapsing
    them into one line would misstate both the amount and the date."""
    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_trial_row(code="PAYMENT_REQUEST", days=7), _trial_row(code="PETTY_CASH", days=21)],
    )
    _priced(monkeypatch, {frozenset({"PAYMENT_REQUEST"}): 28000,
                          frozenset({"PETTY_CASH"}): 28000})
    _named(monkeypatch, {frozenset({"PAYMENT_REQUEST"}): "Payment Request",
                         frozenset({"PETTY_CASH"}): "Petty Cash"})

    disclosure = transfers.trial_disclosure(ENTITY, NEW)

    assert [d["codes"] for d in disclosure] == [["PAYMENT_REQUEST"], ["PETTY_CASH"]]
    assert [d["label"] for d in disclosure] == ["Payment Request", "Petty Cash"]
    assert [d["trial_end"] for d in disclosure] == sorted(
        d["trial_end"] for d in disclosure
    ), "soonest first — it is the one that needs attention"


def test_an_entity_with_no_trials_discloses_nothing(db_session, monkeypatch):
    transfers, _ = _wire(monkeypatch, db_session, rows=[_Row(phase="active")])
    assert transfers.trial_disclosure(ENTITY, NEW) == []


def test_a_trial_that_cannot_be_priced_is_still_disclosed(db_session, monkeypatch):
    """Being told a module is on trial with the amount missing beats being told nothing
    about it at all."""
    from billing.services import checkout

    transfers, _ = _wire(monkeypatch, db_session, rows=[_trial_row()])

    def _boom(*_a, **_k):
        raise RuntimeError("no plan prices that")

    monkeypatch.setattr(checkout, "quote_transfer_charge", _boom)

    disclosure = transfers.trial_disclosure(ENTITY, NEW)

    assert len(disclosure) == 1
    assert disclosure[0]["codes"] == ["PETTY_CASH"]
    assert disclosure[0]["amount"] is None


def test_an_unpriceable_set_still_gets_a_readable_name(db_session, monkeypatch):
    """No catalog row for the combination, so no plan name — but the customer must still
    be told which module is on trial. Falls back to the phrase the emails already use,
    rather than showing them a raw code like ``PETTY_CASH``."""
    from billing.services import store

    transfers, _ = _wire(monkeypatch, db_session, rows=[_trial_row(code="PETTY_CASH")])
    monkeypatch.setattr(store, "billing_plan_for_codes", lambda codes: None)
    _priced(monkeypatch, {frozenset({"PETTY_CASH"}): 28000})

    disclosure = transfers.trial_disclosure(ENTITY, NEW)

    assert disclosure[0]["label"] == "Petty Cash"


def test_the_cancelling_refusal_offers_the_remedy_rather_than_a_wait(db_session, monkeypatch):
    """Un-cancelling while the extension is still pending DELETES it — nobody has been
    billed, so ``_reactivate_module_in_house`` just clears the number. Telling someone to
    wait for the next invoice sent them away for up to a month to reach the same place one
    click would."""
    transfers, _ = _wire(
        monkeypatch, db_session, rows=[_Row(ext_state="pending", ext_amount=2746)],
    )

    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)

    assert "Un-cancel it" in reasons[0]
    assert "next invoice" not in reasons[0]


def test_only_the_most_blocking_reason_reaches_the_screen(db_session, monkeypatch):
    """An entity can trip several at once. Four problems in a stack is a wall to triage,
    not an instruction — and fixing the wrong one first is often impossible anyway."""
    from billing.services import portal

    transfers, _ = _wire(
        monkeypatch, db_session,
        # Cancelling AND nothing billing forward: two reasons, one actionable.
        rows=[_Row(phase="scheduled_cancel", ext_state="pending", ext_amount=2746)],
    )
    all_reasons = transfers.transfer_blockers(
        ENTITY, from_user_id=OLD, to_user_id=NEW
    )
    assert len(all_reasons) > 1, "the service still knows about all of them"

    # The read model starts from a real entity row - the one ``_wire`` seeds.

    monkeypatch.setattr(portal, "_admin_candidates",
                        lambda eid: [{"id": NEW, "name": "N", "email": "n@t.com"}])
    payload = portal.build_subscriber_options(OLD, ENTITY)

    assert payload is not None
    assert len(payload["blockers"]) == 1, "the screen shows one"
    assert payload["blockers"][0] == all_reasons[0], "and it is the most blocking one"


def test_the_screen_is_told_what_the_company_is_paid_through(db_session, monkeypatch):
    """The footer says "paid up until ..." - and that is a fact about the ENTITY, not about
    whoever might take it over.

    It used to be read off a candidate's quote, which exists only when there IS a candidate:
    a company whose payer is its own only admin produced none, and the sentence silently
    disappeared from the screen that is meant to explain what the payer is still on the hook
    for. So the read answers it directly, with no candidates at all."""
    from billing.services import portal

    _wire(monkeypatch, db_session, rows=[_Row(phase="active")])
    monkeypatch.setattr(portal, "_admin_candidates", lambda eid: [])

    payload = portal.build_subscriber_options(OLD, ENTITY)

    assert payload is not None
    assert [c["is_current"] for c in payload["candidates"]] == [True], "only the payer"
    assert payload["paid_through"] == PAID_THROUGH, "and still the date the footer needs"


def test_a_trial_beside_an_active_module_is_priced_as_an_upgrade(db_session, monkeypatch):
    """The conversion does NOT price a trial as a fresh join. By the time it converts the
    payer is already being billed for whatever was active, so the trial costs its margin
    inside the resulting bundle.

    Found on real data: a trial beside one active module quoted 219.41 as a join against
    94.03 as the upgrade — a promise of more than double the real charge, on the screen
    where somebody decides whether to take the company on.
    """
    from billing.services import checkout

    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_Row(code="PAYMENT_REQUEST", phase="active"), _trial_row(code="PETTY_CASH")],
    )
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: {"PAYMENT_REQUEST"})
    asked = _priced(monkeypatch, {frozenset({"PETTY_CASH"}): 9403})
    _named(monkeypatch, "Petty Cash")

    disclosure = transfers.trial_disclosure(ENTITY, NEW)

    assert disclosure[0]["amount"] == 9403
    # Only the converting module is priced; the active one is the baseline, not a line.
    assert [codes for codes, _at in asked] == [frozenset({"PETTY_CASH"})]


def test_the_before_set_reaches_the_pricer(db_session, monkeypatch):
    """Guards the actual regression: dropping ``before`` silently reverts to join pricing,
    and the figure just gets bigger with no error anywhere."""
    from billing.services import checkout

    seen = {}

    def _quote(entity_id, payer_user_id, codes, *, at, before=None):
        seen["before"] = set(before or set())
        return {"amount": 9403, "currency": "HKD", "anchor_is_new": False}

    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_Row(code="PAYMENT_REQUEST", phase="active"), _trial_row(code="PETTY_CASH")],
    )
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: {"PAYMENT_REQUEST"})
    monkeypatch.setattr(checkout, "quote_transfer_charge", _quote)
    _named(monkeypatch, "Petty Cash")

    transfers.trial_disclosure(ENTITY, NEW)

    assert seen["before"] == {"PAYMENT_REQUEST"}, "the already-billed module must be priced against"


def test_a_later_trial_counts_the_earlier_one_as_already_billed(db_session, monkeypatch):
    """Two trials ending on different days convert in sequence. By the time the second
    lands the first is active and being paid for, so it belongs in the second's baseline —
    otherwise the second is quoted as though it were joining alone."""
    from billing.services import checkout

    befores = []

    def _quote(entity_id, payer_user_id, codes, *, at, before=None):
        befores.append(set(before or set()))
        return {"amount": 1000, "currency": "HKD", "anchor_is_new": False}

    transfers, _ = _wire(
        monkeypatch, db_session,
        rows=[_trial_row(code="PAYMENT_REQUEST", days=7),
              _trial_row(code="PETTY_CASH", days=21)],
    )
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: set())
    monkeypatch.setattr(checkout, "quote_transfer_charge", _quote)
    _named(monkeypatch, "X")

    transfers.trial_disclosure(ENTITY, NEW)

    assert befores == [set(), {"PAYMENT_REQUEST"}], (
        "the earlier conversion is part of what the later one upgrades from"
    )


# --- the flip is one transaction ---------------------------------------------------------


def test_the_flip_is_all_or_nothing(db_session, monkeypatch):
    """``_complete`` moves the payer, records the consent, clears the old nomination and
    marks the offer ``accepted`` inside ONE ``transaction.atomic()``. Flask could only order
    those four commits; here a failure in the last of them leaves none of the others."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers, status="charged", attempt=1, key="transfer-k-1")
    from billing.services import store
    from shared_models.models import SubscriptionTransfer

    def boom(*args, **kwargs):
        raise RuntimeError("consent table is unhappy")

    monkeypatch.setattr(store, "record_billing_consent", boom)

    with pytest.raises(RuntimeError):
        transfers._complete(offer, actor_user_id=NEW, now=NOW)

    assert calls["flips"] == [(ENTITY, NEW, PAID_THROUGH)], "the flip was attempted"
    assert SubscriptionTransfer.objects.get(pk=OFFER).status == "charged", (
        "the offer is NOT accepted: the flip's own writes were rolled back with it"
    )
    assert calls["audit"] == [] and calls["swept"] == [], "nothing after the block ran"


# --- the parked window: paid for, and not renewed ---------------------------------------------
#
# Against the REAL store: ``_wire`` stubs ``paid_through_for_entity``, which is the rule these
# pin. The company is the new payer's, on a card of theirs; the handover's first charge is
# parked on ``collect_at``, where the old payer's money runs out.


def _parked_on_real_store(*, card_paid_through=None):
    import uuid

    from billing.services import store
    from billing.tests.engine.conftest import make_entity, make_user, seed_currency
    from shared_models.models import SubscriptionTransfer

    old = make_user(f"old-{uuid.uuid4().hex[:6]}@payer.test")
    new = make_user(f"new-{uuid.uuid4().hex[:6]}@payer.test")
    entity = make_entity(new, name="Parked Co", currency=seed_currency("HKD"))
    group = store.create_billing_account(new.id, "pm_new")
    store.nominate_group_for_entity(entity.id, new.id, group.id)
    store.upsert_module_row(entity.id, "PAYMENT_REQUEST", new.id, phase="active")
    if card_paid_through is not None:
        store.set_group_paid_through(group.id, card_paid_through)
    SubscriptionTransfer.objects.create(
        entity_id=entity.id, from_user_id=old.id, to_user_id=new.id, status="accepted",
        expires_at=NOW + timedelta(days=7), collect_at=PAID_THROUGH,
    )
    return new, entity, group


def test_a_parked_handover_is_paid_through_where_the_old_payers_money_ends(db_session):
    """On a card that has never collected, the company read as paid through NOTHING - no
    access at all - and was switched off the moment the handover was accepted."""
    from billing.services import access, store

    _new, entity, group = _parked_on_real_store()

    assert store.paid_through_for_entity(entity.id) == PAID_THROUGH
    assert store.billing_group(group.id).paid_through is None, "the card's own date is untouched"
    assert access.grants_access(NOW, phase="active",
                                period_end=store.paid_through_for_entity(entity.id))


def test_a_card_paid_further_than_the_parked_window_answers_for_itself(db_session):
    from billing.services import store

    later = PAID_THROUGH + timedelta(days=20)
    _new, entity, _group_ = _parked_on_real_store(card_paid_through=later)

    assert store.paid_through_for_entity(entity.id) == later


def test_a_parked_company_is_left_off_the_new_payers_renewal_until_collected(db_session):
    """A renewal of the new payer's card before the charge date billed the company for the old
    payer's days, and the collection then billed the window again."""
    from billing.services import store
    from billing.services.billing import Period

    new, entity, _group_ = _parked_on_real_store()
    before = Period(ANCHOR, PERIOD_END)                       # 1 Sept - 1 Oct: reaches 12 Sept
    after = Period(PERIOD_END, PERIOD_END + timedelta(days=31))

    assert store.entities_awaiting_handover(new.id, before) == {str(entity.id)}
    assert store.entities_awaiting_handover(new.id, after) == set(), (
        "a period after the parked window starts is billed as normal"
    )



# --- a failed deferred charge is chased like a renewal decline (the user's call, 2026-09-30) ----
#
# It used to void its invoice and try again every hour under a fresh key: a declining card was
# hit hourly with no end, the new payer was told nothing, and dunning - finding nothing open -
# thanked them for a payment nobody made, every day or two.

DECLINED_CHARGE = {"paid": False, "period_end": None, "invoice_id": "in_1", "amount": 0,
                   "currency": None, "anchor": None, "reason": "Your card was declined.",
                   "declined": True, "transient": False}


def _mail(monkeypatch):
    from billing.services import notify

    sent: list = []
    monkeypatch.setattr(notify, "notify",
                        lambda uid, event, *, dedupe_key, context=None:
                        sent.append((uid, event, dedupe_key)) or True)
    return sent


def _last_attempt(monkeypatch, *, status, tried):
    """The last attempt's invoice, as the store and the processor answer for it."""
    from types import SimpleNamespace

    from billing.services import billing_gateway, store

    row = SimpleNamespace(id="row_1", external_id="in_1", status=status,
                          idempotency_key=f"transfer-{OFFER}-1", total=19000, currency="hkd",
                          period_start=PAID_THROUGH, period_end=PERIOD_END)
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda key: row if key == f"transfer-{OFFER}-1" else None)
    answer = {"id": "in_1", "status": status, "attempted": tried,
              "payment_intent": {"id": "pi_1", "last_payment_error": (
                  {"type": "card_error"} if tried else None)}}
    monkeypatch.setattr(billing_gateway, "recheck", lambda record: answer)
    return row


def test_a_declined_deferred_charge_stays_open_and_starts_dunning_with_one_notice(
    db_session, monkeypatch
):
    from billing.services import notify, store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=DECLINED_CHARGE)
    group = _group(monkeypatch)
    started: list = []
    monkeypatch.setattr(store, "begin_group_dunning", lambda gid, at: started.append(gid))
    sent = _mail(monkeypatch)
    offer = _parked(db_session, transfers)

    result = transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()

    assert calls["charges"][0]["keep_open"] is True        # nothing withdrawn
    assert started == [group.id]
    assert sent == [(NEW, notify.RENEWAL_FAILED, f"transfer-{OFFER}-1")]
    assert result["failed"][0]["invoice"] == "in_1"
    assert transfers._aware(offer.collect_at) == PAID_THROUGH, "still owed"


def test_a_declined_deferred_charge_is_not_charged_again_the_next_hour(db_session, monkeypatch):
    """Dunning chases it now - daily, with a give-up. Hourly re-charging is over."""
    from billing.services import store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=DECLINED_CHARGE)
    _group(monkeypatch, dunning_at=PAID_THROUGH)
    monkeypatch.setattr(store, "begin_group_dunning", lambda gid, at: None)
    _mail(monkeypatch)
    _parked(db_session, transfers)
    transfers.collect_due(PAID_THROUGH)
    _last_attempt(monkeypatch, status="open", tried=True)

    later = transfers.collect_due(PAID_THROUGH + timedelta(hours=1))

    assert len(calls["charges"]) == 1
    assert later["failed"][0]["reason"] == "declined; dunning is chasing it"


def test_a_charge_found_paid_since_is_settled_not_charged_again(db_session, monkeypatch):
    """Paid through dunning, a Pay now or the hosted page: the collection settles it."""
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=DECLINED_CHARGE)
    _group(monkeypatch)
    _mail(monkeypatch)
    offer = _parked(db_session, transfers)
    offer.charge_attempt, offer.charge_key = 1, f"transfer-{OFFER}-1"
    offer.save(update_fields=["charge_attempt", "charge_key"])
    _last_attempt(monkeypatch, status="paid", tried=True)

    result = transfers.collect_due(PAID_THROUGH + timedelta(days=2))
    offer.refresh_from_db()

    assert calls["charges"] == []
    assert len(result["collected"]) == 1
    assert offer.collect_at is None and offer.charge_invoice_id == "in_1"
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]


def test_a_processor_outage_keeps_the_charge_owed_and_tells_nobody(db_session, monkeypatch):
    from billing.services import store

    outage = {**DECLINED_CHARGE, "declined": False, "transient": True, "invoice_id": None}
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=outage)
    group = _group(monkeypatch)
    held: list = []
    monkeypatch.setattr(store, "hold_group_grace", lambda gid, at: held.append(gid))
    monkeypatch.setattr(store, "begin_group_dunning",
                        lambda gid, at: pytest.fail("dunning started for an outage"))
    sent = _mail(monkeypatch)
    offer = _parked(db_session, transfers)

    transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()

    assert held == [group.id] and sent == []
    assert transfers._aware(offer.collect_at) == PAID_THROUGH


def test_a_card_gone_since_the_accept_is_told_not_left_silent(db_session, monkeypatch):
    """It started dunning with nothing to collect - and dunning, finding nothing open, called
    it settled. Now the payer is told, and the charge is taken once there is a card again."""
    from billing.services import notify, store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    monkeypatch.setattr(store, "billing_group_for_entity", lambda eid, uid=None: None)
    sent = _mail(monkeypatch)
    offer = _parked(db_session, transfers)

    transfers.collect_due(PAID_THROUGH)
    offer.refresh_from_db()

    assert calls["charges"] == []
    assert [event for _u, event, _k in sent] == [notify.RENEWAL_FAILED]
    assert transfers._aware(offer.collect_at) == PAID_THROUGH


def test_a_charge_unpaid_past_its_grace_is_abandoned(db_session, monkeypatch):
    """As long as a declined renewal is chased, and no longer."""
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    offer = _parked(db_session, transfers)

    result = transfers.collect_due(PAID_THROUGH + timedelta(days=16))
    offer.refresh_from_db()

    assert calls["charges"] == [] and len(result["abandoned"]) == 1
    assert offer.collect_at is None


def test_a_handover_charge_dunning_collected_is_settled(db_session, monkeypatch):
    """Dunning paid the handover's invoice: the claim, the offer and the audit, as the
    collection would have written them."""
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    offer = _parked(db_session, transfers)
    offer.charge_attempt, offer.charge_key = 1, f"transfer-{OFFER}-1"
    offer.save(update_fields=["charge_attempt", "charge_key"])
    _last_attempt(monkeypatch, status="paid", tried=True)

    transfers.settle_paid_handover(
        {"id": "in_1", "metadata": {"transfer_key": f"transfer-{OFFER}-1"}})
    offer.refresh_from_db()

    assert offer.collect_at is None and offer.charge_invoice_id == "in_1"
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]


def test_settling_ignores_an_invoice_that_is_no_handovers(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)

    transfers.settle_paid_handover({"id": "in_r", "metadata": {"renewal_key": "renewal-x"}})

    assert calls["flips"] == []


def test_a_due_handover_charge_is_a_debt_dunning_can_see(db_session):
    """With nothing open at the processor, the parked offer is the only record anything is
    owed - and dunning, blind to it, called the debt settled."""
    from billing.services import store

    new, entity, group = _parked_on_real_store()

    assert store.handover_owed(group, PAID_THROUGH + timedelta(hours=1))
    assert not store.handover_owed(group, PAID_THROUGH - timedelta(days=1))    # not due yet


# --- the review's findings (2026-09-30) ------------------------------------------------------------


def _row_of(key, *, external_id, status):
    from types import SimpleNamespace

    return SimpleNamespace(id="row_1", external_id=external_id, status=status,
                           idempotency_key=key, total=19000, currency="hkd",
                           period_start=PAID_THROUGH, period_end=PERIOD_END)


def _with_last_attempt(db_session, transfers):
    offer = _parked(db_session, transfers)
    offer.charge_attempt, offer.charge_key = 1, f"transfer-{OFFER}-1"
    offer.save(update_fields=["charge_attempt", "charge_key"])
    return offer


def test_a_last_attempt_found_by_its_metadata_is_charged_not_declined(db_session, monkeypatch):
    """Its create answered too late to be recorded: the row had no id, Stripe had a draft
    finalized since. The store records what it finds on a FRESH copy of the row - acted on
    through the old one, the read asked Stripe for invoice None and the refusal read as a
    decline: dunning started and the payer was told their payment failed."""
    from billing.services import billing_gateway, store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=DECLINED_CHARGE)
    _group(monkeypatch)
    sent = _mail(monkeypatch)
    _with_last_attempt(db_session, transfers)
    key = f"transfer-{OFFER}-1"
    reads = iter([_row_of(key, external_id=None, status="draft")])
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda k: next(reads, None) or _row_of(k, external_id="in_1",
                                                                status="open"))
    monkeypatch.setattr(billing_gateway, "find_invoice_by_metadata",
                        lambda *a, **k: {"id": "in_1", "status": "open"})
    monkeypatch.setattr(billing_gateway, "record_found_invoice",
                        lambda record, found: found.get("status"))

    def _recheck(record):
        assert record.external_id == "in_1", "acted on the stale row"
        return {"id": "in_1", "status": "open", "attempted": False,
                "payment_intent": {"id": "pi_1", "last_payment_error": None}}

    monkeypatch.setattr(billing_gateway, "recheck", _recheck)
    monkeypatch.setattr(billing_gateway, "resume_invoice",
                        lambda record, payment_method=None, where="renewal":
                        {"id": record.external_id, "status": "paid"})

    result = transfers.collect_due(PAID_THROUGH + timedelta(hours=1))

    assert len(result["collected"]) == 1
    assert sent == [] and calls["charges"] == []


def test_an_abandoned_handover_withdraws_the_invoice_it_left_open(db_session, monkeypatch):
    """Past its grace, unpaid. Left open, dunning or a Retry payment could still collect it -
    for a handover that is over, and that nothing would then grant anything for."""
    from billing.services import billing_gateway, checkout, store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    offer = _with_last_attempt(db_session, transfers)
    key = f"transfer-{OFFER}-1"
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda k: _row_of(k, external_id="in_1", status="open"))
    monkeypatch.setattr(billing_gateway, "recheck",
                        lambda record: {"id": "in_1", "status": "open", "attempted": True})
    voided: list = []
    monkeypatch.setattr(checkout, "_void_unpaid_invoice",
                        lambda inv, eid, what: voided.append(inv))

    result = transfers.collect_due(PAID_THROUGH + timedelta(days=16))
    offer.refresh_from_db()

    assert len(result["abandoned"]) == 1 and offer.collect_at is None
    assert voided == ["in_1"]
    assert key == offer.charge_key


def test_a_charge_paid_at_the_last_moment_is_settled_not_abandoned(db_session, monkeypatch):
    from billing.services import billing_gateway, checkout, store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW)
    _group(monkeypatch)
    offer = _with_last_attempt(db_session, transfers)
    monkeypatch.setattr(store, "invoice_for_key",
                        lambda k: _row_of(k, external_id="in_1", status="open"))
    monkeypatch.setattr(billing_gateway, "recheck",
                        lambda record: {"id": "in_1", "status": "paid"})
    monkeypatch.setattr(checkout, "_void_unpaid_invoice",
                        lambda *a: pytest.fail("withdrew a paid charge"))

    result = transfers.collect_due(PAID_THROUGH + timedelta(days=16))
    offer.refresh_from_db()

    assert len(result["collected"]) == 1 and result["abandoned"] == []
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]


def test_a_card_whose_collection_is_over_is_not_put_back_into_dunning(db_session, monkeypatch):
    """Its episode was closed on purpose; starting one each pass only to close it again told
    nobody anything."""
    from billing.services import store

    transfers, calls = _wire(monkeypatch, db_session, payer=NEW, charge=DECLINED_CHARGE)
    group = _group(monkeypatch)
    group.paid_through = PAID_THROUGH - timedelta(days=40)           # its access ran out
    started: list = []
    monkeypatch.setattr(store, "begin_group_dunning", lambda gid, at: started.append(gid))
    _mail(monkeypatch)
    _parked(db_session, transfers)

    transfers.collect_due(PAID_THROUGH)

    assert started == []
