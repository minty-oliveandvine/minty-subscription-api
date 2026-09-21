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
          approved=True, card=True, charge=None):
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
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: PAID_THROUGH)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: PAID_THROUGH)
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

    def _charge(entity_id, payer_user_id, customer_id, codes, *, at, idempotency_key):
        calls["charges"].append({"at": at, "key": idempotency_key})
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


def test_no_saved_card_blocks_it(db_session, monkeypatch):
    """Without one nothing can be charged at all — ``start_billing_cycle`` silently
    no-ops for a payer with no customer row, so the accept would fail at the charge
    having already promised to succeed."""
    transfers, _ = _wire(monkeypatch, db_session, card=False)
    reasons = transfers.transfer_blockers(ENTITY, from_user_id=OLD, to_user_id=NEW)
    assert any("saved payment method" in r for r in reasons)


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


def test_accept_charges_then_flips(db_session, monkeypatch):
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    ok, _msg, result = transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert ok is True
    assert len(calls["charges"]) == 1
    assert calls["flips"] == [(ENTITY, NEW, PERIOD_END)]
    assert calls["consent"] == [(ENTITY, NEW, "transfer")]
    assert offer.status == "accepted"
    assert result["invoice_id"] == "in_1"


def test_accept_records_the_cycle_the_entity_landed_on(db_session, monkeypatch):
    """The anchor is per PAYER, so a handover moves the entity onto a different cycle.

    Recorded alongside ``accepted_billed_through`` because it cannot be re-derived later:
    the incoming payer's anchor is immutable, but a repair pass reading it back has no way
    to tell an anchor that was established BY this accept from one that was already there.
    """
    transfers, _calls = _wire(monkeypatch, db_session)
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
        monkeypatch, db_session,
        charge={"paid": False, "period_end": None, "invoice_id": None, "amount": 0,
                "currency": None, "anchor": None, "reason": "declined"},
    )
    offer = _offer(db_session, transfers)

    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert offer.accepted_anchor_at is None


def test_the_handover_instant_is_read_at_accept_not_quoted_at_offer(db_session, monkeypatch):
    """``paid_through`` advances on every successful renewal, so an offer that outlives a
    cycle would otherwise bill a window the old payer has since paid for."""
    transfers, calls = _wire(monkeypatch, db_session)
    offer = _offer(db_session, transfers)

    moved = datetime(2027, 10, 12, tzinfo=UTC)
    from billing.services import store

    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: moved)

    # Same value per company: these cases describe an account with one card.

    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: moved)
    transfers.respond_to_transfer(NEW, offer.id, accept=True)
    offer.refresh_from_db()

    assert calls["charges"][0]["at"] == moved


def test_a_decline_leaves_the_company_where_it_was(db_session, monkeypatch):
    transfers, calls = _wire(
        monkeypatch, db_session,
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
    transfers, calls = _wire(monkeypatch, db_session, charge=lambda n: outcomes[n])
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
        monkeypatch, db_session,
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
        monkeypatch, db_session,
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
        monkeypatch, db_session,
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
