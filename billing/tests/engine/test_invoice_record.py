"""What the gateway writes down about an invoice, and WHEN it writes it.

``subscription_invoice`` exists to make charging a period twice impossible rather than
unlikely, and that property lives entirely in the ordering: the row is claimed under a
unique index BEFORE the processor is called. A record written afterwards would leave the
window the table was built to close — charge succeeds, runner dies, nothing on disk, and
the next run bills the period again.

So these tests are mostly about sequence, not content.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

PERIOD_START = datetime(2027, 2, 8, 13, tzinfo=UTC)
PERIOD_END = datetime(2027, 3, 8, 13, tzinfo=UTC)


def _invoice(*lines):
    from billing.services.billing import Invoice, Line, Period

    return Invoice(
        currency="hkd",
        period=Period(PERIOD_START, PERIOD_END),
        lines=lines or (Line("e1", "Entity One", "Super Minty", 40000),),
    )


class _Record:
    def __init__(self, id="inv_local"):
        self.id = id


class _FakeStripe:
    """Just enough Stripe to get an invoice created, finalized and paid."""

    def __init__(self, log, *, status="paid", fail_on=None):
        self.log = log
        self.status = status
        self.fail_on = fail_on
        self.Invoice = self._Invoices(self)
        self.InvoiceItem = self._Items(self)

    def _step(self, name):
        self.log.append(name)
        if self.fail_on == name:
            raise RuntimeError(f"stripe failed at {name}")

    class _Invoices:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kw):
            self.outer._step("stripe.create")
            return {"id": "in_new", "status": "draft", "created": 1800000000}

        def finalize_invoice(self, invoice_id):
            self.outer._step("stripe.finalize")
            return {"id": invoice_id, "status": "open", "total": 40000,
                    "created": 1800000000,
                    "status_transitions": {"finalized_at": 1800000100}}

        def pay(self, invoice_id):
            self.outer._step("stripe.pay")
            transitions = {"finalized_at": 1800000100}
            # Stripe reports a paid_at only once it is actually paid; a decline leaves
            # the invoice open with no such transition.
            if self.outer.status == "paid":
                transitions["paid_at"] = 1800000200
            return {"id": invoice_id, "status": self.outer.status, "total": 40000,
                    "created": 1800000000, "status_transitions": transitions}

        def retrieve(self, invoice_id):
            self.outer._step("stripe.retrieve")
            return {"id": invoice_id, "status": "draft", "total": 40000,
                    "created": 1800000000}

    class _Items:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kw):
            self.outer._step("stripe.item")
            return {"id": "ii_1"}


def _wire(monkeypatch, *, reserved=_Record(), status="paid", fail_on=None,
          reserve_raises=None):
    """Mock the processor and the persistence seam; return (gateway, log, settled)."""
    from billing.services import billing_gateway, store

    log: list[str] = []
    settled: list[tuple] = []
    stripe = _FakeStripe(log, status=status, fail_on=fail_on)
    monkeypatch.setattr(billing_gateway, "get_stripe", lambda: stripe)
    monkeypatch.setattr(store, "user_for_customer", lambda cid: "u1")

    def _reserve(**kw):
        log.append("reserve")
        if reserve_raises is not None:
            raise reserve_raises
        return reserved

    monkeypatch.setattr(store, "reserve_invoice", _reserve)
    monkeypatch.setattr(
        store, "settle_invoice",
        lambda rid, **kw: log.append("settle") or settled.append((rid, kw)),
    )
    return billing_gateway, log, settled


# --- the ordering, which is the whole guard -----------------------------------


def test_the_local_row_is_claimed_BEFORE_the_processor_is_called(monkeypatch):
    """Recording afterwards would leave exactly the gap this table closes."""
    gateway, log, _settled = _wire(monkeypatch)

    gateway.issue_invoice("cus_1", _invoice(), idempotency_key="renewal-u1-20270208")

    assert log[0] == "reserve"
    assert log.index("reserve") < log.index("stripe.create")


def test_an_already_claimed_key_refuses_to_charge(monkeypatch):
    """The unique index came back saying this period is already being invoiced. Charging
    anyway is the double-bill the whole mechanism exists to prevent."""
    gateway, log, _settled = _wire(monkeypatch, reserved=None)

    with pytest.raises(gateway.BillingError):
        gateway.issue_invoice("cus_1", _invoice(), idempotency_key="renewal-u1-20270208")

    assert "stripe.create" not in log      # never reached the processor


def test_a_KEYED_invoice_that_cannot_be_recorded_is_not_charged(monkeypatch):
    """Fail closed. Without the row there is no guard, and an unguarded renewal is how a
    period gets billed twice."""
    gateway, log, _settled = _wire(monkeypatch, reserve_raises=RuntimeError("db down"))

    with pytest.raises(gateway.BillingError):
        gateway.issue_invoice("cus_1", _invoice(), idempotency_key="renewal-u1-20270208")

    assert "stripe.create" not in log


def test_an_UNKEYED_invoice_is_still_charged_if_it_cannot_be_recorded(monkeypatch):
    """The opposite call, and deliberately so. A mid-period purchase is guarded by the
    user waiting for the response, so the row is only bookkeeping — losing it must not
    cost the customer the thing they just bought."""
    gateway, log, _settled = _wire(monkeypatch, reserve_raises=RuntimeError("db down"))

    result = gateway.issue_invoice("cus_1", _invoice())

    assert result["status"] == "paid"
    assert "stripe.create" in log


# --- what ends up on the row ---------------------------------------------------


def test_the_external_id_is_stamped_as_soon_as_the_invoice_exists(monkeypatch):
    """The "claimed but never confirmed sent" window has to be the create call and
    nothing more — everything after it is recoverable by id."""
    gateway, log, settled = _wire(monkeypatch)

    gateway.issue_invoice("cus_1", _invoice(), idempotency_key="k")

    first_settle = settled[0]
    assert first_settle[1]["external_id"] == "in_new"
    # ...and it happened before any line item was attached.
    assert log.index("settle") < log.index("stripe.item")


def test_the_outcome_is_recorded_with_the_processors_own_timestamps(monkeypatch):
    """Stripe's times, not the local clock: a test-clock run bills a period years from
    "now", and a row stamped with wall time would not line up with the invoice."""
    gateway, _log, settled = _wire(monkeypatch)

    gateway.issue_invoice("cus_1", _invoice(), idempotency_key="k")

    final = settled[-1][1]
    assert final["status"] == "paid"
    assert final["total"] == 40000
    assert final["paid_at"] == datetime.fromtimestamp(1800000200, tz=UTC)
    assert final["issued_at"] == datetime.fromtimestamp(1800000100, tz=UTC)


def test_a_decline_is_recorded_as_what_it_is(monkeypatch):
    """An unpaid row is what stops the next run re-issuing it — see
    ``renewals._already_invoiced``."""
    gateway, _log, settled = _wire(monkeypatch, status="open")

    gateway.issue_invoice("cus_1", _invoice(), idempotency_key="k")

    assert settled[-1][1]["status"] == "open"
    assert settled[-1][1]["paid_at"] is None


def test_a_charge_that_succeeded_is_never_undone_by_a_failed_write(monkeypatch):
    """Raising here would tell the caller a collected payment failed, and that is how a
    customer gets charged a second time."""
    from billing.services import store

    gateway, _log, _settled = _wire(monkeypatch)
    calls = {"n": 0}

    def _boom(rid, **kw):
        calls["n"] += 1
        raise RuntimeError("db went away mid-settle")

    monkeypatch.setattr(store, "settle_invoice", _boom)

    result = gateway.issue_invoice("cus_1", _invoice(), idempotency_key="k")

    assert result["status"] == "paid"
    assert calls["n"] >= 1


def test_a_decline_is_recorded_as_OPEN_before_the_payment_is_even_attempted(monkeypatch):
    """``pay()`` RAISES on a declined card, so a status written only after it would never
    be written at all — the row would sit at "draft" for an invoice that is open and
    owed, and dunning chases what is open."""
    gateway, _log, settled = _wire(monkeypatch, fail_on="stripe.pay")

    with pytest.raises(gateway.BillingError):
        gateway.issue_invoice("cus_1", _invoice(), idempotency_key="k")

    assert settled[-1][1]["status"] == "open"


def test_a_recovered_invoice_stops_reading_as_unpaid(monkeypatch):
    """Dunning collects it days later. A row still saying "open" would make the table
    lie about the one case someone actually asks "was I charged?" of."""
    from billing.services import store

    gateway, _log, settled = _wire(monkeypatch)
    monkeypatch.setattr(store, "invoice_for_external_id", lambda eid: _Record("inv_9"))

    paid, reason = gateway.retry_invoice("in_old")

    assert (paid, reason) == (True, None)
    assert settled[-1][0] == "inv_9"
    assert settled[-1][1]["status"] == "paid"


def test_collecting_an_invoice_with_no_local_row_still_works(monkeypatch):
    """Invoices predating these tables have no row, and neither does one raised from the
    Stripe dashboard. Not finding one is an ordinary answer, not a failure."""
    from billing.services import store

    gateway, _log, settled = _wire(monkeypatch)
    monkeypatch.setattr(store, "invoice_for_external_id", lambda eid: None)

    paid, _reason = gateway.retry_invoice("in_ancient")

    assert paid is True
    assert settled == []


def test_nothing_is_reserved_for_an_invoice_with_no_billable_lines(monkeypatch):
    """A renewal with nothing to bill raises no invoice, so there is nothing to record
    and no key to claim — claiming one would block the period's real invoice."""
    from billing.services.billing import Line

    gateway, log, _settled = _wire(monkeypatch)

    result = gateway.issue_invoice(
        "cus_1", _invoice(Line("e1", "Entity One", "Super Minty", 0)), idempotency_key="k"
    )

    assert result == {}
    assert log == []


def test_the_recorded_total_is_what_was_SENT_not_what_was_computed(monkeypatch):
    """Zero-amount lines are dropped on the way out, so the reservation has to describe
    the invoice the processor was actually asked to collect."""
    from billing.services import store
    from billing.services.billing import Line

    gateway, _log, _settled = _wire(monkeypatch)
    reserved: list[dict] = []
    monkeypatch.setattr(
        store, "reserve_invoice", lambda **kw: reserved.append(kw) or _Record()
    )

    gateway.issue_invoice(
        "cus_1",
        _invoice(
            Line("e1", "Entity One", "Super Minty", 40000),
            Line("e2", "Entity Two", "Super Minty", 0),
        ),
        idempotency_key="k",
    )

    assert [line.entity_id for line in reserved[0]["lines"]] == ["e1"]
