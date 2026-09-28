"""Scenario 1's lifecycle, billed twice — one card, then two — to show the difference.

THE SAME TIMELINE BOTH WAYS. The shape is ``replay_scenarios.LIFECYCLE``, the four-month
account the harness has always used to exercise the machinery rather than the pixels:

    Steady Co     joins on the anchor and runs untouched — the control
    Growth Co     one module, then a second, so it is on the bundle price
    Churn Co      two modules
    Comeback Co   one module

and, on day 87, the card starts declining; on day 97 it is replaced. That replay drives
real Stripe on a test clock over four months. This drives the same shape through the
billing engine in memory, four renewals deep, because what is being compared is what the
engine PRODUCES — the invoices — and that needs no processor at all.

WHY THE "ORIGINAL" LIFECYCLE IS STILL RUNNABLE. Per-entity cards did not add a mode. The
original behaviour — one Stripe customer, one invoice per period covering every company,
one dunning clock — is exactly what this engine does when every company is on ONE card,
and the first test below pins that. It is not a re-implementation of the old code kept
alive for comparison; it is the same code, in the degenerate case the old model was.

WHAT THE COMPARISON IS FOR. `replay_scenarios` says of this scenario:

    "The card failure is deliberately account-level. Dunning lives on
     `user_stripe_customer`, and a renewal is ONE invoice covering every entity — so a
     declining card cannot fail for one company and succeed for another."

Both halves of that were true and are now conditional: they hold for an account whose
companies are all on one card, and are false the moment two are in play. These tests are
where that difference is stated in money — same total, different documents — rather than
in prose that has already gone stale once.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# Day 0 of the scenario. Pinned to a 30-day month for the same reason the harness pins
# ``anchor_30``: the arithmetic below is about which company is on which invoice, and a
# month that changes length underneath it would move the renewal dates without changing
# anything the tests are actually about.
ANCHOR = datetime(2027, 6, 8, 13, tzinfo=UTC)

PETTY_CASH = 28000
BILL = 28000
BUNDLE = 40000          # the pair, priced as the bundle rather than the sum
CURRENCY = "HKD"

# The four companies, and what each of them bills every month once it is running. Names
# and shapes from ``replay_scenarios.LIFECYCLE``; the module SETS are what the fourth
# month looks like, which is the steady state the renewals below charge.
COMPANIES = {
    "steady": ("S1 Steady Co", {"PETTY_CASH"}, PETTY_CASH),
    "growth": ("S1 Growth Co", {"PETTY_CASH", "PAYMENT_REQUEST"}, BUNDLE),
    "churn": ("S1 Churn Co", {"PETTY_CASH"}, PETTY_CASH),
    "comeback": ("S1 Comeback Co", {"PAYMENT_REQUEST"}, BILL),
}

MONTHLY_TOTAL = sum(amount for _n, _c, amount in COMPANIES.values())   # 124000

CARD_GOOD = "pm_good"
CARD_SECOND = "pm_second"
CARD_FAIL = "pm_failing"


# --- the world the engine runs in --------------------------------------------
#
# An in-memory stand-in for ``store`` plus the processor. Only what ``renewals`` and
# ``dunning`` actually read is implemented, and every one of them keeps its real
# signature — a fake that quietly accepts anything would let a call-site change slip
# through as a passing test.


class _Row:
    def __init__(self, entity_id, code, payer="u1", phase="active"):
        self.id = f"{entity_id}:{code}"
        self.entity_id = entity_id
        self.function_code = code
        self.payer_user_id = payer
        self.phase = phase
        self.first_billed_at = None
        self.billed_through = None
        self.extension_state = None
        self.extension_amount = None


class _Group:
    """One card, and the cycle that card owns."""

    def __init__(self, id, card, entities, paid_through):
        self.id = id
        self.payer_user_id = "u1"
        self.stripe_payment_method_id = card
        self.paid_through = paid_through
        self.dunning_started_at = None
        self.dunning_attempts = 0
        self.entities = list(entities)


class _Account:
    def __init__(self, anchor):
        self.user_id = "u1"
        self.anchor_at = anchor
        self.currency = CURRENCY
        self.stripe_customer_id = "cus_u1"
        # Left where the cutover left it: written by nothing, read as a fallback only.
        self.paid_through = anchor
        self.dunning_started_at = None
        self.dunning_attempts = 0


class _Plan:
    def __init__(self, amount):
        self.amount = amount
        self.display_name = "plan"
        self.currency = CURRENCY


class _Reserved:
    """The local ``subscription_invoice`` row, as ``renewals._already_invoiced`` reads it.

    Its presence is the double-billing guard — in production a UNIQUE index on
    ``idempotency_key``. Without it here the runner would re-raise the same period's
    invoice every day a card was declining, which is the exact failure the index exists
    to make impossible.
    """

    def __init__(self, record):
        self.id = record["id"]
        self.external_id = record["id"]
        self._record = record

    @property
    def status(self):
        return self._record["status"]


class _World:
    """The account, its cards, and every invoice the engine raised against it."""

    def __init__(self, layout, *, failing=(), cap=10):
        """``layout`` is {group_id: (card, [company keys])}; ``failing`` are the cards
        that decline while they are the group's card. Every group starts paid through the
        anchor, so the first renewal due is the period the anchor opens.

        ``cap`` is Stripe's confirmation limit - ten declines in our account - after which
        the next call cancels the invoice's payment for good (see ``retry_invoice``)."""
        self.account = _Account(ANCHOR)
        self.rows = [
            _Row(key, code)
            for key, (_name, codes, _amount) in COMPANIES.items()
            for code in sorted(codes)
        ]
        self.groups = [
            _Group(gid, card, entities, ANCHOR)
            for gid, (card, entities) in layout.items()
        ]
        self.failing = set(failing)
        self.invoices: list[dict] = []      # every document raised, in order
        self.blast_radius: set[str] = set()  # companies a failure took past due
        self.by_key: dict[str, dict] = {}
        self.counter = 0
        self.cap = cap
        self.refreshes: list[tuple[str, str]] = []   # (dead invoice, its replacement)
        self.presses: list[dict] = []                # what each Pay now answered

    # -- the shape of the account ---------------------------------------------

    def group(self, group_id):
        return next((g for g in self.groups if str(g.id) == str(group_id)), None)

    def entity_ids_in_group(self, group_id):
        group = self.group(group_id)
        return set(group.entities) if group else set()

    def billing_group_for_entity(self, entity_id, payer_user_id=None):
        return next(
            (g for g in self.groups if str(entity_id) in g.entities), None
        )

    def billing_groups_for_payer(self, user_id):
        return list(self.groups)

    def groups_with_billing(self):
        return list(self.groups)

    def groups_in_dunning(self):
        return sorted(
            (g for g in self.groups if g.dunning_started_at is not None),
            key=lambda g: g.dunning_started_at,
        )

    def module_rows_for_payer(self, user_id):
        return list(self.rows)

    def billing_plan_for_codes(self, codes):
        want = {str(c).upper() for c in codes}
        for _key, (_name, company_codes, amount) in COMPANIES.items():
            if company_codes == want:
                return _Plan(amount)
        return None

    # -- the writes the engine makes ------------------------------------------

    def set_group_paid_through(self, group_id, until):
        group = self.group(group_id)
        if group and (group.paid_through is None or until > group.paid_through):
            group.paid_through = until

    def begin_group_dunning(self, group_id, failed_at):
        group = self.group(group_id)
        if group and group.dunning_started_at is None:
            group.dunning_started_at = failed_at
            group.dunning_attempts = 0
        # The rows of THIS group's companies go past due — the containment, in the
        # place it actually happens.
        moved = set()
        for row in self.rows:
            if group and row.entity_id in group.entities and row.phase == "active":
                row.phase = "past_due"
                moved.add(row.entity_id)
        if moved:
            # THE BLAST RADIUS, captured at the moment of the failure. It cannot be read
            # afterwards: everything recovers by the end of the run, which is the point —
            # what differs between the two worlds is who was taken down on the way.
            self.blast_radius |= moved

    def record_group_dunning_attempt(self, group_id):
        group = self.group(group_id)
        group.dunning_attempts = int(group.dunning_attempts or 0) + 1
        return group.dunning_attempts

    def end_group_dunning(self, group_id, *, status="active"):
        group = self.group(group_id)
        if group is None:
            return
        group.dunning_started_at = None
        group.dunning_attempts = 0
        if status == "active":
            for row in self.rows:
                if row.entity_id in group.entities and row.phase == "past_due":
                    row.phase = "active"

    # -- the processor --------------------------------------------------------

    def issue_invoice(self, customer_id, invoice, **kw):
        """Charge the named card. A failing card leaves the invoice OPEN, as Stripe does."""
        self.counter += 1
        card = kw.get("payment_method")
        paid = card not in self.failing
        record = {
            "id": f"in_{self.counter}",
            "status": "paid" if paid else "open",
            "metadata": kw.get("metadata") or {},
            # Kept for the comparison rather than for the engine: which card, which
            # companies, how much.
            "card": card,
            "group": kw.get("billing_group_id"),
            "total": invoice.total,
            "period_start": invoice.period.start,
            "entities": sorted({line.entity_id for line in invoice.lines}),
            # The charge at issue is the payment's first confirmation.
            "confirms": 1,
            "dead": False,
        }
        self.invoices.append(record)
        key = (kw.get("metadata") or {}).get("renewal_key")
        if key:
            self.by_key[key] = _Reserved(record)
        return record

    def invoice_for_key(self, key):
        return self.by_key.get(key)

    def open_invoices(self, customer_id):
        return [i for i in self.invoices if i["status"] == "open"]

    def retry_invoice(self, invoice_id, payment_method=None):
        """Charge the card the caller names, falling back to the one on the document.

        Stripe's own behaviour, and the distinction the recovery turns on: the invoice
        still names the card that declined, so a retry that does not say otherwise keeps
        trying it — for the whole schedule, however many times the payer replaced it.
        """
        from billing.services import billing_gateway

        record = self.record(invoice_id)
        if record["dead"]:
            return False, billing_gateway.DEAD_PAYMENT
        if self.cap is not None and record["confirms"] >= self.cap:
            # Stripe's limit: the call after the tenth decline cancels the payment for good,
            # whatever card it names.
            record["dead"] = True
            return False, billing_gateway.DEAD_PAYMENT
        record["confirms"] += 1
        card = payment_method or record["card"]
        if card in self.failing:
            return False, "card_declined"
        record["status"] = "paid"
        record["paid_by"] = card
        return True, None

    def refresh_invoice(self, invoice_id, payment_method=None):
        """The re-issue, as ``billing_gateway.refresh_invoice`` leaves it: the same invoice
        raised again on the card named, the original void, the period key moved over."""
        dead = self.record(invoice_id)
        self.counter += 1
        replacement = {
            **dead,
            "id": f"in_{self.counter}",
            "status": "open",
            "card": payment_method or dead["card"],
            "metadata": {**dead["metadata"], "replaces": invoice_id},
            "confirms": 0,
            "dead": False,
        }
        replacement.pop("paid_by", None)
        dead["status"] = "void"
        self.invoices.append(replacement)
        key = dead["metadata"].get("renewal_key")
        if key:
            self.by_key[key] = _Reserved(replacement)
        self.refreshes.append((invoice_id, replacement["id"]))
        return replacement

    def record(self, invoice_id):
        return next(i for i in self.invoices if i["id"] == invoice_id)

    # -- reading the result ---------------------------------------------------

    def paid(self):
        return [i for i in self.invoices if i["status"] == "paid"]

    def per_period(self):
        """{period start: [invoice, ...]} — how many documents each month produced."""
        out: dict[datetime, list[dict]] = {}
        for invoice in self.invoices:
            out.setdefault(invoice["period_start"], []).append(invoice)
        return out

    def collected(self):
        return sum(i["total"] for i in self.paid())

    def phases(self):
        return {row.entity_id: row.phase for row in self.rows}


def _install(monkeypatch, world):
    from billing.services import (
        billing_gateway,
        dunning,
        policy,
        renewals,
        store,
    )

    for name in (
        "billing_group_for_entity", "billing_groups_for_payer", "groups_with_billing",
        "groups_in_dunning", "entity_ids_in_group", "module_rows_for_payer",
        "billing_plan_for_codes", "set_group_paid_through", "begin_group_dunning",
        "record_group_dunning_attempt", "end_group_dunning",
    ):
        monkeypatch.setattr(store, name, getattr(world, name))

    monkeypatch.setattr(store, "billing_group", world.group)
    monkeypatch.setattr(store, "accounts_with_billing", lambda: [world.account])
    monkeypatch.setattr(store, "customer_mapping_for_user", lambda uid: world.account)
    monkeypatch.setattr(
        store, "billing_cycle_for_user", lambda uid: (ANCHOR, CURRENCY)
    )
    monkeypatch.setattr(store, "pending_extensions_for_payer", lambda uid: [])
    monkeypatch.setattr(store, "mark_extensions_invoiced", lambda ids: 0)
    monkeypatch.setattr(store, "invoice_for_key", world.invoice_for_key)
    monkeypatch.setattr(
        renewals, "_entity_names",
        lambda ids: {str(i): COMPANIES[str(i)][0] for i in ids},
    )
    # Mail is not what is being compared, and it reaches for an app context.
    monkeypatch.setattr(renewals, "_notify_renewals", lambda issued, failed: None)
    monkeypatch.setattr(dunning, "_notify_dunning", lambda *a, **k: None)
    monkeypatch.setattr(dunning, "_restore_access", lambda uid: None)
    monkeypatch.setattr(policy, "current", lambda: policy.DEFAULTS)
    monkeypatch.setattr(billing_gateway, "issue_invoice", world.issue_invoice)
    monkeypatch.setattr(billing_gateway, "open_invoices", world.open_invoices)
    monkeypatch.setattr(billing_gateway, "retry_invoice", world.retry_invoice)
    monkeypatch.setattr(billing_gateway, "refresh_invoice", world.refresh_invoice)
    # Pay now asks whether there is a card to charge at all; the world's group always has one.
    from billing.services import stripe_client

    monkeypatch.setattr(stripe_client, "customer_default_payment_method", lambda cid: CARD_GOOD)
    return renewals, dunning


def _live(monkeypatch, world, months=4, card_events=(), presses=()):
    """Run the lifecycle: four monthly renewals, with dunning running every day between.

    Days, not months, because dunning is a daily job and the retry schedule is what
    decides whether a declining card recovers — stepping a month at a time would skip
    every retry slot and prove nothing about either world.

    ``card_events`` is ``(day, group_id, card)``, the harness's own ``("recard", CARD, day)``
    shape. Replacing a card is the group's ``stripe_payment_method_id`` changing and
    nothing else: the group keeps its cycle, its dunning clock and its companies, which
    is exactly what replacing a card does.

    ``presses`` is ``(day, group_id, card)``: the customer replaces the card AFTER that day's
    scheduled run and presses Pay now (``dunning.retry_now``); each answer lands in
    ``world.presses``.
    """
    from billing.services import clock
    from billing.services.billing import add_months

    renewals, dunning = _install(monkeypatch, world)
    schedule = {(d.date(), gid): card for d, gid, card in card_events}
    pressed: dict = {}
    for d, gid, card in presses:
        pressed.setdefault(d.date(), []).append((gid, card))
    day = ANCHOR
    # Whole PERIODS, not 30-day blocks: the periods are calendar months re-derived from
    # the anchor, so counting days would stop part-way through the fourth one on some
    # anchors and half-way into a fifth on others.
    end = add_months(ANCHOR, months)
    while day < end:
        for group in world.groups:
            card = schedule.get((day.date(), group.id))
            if card is not None:
                group.stripe_payment_method_id = card
        renewals.run_renewals(day, scope=["u1"], issue=True)
        dunning.collect_due(day)
        for gid, card in pressed.get(day.date(), ()):
            world.group(gid).stripe_payment_method_id = card
            monkeypatch.setattr(clock, "now", lambda at=day: at)
            world.presses.append(dunning.retry_now("u1", group_id=gid))
        day += timedelta(days=1)
    return world


# --- the original: everything on one card -------------------------------------


ONE_CARD = {"gA": (CARD_GOOD, list(COMPANIES))}

TWO_CARDS = {
    # Steady and Growth stay on the card they always had; Churn and Comeback are moved
    # onto a second one. The split is deliberately across the middle of the account so
    # both invoices carry more than one company.
    "gA": (CARD_GOOD, ["steady", "growth"]),
    "gB": (CARD_SECOND, ["churn", "comeback"]),
}


def test_the_original_lifecycle_is_unchanged_when_every_company_is_on_one_card(monkeypatch):
    """THE BACKWARDS-COMPATIBILITY PIN, and the control for everything below.

    One card is what every account looked like before this feature and what the backfill
    left behind. It must still produce exactly what it always did: ONE invoice a month,
    carrying every company, charged to that card.
    """
    world = _live(monkeypatch, _World(ONE_CARD))

    per_period = world.per_period()
    assert len(per_period) == 4, "four months, four renewals"
    for period_start, invoices in sorted(per_period.items()):
        assert len(invoices) == 1, f"{period_start:%b} raised more than one document"
        only = invoices[0]
        assert only["entities"] == sorted(COMPANIES), "every company on the one invoice"
        assert only["total"] == MONTHLY_TOTAL
        assert only["card"] == CARD_GOOD

    assert world.collected() == MONTHLY_TOTAL * 4


def test_the_same_lifecycle_on_two_cards_raises_two_invoices_a_month(monkeypatch):
    """THE DIFFERENCE, in documents. Same companies, same months, same money.

    The engine bills a CARD, not a payer, so the four lines that used to sit on one
    invoice are partitioned across two — each charged to its own card. Nothing is
    created, nothing is lost, and the split is by nomination rather than by anything the
    invoice itself decides.
    """
    world = _live(monkeypatch, _World(TWO_CARDS))

    per_period = world.per_period()
    assert len(per_period) == 4
    for period_start, invoices in sorted(per_period.items()):
        assert len(invoices) == 2, f"{period_start:%b} did not split"
        by_card = {i["card"]: i for i in invoices}
        assert set(by_card) == {CARD_GOOD, CARD_SECOND}
        assert by_card[CARD_GOOD]["entities"] == ["growth", "steady"]
        assert by_card[CARD_SECOND]["entities"] == ["churn", "comeback"]
        # The partition is exhaustive and disjoint — the property that makes "same
        # money, different documents" true rather than approximately true.
        assert (
            by_card[CARD_GOOD]["total"] + by_card[CARD_SECOND]["total"]
            == MONTHLY_TOTAL
        )

    assert world.collected() == MONTHLY_TOTAL * 4, "the account pays the same as before"


def test_two_invoices_for_one_period_do_not_collide_on_the_key(monkeypatch):
    """The guard that makes the split possible at all.

    Under the old payer-and-period key the second card's invoice is refused as a
    double-bill of the first, and one card's companies are simply never charged — the
    double-billing guard causing free service.
    """
    world = _live(monkeypatch, _World(TWO_CARDS), months=1)

    keys = [i["metadata"]["renewal_key"] for i in world.invoices]
    assert len(keys) == len(set(keys)) == 2
    assert all(key.startswith("renewal-u1-") for key in keys)
    # The group is what makes them differ; the payer and the period are identical.
    assert keys[0].rsplit("-", 1)[0] == keys[1].rsplit("-", 1)[0]


# --- the card that declines ---------------------------------------------------
#
# Scenario 1's card fails on day 87 and is replaced on day 97, which straddles the day-92
# renewal. `replay_scenarios` states the consequence as a fact about the design: "a
# declining card cannot fail for one company and succeed for another". That was true of
# an account with one card. These two tests are the before and after of it.


FAILS_ON = ANCHOR + timedelta(days=87)
FIXED_ON = ANCHOR + timedelta(days=97)


def _breaks_and_is_fixed(group_id):
    """Scenario 1's card events, against one group: dies on day 87, replaced on day 97.

    Straddling the day-92 renewal on purpose — that renewal is the one that has to fail,
    and the ten days of retries before the replacement are what make the recovery a
    recovery rather than a card that happened to work first time.
    """
    return ((FAILS_ON, group_id, CARD_FAIL), (FIXED_ON, group_id, CARD_GOOD))


def test_on_one_card_a_decline_takes_every_company_down_with_it(monkeypatch):
    """The original behaviour, and the reason for the change.

    Steady Co did nothing, owes nothing anybody disputes, and is billed on the same
    invoice as everyone else — so when that invoice fails, it goes past due too.
    """
    world = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, world, card_events=_breaks_and_is_fixed("gA"))

    # The document that failed carried EVERY company — there was no other it could have
    # been on — and the total was the whole account's.
    declined = [i for i in world.invoices if i.get("paid_by")]
    assert declined, "the card never actually declined"
    assert declined[0]["entities"] == sorted(COMPANIES)
    assert declined[0]["total"] == MONTHLY_TOTAL
    # ...so every company went past due, Steady Co included. It joined first, changed
    # nothing for four months, and lost access to a card failure on somebody else's line.
    assert world.blast_radius == set(COMPANIES)


def test_on_two_cards_the_decline_stops_at_the_card_that_declined(monkeypatch):
    """THE HEADLINE DIFFERENCE. Same failure, same days, half the blast radius.

    The companies on the good card are billed, paid and left active throughout. Only the
    ones on the failing card go past due — and when the card is replaced, dunning
    collects on the retry schedule and they come back.
    """
    layout = {
        "gA": (CARD_GOOD, ["steady", "growth"]),
        "gB": (CARD_GOOD, ["churn", "comeback"]),
    }
    world = _World(layout, failing={CARD_FAIL})
    _live(monkeypatch, world, card_events=_breaks_and_is_fixed("gB"))

    good = [i for i in world.invoices if i["entities"] == ["growth", "steady"]]
    assert len(good) == 4 and all(i["status"] == "paid" for i in good), (
        "the healthy card kept billing and paying throughout"
    )
    # THE DIFFERENCE, stated as who was taken down: two companies, not four. Steady Co
    # was never touched by a failure that was not its own.
    assert world.blast_radius == {"churn", "comeback"}
    # And the pair on the replaced card recovered — dunning collected once the card was
    # good again, inside the retry schedule — so by the end nothing is outstanding.
    assert world.phases() == {key: "active" for key in COMPANIES}
    assert not [i for i in world.invoices if i["status"] != "paid"]


def test_the_two_worlds_collect_the_same_money_through_the_same_failure(monkeypatch):
    """Containment is about WHO is affected, not about how much is collected.

    A split account that recovers pays exactly what a single-card account that recovers
    pays. If these ever diverge, the difference is money — either a period billed twice
    or one given away — and no amount of nicer failure behaviour is worth that.
    """
    one = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, one, card_events=_breaks_and_is_fixed("gA"))

    two = _World(
        {"gA": (CARD_GOOD, ["steady", "growth"]),
         "gB": (CARD_GOOD, ["churn", "comeback"])},
        failing={CARD_FAIL},
    )
    _live(monkeypatch, two, card_events=_breaks_and_is_fixed("gB"))

    assert one.collected() == two.collected()
    assert one.collected() == MONTHLY_TOTAL * 4


def test_a_replaced_card_is_what_the_retry_actually_charges(monkeypatch):
    """The regression this lifecycle caught, pinned.

    ``issue_invoice`` names the card ON the invoice, so a retry that does not say
    otherwise re-charges the card that declined — for the whole schedule, however many
    times the payer replaced it. Before invoices named a card at all, ``Invoice.pay``
    fell back to the customer default and replacing it healed the account by accident.
    Dunning now passes the group's CURRENT card, which is what makes recovery possible.
    """
    world = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, world, card_events=_breaks_and_is_fixed("gA"))

    recovered = [i for i in world.invoices if i.get("paid_by")]
    assert recovered, "nothing had to be retried"
    # Raised against the dead card, settled by the replacement.
    assert recovered[0]["card"] == CARD_FAIL
    assert recovered[0]["paid_by"] == CARD_GOOD


# --- the report ---------------------------------------------------------------


def test_report_the_difference(monkeypatch):
    """The side-by-side, printed. Run with ``pytest -s -k report_the_difference``.

    The tests above state the difference as constraints; this is for reading it. It still
    asserts, because a report nobody checks is a report that quietly stops being true —
    but what it asserts is the same thing it prints.
    """
    one = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, one, card_events=_breaks_and_is_fixed("gA"))
    two = _World(
        {"gA": (CARD_GOOD, ["steady", "growth"]),
         "gB": (CARD_GOOD, ["churn", "comeback"])},
        failing={CARD_FAIL},
    )
    _live(monkeypatch, two, card_events=_breaks_and_is_fixed("gB"))

    lines = ["", "Scenario 1, four months — the same lifecycle billed two ways", ""]
    for label, world in (("ONE CARD (the original)", one), ("TWO CARDS", two)):
        lines.append(f"  {label}")
        for period_start, invoices in sorted(world.per_period().items()):
            for invoice in invoices:
                names = ", ".join(COMPANIES[e][0] for e in invoice["entities"])
                settled = invoice.get("paid_by")
                how = f" (retried on {settled})" if settled else ""
                lines.append(
                    f"    {period_start:%d %b}  {invoice['card']:<10} "
                    f"{invoice['total'] / 100:>9,.2f}  {names}{how}"
                )
        lines.append(
            f"      {len(world.invoices)} invoice(s), "
            f"{world.collected() / 100:,.2f} collected, "
            f"{len(world.blast_radius)} of {len(COMPANIES)} companies "
            f"past due at the failure"
        )
        lines.append("")
    print("\n".join(lines))

    # Same money, different documents, different blast radius — the whole comparison.
    assert one.collected() == two.collected() == MONTHLY_TOTAL * 4
    assert len(one.invoices) < len(two.invoices)
    assert one.blast_radius == set(COMPANIES)
    assert two.blast_radius == {"churn", "comeback"}


# --- Stripe's confirmation limit ------------------------------------------------------------
#
# Stripe cancels an invoice's payment once it has been confirmed too many times - ten declines
# in our account - and it can never be paid after that. ``_World`` counts: the call after an
# invoice's tenth decline kills it. Scenario 1's card is fixed on day 97, five retries in, so
# none of the tests above ever reach the limit; these do. Before the refresh, a card fixed on
# the tenth day or later could never pay: every retry was refused until the give-up.


def _third_renewal():
    """The day-92 renewal Scenario 1's card has to break."""
    from billing.services.billing import add_months

    return add_months(ANCHOR, 3)


def _dies_and_is_fixed(group_id, days_after_renewal):
    return ((FAILS_ON, group_id, CARD_FAIL),
            (_third_renewal() + timedelta(days=days_after_renewal), group_id, CARD_GOOD))


@pytest.mark.parametrize("late", [10, 11, 12, 13])
def test_a_card_fixed_after_stripe_gave_up_is_still_collected(monkeypatch, late):
    """THE POINT. The renewal plus nine retries are ten declines; the tenth retry finds the
    invoice dead, re-issues it, and from then on the replacement is what is charged - so a
    card fixed on day ten, eleven, twelve or thirteen still pays."""
    world = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, world, card_events=_dies_and_is_fixed("gA", late))

    assert len(world.refreshes) == 1
    dead, replacement = world.refreshes[0]
    assert world.record(dead)["status"] == "void"
    assert world.record(replacement)["paid_by"] == CARD_GOOD
    assert world.phases() == {key: "active" for key in COMPANIES}
    # Nothing given away, nothing charged twice: the same four months as a card fixed in time.
    assert world.collected() == MONTHLY_TOTAL * 4


def test_a_card_never_fixed_is_refreshed_once_and_abandoned_at_give_up(monkeypatch):
    """One replacement, not one a day - and at the give-up it is left open, exactly as the
    original was before."""
    world = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, world, card_events=((FAILS_ON, "gA", CARD_FAIL),))

    assert len(world.refreshes) == 1
    dead, replacement = world.refreshes[0]
    assert world.record(dead)["status"] == "void"
    assert world.record(replacement)["status"] == "open"
    assert world.record(replacement)["confirms"] == 4        # retries ten to thirteen


def test_a_card_fixed_before_stripe_gave_up_never_refreshes(monkeypatch):
    """Scenario 1 as it always was: fixed five retries in, well inside the limit."""
    world = _World(ONE_CARD, failing={CARD_FAIL})
    _live(monkeypatch, world, card_events=_breaks_and_is_fixed("gA"))

    assert world.refreshes == []


def test_pay_now_after_stripe_gave_up_refreshes_and_collects(monkeypatch):
    """The customer fixes the card after the tenth decline and presses Pay now that same day:
    the press is the call that finds the invoice dead, and it collects on the replacement."""
    world = _World(ONE_CARD, failing={CARD_FAIL})
    press = _third_renewal() + timedelta(days=9)     # after the renewal and nine retries
    _live(monkeypatch, world, card_events=((FAILS_ON, "gA", CARD_FAIL),),
          presses=((press, "gA", CARD_GOOD),))

    assert [answer["status"] for answer in world.presses] == ["paid"]
    assert world.presses[0]["refreshed"] == world.refreshes[0][0]
    assert world.phases() == {key: "active" for key in COMPANIES}
