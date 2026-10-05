"""Unit tests for the renewal runner.

This is the code that takes money on Minty's own arithmetic, so the cases below are the
ways it could take the wrong amount, take it twice, or fail to take it at all.

Its first live exercise charged a real payer twice for catch-up periods — a global sweep
driven by a test clock belonging to a different customer. ``scope`` and
``test_billing_everyone_has_to_be_asked_for_explicitly`` exist because of that.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
PAID_THROUGH = datetime(2027, 2, 8, 13, tzinfo=UTC)
NOW = datetime(2027, 2, 9, 13, tzinfo=UTC)
# The processor's answer for an invoice whose charge was TRIED and refused.
DECLINED = {"id": "in_old", "status": "open", "attempted": True,
            "payment_intent": {"id": "pi_1", "last_payment_error": {
                "type": "card_error", "code": "card_declined"}}}


class _Account:
    def __init__(self, user_id="u1", paid_through=PAID_THROUGH):
        self.user_id = user_id
        self.anchor_at = ANCHOR
        self.paid_through = paid_through
        self.stripe_customer_id = f"cus_{user_id}"


class _Row:
    def __init__(self, entity_id="e1", code="PAYMENT_REQUEST", phase="active",
                 billed_through=None):
        self.entity_id = entity_id
        self.function_code = code
        self.phase = phase
        self.payer_user_id = "u1"
        # Money already collected for this row from outside the payer's own cycle —
        # a subscriber transfer, invoiced at accept. None on every ordinary row.
        self.billed_through = billed_through


class _Group:
    """A ``payer_billing_group``: one card, and the cycle that card owns.

    ``entities`` None means "every company this payer has", which is the shape of every
    account before a second card is nominated — and therefore the shape the tests written
    before per-entity cards still describe.
    """

    def __init__(self, id="g1", payer_user_id="u1", card="pm_1",
                 paid_through=PAID_THROUGH, entities=None):
        self.id = id
        self.payer_user_id = payer_user_id
        self.stripe_payment_method_id = card
        self.paid_through = paid_through
        self.dunning_started_at = None
        self.dunning_attempts = 0
        self.entities = entities


class _Plan:
    def __init__(self, name="Super Minty", amount=40000, currency="HKD"):
        self.display_name = name
        self.amount = amount
        self.currency = currency


class _Record:
    """A ``subscription_invoice`` row: the local record of an invoice already raised.

    ``external_id`` is what makes it authoritative. NULL means the key was claimed and
    nothing came back, so the row cannot say whether the processor has the invoice.
    """

    def __init__(self, id="inv_local", external_id="in_old", status="paid", total=40000):
        self.id = id
        self.external_id = external_id
        self.status = status
        self.total = total


def _wire(monkeypatch, *, accounts=None, rows=None, plan=_Plan(), issued=None,
          existing=None, found=None, extensions=None, groups=None, at_processor=None,
          dunning_since=None, notice_sent=True):
    """Mock the store and the gateway; return (renewals, calls).

    ``existing`` is the LOCAL row for this period's key (the guard). ``found`` is what
    the processor returns if the runner ever has to fall back to scanning it.

    ``groups`` are the cards. Left out, each account gets ONE holding everything it has,
    which is what the backfill produces and what every account looked like before a
    second card could be nominated — so the cases below keep describing the same money.
    ``calls["paid_through"]`` and ``calls["dunning"]`` still record the PAYER, so those
    assertions read the same either way; the group is recorded alongside for the cases
    that are about containment.
    """
    from billing.services import billing_gateway, renewals, store

    accounts = accounts if accounts is not None else [_Account()]
    rows = rows if rows is not None else [_Row()]
    if groups is None:
        groups = [
            _Group(id=f"g_{a.user_id}", payer_user_id=a.user_id,
                   card=f"pm_{a.user_id}", paid_through=a.paid_through)
            for a in accounts
        ]
    for group in groups:
        # A card whose dunning is running: the customer has been told of a decline.
        group.dunning_started_at = dunning_since or group.dunning_started_at

    def _group(group_id):
        return next((g for g in groups if str(g.id) == str(group_id)), None)

    def _entities_in(group_id):
        group = _group(group_id)
        if group is None:
            return set()
        if group.entities is not None:
            return {str(e) for e in group.entities}
        # Everything the row mock hands back, exactly as ``module_rows_for_payer`` does
        # here: it answers the same rows for any payer, and a group that filtered them by
        # payer would leave a second account with no companies and nothing to bill.
        return {str(r.entity_id) for r in rows}

    def _group_for_entity(entity_id, payer_user_id=None):
        for group in groups:
            if payer_user_id and str(group.payer_user_id) != str(payer_user_id):
                continue
            if str(entity_id) in _entities_in(group.id):
                return group
        return None

    monkeypatch.setattr(store, "accounts_with_billing", lambda: accounts)
    monkeypatch.setattr(store, "groups_with_billing", lambda: list(groups))
    monkeypatch.setattr(
        store, "billing_groups_for_payer",
        lambda uid: [g for g in groups if str(g.payer_user_id) == str(uid)],
    )
    monkeypatch.setattr(store, "billing_group", _group)
    monkeypatch.setattr(store, "entity_ids_in_group", _entities_in)
    monkeypatch.setattr(store, "billing_group_for_entity", _group_for_entity)
    monkeypatch.setattr(store, "module_rows_for_payer", lambda uid: rows)
    monkeypatch.setattr(store, "billing_plan_for_codes", lambda codes: plan)
    # Handovers whose first charge is parked; none unless a test says otherwise.
    monkeypatch.setattr(store, "entities_awaiting_handover", lambda uid, period: set())
    # Cancel-extensions owed but not yet collected; none unless a test says otherwise.
    monkeypatch.setattr(
        store, "pending_extensions_for_payer", lambda uid: extensions or []
    )
    monkeypatch.setattr(
        store, "billing_cycle_for_user", lambda uid: (ANCHOR, "HKD")
    )
    monkeypatch.setattr(
        renewals, "_entity_names", lambda ids: {str(i): f"Entity {i}" for i in ids}
    )

    calls = {"paid_through": [], "dunning": [], "issued": [], "lookups": [],
             "marked": [], "keys": [], "discarded": [], "settled": [],
             "group_paid_through": [], "group_dunning": []}
    monkeypatch.setattr(
        store, "invoice_for_key",
        lambda key: calls["keys"].append(key) or existing,
    )
    monkeypatch.setattr(
        store, "discard_invoice", lambda rid: calls["discarded"].append(rid)
    )
    monkeypatch.setattr(
        store, "settle_invoice",
        lambda rid, **kw: calls["settled"].append((rid, kw)),
    )
    monkeypatch.setattr(
        store, "mark_extensions_invoiced",
        lambda ids: calls["marked"].append(list(ids)) or len(list(ids)),
    )

    def _group_paid_through(group_id, until):
        group = _group(group_id)
        calls["group_paid_through"].append((str(group_id), until))
        calls["paid_through"].append((group.payer_user_id if group else None, until))

    def _group_dunning(group_id, when):
        group = _group(group_id)
        calls["group_dunning"].append((str(group_id), when))
        calls["dunning"].append((group.payer_user_id if group else None, when))

    monkeypatch.setattr(store, "set_group_paid_through", _group_paid_through)
    monkeypatch.setattr(store, "begin_group_dunning", _group_dunning)
    calls["held"], calls["released"], calls["rechecked"] = [], [], []
    monkeypatch.setattr(store, "hold_group_grace",
                        lambda gid, at: calls["held"].append((str(gid), at)) or 1)
    monkeypatch.setattr(store, "release_group_grace",
                        lambda gid, at: calls["released"].append((str(gid), at)) or 0)

    # What the processor holds for a row the runner re-reads before acting on it
    # (``billing_gateway.recheck``): by default exactly what the row says, never charged.
    def _recheck(record):
        calls["rechecked"].append(record)
        answer = at_processor if at_processor is not None else {
            "id": record.external_id, "status": record.status, "attempted": False,
            "payment_intent": {"id": "pi_1", "last_payment_error": None},
        }
        record.status = answer.get("status")          # recorded, as the gateway records it
        return answer

    monkeypatch.setattr(billing_gateway, "recheck", _recheck)
    from billing.services import notify, policy

    monkeypatch.setattr(policy, "current", lambda: policy.DEFAULTS)
    monkeypatch.setattr(notify, "already_sent", lambda event, key: notice_sent)
    monkeypatch.setattr(
        billing_gateway, "find_invoice_by_metadata",
        lambda cid, k, v: calls["lookups"].append((cid, v)) or found,
    )
    monkeypatch.setattr(
        billing_gateway, "issue_invoice",
        lambda cid, inv, **kw: calls["issued"].append((cid, inv, kw))
        or (issued if issued is not None else {"id": "in_1", "status": "paid"}),
    )
    return renewals, calls


# --- the blast radius ----------------------------------------------------------


def test_billing_everyone_has_to_be_asked_for_explicitly(monkeypatch):
    """Omitting ``scope`` must be an error, not a full sweep. A global run driven by an
    injected clock is what charged a real payer for periods that were not due."""
    renewals, _calls = _wire(monkeypatch)

    with pytest.raises(TypeError):
        renewals.run_renewals(NOW, issue=True)


def test_scope_limits_who_is_billed(monkeypatch):
    renewals, calls = _wire(
        monkeypatch,
        accounts=[_Account("u1"), _Account("u2")],
    )

    result = renewals.run_renewals(NOW, scope=["u2"], issue=True)

    assert [e["user_id"] for e in result["issued"]] == ["u2"]
    assert [c[0] for c in calls["paid_through"]] == ["u2"]


def test_all_payers_still_reaches_everyone(monkeypatch):
    renewals, _calls = _wire(monkeypatch, accounts=[_Account("u1"), _Account("u2")])

    result = renewals.run_renewals(NOW, scope=renewals.ALL_PAYERS, issue=True)

    assert {e["user_id"] for e in result["issued"]} == {"u1", "u2"}


# --- shadow --------------------------------------------------------------------


def test_shadow_mode_charges_nothing_and_records_nothing(monkeypatch):
    renewals, calls = _wire(monkeypatch)

    result = renewals.run_renewals(NOW, scope=renewals.ALL_PAYERS, issue=False)

    assert result["planned"] and not result["issued"]
    assert calls["issued"] == []
    assert calls["paid_through"] == []


# --- the amount ----------------------------------------------------------------


def test_one_line_per_entity_priced_by_the_module_SET(monkeypatch):
    """Two modules on one entity are the bundle price, not the sum — the bundle IS the
    discount, so summing standalone prices would overcharge."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row("e1", "PAYMENT_REQUEST"), _Row("e1", "PETTY_CASH"), _Row("e2", "PAYMENT_REQUEST")],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    _cid, invoice, _kw = calls["issued"][0]
    assert len(invoice.lines) == 2          # one per entity, not per module
    assert invoice.total == 80000           # 2 x bundle, not 3 x standalone


def test_trials_and_cancelling_modules_are_not_billed(monkeypatch):
    """Charging either would bill for something the customer was told was not coming.

    Such a payer is filtered out by ``due_renewals`` rather than reaching the runner and
    being skipped — a payer with nothing billable is not due, which is the cleaner
    reading of the same rule."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row("e1", "PAYMENT_REQUEST", phase="trial"),
              _Row("e2", "PAYMENT_REQUEST", phase="scheduled_cancel")],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result == {"planned": [], "issued": [], "failed": [], "skipped": []}


def test_an_unpriceable_combination_is_skipped_not_guessed(monkeypatch):
    """Falling back to a sum of standalone prices would silently overcharge by the
    bundle discount — the kind of error nobody notices until a customer does."""
    renewals, calls = _wire(monkeypatch, plan=None)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result["skipped"]


# --- taking the money ----------------------------------------------------------


def test_paid_through_advances_only_after_a_successful_charge(monkeypatch):
    renewals, calls = _wire(monkeypatch)

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]


def test_a_declined_renewal_does_not_advance_and_starts_dunning(monkeypatch):
    """Advancing first would skip the period forever — a free month nobody notices.
    Starting dunning is what turns a decline into the retry schedule rather than a
    silent lapse."""
    renewals, calls = _wire(monkeypatch, issued={"id": "in_1", "status": "open"})

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["paid_through"] == []
    assert calls["dunning"] == [("u1", NOW)]
    assert result["failed"]


def test_a_crash_mid_charge_still_starts_dunning(monkeypatch):
    renewals, calls = _wire(monkeypatch)
    from billing.services import billing_gateway

    def _boom(*a, **k):
        raise RuntimeError("processor unreachable")

    monkeypatch.setattr(billing_gateway, "issue_invoice", _boom)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["paid_through"] == []
    assert calls["dunning"] == [("u1", NOW)]
    assert result["failed"]


# --- billing twice -------------------------------------------------------------


def test_a_period_already_invoiced_is_adopted_not_re_charged(monkeypatch):
    """The case a naive runner double-bills: the money was collected on an earlier run
    that died before recording it. Catching up costs nothing; re-issuing bills the
    customer twice for one month."""
    renewals, calls = _wire(monkeypatch, existing=_Record(status="paid"))

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []                       # nothing charged
    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]
    assert result["skipped"][0]["reason"] == "already invoiced; adopted"


def test_an_existing_UNPAID_invoice_is_not_re_issued_either(monkeypatch):
    """It is already out there - declined, and dunning is chasing it: a second one would ask
    twice."""
    renewals, calls = _wire(monkeypatch, existing=_Record(status="open"),
                            at_processor=DECLINED, dunning_since=NOW)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert calls["paid_through"] == []                 # nothing collected yet
    assert result["skipped"][0]["reason"] == "already invoiced; unpaid"


def test_the_guard_is_a_local_lookup_not_a_scan_of_the_processor(monkeypatch):
    """The point of ``subscription_invoice``. This used to LIST the payer's Stripe
    invoices and scan their metadata once per payer, every run — on the ordinary path
    where nothing has been billed yet and the scan can only ever come back empty."""
    renewals, calls = _wire(monkeypatch)

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    # Asked the index: the plain key, a replay's scoped form of it (``claimed_period_key``),
    # then the plain key again to charge under.
    assert calls["keys"] == ["renewal-u1-20270208-g_u1", "renewal-u1-20270208-g_u1-cus_u1",
                             "renewal-u1-20270208-g_u1"]
    assert calls["lookups"] == []                      # never asked Stripe
    assert calls["issued"]


# --- a replay's scoped claim (the dev database's lived data) --------------------


PLAIN = "renewal-u1-20270208-g_u1"
SCOPED = f"{PLAIN}-cus_u1"          # ``replay_scope("cus_u1")`` - shorter than 12, so whole


def _only_under(monkeypatch, calls, rows: dict):
    """Answer ``invoice_for_key`` from ``rows`` alone - the wiring answers every key."""
    from billing.services import store

    monkeypatch.setattr(
        store, "invoice_for_key", lambda key: calls["keys"].append(key) or rows.get(key)
    )


def test_a_period_claimed_only_under_a_replays_scoped_key_is_not_raised_again(monkeypatch):
    """A replay claims the period as ``<key>-<customer[-12:]>`` and leaves it on the dev
    database. Asked by the plain key alone the runner found nothing and raised the period a
    SECOND time - three payers, 28-29 Sep 2026. The scoped row IS this period's invoice."""
    renewals, calls = _wire(monkeypatch, at_processor=DECLINED, dunning_since=NOW)
    _only_under(monkeypatch, calls, {SCOPED: _Record(status="open")})

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result["skipped"][0]["reason"] == "already invoiced; unpaid"


def test_the_plain_key_wins_when_both_are_claimed(monkeypatch):
    from billing.services import renewals

    _r, calls = _wire(monkeypatch)
    _only_under(monkeypatch, calls, {PLAIN: _Record(), SCOPED: _Record()})

    assert renewals.claimed_period_key(PLAIN, "cus_u1") == PLAIN


def test_a_retired_key_is_not_this_periods_claim(monkeypatch):
    """``store.retired_key`` (``<key>~<id>``) has handed its period over - to a refresh, or
    out of the way of a voided duplicate. Only the replay's ``-`` form is resolved."""
    from billing.services import renewals, store

    _r, calls = _wire(monkeypatch)
    _only_under(monkeypatch, calls, {store.retired_key(PLAIN, "in_dup"): _Record(status="void")})

    assert renewals.claimed_period_key(PLAIN, "cus_u1") == PLAIN
    assert renewals.claimed_period_key(PLAIN, None) == PLAIN


def test_the_scope_is_the_customers_last_twelve_characters():
    from billing.services import renewals

    assert renewals.replay_scope("cus_ABCDEFGHIJKLmnopqrstuv") == "KLmnopqrstuv"


def test_a_reservation_that_was_never_confirmed_sent_asks_the_processor(monkeypatch):
    """A row with no ``external_id`` means the key was claimed and nothing came back, so
    the local record cannot say whether the invoice exists. Assuming it does would leave
    the payer never billed for the period — so this is the one case that still scans."""
    from billing.services import billing_gateway

    existing = _Record(external_id=None, status="draft")
    renewals, calls = _wire(
        monkeypatch,
        existing=existing,
        found={"id": "in_found", "status": "paid", "total": 40000, "created": 1800000000,
               "status_transitions": {"finalized_at": 1800000100, "paid_at": 1800000200},
               "hosted_invoice_url": "https://invoice.stripe.com/i/in_found"},
    )
    captured = []
    monkeypatch.setattr(billing_gateway, "_capture_payment_method",
                        lambda record, invoice_id: captured.append((record, invoice_id)))

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["lookups"] == [("cus_u1", "renewal-u1-20270208-g_u1")]
    assert calls["issued"] == []                       # it was already charged
    assert result["skipped"][0]["reason"] == "already invoiced; adopted"
    # And the row is completed - ALL of it, as the normal path writes it - so the next run
    # needs no scan, and the list shows when it was paid and links to it rather than "—".
    assert calls["settled"] == [(
        "inv_local",
        {"external_id": "in_found", "status": "paid", "total": 40000,
         "issued_at": datetime(2027, 1, 15, 8, 1, 40, tzinfo=UTC),
         "paid_at": datetime(2027, 1, 15, 8, 3, 20, tzinfo=UTC),
         "hosted_invoice_url": "https://invoice.stripe.com/i/in_found"},
    )]
    # ...and which card paid it, exactly as a charge made on the normal path records it.
    assert captured == [(existing, "in_found")]


def test_a_reservation_the_processor_never_saw_is_discarded_and_retried(monkeypatch):
    """The other half of that case. The claim has to be released, or the guard becomes a
    permanent hold on a charge nobody ever made — the payer is never billed again."""
    renewals, calls = _wire(
        monkeypatch,
        existing=_Record(external_id=None, status="draft"),
        found=None,
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["discarded"] == ["inv_local"]
    assert result["issued"], "the period must still get charged"
    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]


# --- a row that can still move is asked about, not believed ---------------------------------
#
# There is no webhook. A renewal paid just before a crash - or settled somewhere this code
# cannot see - kept whatever its row said when the process stopped, and "open" was answered
# from the store for ever: never adopted, its period never advanced.


def test_a_row_left_open_that_the_processor_says_is_paid_is_adopted(monkeypatch):
    existing = _Record(external_id="in_open", status="open")
    renewals, calls = _wire(monkeypatch, existing=existing,
                            at_processor={"id": "in_open", "status": "paid"})

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["rechecked"] == [existing]
    assert calls["issued"] == []
    assert result["skipped"][0]["reason"] == "already invoiced; adopted"
    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]


def test_a_settled_row_is_answered_from_the_store_without_asking(monkeypatch):
    """Paid, void and uncollectible do not move again: no processor read for those."""
    from billing.services import billing_gateway

    renewals, calls = _wire(monkeypatch, existing=_Record(external_id="in_paid", status="paid"))
    monkeypatch.setattr(billing_gateway, "refresh_record",
                        lambda record: pytest.fail("a settled row was re-read"))

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["skipped"][0]["reason"] == "already invoiced; adopted"
    assert calls["lookups"] == [] and calls["rechecked"] == []


# --- a draft our own issue left behind is FINISHED, not skipped ------------------------------
#
# ``issue_invoice`` records the finalize before it asks for payment, so a row still reading
# "draft" was never charged. Skipped as "already invoiced" it was never billed at all: dunning
# chases only open invoices and nothing else finalizes a draft.


class _LineRow:
    """A recorded ``subscription_invoice_line``, as ``store.invoice_lines`` returns it."""

    def __init__(self, entity_name="Entity e1", product_name="Petty Cash", kind="full"):
        self.entity_name = entity_name
        self.product_name = product_name
        self.kind = kind
        self.at = None


def _wire_resume(monkeypatch, *, existing, answer):
    """The renewal world, with ``resume_invoice`` answering ``answer``; returns the calls."""
    from billing.services import billing_gateway, store

    renewals, calls = _wire(monkeypatch, existing=existing)
    calls["resumed"] = []
    monkeypatch.setattr(
        billing_gateway, "resume_invoice",
        lambda record, payment_method=None: calls["resumed"].append((record, payment_method))
        or answer,
    )
    monkeypatch.setattr(store, "invoice_lines", lambda invoice_id: [_LineRow()])
    return renewals, calls


def test_a_draft_its_own_issue_left_is_finished_on_the_cards_current_card(monkeypatch):
    existing = _Record(external_id="in_draft", status="draft", total=40000)
    renewals, calls = _wire_resume(monkeypatch, existing=existing,
                                   answer={"id": "in_draft", "status": "paid"})

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["resumed"] == [(existing, "pm_u1")]    # the account's card, as it is now
    assert calls["issued"] == []                        # finished, never raised a second time
    assert calls["lookups"] == []
    assert [e["invoice"] for e in result["issued"]] == ["in_draft"]
    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]
    # The receipt states what was RESERVED and charged, not today's pricing.
    assert result["issued"][0]["total"] == 40000
    assert result["issued"][0]["lines"] == ["Entity e1 - Petty Cash"]


def test_a_draft_found_by_its_metadata_is_finished_in_the_same_pass(monkeypatch):
    """The crash came before the draft's id was recorded: the scan finds it, and the same
    pass finishes it rather than skipping the period until the next one."""
    existing = _Record(external_id=None, status="draft")
    renewals, calls = _wire_resume(monkeypatch, existing=existing,
                                   answer={"id": "in_draft", "status": "paid"})
    monkeypatch.setattr(renewals, "_already_invoiced", _found_draft(existing))

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["resumed"] == [(existing, "pm_u1")]
    assert calls["issued"] == []
    assert [e["invoice"] for e in result["issued"]] == ["in_draft"]


def _found_draft(existing):
    """``_already_invoiced`` finding the draft by metadata and recording its id."""

    def _already(customer_id, key, *, metadata_key="renewal_key", record=None):
        existing.external_id = "in_draft"
        return "draft"

    return _already


def test_a_draft_that_is_not_finished_is_skipped_without_a_word_to_the_customer(monkeypatch):
    """``resume_invoice`` refused it (and said so, as a STRANDED DRAFT): nothing charged,
    nothing mailed, no dunning started over a document nobody could pay."""
    renewals, calls = _wire_resume(
        monkeypatch, existing=_Record(external_id="in_draft", status="draft"), answer=None,
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["skipped"][0]["reason"] == "left a draft; not finished"
    assert result["issued"] == [] and result["failed"] == []
    assert calls["dunning"] == []
    assert calls["paid_through"] == []


def test_a_decline_on_the_finish_is_a_failed_renewal(monkeypatch):
    from billing.services import billing_gateway, store

    existing = _Record(external_id="in_draft", status="draft")
    renewals, calls = _wire(monkeypatch, existing=existing)
    monkeypatch.setattr(store, "invoice_lines", lambda invoice_id: [_LineRow()])

    def _declined(record, payment_method=None):
        record.status = "open"            # finalized - and recorded - before the charge
        raise billing_gateway.BillingError("declined", invoice_id="in_draft")

    monkeypatch.setattr(billing_gateway, "resume_invoice", _declined)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["failed"][0]["status"] == "error"
    assert result["failed"][0]["invoice"] == "in_draft"
    assert calls["dunning"] == [("u1", NOW)]


def test_an_error_before_finalizing_names_the_draft_it_left(monkeypatch, caplog):
    """The error is logged anyway; what was missing is WHICH document it left behind - the
    one thing an operator needs to find - and what happens to it next."""
    from billing.services import billing_gateway, store

    renewals, calls = _wire(monkeypatch)
    left = _Record(external_id="in_left", status="draft")
    # Nothing before the issue (the plain key, a replay's scoped form, the charge's own read);
    # the draft after it.
    rows = iter([None, None, None, left])
    monkeypatch.setattr(store, "invoice_for_key", lambda key: next(rows))

    def _stopped(customer_id, invoice, **kw):
        raise billing_gateway.BillingError("finalize failed", invoice_id="in_left")

    monkeypatch.setattr(billing_gateway, "issue_invoice", _stopped)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["failed"][0]["invoice"] == "in_left"
    stranded = [r.getMessage() for r in caplog.records
                if r.levelname == "ERROR" and "STRANDED DRAFT in_left" in r.getMessage()]
    assert len(stranded) == 1
    assert billing_gateway.RESUMED in stranded[0]


def test_the_period_key_is_stable_for_the_same_period(monkeypatch):
    """It is both the idempotency key and the invoice metadata, so it has to survive a
    process restart — anything derived from "now" would not."""
    from billing.services import renewals
    from billing.services.billing import Period

    period = Period(datetime(2027, 2, 8, 13, tzinfo=UTC),
                    datetime(2027, 3, 8, 13, tzinfo=UTC))

    assert renewals.period_key("u1", period) == renewals.period_key("u1", period)
    assert renewals.period_key("u1", period) != renewals.period_key("u2", period)


class _Extension:
    def __init__(self, entity_id="e1", code="PAYMENT_REQUEST", amount=4258, id="ext_1"):
        self.id = id
        self.entity_id = entity_id
        self.function_code = code
        self.extension_amount = amount


def test_a_cancel_extension_rides_the_next_invoice(monkeypatch):
    """Cancelling records what is owed rather than charging it, so that an expired card
    cannot stop somebody leaving. The runner collects it — the role Stripe's anchor
    invoice used to play."""
    renewals, calls = _wire(monkeypatch, extensions=[_Extension(amount=4258)])

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    _cid, invoice, _kw = calls["issued"][0]
    assert invoice.total == 40000 + 4258            # renewal + extension
    assert any("cancellation" in line.description for line in invoice.lines)


def test_an_extension_line_is_named_from_the_catalog_not_from_the_code(monkeypatch):
    """It used to title-case the module CODE. "PETTY_CASH" happens to come out "Petty
    Cash", so the bug hid until a Payment Request extension printed "Bill" — a word the
    catalog does not use — on an invoice whose other lines said "Payment Request".

    Priced as a ONE-MODULE set: the extension is for the module that was cancelled, so
    naming it after the entity's bundle would claim a charge for something still held.
    """
    from billing.services import store

    renewals, calls = _wire(monkeypatch, extensions=[_Extension(code="PAYMENT_REQUEST")])
    asked: list[list[str]] = []

    def _plan_for(codes):
        codes = [str(c).upper() for c in codes]
        asked.append(codes)
        return {
            ("PAYMENT_REQUEST",): _Plan("Payment Request", 28000),
            ("PETTY_CASH",): _Plan("Petty Cash", 28000),
        }.get(tuple(sorted(codes)), _Plan())

    monkeypatch.setattr(store, "billing_plan_for_codes", _plan_for)

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    _cid, invoice, _kw = calls["issued"][0]
    extension = next(ln for ln in invoice.lines if "cancellation" in ln.description)
    assert extension.product_name == "Payment Request (access after cancellation)"
    assert "Bill (" not in extension.description
    assert ["PAYMENT_REQUEST"] in asked, "the extension is priced per module, not per bundle"


def test_a_module_with_no_catalog_row_still_gets_billed(monkeypatch):
    """A label must never cost a collection. Falls back to the old title-cased code."""
    from billing.services import store

    renewals, calls = _wire(monkeypatch, extensions=[_Extension(code="PETTY_CASH")])
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        lambda codes: None if list(codes) == ["PETTY_CASH"] else _Plan(),
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    _cid, invoice, _kw = calls["issued"][0]
    extension = next(ln for ln in invoice.lines if "cancellation" in ln.description)
    assert extension.product_name == "Petty Cash (access after cancellation)"
    assert invoice.total == 40000 + 4258


def test_a_payer_whose_LAST_entity_was_cancelled_is_still_billed(monkeypatch):
    """The case this mechanism exists for. Nothing renews, so the payer has no billable
    modules at all — but they still owe the days they were promised. Filtering them out
    for having nothing to renew would give those days away."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row("e1", "PAYMENT_REQUEST", phase="scheduled_cancel")],   # nothing billable
        extensions=[_Extension(amount=4258)],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["issued"], "the extension must still be collected"
    _cid, invoice, _kw = calls["issued"][0]
    assert invoice.total == 4258                    # extension only
    assert invoice.currency == "hkd"                # from the payer, not a plan


def test_a_payer_who_is_not_due_is_left_alone(monkeypatch):
    renewals, calls = _wire(
        monkeypatch, accounts=[_Account("u1", paid_through=NOW + timedelta(days=5))]
    )

    result = renewals.run_renewals(NOW, scope=renewals.ALL_PAYERS, issue=True)

    assert calls["issued"] == []
    assert result == {"planned": [], "issued": [], "failed": [], "skipped": []}


# --- closing the extension out --------------------------------------------------
#
# pending_extensions_for_payer selects on extension_state == "pending", so an extension
# that is never moved off it is picked up by EVERY later renewal: the customer pays the
# same cancellation fee once a month, forever, for a module they already left.
#
# Found in live data, not here — a cancelled module sat at "pending" with its amount
# after the invoice carrying it had been paid. EXT_INVOICED existed in constants.py and
# was assigned nowhere in the codebase.


def test_a_collected_extension_is_closed_out_so_it_cannot_ride_again(monkeypatch):
    renewals, calls = _wire(monkeypatch, extensions=[_Extension(id="ext_9")])

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["marked"] == [["ext_9"]]


def test_the_run_that_advances_the_cycle_is_the_run_that_stamps_the_extension(monkeypatch):
    """The assumption ``access.is_covered_this_period`` now rests on.

    That predicate has to know whether a cancelling module was billed for the period it is
    being asked about. The account's ``paid_through`` cannot say: it moves for the whole
    PAYER while ``billable_codes_by_entity`` leaves the cancelling module off the invoice.
    The per-row half of the answer is ``extension_state == "invoiced"`` — which only works
    because BOTH happen in this one run, for this one payer.

    So this pins the pairing rather than either half: one entity still billing, one
    cancelling and owing an extension, and after the run the cycle has moved AND the
    extension is closed. Split them across runs and the predicate silently goes back to
    counting a module for a period nobody billed it for — which undercharged every
    reinstatement made after a renewal.
    """
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row("e1", "PAYMENT_REQUEST", phase="active"),
              _Row("e2", "PETTY_CASH", phase="scheduled_cancel")],
        extensions=[_Extension(id="ext_9", entity_id="e2", code="PETTY_CASH")],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["marked"] == [["ext_9"]], "the extension is closed by this run"
    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))], (
        "and the same run moves the cycle past the period it was never billed for"
    )
    # The cancelling module is on the invoice ONLY as its extension line — never as a
    # plan line for the period that just started. That is the whole reason the date lies.
    lines = calls["issued"][0][1].lines
    assert [line.entity_id for line in lines] == ["e1", "e2"]
    assert "access after cancellation" in lines[1].product_name


def test_an_extension_is_closed_out_by_the_invoice_that_CARRIES_it(monkeypatch):
    """Raised, not paid — because a declined renewal is not an abandoned one.

    This used to wait for payment, on the reasoning that marking a failed charge would
    drop the fee silently. That holds only if nothing chases the invoice afterwards, and
    dunning does: the same document, extension lines and all, is retried for the whole
    past-due window. Leaving the rows pending meant that when dunning finally collected,
    the NEXT renewal added them again and the customer paid for one cancellation twice.

    ``paid_through`` still does not move — that one really does depend on the money.
    """
    renewals, calls = _wire(
        monkeypatch,
        extensions=[_Extension()],
        issued={"id": "in_1", "status": "open"},      # declined
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["marked"] == [["ext_1"]]
    assert calls["paid_through"] == []


def test_nothing_is_closed_out_when_no_invoice_was_raised_at_all(monkeypatch):
    """The one case that must still leave them pending.

    Issuing threw, so there is no document carrying the extension and nothing to chase.
    Closing it here would be the silent drop the old rule was written to prevent.
    """
    from billing.services import billing_gateway

    renewals, calls = _wire(monkeypatch, extensions=[_Extension()])

    def _boom(*a, **k):
        raise RuntimeError("processor unreachable")

    monkeypatch.setattr(billing_gateway, "issue_invoice", _boom)

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["marked"] == []
    assert calls["dunning"], "a failed issue should still start dunning"


def test_an_adopted_invoice_still_closes_its_extensions(monkeypatch):
    """The crash-recovery path: an earlier run charged the customer and died before
    recording it. The extension rode THAT invoice, so leaving it pending would put it on
    the next one too — the double charge this whole mechanism guards against."""
    renewals, calls = _wire(
        monkeypatch,
        extensions=[_Extension(id="ext_3")],
        existing=_Record(status="paid"),
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []                      # nothing re-charged
    assert calls["marked"] == [["ext_3"]]
    assert result["skipped"][0]["reason"] == "already invoiced; adopted"


def test_an_unpaid_adopted_invoice_ALSO_closes_its_extensions(monkeypatch):
    """Owed is not the same as unbilled.

    The extension is still owed — but it is owed ON the invoice that already carries it,
    which dunning is chasing. Keeping it pending as well would bill it a second time on
    the next period while the first copy was still being collected.
    """
    renewals, calls = _wire(
        monkeypatch,
        extensions=[_Extension()],
        existing=_Record(status="open"),
        at_processor=DECLINED,
        dunning_since=NOW,
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["marked"] == [["ext_1"]]
    assert calls["paid_through"] == []                # unpaid, so the cycle stands still
    assert result["skipped"][0]["reason"] == "already invoiced; unpaid"


# --- an entity already charged for the period ----------------------------------
#
# A purchase or a trial conversion landing ON a period boundary bills its own entity for
# the whole of that period. The renewal for the same period must therefore skip THAT
# entity — and only that one. Getting either half wrong costs real money: re-billing it
# charges twice for the same days, and skipping the account wholesale (which is what
# advancing ``paid_through`` from the conversion used to do) leaves every sibling unbilled
# for the month, silently, because a payer who is not due raises no invoice to miss.


class _BilledRow(_Row):
    def __init__(self, entity_id="e1", code="PAYMENT_REQUEST", phase="active", first_billed_at=None):
        super().__init__(entity_id, code, phase)
        self.first_billed_at = first_billed_at


def test_an_entity_billed_inside_the_period_is_not_renewed_for_it_again(monkeypatch):
    """It already paid for these days in its own invoice."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[_BilledRow(entity_id="e1", first_billed_at=PAID_THROUGH)],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == [], "the entity was charged twice for one period"
    assert result["skipped"][0]["reason"] == "already covered this period"


def test_its_SIBLINGS_are_still_billed_for_that_period(monkeypatch):
    """The bug this pair exists for.

    One entity converting on the boundary must not settle the account. Steady Co went a
    full month unbilled because a sibling's conversion advanced ``paid_through`` and the
    renewal then found the payer not due at all.
    """
    renewals, calls = _wire(
        monkeypatch,
        rows=[
            _BilledRow(entity_id="converted", first_billed_at=PAID_THROUGH),
            _BilledRow(entity_id="sibling", first_billed_at=ANCHOR),
        ],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(calls["issued"]) == 1
    invoice = calls["issued"][0][1]
    assert [line.entity_id for line in invoice.lines] == ["sibling"]


def test_a_period_covered_by_someone_elses_invoice_still_advances_the_cycle(monkeypatch):
    """Otherwise the account is due forever and eventually lapses for non-payment.

    Nothing is billable, but the period IS paid for. Leaving ``paid_through`` behind
    would re-check the account every day and, past the grace window, revoke access over
    money that was collected.
    """
    renewals, calls = _wire(
        monkeypatch,
        rows=[_BilledRow(entity_id="e1", first_billed_at=PAID_THROUGH)],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]


def test_an_account_with_nothing_left_does_not_advance(monkeypatch):
    """"Nothing billable" and "already covered" are different nothings.

    A payer whose modules have all lapsed must not have their cycle rolled forward, or
    they collect free periods for as long as the job runs.
    """
    renewals, calls = _wire(monkeypatch, rows=[_BilledRow(phase="expired")])

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["paid_through"] == []
    assert calls["issued"] == []


def test_a_past_period_does_not_exempt_an_entity_forever(monkeypatch):
    """``first_billed_at`` falls inside exactly one period; later ones renew normally."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[_BilledRow(entity_id="e1", first_billed_at=ANCHOR - timedelta(days=400))],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(calls["issued"]) == 1


# --- a transferred entity's claim -----------------------------------------------
#
# ``billed_through`` is money already collected for an entity from outside this payer's
# cycle: a subscriber transfer invoices the NEW payer at accept for the window the old
# payer's payment did not reach. The renewal then has two jobs, and doing only the first
# is worse than doing neither — excluding the entity while leaving ``paid_through``
# behind keeps the account permanently due, so it is re-billed the very next day.
#
# The renewal period here is [8 Feb 13:00, 8 Mar 13:00).

PERIOD_START = datetime(2027, 2, 8, 13, tzinfo=UTC)
PERIOD_END = datetime(2027, 3, 8, 13, tzinfo=UTC)


def test_a_claim_reaching_into_the_period_is_not_billed_again(monkeypatch):
    """The entity was paid for at accept. Billing it here charges the same days twice."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row(billed_through=datetime(2027, 2, 20, tzinfo=UTC))],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result["skipped"][0]["reason"] == "nothing billable"
    # NOT advanced: the claim stops partway through, so the rest of the period really is
    # unpaid and the account is legitimately still due.
    assert calls["paid_through"] == []


def test_a_parked_handover_is_left_off_the_renewal_and_the_card_is_not_advanced(monkeypatch):
    """The company was handed over with its first charge PARKED for a day inside this period:
    that window is the handover's own collection to take, and the days before it were the old
    payer's. Billed here too, the new payer paid both twice over. Left off, the card is still
    due - never advanced past a window nobody has paid for yet."""
    from billing.services import store

    renewals, calls = _wire(monkeypatch)
    monkeypatch.setattr(store, "entities_awaiting_handover",
                        lambda uid, period: {str(r.entity_id) for r in [_Row()]})

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result["skipped"][0]["reason"] == "nothing billable"
    assert calls["paid_through"] == []


def test_a_claim_covering_the_period_advances_the_cycle(monkeypatch):
    """Excluding without advancing is the trap. The claim covers these days outright, so
    the account is settled and its cycle has to move even though nothing was invoiced."""
    renewals, calls = _wire(monkeypatch, rows=[_Row(billed_through=PERIOD_END)])

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result["skipped"][0]["reason"] == "already covered this period"
    assert calls["paid_through"] == [("u1", PERIOD_END)]


def test_a_claim_landing_on_the_period_start_is_not_sticky(monkeypatch):
    """Periods are half-open and tile: the instant that ends one begins the next. A claim
    expiring exactly at this period's start bought none of it, so it renews at full price
    — the off-by-one that would otherwise hand over a free month."""
    renewals, calls = _wire(monkeypatch, rows=[_Row(billed_through=PERIOD_START)])

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(calls["issued"]) == 1
    assert calls["issued"][0][1].total == 40000


def test_a_dead_row_s_stale_claim_excludes_nothing(monkeypatch):
    """A cancelled row keeps whatever claim it had. Letting that suppress the entity would
    stop the LIVE module beside it being billed — the entity would run on for free."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[
            _Row(code="PAYMENT_REQUEST"),
            _Row(code="PETTY_CASH", phase="cancelled", billed_through=PERIOD_END),
        ],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(calls["issued"]) == 1
    assert calls["paid_through"] == [("u1", PERIOD_END)]


def test_one_entity_s_claim_does_not_shield_another(monkeypatch):
    """The claim is per ENTITY, not per account. A payer who took over one company still
    owes for the others on the same invoice."""
    renewals, calls = _wire(
        monkeypatch,
        rows=[
            _Row(entity_id="e1", billed_through=PERIOD_END),
            _Row(entity_id="e2"),
        ],
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(calls["issued"]) == 1
    lines = calls["issued"][0][1].lines
    assert [line.entity_id for line in lines] == ["e2"]


def test_a_naive_claim_does_not_stop_the_run(monkeypatch):
    """Some drivers hand back naive datetimes. Comparing one against an aware period
    raises rather than answering, and an exception here would stop the payer being billed
    at all — the same defence ``entities_billed_in`` already carries."""
    renewals, calls = _wire(
        monkeypatch, rows=[_Row(billed_through=PERIOD_END.replace(tzinfo=None))]
    )

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert calls["paid_through"] == [("u1", PERIOD_END)]


# --- one payer, two cards --------------------------------------------------------
#
# The whole point of a per-entity payment method. A payer with two cards is billed twice
# for one period — once per card, each for its own companies — and the two outcomes are
# independent. Everything above this line describes an account with a single card, which
# is what the backfill leaves behind and still the common case.


def _two_cards():
    """Two companies of one payer, on two different cards."""
    return dict(
        rows=[_Row(entity_id="e1"), _Row(entity_id="e2")],
        groups=[
            _Group(id="gA", card="pm_A", entities=["e1"]),
            _Group(id="gB", card="pm_B", entities=["e2"]),
        ],
    )


def test_two_cards_raise_two_invoices_each_charged_to_its_own(monkeypatch):
    """One invoice per CARD, not per payer — and each names the card it is charged to.

    Set on the invoice rather than by moving the customer default, which would repoint
    every other company of this payer mid-run.
    """
    renewals, calls = _wire(monkeypatch, **_two_cards())

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(result["issued"]) == 2
    charged = {kw["payment_method"]: inv for _cid, inv, kw in calls["issued"]}
    assert set(charged) == {"pm_A", "pm_B"}
    # Each document carries only its own card's company.
    assert {line.entity_id for line in charged["pm_A"].lines} == {"e1"}
    assert {line.entity_id for line in charged["pm_B"].lines} == {"e2"}
    # And each advances its own cycle, not the account's.
    assert sorted(g for g, _until in calls["group_paid_through"]) == ["gA", "gB"]


def test_the_two_invoices_do_not_collide_on_the_idempotency_key(monkeypatch):
    """The group is IN the key, and has to be.

    Under a payer-and-period key the second card's invoice would be refused as a
    double-bill of the first — so one card's companies would simply never be charged, and
    the guard that exists to prevent overcharging would be causing free service.
    """
    renewals, calls = _wire(monkeypatch, **_two_cards())

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    keys = [kw["idempotency_key"] for _cid, _inv, kw in calls["issued"]]
    assert sorted(keys) == [
        "renewal-u1-20270208-gA", "renewal-u1-20270208-gB",
    ]
    assert len(set(keys)) == 2


def test_a_decline_on_one_card_leaves_the_other_alone(monkeypatch):
    """CONTAINMENT — the reason the cycle and the dunning clock moved onto the card.

    Card B declines, card A clears. Only B goes into collection; A's company is paid for
    and stays up. Under one clock per payer, B failing put every company of this payer
    into the past-due grace window and then terminated them for a debt that was not
    theirs.
    """
    def _issue(cid, invoice, **kw):
        paid = kw.get("payment_method") == "pm_A"
        return {"id": "in_A" if paid else "in_B",
                "status": "paid" if paid else "open"}

    renewals, calls = _wire(monkeypatch, **_two_cards())
    from billing.services import billing_gateway

    monkeypatch.setattr(
        billing_gateway, "issue_invoice",
        lambda cid, inv, **kw: calls["issued"].append((cid, inv, kw)) or _issue(cid, inv, **kw),
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert [e["billing_group_id"] for e in result["issued"]] == ["gA"]
    assert [e["billing_group_id"] for e in result["failed"]] == ["gB"]
    # Only the failing card collects, and only the paying one advances.
    assert [g for g, _when in calls["group_dunning"]] == ["gB"]
    assert [g for g, _until in calls["group_paid_through"]] == ["gA"]


def test_a_card_that_is_not_due_yet_is_not_billed_with_the_one_that_is(monkeypatch):
    """Each card buys its own periods, so each comes due on its own date."""
    later = PERIOD_END + timedelta(days=40)
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row(entity_id="e1"), _Row(entity_id="e2")],
        groups=[
            _Group(id="gA", card="pm_A", entities=["e1"]),
            _Group(id="gB", card="pm_B", entities=["e2"], paid_through=later),
        ],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert [e["billing_group_id"] for e in result["issued"]] == ["gA"]
    assert [kw["payment_method"] for _cid, _inv, kw in calls["issued"]] == ["pm_A"]


def test_a_card_with_no_cycle_of_its_own_is_never_due(monkeypatch):
    """A NULL ``paid_through`` is skipped — and that is why moving a company between
    cards has to carry its paid days across.

    Without the carry, re-pointing a company at a freshly nominated card would leave that
    card with no cycle, this check would skip it every day, and the company would run on
    unbilled with no invoice ever raised to notice the absence of.
    """
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row(entity_id="e1")],
        groups=[_Group(id="gNew", card="pm_new", entities=["e1"], paid_through=None)],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["issued"] == []
    assert result == {"planned": [], "issued": [], "failed": [], "skipped": []}


def test_moving_a_company_hands_its_cycle_to_the_new_card(monkeypatch):
    """``store._carry_paid_days``, at the branch that needs no database.

    The card it left had bought days up to a date; the card it joins has never collected.
    The new card takes the cycle over at exactly that instant — no gap, and no period
    charged twice.
    """
    from types import SimpleNamespace

    from billing.services import store

    leaving = SimpleNamespace(id="gA", paid_through=PERIOD_END)
    joining = SimpleNamespace(id="gB", paid_through=None)

    store._carry_paid_days("e1", leaving, joining)

    assert joining.paid_through == PERIOD_END


def test_a_card_already_paid_further_ahead_keeps_its_own_date(monkeypatch):
    """The company joins the cycle the card is already on rather than winding it back.

    Winding it back would re-bill every OTHER company on that card for days it had
    already collected — one moved company must not reopen a settled period for the rest.
    """
    from types import SimpleNamespace

    from billing.services import store

    later = PERIOD_END + timedelta(days=30)
    leaving = SimpleNamespace(id="gA", paid_through=PERIOD_END)
    joining = SimpleNamespace(id="gB", paid_through=later)

    store._carry_paid_days("e1", leaving, joining)

    assert joining.paid_through == later


def test_a_company_on_no_card_is_not_billed_on_someone_elses(monkeypatch):
    """No nomination is not "use the default" — it is not billed at all, and says so.

    A silent fallback is how one card came to pay for every company in the first place.
    The alternative to skipping is charging a card the payer never chose for it.
    """
    renewals, calls = _wire(
        monkeypatch,
        rows=[_Row(entity_id="e1"), _Row(entity_id="e_orphan")],
        groups=[_Group(id="gA", card="pm_A", entities=["e1"])],
    )

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert len(calls["issued"]) == 1
    _cid, invoice, kw = calls["issued"][0]
    assert kw["payment_method"] == "pm_A"
    assert {line.entity_id for line in invoice.lines} == {"e1"}
    assert "e_orphan" not in str(result)


# --- the charge, and recording it, are two steps ----------------------------------------------
#
# A write that failed AFTER the money moved used to land in the charge's own except: a paid
# customer went into dunning, was told "We couldn't process your payment", and then "Thank you
# for your payment" when dunning found nothing to collect.


def test_a_paid_renewal_whose_recording_failed_is_not_a_decline(monkeypatch, caplog):
    from billing.services import store

    renewals, calls = _wire(monkeypatch)

    def _db_down(group_id, until):
        raise RuntimeError("database went away")

    monkeypatch.setattr(store, "set_group_paid_through", _db_down)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert [e["invoice"] for e in result["issued"]] == ["in_1"]
    assert result["failed"] == [] and calls["dunning"] == []
    assert any(r.levelname == "ERROR" and "recording it failed" in r.getMessage()
               for r in caplog.records)


def test_a_paid_renewal_releases_a_silent_grace(monkeypatch):
    renewals, calls = _wire(monkeypatch)

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["released"] == [("g_u1", NOW)]


# --- the processor failing is not the card declining (the user's rule, 2026-09-30) ------------


def test_a_processor_outage_holds_the_grace_without_dunning_or_mail(monkeypatch):
    """Nothing is known about whether the customer can pay: nobody is told, dunning does not
    start, and the companies keep their grace while the next pass tries again."""
    from billing.services import billing_gateway

    renewals, calls = _wire(monkeypatch)

    def _outage(customer_id, invoice, **kw):
        raise billing_gateway.BillingError("could not connect", retryable=True)

    monkeypatch.setattr(billing_gateway, "issue_invoice", _outage)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["held"] == [("g_u1", NOW)]
    assert calls["dunning"] == [] and result["failed"] == []
    assert result["skipped"][0]["reason"] == "processor unavailable; retrying next pass"


def test_an_outage_while_asking_after_a_reservation_is_not_a_decline(monkeypatch):
    """The scan for a reservation nobody confirmed raises the processor's error unwrapped."""
    import stripe as stripe_lib

    from billing.services import billing_gateway

    renewals, calls = _wire(monkeypatch, existing=_Record(external_id=None, status="draft"))

    def _outage(customer_id, key, value, **kw):
        raise stripe_lib.APIConnectionError("no route to Stripe")

    monkeypatch.setattr(billing_gateway, "find_invoice_by_metadata", _outage)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["held"] and calls["dunning"] == [] and result["failed"] == []


def test_a_decline_still_starts_dunning_and_says_so(monkeypatch):
    from billing.services import billing_gateway

    renewals, calls = _wire(monkeypatch)

    def _declined(customer_id, invoice, **kw):
        raise billing_gateway.BillingError("Your card was declined.", invoice_id="in_1")

    monkeypatch.setattr(billing_gateway, "issue_invoice", _declined)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["dunning"] == [("u1", NOW)] and calls["held"] == []
    assert result["failed"][0]["invoice"] == "in_1"


def test_an_outage_that_outlasts_the_grace_turns_critical(monkeypatch, caplog):
    """The ERROR fires every pass; with three days of grace left the grace ending is the one
    thing that would reach the customer, so it is said louder."""
    from billing.services import billing_gateway

    late = NOW - timedelta(days=13)                   # the 15-day grace ends in two days
    renewals, calls = _wire(monkeypatch, accounts=[_Account(paid_through=late)])

    def _outage(customer_id, invoice, **kw):
        raise billing_gateway.BillingError("could not connect", retryable=True)

    monkeypatch.setattr(billing_gateway, "issue_invoice", _outage)

    renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert any(r.levelname == "CRITICAL" and "grace" in r.getMessage() for r in caplog.records)


# --- an open invoice nobody charged (the user's call: charge it) ----------------------------------


def test_an_open_renewal_nobody_charged_is_charged_on_the_current_card(monkeypatch):
    """A crash between recording it open and charging it. Dunning never started, so dunning
    never looks: skipped as "already invoiced", it sat there while the companies went dark."""
    from billing.services import billing_gateway, store

    existing = _Record(external_id="in_open", status="open")
    renewals, calls = _wire(monkeypatch, existing=existing)
    resumed = []
    monkeypatch.setattr(
        billing_gateway, "resume_invoice",
        lambda record, payment_method=None: resumed.append((record, payment_method))
        or {"id": "in_open", "status": "paid"},
    )
    monkeypatch.setattr(store, "invoice_lines", lambda invoice_id: [_LineRow()])

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert resumed == [(existing, "pm_u1")]
    assert calls["issued"] == []                      # the same invoice, never a second one
    assert [e["invoice"] for e in result["issued"]] == ["in_open"]
    assert calls["paid_through"] == [("u1", datetime(2027, 3, 8, 13, tzinfo=UTC))]


def test_an_open_renewal_dunning_owns_is_left_to_dunning(monkeypatch):
    from billing.services import billing_gateway

    renewals, calls = _wire(monkeypatch, existing=_Record(external_id="in_open", status="open"),
                            dunning_since=NOW)
    monkeypatch.setattr(billing_gateway, "resume_invoice",
                        lambda record, payment_method=None: pytest.fail("charged"))

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["skipped"][0]["reason"] == "already invoiced; unpaid"


def test_an_open_renewal_after_access_ran_out_is_not_charged(monkeypatch, caplog):
    """Collection is over: the business does not chase those. Never charged either, so a
    person has to look - said at ERROR."""
    from billing.services import billing_gateway

    long_ago = NOW - timedelta(days=40)
    renewals, calls = _wire(monkeypatch, accounts=[_Account(paid_through=long_ago)],
                            existing=_Record(external_id="in_open", status="open"))
    monkeypatch.setattr(billing_gateway, "resume_invoice",
                        lambda record, payment_method=None: pytest.fail("charged"))

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["skipped"][0]["reason"] == "already invoiced; unpaid"
    assert any(r.levelname == "ERROR" and "NEVER CHARGED" in r.getMessage()
               for r in caplog.records)


# --- the decline notice, per card and never lost ----------------------------------------------------


def test_a_decline_whose_dunning_never_started_is_started_and_mailed(monkeypatch):
    """Declined at the processor, but the process died before recording it: no dunning, no
    notice. The next pass starts both - the notice once, by its period key."""
    from billing.services import store

    renewals, calls = _wire(monkeypatch, existing=_Record(external_id="in_old", status="open"),
                            at_processor=DECLINED, notice_sent=False)
    monkeypatch.setattr(store, "invoice_lines", lambda invoice_id: [_LineRow()])

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert calls["dunning"] == [("u1", NOW)]
    assert [e["invoice"] for e in result["failed"]] == ["in_old"]


def test_a_decline_already_told_is_not_told_again(monkeypatch):
    renewals, calls = _wire(monkeypatch, existing=_Record(external_id="in_old", status="open"),
                            at_processor=DECLINED, dunning_since=NOW, notice_sent=True)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert result["failed"] == [] and calls["dunning"] == []


def test_a_decline_is_mailed_before_the_next_card_is_billed(monkeypatch):
    """Mail waited for the end of the batch, so anything that stopped the batch - an
    exception, a restart - lost every decline notice before it."""
    from billing.services import billing_gateway, notify

    renewals, calls = _wire(monkeypatch, **_two_cards())
    mailed = []
    monkeypatch.setattr(notify, "notify_many", lambda events: mailed.extend(events))
    charges = iter([{"id": "in_A", "status": "open"}, KeyboardInterrupt])

    def _issue(customer_id, invoice, **kw):
        answer = next(charges)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    monkeypatch.setattr(billing_gateway, "issue_invoice", _issue)

    with pytest.raises(KeyboardInterrupt):
        renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert [event for _uid, event, _key, _ctx in mailed] == [notify.RENEWAL_FAILED]


def test_one_card_that_raises_does_not_stop_the_pass(monkeypatch):
    from billing.services import renewals as renewals_mod

    renewals, calls = _wire(monkeypatch, **_two_cards())
    real = renewals_mod.build_renewal

    def _build(user_id, period, group_id=None, **kw):
        if group_id == "gA":
            raise RuntimeError("pricing blew up")
        return real(user_id, period, group_id=group_id, **kw)

    monkeypatch.setattr(renewals_mod, "build_renewal", _build)

    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)

    assert [e["billing_group_id"] for e in result["issued"]] == ["gB"]
    assert ("gA", NOW) in calls["held"]
    assert any(e.get("reason") == "could not be billed; retrying next pass"
               for e in result["skipped"])
