"""Re-issuing an invoice the processor will no longer collect (``billing_gateway.refresh_invoice``).

Stripe cancels an invoice's payment once it has been confirmed too many times - "a variable
upper limit on how many times a PaymentIntent can be confirmed", ten declines in our account -
and a cancelled payment "can't be undone": the invoice keeps pointing at it and every later
attempt is refused ("This invoice can no longer be paid"). Before this, a customer who fixed
their card on day eleven could not pay us at all. The refresh re-issues the invoice as an
identical replacement, and dunning collects on that.

What is pinned:

* DETECTION. ``retry_invoice`` reports ``DEAD_PAYMENT`` for an invoice already dead, without
  paying it, AND after the failed call that crossed the limit (that call is the one that
  cancels); an ordinary decline keeps the processor's words.
* THE REPLACEMENT IS THE SAME INVOICE - lines, their order, memo, metadata plus ``replaces`` -
  on the card named, open and not yet charged.
* THE ORDER. The replacement is open before the original is voided, so the processor's open
  list never goes empty half-way (dunning would read that as "settled elsewhere" and restore
  access unpaid).
* THE KEY MOVES. The renewal run and the invoice list ask by the period key; after a refresh it
  names the replacement, and the retired key names no period at all.
* EVERY INTERRUPTION RESUMES to exactly one replacement.
* What cannot be re-issued automatically is refused - logged, nothing created.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.django_db

START = datetime(2027, 3, 6, 12, tzinfo=UTC)
END = datetime(2027, 4, 6, 12, tzinfo=UTC)
CUSTOMER = "cus_refresh"
DEAD_CARD = "pm_dead"
GOOD_CARD = "pm_good"
NO_LONGER = ("This invoice can no longer be paid. Consider voiding, marking as uncollectible, "
             "or marking as paid out of band instead.")
CROSSED = ("This PaymentIntent's payment_method could not be updated because it has a status "
           "of canceled.")


# --- a processor with a memory ---------------------------------------------------------------


class _StripeError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.user_message = message


class _Listing:
    def __init__(self, items):
        self._items = list(items)

    def auto_paging_iter(self):
        return iter(self._items)


class _Stripe:
    """Just enough Stripe for a refresh: invoices that keep their items, their payment and the
    keys they were created under, Stripe's confirmation limit, and a way to fail at any step -
    once, before or after its side effect ("after" is an answer lost on the way back)."""

    LIMIT = 10

    def __init__(self):
        self.invoices: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.keys: dict[str, object] = {}
        self.declining: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, str] = {}
        self.counter = 0
        self.Invoice = _Invoices(self)
        self.InvoiceItem = _Items(self)

    def next_id(self, prefix):
        self.counter += 1
        return f"{prefix}_{self.counter}"

    def step(self, name, when):
        if self.fail.get(name) == when:
            del self.fail[name]
            raise _StripeError(f"processor unreachable at {name} ({when})")

    def view(self, invoice_id, expand=None):
        invoice = dict(self.invoices[invoice_id])
        intent = invoice["payment_intent"]
        invoice["payment_intent"] = (
            dict(intent) if expand and "payment_intent" in expand else intent["id"]
        )
        invoice["metadata"] = dict(invoice["metadata"])
        return invoice

    def kill(self, invoice_id):
        """Stripe's confirmation limit, reached: the payment is cancelled for good."""
        self.invoices[invoice_id]["payment_intent"]["status"] = "canceled"

    def live(self, key, value):
        """Every invoice not voided that carries ``metadata[key] == value``."""
        return [i for i in self.invoices.values()
                if i["metadata"].get(key) == value and i["status"] != "void"]


class _Invoices:
    def __init__(self, stripe):
        self.s = stripe

    def create(self, **kw):
        s = self.s
        key = kw.pop("idempotency_key", None)
        s.step("create", "before")
        if key and key in s.keys:
            return s.view(s.keys[key])
        invoice_id = s.next_id("in")
        s.invoices[invoice_id] = {
            "id": invoice_id, "status": "draft", "customer": kw["customer"],
            "currency": kw["currency"], "description": kw.get("description"),
            "metadata": dict(kw.get("metadata") or {}),
            "default_payment_method": kw.get("default_payment_method"),
            "payment_intent": {"id": f"pi_{invoice_id}", "status": "requires_payment_method",
                               "confirms": 0},
            "created": 1800000000 + s.counter, "total": 0, "status_transitions": {},
        }
        s.items[invoice_id] = []
        if key:
            s.keys[key] = invoice_id
        s.calls.append(("create", invoice_id))
        s.step("create", "after")
        return s.view(invoice_id)

    def retrieve(self, invoice_id, expand=None, **kw):
        return self.s.view(invoice_id, expand)

    def finalize_invoice(self, invoice_id, **kw):
        s = self.s
        s.step("finalize", "before")
        invoice = s.invoices[invoice_id]
        if invoice["status"] == "draft":
            invoice["status"] = "open"
            invoice["total"] = sum(item["amount"] for item in s.items[invoice_id])
            invoice["status_transitions"] = {"finalized_at": 1800000100}
            s.calls.append(("finalize", invoice_id))
        s.step("finalize", "after")
        return s.view(invoice_id)

    def pay(self, invoice_id, payment_method=None, **kw):
        s = self.s
        invoice = s.invoices[invoice_id]
        intent = invoice["payment_intent"]
        s.calls.append(("pay", invoice_id))
        if intent["status"] == "canceled":
            raise _StripeError(NO_LONGER)
        if intent["confirms"] >= s.LIMIT:
            # The call that crosses the limit is the one that cancels - and it fails with an
            # invalid-request error, not a decline.
            intent["status"] = "canceled"
            raise _StripeError(CROSSED)
        intent["confirms"] += 1
        if (payment_method or invoice["default_payment_method"]) in s.declining:
            raise _StripeError("Your card was declined.")
        invoice["status"] = "paid"
        invoice["status_transitions"] = {**invoice["status_transitions"],
                                         "paid_at": 1800000200}
        intent["status"] = "succeeded"
        return s.view(invoice_id)

    def void_invoice(self, invoice_id, **kw):
        s = self.s
        s.step("void", "before")
        s.invoices[invoice_id]["status"] = "void"
        s.calls.append(("void", invoice_id))
        return s.view(invoice_id)

    def list(self, customer=None, status=None, **kw):
        s = self.s
        found = [
            s.view(invoice_id) for invoice_id, invoice in s.invoices.items()
            if (customer is None or invoice["customer"] == customer)
            and (status is None or invoice["status"] == status)
        ]
        return _Listing(sorted(found, key=lambda i: i["created"]))


class _Items:
    def __init__(self, stripe):
        self.s = stripe

    def create(self, **kw):
        s = self.s
        key = kw.pop("idempotency_key", None)
        s.step("item", "before")
        if key and key in s.keys:
            return s.keys[key]
        item = {
            "id": s.next_id("ii"), "invoice": kw["invoice"], "amount": kw["amount"],
            "currency": kw["currency"], "description": kw.get("description"),
            "metadata": dict(kw.get("metadata") or {}), "period": dict(kw.get("period") or {}),
        }
        s.items[kw["invoice"]].append(item)
        if key:
            s.keys[key] = item
        s.calls.append(("item", kw["invoice"]))
        s.step("item", "after")
        return item

    def list(self, invoice=None, **kw):
        # Newest first, as Stripe lists them.
        return _Listing(reversed(self.s.items[invoice]))


# --- the world: a payer, a company, and a renewal nobody can pay any more ----------------------


def _world(monkeypatch):
    from billing.services import billing_gateway, store
    from billing.tests.engine.conftest import make_entity, make_user, seed_currency

    stripe = _Stripe()
    stripe.declining.add(DEAD_CARD)
    monkeypatch.setattr(billing_gateway, "get_stripe", lambda: stripe)
    currency = seed_currency("HKD")
    payer = make_user(f"refresh-{uuid.uuid4().hex[:6]}@payer.test")
    entity = make_entity(payer, name="Refresh Co", currency=currency)
    account = store.create_billing_account(payer.id, DEAD_CARD, billing_company="Refresh Ltd")
    return stripe, payer, entity, account


def _renewal(stripe, payer, entity, account, *, key="renewal-refresh-20270306-g"):
    """Raise a renewal through the real gateway on a declining card: open, owed, one attempt."""
    from billing.services import billing_gateway
    from billing.services.billing import Invoice, Line, Period

    invoice = Invoice(
        currency="hkd",
        period=Period(START, END),
        lines=(
            Line(str(entity.id), "Refresh Co", "Petty Cash", 28000,
                 period_start=START, period_end=END, unit_amount=28000),
            # A cancellation's extension, already marked invoiced: exactly what a rebuild
            # would lose, and what the copy must carry.
            Line(str(entity.id), "Refresh Co", "Payment Request (access after cancellation)",
                 8129, period_start=START,
                 period_end=datetime(2027, 3, 27, 12, tzinfo=UTC)),
        ),
    )
    with pytest.raises(billing_gateway.BillingError):
        billing_gateway.issue_invoice(
            CUSTOMER, invoice, memo="Renewal", idempotency_key=key,
            metadata={"renewal_key": key, "billing_group": str(account.id)},
            payer_user_id=payer.id, payment_method=DEAD_CARD,
            billing_group_id=account.id,
        )
    return next(i for i, inv in stripe.invoices.items() if inv["metadata"]["renewal_key"] == key)


def _dead(monkeypatch):
    stripe, payer, entity, account = _world(monkeypatch)
    dead = _renewal(stripe, payer, entity, account)
    stripe.kill(dead)
    return stripe, dead


def _row(external_id):
    from billing.services import store

    return store.invoice_for_external_id(external_id)


# --- detection ------------------------------------------------------------------------------


def test_a_dead_invoice_is_reported_without_being_paid(monkeypatch):
    from billing.services import billing_gateway

    stripe, dead = _dead(monkeypatch)
    stripe.calls.clear()

    assert billing_gateway.retry_invoice(dead, GOOD_CARD) == (False, billing_gateway.DEAD_PAYMENT)
    assert ("pay", dead) not in stripe.calls
    assert _row(dead).status == "open"     # nothing recorded for it here


def test_the_call_that_crosses_the_limit_is_reported_dead(monkeypatch):
    """Ten declines, then the eleventh call cancels the payment - and fails with an
    invalid-request error. The state decides, not the error."""
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    dead = _renewal(stripe, payer, entity, account)          # decline 1
    answers = [billing_gateway.retry_invoice(dead, DEAD_CARD) for _ in range(10)]

    assert answers[:9] == [(False, "Your card was declined.")] * 9
    assert answers[9] == (False, billing_gateway.DEAD_PAYMENT)


def test_an_ordinary_decline_keeps_the_processors_words(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    dead = _renewal(stripe, payer, entity, account)

    assert billing_gateway.retry_invoice(dead, DEAD_CARD) == (False, "Your card was declined.")


def test_a_failed_recheck_keeps_the_original_reason(monkeypatch):
    """The re-read after a failure cannot turn a real reason into a guess."""
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    dead = _renewal(stripe, payer, entity, account)
    reads = {"n": 0}
    original = stripe.Invoice.retrieve

    def _retrieve(invoice_id, expand=None, **kw):
        reads["n"] += 1
        if reads["n"] > 1:
            raise _StripeError("network down")
        return original(invoice_id, expand)

    monkeypatch.setattr(stripe.Invoice, "retrieve", _retrieve)
    assert billing_gateway.retry_invoice(dead, DEAD_CARD) == (False, "Your card was declined.")


# --- the replacement -----------------------------------------------------------------------------


def test_the_replacement_is_the_same_invoice_on_the_card_named(monkeypatch):
    from billing.services import billing_gateway, store

    stripe, dead = _dead(monkeypatch)
    replacement = billing_gateway.refresh_invoice(dead, GOOD_CARD)

    new = stripe.invoices[replacement["id"]]
    old = stripe.invoices[dead]
    assert new["status"] == "open"                            # raised, not yet charged
    assert new["payment_intent"]["confirms"] == 0
    assert new["default_payment_method"] == GOOD_CARD
    assert new["description"] == old["description"]
    assert new["metadata"] == {**old["metadata"], "replaces": dead}
    shown = [(i["amount"], i["description"], i["metadata"], i["period"])
             for i in stripe.items[replacement["id"]]]
    assert shown == [(i["amount"], i["description"], i["metadata"], i["period"])
                     for i in stripe.items[dead]]             # same lines, same order

    was, now = _row(dead), _row(replacement["id"])
    assert (now.total, now.currency, now.memo, now.billing_group_id, now.payer_user_id,
            now.period_start, now.period_end) == (
        was.total, was.currency, was.memo, was.billing_group_id, was.payer_user_id,
        was.period_start, was.period_end)
    lines = sorted((line.entity_id, line.product_name, line.amount, line.kind,
                    line.period_start, line.period_end, line.unit_amount)
                   for line in store.invoice_lines(now.id))
    assert lines == sorted((line.entity_id, line.product_name, line.amount, line.kind,
                            line.period_start, line.period_end, line.unit_amount)
                           for line in store.invoice_lines(was.id))


def test_the_replacement_is_open_before_the_original_is_voided(monkeypatch):
    from billing.services import billing_gateway

    stripe, dead = _dead(monkeypatch)
    stripe.calls.clear()
    replacement = billing_gateway.refresh_invoice(dead, GOOD_CARD)

    assert stripe.calls.index(("finalize", replacement["id"])) < stripe.calls.index(("void", dead))
    assert stripe.invoices[dead]["status"] == "void"


def test_the_period_key_moves_to_the_replacement(monkeypatch):
    from billing.services import billing_gateway, renewals, store

    stripe, dead = _dead(monkeypatch)
    key = _row(dead).idempotency_key
    replacement = billing_gateway.refresh_invoice(dead, GOOD_CARD)

    assert store.invoice_for_key(key).external_id == replacement["id"]
    assert (_row(dead).status, _row(dead).idempotency_key) == ("void", f"{key}~{dead}")
    # The renewal run's "already invoiced?" answers for the document that can be paid.
    assert renewals._already_invoiced(CUSTOMER, key) == "open"
    billing_gateway.retry_invoice(replacement["id"], GOOD_CARD)
    assert renewals._already_invoiced(CUSTOMER, key) == "paid"


def test_the_retired_key_names_no_period(monkeypatch):
    """``~``, not ``-``: dunning reads ``<key>-...`` as the SAME period, so a retired key joined
    with a dash would go on claiming the period it handed over."""
    from billing.services import dunning, store

    key = "renewal-u1-20270306-g1"
    assert not dunning._names_period(store.retired_key(key, "in_1"), key)
    assert dunning._names_period(f"{key}-abcdef123456", key)      # a replay's scope still does


# --- interruptions ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "when"),
    [
        ("create", "before"),      # the processor never reached
        ("create", "after"),       # it created the invoice; the answer was lost
        ("item", "after"),         # half the items copied
        ("finalize", "before"),
        ("finalize", "after"),     # open at the processor, still a draft here
        ("supersede", "raise"),    # the key never moved
        ("void", "before"),        # the key moved; the original still open
    ],
)
def test_an_interrupted_refresh_finishes_with_exactly_one_replacement(monkeypatch, step, when):
    from billing.services import billing_gateway, store

    stripe, dead = _dead(monkeypatch)
    key = _row(dead).idempotency_key
    if step == "supersede":
        real = store.supersede_invoice
        failing = {"once": True}

        def _supersede(dead_id, replacement_id):
            if failing.pop("once", False):
                raise RuntimeError("database gone")
            return real(dead_id, replacement_id)

        monkeypatch.setattr(store, "supersede_invoice", _supersede)
    else:
        stripe.fail[step] = when

    try:
        billing_gateway.refresh_invoice(dead, GOOD_CARD)
    except Exception:
        pass
    # What the next attempt does: the dead invoice is still open at the processor with its
    # payment cancelled, so it walks back in through the same door.
    replacement = billing_gateway.refresh_invoice(dead, GOOD_CARD)

    assert stripe.live("replaces", dead) == [stripe.invoices[replacement["id"]]]
    assert stripe.invoices[replacement["id"]]["status"] == "open"
    assert len(stripe.items[replacement["id"]]) == len(stripe.items[dead])
    assert stripe.invoices[dead]["status"] == "void"
    assert store.invoice_for_key(key).external_id == replacement["id"]
    rows = [r for r in (store.invoice_for_key(store.refresh_key(dead)),
                        store.invoice_for_key(key)) if r is not None]
    assert {r.external_id for r in rows} == {replacement["id"]}   # one local row for it


def test_a_second_refresh_changes_nothing(monkeypatch):
    from billing.services import billing_gateway

    stripe, dead = _dead(monkeypatch)
    first = billing_gateway.refresh_invoice(dead, GOOD_CARD)
    creates = [c for c in stripe.calls if c[0] == "create"]

    again = billing_gateway.refresh_invoice(dead, GOOD_CARD)

    assert again["id"] == first["id"]
    assert [c for c in stripe.calls if c[0] == "create"] == creates


def test_a_replacement_of_a_replacement_is_found_from_the_first(monkeypatch):
    """The dead invoice's retired key follows the period key to whatever is live NOW."""
    from billing.services import billing_gateway, store

    stripe, dead = _dead(monkeypatch)
    first = billing_gateway.refresh_invoice(dead, GOOD_CARD)
    stripe.kill(first["id"])
    second = billing_gateway.refresh_invoice(first["id"], GOOD_CARD)

    assert store.replacement_of(_row(dead)).external_id == second["id"]
    assert store.replacement_of(_row(first["id"])).external_id == second["id"]


# --- what is refused ---------------------------------------------------------------------------


def test_an_invoice_with_no_local_record_is_not_refreshed(monkeypatch):
    """Raised from the dashboard, or before these tables: nothing says what it was sent with."""
    from billing.services import billing_gateway

    stripe, _payer, _entity, _account = _world(monkeypatch)
    orphan = stripe.Invoice.create(customer=CUSTOMER, currency="hkd", metadata={})["id"]
    stripe.Invoice.finalize_invoice(orphan)
    stripe.kill(orphan)
    stripe.calls.clear()

    assert billing_gateway.refresh_invoice(orphan, GOOD_CARD) is None
    assert [c for c in stripe.calls if c[0] == "create"] == []


def test_lines_that_disagree_with_the_processor_are_not_refreshed(monkeypatch):
    from billing.services import billing_gateway, store

    stripe, dead = _dead(monkeypatch)
    store.invoice_lines(_row(dead).id)[0].delete()
    stripe.calls.clear()

    assert billing_gateway.refresh_invoice(dead, GOOD_CARD) is None
    assert [c for c in stripe.calls if c[0] == "create"] == []


def test_a_payable_invoice_is_not_refreshed(monkeypatch):
    from billing.services import billing_gateway

    stripe, payer, entity, account = _world(monkeypatch)
    alive = _renewal(stripe, payer, entity, account)

    assert billing_gateway.refresh_invoice(alive, GOOD_CARD) is None
    assert stripe.invoices[alive]["status"] == "open"


def test_supersede_is_safe_to_run_twice(monkeypatch):
    from billing.services import billing_gateway, store

    stripe, dead = _dead(monkeypatch)
    key = _row(dead).idempotency_key
    replacement = billing_gateway.refresh_invoice(dead, GOOD_CARD)

    store.supersede_invoice(_row(dead).id, _row(replacement["id"]).id)

    assert store.invoice_for_key(key).external_id == replacement["id"]
    assert _row(dead).idempotency_key == f"{key}~{dead}"
