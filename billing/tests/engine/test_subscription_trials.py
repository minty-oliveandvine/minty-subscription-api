"""Unit tests for APP-LEVEL trials.

A trial creates no billing object at all: it's a row in ``entity_module_subscription``
(phase=trial, trial_end = now + TRIAL_PERIOD_DAYS) plus an access-gate flip. Money is
only involved when the trial ENDS, where the presence of a card — and of consent for
THIS entity — decides convert vs expire (``convert_or_expire_due_trials``).

Covers:
* **Nothing billed on start** — no customer, no card, no charge is created.
* **Once per module** — any existing trial/subscription row rejects a new trial.
* **Trial end** — card + consent converts to paid via Minty's own invoice; anything
  missing expires the trial and revokes access.

NOTE: imports are done INSIDE each test. The conftest ``app`` fixture clears and
re-imports project modules mid-session, so importing at call time keeps every
reference mutually consistent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .fakes import fake_model

# The price catalog is patched BY DOTTED PATH, not via an imported reference (Minty's
# conftest re-imports project modules mid-session; kept as the module was written).
_CATALOG = "billing.services.catalog"

# The anchor the in-house cases bill against, and a "now" inside that period. Pinned
# because ``period_containing`` extrapolates from the anchor: left at real time, "now"
# sits BEFORE the anchor and the payer is billed for a period they were never in.
_ANCHOR = datetime(2027, 1, 8, 13, tzinfo=UTC)
_NOW = datetime(2027, 1, 20, 13, tzinfo=UTC)
_PERIOD_END = datetime(2027, 2, 8, 13, tzinfo=UTC)


class _FakeEntity:
    id = "e1"
    name = "Acme"


class _FakeUser:
    id = "u1"
    email = "u1@example.com"


class _Row:
    """Stand-in for an entity_module_subscription row.

    Carries a SUBSET of the real model's columns — never anything the model lacks.
    ``phase`` matters here: the trial-end job reads it to tell a running trial from a
    CANCELLED one (which keeps its free days but must never convert).
    """

    def __init__(self, code="PAYMENT_REQUEST", payer="u1", entity_id="e1", phase="trial",
                 trial_end=None):
        self.entity_id = entity_id
        self.function_code = code
        self.payer_user_id = payer
        self.phase = phase
        self.trial_end = trial_end
        self.first_billed_at = None


def _plan(code="PAYMENT_REQUEST"):
    from billing.services import catalog

    return catalog.PlanView(
        "fn_bill", code, code.title().replace("_", " "), 28000, "HKD", "month", 1, True
    )


class _Plan:
    def __init__(self, amount):
        self.amount = amount
        self.display_name = "plan"
        self.currency = "HKD"


class _Group:
    """The card this company is billed on. A conversion charges it, and nothing else."""

    def __init__(self, id="g1", card="pm_1", paid_through=None):
        self.id = id
        self.payer_user_id = "u1"
        self.stripe_payment_method_id = card
        self.paid_through = paid_through


def _setup(monkeypatch, *, existing_row=None, now=None, anchor=None, paid=True):
    """Wire the store, the biller and the access gate; return (checkout, calls).

    ``anchor`` seeds the payer's billing cycle (None = never billed, so the conversion
    anchors them). The anchor mock is STATEFUL: ``_bill_module_change_in_house`` writes
    an anchor and immediately reads it back, so a mock that kept answering None would
    make every first conversion look unbillable.
    """
    from billing.services import changes, checkout, store
    from billing.services import clock as clock_mod

    if now is not None:
        monkeypatch.setattr(clock_mod, "now", lambda: now)

    calls = {"writes": [], "access": [], "anchors": [], "charged": [],
             "paid_through": []}
    cycle = {"anchor": anchor}

    monkeypatch.setattr(f"{_CATALOG}.plan_for_module", lambda code: _plan(code))
    # "no mapping row" makes _resolve_customer_id ask Stripe by search; these are unit
    # tests, so Stripe has never heard of anyone (the two no-customer tests depend on it)
    monkeypatch.setattr(checkout, "find_customer_by_user", lambda uid: None)
    monkeypatch.setattr(store, "module_row", lambda eid, code: existing_row)
    # Default these tests to a CONSENTED entity so they keep testing what they're about
    # (conversion mechanics). The consent gate itself is covered separately.
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: True)
    monkeypatch.setattr(store, "record_billing_consent", lambda eid, uid, source: None)
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: "cus_1")
    monkeypatch.setattr(checkout, "trial_payment_method", lambda cid: "pm_1")
    # The card this company is nominated onto. Present by default: these tests are about
    # conversion mechanics, and a company with no nomination is not charged at all — a
    # rule with its own case below.
    # ``paid_through`` follows the anchor: a payer with a cycle has been charged, and the
    # charge that anchored them was on this card. A card that has never collected is a
    # real state — a payer nominating a SECOND one — and it has its own case below.
    group = _Group(paid_through=anchor)
    monkeypatch.setattr(
        store, "billing_group_for_entity", lambda eid, uid=None: group
    )
    monkeypatch.setattr(
        store, "set_group_paid_through",
        lambda gid, until: calls["paid_through"].append(("u1", until)),
    )
    monkeypatch.setattr(checkout, "_billed_codes_in_house", lambda eid: set())
    # No earlier attempt at a conversion on record (``_resolve_prior_conversions``).
    monkeypatch.setattr(store, "invoices_with_key_prefix", lambda payer, prefix: [])

    monkeypatch.setattr(
        store, "upsert_module_row",
        lambda e, code, payer, **f: calls["writes"].append((e, code, payer, f))
        or _Row(code, payer, e),
    )
    # Read back when aligning the entity's other active rows; no siblings unless a test
    # says otherwise.
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: [])

    def _start_cycle(uid, at, currency):
        calls["anchors"].append((uid, at, currency))
        cycle["anchor"] = at

    monkeypatch.setattr(
        store, "billing_cycle_for_user", lambda uid: (cycle["anchor"], "HKD")
    )
    monkeypatch.setattr(store, "start_billing_cycle", _start_cycle)
    monkeypatch.setattr(
        store, "billing_plan_for_codes",
        lambda codes: _Plan(40000 if len(set(codes)) > 1 else 28000),
    )
    monkeypatch.setattr(
        changes, "issue_change",
        lambda cid, eid, name, before, after, period, at, **kw: calls["charged"].append(
            {"customer": cid, "entity": eid, "before": set(before), "after": set(after),
             "start": period.start, "end": period.end,
             "card": getattr(kw.get("group"), "stripe_payment_method_id", None)}
        ) or {"id": "in_1", "status": "paid" if paid else "open"},
    )
    monkeypatch.setattr(
        checkout, "_set_module_access",
        lambda eid, code, enabled: calls["access"].append((eid, code, enabled)),
    )
    return checkout, calls


# --- starting a trial ---------------------------------------------------------


def test_trial_start_bills_nothing_and_writes_the_row(monkeypatch):
    checkout, calls = _setup(monkeypatch, now=_NOW)
    from billing.services import changes

    def _boom(*a, **k):
        raise AssertionError("an app-level trial must not bill anything")

    monkeypatch.setattr(changes, "issue_change", _boom)
    monkeypatch.setattr(checkout, "create_setup_checkout_session", _boom)

    result = checkout.start_module_trials(_FakeEntity(), _FakeUser(), ["PAYMENT_REQUEST"])

    assert result == ["PAYMENT_REQUEST"]
    entity_id, code, payer, fields = calls["writes"][0]
    assert (entity_id, code, payer) == ("e1", "PAYMENT_REQUEST", "u1")  # acting user is the payer
    assert fields["phase"] == "trial"
    # No ``trial_used`` / ``trial_start``: both were written here and read nowhere, and
    # the schema no longer carries them. "Has this module been trialled" is answered by
    # the row existing at all — which is exactly what start_module_trial checks, and is
    # covered by test_trial_rejected_when_module_already_has_a_row below.
    assert "trial_used" not in fields
    assert "trial_start" not in fields
    # Access runs to trial_end, which is TRIAL_PERIOD_DAYS out from now.
    assert fields["app_access_until"] == fields["trial_end"]
    assert fields["trial_end"] - _NOW == timedelta(days=checkout.TRIAL_PERIOD_DAYS)
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", True)]  # module switched on


def test_trial_rejected_when_module_already_has_a_row(monkeypatch):
    """Once per module: any existing trial/subscription row disqualifies it."""
    checkout, calls = _setup(monkeypatch, existing_row=_Row())

    with pytest.raises(checkout.CheckoutError) as exc:
        checkout.start_module_trials(_FakeEntity(), _FakeUser(), ["PAYMENT_REQUEST"])

    assert exc.value.status == 409
    assert calls["writes"] == []
    assert calls["access"] == []


def test_start_trial_requires_codes(monkeypatch):
    checkout, calls = _setup(monkeypatch)

    with pytest.raises(checkout.CheckoutError):
        checkout.start_module_trials(_FakeEntity(), _FakeUser(), [])


def test_onboarding_finalize_starts_app_trials(monkeypatch):
    checkout, calls = _setup(monkeypatch)
    monkeypatch.setattr(
        "billing.services.entity_modules.get_enabled_modules_for_entities",
        lambda ids: {"e1": {"PAYMENT_REQUEST"}},
    )

    created = checkout.start_trials_for_enabled_modules(_FakeEntity(), _FakeUser())

    assert len(created) == 1
    assert calls["writes"][0][1] == "PAYMENT_REQUEST"
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", True)]


# --- ending a trial: converting -----------------------------------------------


def test_conversion_is_collected_by_an_in_house_invoice(monkeypatch):
    """A due trial with a card and consent converts, and the money is collected by
    Minty's own invoice against the payer's period — there is no subscription."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert result["converted"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]
    assert len(calls["charged"]) == 1
    charge = calls["charged"][0]
    assert charge["customer"] == "cus_1"
    assert charge["after"] == {"PAYMENT_REQUEST"}
    assert (charge["start"], charge["end"]) == (_ANCHOR, _PERIOD_END)
    # The account's cycle is NOT touched. This charge covered one entity, and
    # ``paid_through`` speaks for the whole payer: ``renewals.due_renewals`` reads it to
    # decide whether the account owes anything, so moving it here told that run every
    # OTHER entity was settled too and they went unbilled for the month. Only a renewal
    # covers everyone, so only a renewal advances it. A payer whose FIRST charge this is
    # still gets one written — see ``test_a_first_conversion_still_starts_the_cycle``.
    assert calls["paid_through"] == []
    # Phase moved off `trial` so a re-run can't convert twice, and the row is stamped as
    # having been billed.
    converted = [w for w in calls["writes"] if w[3].get("phase") == "active"]
    assert converted and converted[-1][3]["first_billed_at"] == _NOW


def test_a_first_conversion_still_starts_the_cycle(monkeypatch):
    """The one case that may write ``paid_through``: a payer who has never been billed.

    ``due_renewals`` skips a NULL ``paid_through`` outright, so if the first charge on an
    account left it unset the account would never come due and would never renew — the
    opposite failure to the one the rule exists to prevent.
    """
    from billing.services import store

    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=None)
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    checkout.convert_or_expire_due_trials()

    assert calls["anchors"], "a payer with no anchor should have had one written"
    assert len(calls["paid_through"]) == 1
    assert calls["paid_through"][0][0] == "u1"


def test_two_modules_due_together_bill_as_one_change(monkeypatch):
    """ONE invoice for ONE event. Both of an entity's modules ending together is a
    single change to the bundle price — billing them row-by-row cut two invoices seconds
    apart, the first charging a standalone price the customer never chose and the
    second immediately crediting it back."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import store

    rows = [_Row(code="PETTY_CASH"), _Row(code="PAYMENT_REQUEST")]
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: rows)

    result = checkout.convert_or_expire_due_trials()

    # ONE charge carrying BOTH codes -> one invoice.
    assert len(calls["charged"]) == 1
    assert calls["charged"][0]["after"] == {"PAYMENT_REQUEST", "PETTY_CASH"}
    assert result["expired"] == []
    assert {entry["code"] for entry in result["converted"]} == {"PAYMENT_REQUEST", "PETTY_CASH"}


def test_conversion_records_the_payers_billing_anchor(monkeypatch):
    """The anchor every future period is derived from, recorded the first time anything
    is billed for the payer.

    A payer with no anchor is anchored at the conversion moment, so this period starts
    here and is charged in full rather than prorated against a period they were never
    part of."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=None)
    from billing.services import store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    checkout.convert_or_expire_due_trials()

    assert len(calls["anchors"]) == 1
    user_id, anchor, currency = calls["anchors"][0]
    assert (user_id, anchor) == ("u1", _NOW)
    assert currency == "HKD"
    # The period billed starts at the brand-new anchor, i.e. a full period from now.
    assert calls["charged"][0]["start"] == _NOW


def test_the_anchor_is_recorded_once_and_never_moved(monkeypatch):
    """Moving it would retroactively redraw every past period, so a payer who already
    has one is left alone — a second entity converting must not re-anchor them."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=datetime(2026, 9, 8, 13, tzinfo=UTC))
    from billing.services import store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    checkout.convert_or_expire_due_trials()

    assert calls["anchors"] == []  # untouched


def test_an_unpaid_in_house_invoice_expires_the_trial(monkeypatch):
    """Same rule as a declined card: never hand over modules that were not paid for."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR, paid=False)
    from billing.services import store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert result["converted"] == []
    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", False)]  # access revoked, not granted


def test_an_unpaid_conversion_invoice_is_VOIDED_not_left_open(monkeypatch):
    """Withdraw the bill along with the modules.

    Leaving it open bills the customer for a module explicitly not granted — the row's
    ``first_billed_at`` stays NULL, so the system's own record says no charge happened,
    beside an open document saying one did. And it does not just sit there: dunning
    chases the payer's OLDEST open invoice, so a later episode retries this first, for a
    trial that expired months earlier and can never be granted again.
    """
    from billing.services import billing_gateway, store

    checkout, _calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR, paid=False)
    voided: list[str] = []
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: voided.append(iid))
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert voided == ["in_1"]
    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]


def test_a_DECLINED_card_voids_its_conversion_invoice(monkeypatch):
    """The path a real decline takes, and the one the first fix missed.

    ``Invoice.pay`` RAISES on a decline rather than returning an unpaid invoice, so the
    status check never sees one — the whole thing arrives as a ``BillingError``. By then
    the invoice is finalized and open, which is why the error carries its id.

    Fixing only the returned-status branch left the live orphan untouched: a replay of
    the give-up scenario still ended with 243.87 sitting open against a trial that had
    expired and could never be granted.
    """
    from billing.services import billing_gateway, changes, store

    checkout, _calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    voided: list[str] = []

    def _declined(*_a, **_k):
        raise billing_gateway.BillingError("card declined", invoice_id="in_declined")

    monkeypatch.setattr(changes, "issue_change", _declined)
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: voided.append(iid))
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert voided == ["in_declined"]
    assert result["converted"] == []
    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]


def test_a_conversion_whose_answer_was_lost_converts(monkeypatch):
    """The charge went through and its answer never arrived: the void finds it PAID. Expiring
    the trial then left a customer charged for modules they were refused - and a restart
    charged them again."""
    from billing.services import billing_gateway, changes, store

    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)

    def _lost(*_a, **_k):
        raise billing_gateway.BillingError("no answer from the processor", invoice_id="in_lost")

    monkeypatch.setattr(changes, "issue_change", _lost)
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: "paid")
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert result["converted"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]
    assert result["expired"] == []
    assert calls["access"] != [("e1", "PAYMENT_REQUEST", False)]


def test_a_processor_outage_at_the_trial_end_keeps_the_trial(monkeypatch):
    """The user's rule (2026-09-30): Stripe failing is not the card declining. The trial is
    not expired and nothing is withdrawn - the next pass tries again, and finds whatever this
    attempt raised by its key."""
    from billing.services import billing_gateway, changes, store

    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    voided: list[str] = []

    def _outage(*_a, **_k):
        raise billing_gateway.BillingError("could not connect", invoice_id="in_x", retryable=True)

    monkeypatch.setattr(changes, "issue_change", _outage)
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: voided.append(iid))
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [
        _Row(code="PAYMENT_REQUEST", trial_end=_NOW - timedelta(hours=2))])

    result = checkout.convert_or_expire_due_trials()

    assert result == {"converted": [], "expired": [],
                      "deferred": [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]}
    assert voided == []
    assert ("e1", "PAYMENT_REQUEST", False) not in calls["access"]


def test_a_trial_the_processor_failed_for_the_whole_grace_expires(monkeypatch, caplog):
    """Not for ever: a revoked key nobody noticed must not keep a trial free indefinitely."""
    from billing.services import billing_gateway, changes, policy, store

    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    monkeypatch.setattr(policy, "current", lambda: policy.DEFAULTS)

    def _outage(*_a, **_k):
        raise billing_gateway.BillingError("could not connect", retryable=True)

    monkeypatch.setattr(changes, "issue_change", _outage)
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [
        _Row(code="PAYMENT_REQUEST", trial_end=_NOW - timedelta(days=16))])

    result = checkout.convert_or_expire_due_trials()

    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]
    assert any(r.levelname == "ERROR" and "past the grace window" in r.getMessage()
               for r in caplog.records)


def test_a_failure_with_no_invoice_raised_voids_nothing(monkeypatch):
    """``Invoice.create`` itself failing leaves no document, so there is nothing to
    withdraw — and calling void with None would be an error of its own."""
    from billing.services import billing_gateway, changes, store

    checkout, _calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    voided: list[str] = []

    def _exploded(*_a, **_k):
        raise billing_gateway.BillingError("processor unreachable")  # no invoice_id

    monkeypatch.setattr(changes, "issue_change", _exploded)
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: voided.append(iid))
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert voided == []
    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]


def test_a_void_that_fails_still_expires_the_trial(monkeypatch):
    """The refusal has already happened; a failed void must not turn it into a crash."""
    from billing.services import billing_gateway, store

    checkout, _calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR, paid=False)

    def _boom(_iid):
        raise RuntimeError("processor unreachable")

    monkeypatch.setattr(billing_gateway, "void_invoice", _boom)
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]


def test_a_PAID_conversion_voids_nothing(monkeypatch):
    """The guard is on the failure path only — a successful charge must be left alone."""
    from billing.services import billing_gateway, store

    checkout, _calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)  # paid=True
    voided: list[str] = []
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: voided.append(iid))
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    result = checkout.convert_or_expire_due_trials()

    assert voided == []
    assert result["converted"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]


# --- ending a trial: expiring -------------------------------------------------


def test_due_trial_without_a_card_expires_and_revokes_access(monkeypatch):
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import changes, store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row()])
    monkeypatch.setattr(checkout, "trial_payment_method", lambda cid: None)  # no card

    def _boom(*a, **k):
        raise AssertionError("must not bill a trial with no card")

    monkeypatch.setattr(changes, "issue_change", _boom)

    result = checkout.convert_or_expire_due_trials()

    assert result == {"converted": [], "expired": [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}],
                      "deferred": []}
    assert calls["writes"][-1][3]["phase"] == "expired"
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", False)]  # access revoked


def test_due_trial_expires_when_the_entity_never_consented_to_billing(monkeypatch):
    """THE NO-CLICK CHARGE. The payer's card is shared across every entity they pay
    for, so a card saved for entity #1 must NOT silently fund entity #2's trial
    conversion — that would charge a customer who never once agreed to pay for that
    entity, with no user action at all. Expire instead."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import changes, store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row()])
    monkeypatch.setattr(store, "has_billing_consent", lambda eid, user_id=None: False)  # never authorised

    def _boom(*a, **k):
        raise AssertionError("must not bill an entity the payer never authorised")

    monkeypatch.setattr(changes, "issue_change", _boom)

    result = checkout.convert_or_expire_due_trials()

    assert result == {"converted": [], "expired": [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}],
                      "deferred": []}
    assert calls["writes"][-1][3]["phase"] == "expired"
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", False)]


def test_due_trial_expires_when_no_card_is_nominated_for_the_entity(monkeypatch):
    """Consent says the payer may be billed for this company; it does not say on WHICH
    card. There is deliberately no fallback to the account default, so an unnominated
    company converting would charge a card the payer never chose for it — unattended,
    with nobody in the loop to notice. Expire instead; the settings page asks."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import changes, store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row()])
    monkeypatch.setattr(store, "billing_group_for_entity", lambda eid, uid=None: None)

    def _boom(*a, **k):
        raise AssertionError("must not bill a company with no card nominated")

    monkeypatch.setattr(changes, "issue_change", _boom)

    result = checkout.convert_or_expire_due_trials()

    assert result == {"converted": [], "expired": [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}],
                      "deferred": []}
    assert calls["writes"][-1][3]["phase"] == "expired"
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", False)]


def test_a_conversion_is_charged_to_the_card_the_company_is_on(monkeypatch):
    """Not the account default, and not whichever card the payer used last."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import store

    monkeypatch.setattr(
        store, "billing_group_for_entity",
        lambda eid, uid=None: _Group(id="g2", card="pm_second", paid_through=_ANCHOR),
    )
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    checkout.convert_or_expire_due_trials()

    assert calls["charged"][0]["card"] == "pm_second"


def test_a_second_card_gets_its_own_cycle_started(monkeypatch):
    """The payer is anchored already, but THIS card has collected nothing.

    ``due_renewals`` skips a NULL ``paid_through`` outright, so leaving it unset would
    mean the new card silently never renewed — the company would run on, unbilled.
    """
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import store

    monkeypatch.setattr(
        store, "billing_group_for_entity",
        lambda eid, uid=None: _Group(id="g2", card="pm_second", paid_through=None),
    )
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row(code="PAYMENT_REQUEST")])

    checkout.convert_or_expire_due_trials()

    assert calls["paid_through"] == [("u1", _PERIOD_END)]


def test_a_cancelled_trial_expires_instead_of_converting(monkeypatch):
    """Cancelling a trial keeps its free days but must never turn into a charge — that
    IS what cancelling means. Even with a card on file AND consent for the entity, a
    cancelled trial expires at term end."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import changes, store

    cancelled = _Row(phase="scheduled_cancel")
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [cancelled])

    def _boom(*a, **k):
        raise AssertionError("a cancelled trial must never be billed")

    monkeypatch.setattr(changes, "issue_change", _boom)

    result = checkout.convert_or_expire_due_trials()

    assert result == {"converted": [], "expired": [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}],
                      "deferred": []}
    assert calls["writes"][-1][3]["phase"] == "expired"
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", False)]  # free days used up, access ends


def test_due_trial_without_a_customer_expires(monkeypatch):
    """A payer who never added a card has no Stripe customer at all — expire, don't crash."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import store

    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [_Row()])
    monkeypatch.setattr(store, "customer_id_for_user", lambda uid: None)

    result = checkout.convert_or_expire_due_trials()

    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]
    assert calls["access"] == [("e1", "PAYMENT_REQUEST", False)]


def test_one_failing_entity_does_not_stop_the_rest(monkeypatch):
    """The job is isolated per ENTITY — an entity bills as one change, so that's the
    unit of work. One entity's failure must not strand every other entity's due trial."""
    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    from billing.services import store

    bad, good = _Row(code="PAYMENT_REQUEST", entity_id="e1"), _Row(code="PETTY_CASH", entity_id="e2")
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [bad, good])

    # The first entity blows up; the second must still be processed.
    seen = {"n": 0}

    def flaky(uid):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("billing down")
        return None

    monkeypatch.setattr(store, "customer_id_for_user", flaky)

    result = checkout.convert_or_expire_due_trials()

    assert result["expired"] == [{"entity_id": "e2", "code": "PETTY_CASH"}]


# --- how a running trial presents ---------------------------------------------


def test_module_card_surfaces_an_app_level_trial(app, monkeypatch):
    """An app-level trial has no billing object behind it, so it must be surfaced from
    the module ROW. It must show as trialing and be cancellable — the Cancel button
    gates on ``can_cancel``, not on a subscription id it will never have."""
    import billing.services.entity_modules as modules_mod
    from billing.services import store

    pytest.importorskip("billing.services.cards")  # slice D

    now = datetime.now(UTC)

    class _Fn:
        id = "fn_pc"
        function_code = "PETTY_CASH"
        function_name = "Petty Cash"
        description = "d"
        is_active = True

    class _TrialRow:
        entity_id = "e1"
        function_code = "PETTY_CASH"
        payer_user_id = "u1"
        phase = "trial"
        trial_end = now + timedelta(days=20)
        app_access_until = now + timedelta(days=20)
        first_billed_at = None

    _FakeEntityFunction = fake_model([_Fn()])
    # the cards live in subscription/services/cards.py (modules.py is a re-export shim) and
    # bind EntityFunction themselves; patching the shim alone left the real query running
    # against whatever tables an earlier test had happened to create
    from billing.services import cards as cards_mod

    monkeypatch.setattr(cards_mod, "EntityFunction", _FakeEntityFunction)
    monkeypatch.setattr(modules_mod, "MODULE_CODES", ("PETTY_CASH",))
    monkeypatch.setattr(modules_mod, "_entity_customer_id", lambda eid: None)
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: [_TrialRow()])
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: None)
    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: None)
    monkeypatch.setattr(f"{_CATALOG}.available_plans", lambda: [])
    monkeypatch.setattr(
        "billing.services.stripe_client.customer_default_payment_method",
        lambda cid: None,
    )

    with app.app_context():
        card = modules_mod.get_module_cards("e1")[0]

    assert card["subscription_status"] == "trialing"  # surfaced from the row
    assert card["is_subscribed"] is True
    assert card["can_cancel"] is True  # the Cancel button appears
    assert card["trial_eligible"] is False  # the trial is already used
    assert card["needs_card"] is True  # no card -> it will expire, not convert


def test_a_retried_conversion_adopts_what_its_last_attempt_paid(monkeypatch):
    """The processor failed at the trial end, then the attempt it left turned out PAID. The
    next pass must find it (``_resolve_prior_conversions``) - and never raise a second charge."""
    from billing.services import billing_gateway, changes, store
    from billing.services.billing import Period

    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    monkeypatch.setattr(checkout, "_resolve_prior_conversions",
                        lambda *a, **k: Period(_ANCHOR, _PERIOD_END))

    def _second_charge(*_a, **_k):
        raise AssertionError("charged the conversion a second time")

    monkeypatch.setattr(changes, "issue_change", _second_charge)
    monkeypatch.setattr(billing_gateway, "void_invoice", lambda iid: None)
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [
        _Row(code="PAYMENT_REQUEST", trial_end=_NOW - timedelta(hours=2))])

    result = checkout.convert_or_expire_due_trials()

    assert result["converted"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]


def test_a_retried_conversion_whose_last_attempt_was_refused_expires(monkeypatch):
    from billing.services import changes, store

    checkout, calls = _setup(monkeypatch, now=_NOW, anchor=_ANCHOR)
    monkeypatch.setattr(checkout, "_resolve_prior_conversions", lambda *a, **k: "declined")
    monkeypatch.setattr(changes, "issue_change",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("charged again")))
    monkeypatch.setattr(store, "due_trials", lambda now, limit=None: [
        _Row(code="PAYMENT_REQUEST", trial_end=_NOW - timedelta(hours=2))])

    result = checkout.convert_or_expire_due_trials()

    assert result["expired"] == [{"entity_id": "e1", "code": "PAYMENT_REQUEST"}]
