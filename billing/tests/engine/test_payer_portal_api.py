"""The payer portal read model and its gate.

Two things are worth pinning down here, and the table layout is neither.

* **The status a module gets is the one the entity's own card would give it.** The
  portal derives status from ``access.py`` rather than from the phase column, because a
  phase stays ``active`` until something writes it while access ends on a date that
  passes unattended. Every branch of that derivation is exercised below, including the
  two that look like duplicates and are not: a cancelled TRIAL keeps its free days, a
  cancelled PAID module keeps days it was charged for.

* **The gate is the payer, and there is no id to tamper with.** The endpoint takes no
  entity, filters on ``payer_user_id``, and therefore cannot be pointed at somebody
  else's companies. What the tests can check is the other half: that a token without a
  user is refused, and that an entity-scoped token is NOT refused — profile is reached
  with an unscoped one, so requiring the claim would break the only path the UI uses.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from .fakes import fake_model

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _row(code, phase, **fields):
    """A module row with the optional dates absent — set only what the branch needs."""
    return SimpleNamespace(
        entity_id=fields.pop("entity_id", "e1"),
        function_code=code,
        phase=phase,
        trial_end=fields.pop("trial_end", None),
        app_access_until=fields.pop("app_access_until", None),
        first_billed_at=fields.pop("first_billed_at", None),
        **fields,
    )


# --- Status derivation -------------------------------------------------------


def _state(row, *, paid_through=None):
    from billing.services import portal

    return portal._module_state(
        row, now=NOW, paid_through=paid_through, grace_days=7
    )


def test_no_row_reads_not_subscribed(app):
    from billing.services import portal

    state = _state(None)
    assert state["status"] == portal.STATUS_NOT_SUBSCRIBED
    assert state["date"] is None


def test_running_trial_shows_its_own_end_date(app):
    from billing.services import portal

    ends = NOW + timedelta(days=9)
    state = _state(_row("PAYMENT_REQUEST", "trial", trial_end=ends))
    assert state["status"] == portal.STATUS_TRIALING
    assert state["date_label"] == "Trial ends"
    assert state["date"] == ends


def test_active_module_shows_the_payers_paid_through(app):
    """"Next billing" is an ACCOUNT fact. One payer, one cycle, one date — so it comes
    from paid_through and not from anything on the module row."""
    from billing.services import portal

    paid_to = NOW + timedelta(days=20)
    state = _state(
        _row("PETTY_CASH", "active", first_billed_at=NOW - timedelta(days=40)),
        paid_through=paid_to,
    )
    assert state["status"] == portal.STATUS_ACTIVE
    assert state["date_label"] == "Next billing"
    assert state["date"] == paid_to


def test_cancelled_paid_module_expires_on_its_bought_days(app):
    from billing.services import portal

    until = NOW + timedelta(days=38)
    state = _state(
        _row(
            "PETTY_CASH",
            "scheduled_cancel",
            app_access_until=until,
            first_billed_at=NOW - timedelta(days=40),
        ),
        paid_through=NOW + timedelta(days=20),
    )
    assert state["status"] == portal.STATUS_CANCELLED
    assert state["date_label"] == "Expires"
    assert state["date"] == until


def test_cancelled_trial_keeps_its_free_days_and_says_so(app):
    """Same phase as the case above and a different sentence. Nothing was bought, so the
    date is the trial's own end — printing "Expires" against a paid-through it never had
    would invent a purchase."""
    from billing.services import portal

    ends = NOW + timedelta(days=5)
    state = _state(
        _row("PAYMENT_REQUEST", "scheduled_cancel", trial_end=ends, app_access_until=ends)
    )
    assert state["status"] == portal.STATUS_CANCELLED
    assert state["date_label"] == "Trial ends"
    assert state["date"] == ends


def test_past_due_is_never_folded_into_active(app):
    from billing.services import portal

    paid_to = NOW - timedelta(days=2)
    state = _state(
        _row("PAYMENT_REQUEST", "past_due", first_billed_at=NOW - timedelta(days=40)),
        paid_through=paid_to,
    )
    assert state["status"] == portal.STATUS_PAST_DUE
    assert state["date_label"] == "Access ends"
    # Paid-through plus the grace window, not paid-through itself.
    assert state["date"] == paid_to + timedelta(days=7)


def test_lapsed_paid_module_reads_ended_not_not_subscribed(app):
    """A phase left at ``active`` past its paid-through. It grants no access, and the
    honest line is the date it stopped: "not subscribed" would tell a customer they
    never bought something they did."""
    from billing.services import portal

    state = _state(
        _row("PETTY_CASH", "active", first_billed_at=NOW - timedelta(days=90)),
        paid_through=NOW - timedelta(days=30),
    )
    assert state["status"] == portal.STATUS_ENDED
    assert state["date_label"] == "Ended"
    # The day it really stopped: the paid period it ran out of, already behind us.
    assert state["date"] == NOW - timedelta(days=30)
    # The mirror image of the test below: a module that was PAID for must never be
    # described as an expired trial, which would deny the purchase outright.
    assert state["status"] != portal.STATUS_TRIAL_EXPIRED


def test_a_module_cut_off_early_prints_no_date_rather_than_the_accounts(app):
    """Terminated on the spot: ``cancelled``, access cut, no ``app_access_until`` of its own.
    The last date to fall back on was the billing ACCOUNT's paid-through - still ahead,
    because the account renews for its other companies - so the list read "Ended 18 Oct
    2026", a date to come under a word that says it is past. It prints none instead."""
    from billing.services import portal

    state = _state(
        _row("PETTY_CASH", "cancelled", first_billed_at=NOW - timedelta(days=60)),
        paid_through=NOW + timedelta(days=23),
    )
    assert state["status"] == portal.STATUS_ENDED
    assert state["date"] is None
    assert state["date_label"] is None

    # Its own access end, once passed, is still printed.
    until = NOW - timedelta(days=4)
    ran_out = _state(
        _row("PETTY_CASH", "cancelled", first_billed_at=NOW - timedelta(days=60),
             app_access_until=until),
        paid_through=NOW + timedelta(days=23),
    )
    assert (ran_out["date_label"], ran_out["date"]) == ("Ended", until)


def test_a_trial_that_ran_out_says_so_rather_than_just_ended(app):
    """Free days that expired, with nothing ever charged. ``first_billed_at`` is the only
    thing separating this from the lapsed PAID module above — they reach the same branch
    and need opposite words."""
    from billing.services import portal

    ended = NOW - timedelta(days=3)
    state = _state(_row("PAYMENT_REQUEST", "trial", trial_end=ended))

    assert state["status"] == portal.STATUS_TRIAL_EXPIRED
    assert portal.STATUS_LABELS[state["status"]] == "trial expired"
    assert state["date_label"] == "Trial ended"
    assert state["date"] == ended


# --- The assembled table -----------------------------------------------------


@pytest.fixture
def payer_portal(app, monkeypatch):
    """Drive ``build_payer_subscriptions`` off synthetic rows.

    Every model query and store read is replaced. What is under test is the assembly —
    grouping by entity, searching, sorting, paging — and going through real tables would
    need the second schema attached on the builder's own connection for no gain.
    """
    from billing.services import portal

    def _install(rows, entities, *, paid_through=None, anchor=None):
        by_id = {e["id"]: e for e in entities}

        monkeypatch.setattr(
            portal,
            "Entity",
            fake_model([
                SimpleNamespace(id=e["id"], name=e["name"], country_code=e.get("country"))
                for e in entities
            ]),
        )
        monkeypatch.setattr(
            portal,
            "EntityFunction",
            fake_model([
                SimpleNamespace(function_code="PETTY_CASH", function_name="Petty Cash"),
                SimpleNamespace(function_code="PAYMENT_REQUEST", function_name="Bill Payment"),
            ]),
        )
        monkeypatch.setattr(
            portal,
            "CountryInfo",
            fake_model([
                SimpleNamespace(country_code="HK", country_name_en="Hong Kong"),
                SimpleNamespace(country_code="SG", country_name_en="Singapore"),
            ]),
        )
        # One row with no id: it answers for whatever payer id the test asks about.
        monkeypatch.setattr(
            portal,
            "User",
            fake_model([SimpleNamespace(first_name="Harry", last_name="Kim", email="hk@example.com")]),
        )

        from billing.services import clock, policy
        from billing.services import store as sub_store

        monkeypatch.setattr(clock, "now", lambda: NOW)
        monkeypatch.setattr(
            policy, "current", lambda: SimpleNamespace(past_due_window_days=7)
        )
        monkeypatch.setattr(sub_store, "module_rows_for_payer", lambda _u: rows)
        monkeypatch.setattr(
            sub_store, "paid_through_for_user", lambda _u: paid_through
        )
        # The screen reads it PER COMPANY now — each is billed on the card it was put on,
        # and two rows of one payer can hold two different dates. Same value here: these
        # cases describe an account with one card.
        monkeypatch.setattr(
            sub_store, "paid_through_for_entity", lambda _e: paid_through
        )
        monkeypatch.setattr(
            sub_store, "billing_cycle_for_user", lambda _u: (anchor, "HKD")
        )
        return by_id

    return _install


def _entity(portal_result, name):
    return next(e for e in portal_result["entities"] if e["entity_name"] == name)


def test_every_canonical_module_gets_a_line_even_when_never_held(
    app, payer_portal
):
    """The design's Amazon row: Petty Cash live, Bill Payment "not subscribed". A module
    with no row still needs a line, or the table silently implies it does not exist."""
    from billing.services import portal

    payer_portal(
        rows=[
            _row(
                "PETTY_CASH",
                "active",
                entity_id="e1",
                first_billed_at=NOW - timedelta(days=40),
            )
        ],
        entities=[{"id": "e1", "name": "Amazon", "country": "HK"}],
        paid_through=NOW + timedelta(days=28),
    )

    with app.app_context():
        result = portal.build_payer_subscriptions("u1")

    row = _entity(result, "Amazon")
    assert [m["code"] for m in row["modules"]] == ["PETTY_CASH", "PAYMENT_REQUEST"]
    assert row["modules"][0]["status_label"] == "active"
    assert row["modules"][1]["status_label"] == "not subscribed"
    assert row["modules"][1]["date"] is None
    assert row["country"] == "Hong Kong"
    assert row["settings_path"] == "/entity/settings/module/e1"


def test_search_matches_the_status_words_on_screen(app, payer_portal):
    """A customer looking for what is winding down types "cancelled" — the word the badge
    shows — so the haystack includes status labels, not just names."""
    from billing.services import portal

    payer_portal(
        rows=[
            _row("PETTY_CASH", "active", entity_id="e1",
                 first_billed_at=NOW - timedelta(days=40)),
            _row("PETTY_CASH", "scheduled_cancel", entity_id="e2",
                 app_access_until=NOW + timedelta(days=10),
                 first_billed_at=NOW - timedelta(days=40)),
        ],
        entities=[
            {"id": "e1", "name": "Apple", "country": "HK"},
            {"id": "e2", "name": "Microsoft", "country": "HK"},
        ],
        paid_through=NOW + timedelta(days=28),
    )

    with app.app_context():
        result = portal.build_payer_subscriptions("u1", query="cancelled")

    assert [e["entity_name"] for e in result["entities"]] == ["Microsoft"]
    assert result["total"] == 1


def test_status_sort_puts_what_needs_a_decision_first(app, payer_portal):
    """Ascending by status is only useful one way round: overdue above healthy."""
    from billing.services import portal

    payer_portal(
        rows=[
            _row("PETTY_CASH", "active", entity_id="e1",
                 first_billed_at=NOW - timedelta(days=40)),
            _row("PETTY_CASH", "past_due", entity_id="e2",
                 first_billed_at=NOW - timedelta(days=40)),
            _row("PETTY_CASH", "trial", entity_id="e3",
                 trial_end=NOW + timedelta(days=5)),
        ],
        entities=[
            {"id": "e1", "name": "Apple", "country": "HK"},
            {"id": "e2", "name": "Microsoft", "country": "HK"},
            {"id": "e3", "name": "TSMC", "country": "SG"},
        ],
        paid_through=NOW + timedelta(days=28),
    )

    with app.app_context():
        result = portal.build_payer_subscriptions("u1", sort="status")

    assert [e["entity_name"] for e in result["entities"]] == [
        "Microsoft",
        "TSMC",
        "Apple",
    ]


def test_paging_reports_the_full_total_not_the_window(app, payer_portal):
    """"Showing 1–2 of 5" needs both numbers; returning len(window) as the total would
    make the footer claim the payer has two companies."""
    from billing.services import portal

    payer_portal(
        rows=[
            _row("PETTY_CASH", "trial", entity_id=f"e{i}",
                 trial_end=NOW + timedelta(days=i))
            for i in range(1, 6)
        ],
        entities=[
            {"id": f"e{i}", "name": f"Entity {i}", "country": "HK"}
            for i in range(1, 6)
        ],
    )

    with app.app_context():
        result = portal.build_payer_subscriptions("u1", page=2, per_page=2)

    assert result["total"] == 5
    assert result["pages"] == 3
    assert result["page"] == 2
    assert [e["entity_name"] for e in result["entities"]] == ["Entity 3", "Entity 4"]


def test_page_past_the_end_clamps_rather_than_emptying(app, payer_portal):
    """A stale ?page=9 after a search narrows the set must not show a blank table."""
    from billing.services import portal

    payer_portal(
        rows=[_row("PAYMENT_REQUEST", "trial", entity_id="e1", trial_end=NOW + timedelta(days=3))],
        entities=[{"id": "e1", "name": "Apple", "country": "HK"}],
    )

    with app.app_context():
        result = portal.build_payer_subscriptions("u1", page=9, per_page=2)

    assert result["page"] == 1
    assert len(result["entities"]) == 1


def _line(entity_id, entity_name, product, amount, kind="full"):
    """``kind`` is what the description reads to tell a renewal from an upgrade:
    ``full`` whole period, ``remaining`` prorated charge, ``unused`` credit."""
    return SimpleNamespace(
        entity_id=entity_id,
        entity_name=entity_name,
        product_name=product,
        amount=amount,
        kind=kind,
    )


def _invoice(ref, *, status="paid", total=40000, lines=(), issued=None, ident="i1",
             memo=None, paid=None):
    return SimpleNamespace(
        id=f"{ident}-0000-0000-0000-000000000000",
        external_id=ref,
        status=status,
        total=total,
        currency="HKD",
        issued_at=issued or NOW,
        created_at=issued or NOW,
        # Settlement is its own moment and defaults to "not yet": the tests below are about
        # description, money and status, and a fake that borrowed ``issued_at`` here would
        # have hidden the very bug the "Paid date" column shipped with.
        paid_at=paid,
        period_start=NOW - timedelta(days=30),
        period_end=NOW,
        memo=memo,
        # The service reads ``invoice.lines.all()`` (a related manager); a list with ``all``.
        lines=_Lines(lines),
    )


class _Lines(list):
    def all(self):
        return list(self)


@pytest.fixture
def payer_invoices(app, monkeypatch):
    """Drive ``build_payer_invoices`` off synthetic invoices."""
    import shared_models.models as model
    from billing.services import portal

    def _install(invoices):
        # Patched on ``shared_models.models`` because that is where ``build_payer_invoices``
        # imports it at call time. ``order_by`` on the fake keeps the order given.
        monkeypatch.setattr(model, "SubscriptionInvoice", fake_model(invoices))
        # Money formatting reaches for currency_info; the symbol is not what is under test.
        monkeypatch.setattr(portal, "_money", lambda amt, cur: f"HK${amt / 100:,.2f}")
        # Which failed row carries Retry payment reads the payer's cards and the engine's
        # rule, over real rows - test_invoice_retry.py's subject, not these rows' wording.
        monkeypatch.setattr(portal, "retryable_invoice_ids", lambda _user, _failed=None: set())
        return portal

    return _install


def test_filtering_by_entity_shows_that_entitys_share_not_the_invoice_total(
    app, payer_invoices
):
    """One invoice, two companies. Printing the whole total against one of them would
    overstate what that company cost by whatever the other one rode in on."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=68000,
                lines=[
                    _line("e1", "Acme", "Super Minty", 40000),
                    _line("e2", "Beta", "Petty Cash", 28000),
                ],
            )
        ]
    )

    with app.app_context():
        everything = portal.build_payer_invoices("u1")
        just_acme = portal.build_payer_invoices("u1", entity_id="e1")

    assert everything["invoices"][0]["amount"] == "HK$680.00"
    assert just_acme["invoices"][0]["amount"] == "HK$400.00"
    # ...and the description narrows with it, headline and detail together.
    assert everything["invoices"][0]["description"] == "Renewal · Super Minty, Petty Cash"
    assert everything["invoices"][0]["description_detail"] == (
        "7 Jul – 6 Aug 2026 · 2 entities"
    )
    assert just_acme["invoices"][0]["description"] == "Renewal · Super Minty"
    assert just_acme["invoices"][0]["description_detail"] == "7 Jul – 6 Aug 2026 · Acme"


def test_an_invoice_with_no_line_for_that_entity_drops_out(app, payer_invoices):
    portal = payer_invoices(
        [
            _invoice("in_1", lines=[_line("e1", "Acme", "Super Minty", 40000)]),
            _invoice("in_2", ident="i2", lines=[_line("e2", "Beta", "Petty Cash", 28000)]),
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1", entity_id="e2")

    assert result["total"] == 1
    assert result["invoices"][0]["reference"] == "in_2"


def _on(invoice, group_id):
    """An invoice raised by one billing account (``billing_group_id``); left unset it is a
    pre-accounts invoice, which carries none."""
    invoice.billing_group_id = group_id
    return invoice


@pytest.fixture
def two_accounts(monkeypatch):
    """The payer's accounts, oldest first — the order legacy invoices are attributed by."""
    from billing.services import store as sub_store

    monkeypatch.setattr(
        sub_store, "billing_groups_for_payer",
        lambda _u: [SimpleNamespace(id="g_old"), SimpleNamespace(id="g_new")],
    )


def test_an_account_shows_the_invoices_it_raised(app, payer_invoices, two_accounts):
    """08-B is ONE account's page. A pre-accounts invoice belongs to the OLDEST account —
    where dunning collects it — so the page and the collection agree whose debt it is."""
    portal = payer_invoices(
        [
            _on(_invoice("in_new", ident="i1"), "g_new"),
            _on(_invoice("in_old", ident="i2"), "g_old"),
            _invoice("in_legacy", ident="i3"),
        ]
    )

    with app.app_context():
        old = portal.build_payer_invoices("u1", account_id="g_old")
        new = portal.build_payer_invoices("u1", account_id="g_new")

    assert [r["reference"] for r in old["invoices"]] == ["in_old", "in_legacy"]
    assert [r["reference"] for r in new["invoices"]] == ["in_new"]
    assert (old["account_id"], new["account_id"]) == ("g_old", "g_new")


def test_someone_elses_account_shows_no_invoices(app, payer_invoices, two_accounts):
    """A filter, not a permission — but an account that is not the caller's must match
    nothing rather than fall through to everything."""
    portal = payer_invoices([_on(_invoice("in_1"), "g_new"), _invoice("in_2", ident="i2")])

    with app.app_context():
        result = portal.build_payer_invoices("u1", account_id="g_somebody_elses")

    assert result["invoices"] == []
    assert result["entity_options"] == []


def test_the_entity_filter_narrows_within_an_account(app, payer_invoices, two_accounts):
    portal = payer_invoices(
        [
            _on(_invoice("in_1", lines=[_line("e1", "Acme", "Petty Cash", 28000)]), "g_new"),
            _on(
                _invoice("in_2", ident="i2", lines=[_line("e2", "Beta", "Petty Cash", 28000)]),
                "g_new",
            ),
            _on(
                _invoice("in_3", ident="i3", lines=[_line("e2", "Beta", "Petty Cash", 28000)]),
                "g_old",
            ),
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1", account_id="g_new", entity_id="e2")

    assert [r["reference"] for r in result["invoices"]] == ["in_2"]
    assert [e["name"] for e in result["entity_options"]] == ["Acme", "Beta"]


def test_the_next_billing_date_is_ahead_of_the_anchor(app, payer_portal):
    """The anchor is the FIRST charge and never moves; the landing printed it as "Next
    Billing Date". ``next_billing`` is the boundary ahead — NOW is 6 Aug, anchored 28 Jun,
    so 28 Aug."""
    from billing.services import portal

    payer_portal(rows=[], entities=[], anchor=datetime(2026, 6, 28, 13, tzinfo=UTC))

    with app.app_context():
        billing = portal.build_payer_subscriptions("u1")["billing"]

    assert billing["anchor"] == "28 Jun 2026"
    assert billing["next_billing"] == "28 Aug 2026"
    assert billing["next_billing_iso"].startswith("2026-08-28")


def test_the_entity_dropdown_comes_from_history_not_current_subscriptions(
    app, payer_invoices
):
    """`entity_name` is a SNAPSHOT on the line. A company you have stopped paying for
    still has invoices and still belongs in the filter."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                lines=[
                    _line("e2", "Zeta", "Petty Cash", 28000),
                    _line("e1", "Acme", "Super Minty", 40000),
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert [e["name"] for e in result["entity_options"]] == ["Acme", "Zeta"]


def test_a_repeated_product_is_named_once(app, payer_invoices):
    """Two companies on the same plan is one product, not two identical words."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                lines=[
                    _line("e1", "Acme", "Super Minty", 40000),
                    _line("e2", "Beta", "Super Minty", 40000),
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "Renewal · Super Minty"


def test_an_upgrade_is_described_by_what_was_bought_not_what_was_credited(
    app, payer_invoices
):
    """An upgrade carries both halves of the change: the old plan credited and the new
    one charged. Naming both read "Petty Cash · Super Minty" — which describes the
    transition, and on one entity looks like two subscriptions. Naming only the new one
    lost the change entirely: three invoices in a month all read "Super Minty"."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=12000,
                lines=[
                    _line("e1", "Acme", "Super Minty", 40000, kind="remaining"),
                    _line("e1", "Acme", "Petty Cash", -28000, kind="unused"),
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "Upgrade · Petty Cash → Super Minty"
    assert result["invoices"][0]["description_detail"] == "7 Jul – 6 Aug 2026 · Acme"


def test_a_module_starting_mid_period_is_not_called_a_renewal(app, payer_invoices):
    """Prorated, nothing to credit — the customer bought something they did not have."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=10267,
                lines=[_line("e1", "Acme", "Payment Request", 10267, kind="remaining")],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "New subscription · Payment Request"


def test_an_extension_only_invoice_says_what_it_is(app, payer_invoices):
    """Access bought past a cancellation, and nothing else on the invoice. Reading
    "Petty Cash" here told a customer they had been billed for a subscription they had
    just cancelled."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=4000,
                lines=[
                    _line("e1", "Acme", "Petty Cash (access extension)", 4000,
                          kind="remaining")
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "Access extension · Petty Cash"
    # Not repeated in the detail: the headline already said it.
    assert "extension" not in result["invoices"][0]["description_detail"]


def test_a_renewal_that_only_collects_an_extension_says_what_it_is(app, payer_invoices):
    """A card whose last company was cancelled still renews - to collect the extension that
    company was promised access for. The renewal runner writes that line as a WHOLE-PERIOD
    one (``Line.kind``'s default), and a whole-period line alone used to make the invoice a
    "Renewal · Petty Cash": the renewal of a module nobody has any more."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=4000,
                lines=[
                    _line("e1", "Acme", "Petty Cash (access after cancellation)", 4000,
                          kind="full")
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "Access extension · Petty Cash"


def test_an_invoice_that_only_credits_still_names_its_product(app, payer_invoices):
    """Dropping negative lines must not leave a credit note with an empty description."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=-28000,
                lines=[_line("e1", "Acme", "Petty Cash", -28000, kind="unused")],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "Credit · Petty Cash"


def test_a_renewal_carrying_an_extension_is_still_a_renewal_and_says_so(
    app, payer_invoices
):
    """The whole-period lines are why the invoice exists; the extension rides along. It
    is called out in the detail because a cancellation charge is the one line on a
    renewal a customer is not expecting.

    The "(access after cancellation)" suffix stays off the headline — summarised into one
    cell it makes the same product look like two, and dedupe cannot see it."""
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=32000,
                lines=[
                    _line("e1", "Acme", "Petty Cash", 28000),
                    # Whole-period, as the renewal runner writes an extension it collects.
                    _line("e2", "Beta", "Petty Cash (access after cancellation)", 4000,
                          kind="full"),
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["description"] == "Renewal · Petty Cash"
    assert result["invoices"][0]["description_detail"] == (
        "7 Jul – 6 Aug 2026 · 2 entities · 1 access extension"
    )


def test_the_memo_travels_with_the_row_rather_than_being_rebuilt(app, payer_invoices):
    """The only place the arithmetic behind a prorated figure is written down, and it was
    written when the biller knew the figures. Re-deriving it from the columns is how the
    two start disagreeing."""
    memo = (
        "Acme changed from Petty Cash to Super Minty on 20 Jun 2026, with 16 of 30 days "
        "left in the period. Unused Petty Cash credited 149.33; Super Minty charged "
        "213.33 for the same days. Net 64.00."
    )
    portal = payer_invoices(
        [
            _invoice(
                "in_1",
                total=6400,
                memo=memo,
                lines=[
                    _line("e1", "Acme", "Super Minty", 21333, kind="remaining"),
                    _line("e1", "Acme", "Petty Cash", -14933, kind="unused"),
                ],
            )
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["memo"] == memo


def test_statuses_are_translated_from_the_processors_vocabulary(app, payer_invoices):
    portal = payer_invoices(
        [
            _invoice("in_1", status="paid", lines=[_line("e1", "A", "Petty Cash", 1)]),
            _invoice("in_2", ident="i2", status="uncollectible",
                     lines=[_line("e1", "A", "Petty Cash", 1)]),
            _invoice("in_3", ident="i3", status="open",
                     lines=[_line("e1", "A", "Petty Cash", 1)]),
        ]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert [i["status_label"] for i in result["invoices"]] == ["Paid", "Failed", "Unpaid"]


def test_a_reserved_invoice_still_has_something_to_show(app, payer_invoices):
    """`external_id` is NULL when a row was reserved but nothing was ever sent. A blank
    reference column would be worse than a local one."""
    reserved = _invoice(None, lines=[_line("e1", "Acme", "Petty Cash", 28000)])
    portal = payer_invoices([reserved])

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    reference = result["invoices"][0]["reference"]
    assert reference == f"#{reserved.id[:8].upper()}"
    assert reference.strip("#")


def test_the_payment_method_is_not_guessed(app, payer_invoices):
    """Which card paid an invoice is not stored, and the account's CURRENT card is a
    different question. None, so the UI can say "not recorded" instead of a wrong card."""
    portal = payer_invoices(
        [_invoice("in_1", lines=[_line("e1", "Acme", "Petty Cash", 28000)])]
    )

    with app.app_context():
        result = portal.build_payer_invoices("u1")

    assert result["invoices"][0]["payment_method"] is None


def test_when_it_was_paid_is_not_when_it_was_raised(app, payer_invoices):
    """The grid's "Paid date" column reads ``paid``, and for a while there was nothing to
    read: the row carried only ``date`` (``issued_at``), so the page printed the day the
    invoice went out under a heading that promised the day it settled. Two days apart here
    precisely so a row that borrowed the wrong one could not pass."""
    settled = NOW + timedelta(days=2)
    portal = payer_invoices(
        [_invoice("in_1", paid=settled, lines=[_line("e1", "Acme", "Petty Cash", 28000)])]
    )

    with app.app_context():
        row = portal.build_payer_invoices("u1")["invoices"][0]

    assert row["paid_iso"] == settled.isoformat()
    assert row["paid"] != row["date"]


def test_an_unsettled_invoice_has_no_paid_date_at_all(app, payer_invoices):
    """Open, failed, void - none of them settled, and the column says so with a dash. The
    one thing it must never do is fall back to ``issued_at`` and look answered."""
    portal = payer_invoices(
        [_invoice("in_1", status="open", lines=[_line("e1", "Acme", "Petty Cash", 28000)])]
    )

    with app.app_context():
        row = portal.build_payer_invoices("u1")["invoices"][0]

    assert row["paid"] is None
    assert row["paid_iso"] is None
    assert row["date"] is not None


def test_a_list_where_nothing_failed_reads_nothing_about_retrying(
    app, payer_invoices, monkeypatch
):
    """*Retry payment*'s rule reads the payer's cards and the billing policy. A list with no
    failed invoice has no row to put the button on, so it must not pay for that read - which
    is most payers, on every visit."""
    portal = payer_invoices([_invoice("in_1", lines=[_line("e1", "Acme", "Petty Cash", 28000)])])

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("the retry rule ran for a list with nothing failed")

    monkeypatch.setattr(portal, "retryable_invoice_ids", _must_not_run)

    with app.app_context():
        row = portal.build_payer_invoices("u1")["invoices"][0]

    assert row["retryable"] is False


def test_retrying_is_judged_over_every_failed_invoice_before_any_narrowing(
    app, payer_invoices, monkeypatch
):
    """The engine picks ONE invoice per card from all of that card's open bills, so the page
    asks with all of them too. Narrowed to one company first, the rule would see only that
    company's bills - and, with the current renewal filtered out, offer a mid-period charge
    the engine would refuse to collect. The rows it gets are the list's own: none re-read."""
    acme = _invoice("in_1", status="open", lines=[_line("e1", "Acme", "Petty Cash", 28000)])
    beta = _invoice("in_2", ident="i2", status="uncollectible",
                    lines=[_line("e2", "Beta", "Petty Cash", 28000)])
    paid = _invoice("in_3", ident="i3", lines=[_line("e2", "Beta", "Petty Cash", 28000)])
    portal = payer_invoices([acme, beta, paid])
    asked: list[list[str]] = []

    def _retryable(_user, failed=None):
        asked.append(sorted(inv.external_id for inv in failed))
        return {beta.id}

    monkeypatch.setattr(portal, "retryable_invoice_ids", _retryable)

    with app.app_context():
        rows = portal.build_payer_invoices("u1", entity_id="e2")["invoices"]

    assert asked == [["in_1", "in_2"]]
    assert [(r["reference"], r["retryable"]) for r in rows] == [("in_2", True), ("in_3", False)]


def test_a_currency_code_is_spaced_off_the_amount(app, monkeypatch):
    """"HKD57.54" scans as one token rather than a currency and an amount.

    ``currency_info`` records no symbol for plenty of currencies and the lookup falls back
    to the bare code, so this is the ordinary case rather than the edge one. Same rule as
    ``modules._fmt_money``: a code is spaced, a glyph is not.
    """
    import shared_models.models as models_db
    from billing.services import portal

    monkeypatch.setattr(models_db, "CurrencyInfo", fake_model([]))

    with app.app_context():
        assert portal._money(5754, "HKD") == "HKD 57.54"


def test_a_currency_glyph_is_not_spaced_off_the_amount(app, monkeypatch):
    """"HK$ 57.54" is not how a symbol is written.

    Patched on ``shared_models.models`` because that is where the lookup reads it: the
    symbol resolution lives in ``money.symbol``, which imports ``CurrencyInfo`` from there
    at call time, so patching the module attribute intercepts it.
    """
    import shared_models.models as models_db
    from billing.services import portal

    monkeypatch.setattr(models_db, "CurrencyInfo", fake_model([SimpleNamespace(symbol="HK$")]))

    with app.app_context():
        assert portal._money(5754, "HKD") == "HK$57.54"


# --- Change subscriber (read only) -------------------------------------------


def _member(user_id, first, last, email):
    return SimpleNamespace(
        id=user_id, first_name=first, last_name=last, email=email, username=email, approved=True
    )


@pytest.fixture
def subscriber_options(app, monkeypatch):
    """Drive ``build_subscriber_options`` off a synthetic entity, payer and members."""
    from billing.services import portal
    from billing.services import store as sub_store

    def _install(*, payer_id="u1", entity=("e1", "NVIDIA"), users=(), members=None):
        monkeypatch.setattr(
            portal,
            "Entity",
            fake_model([SimpleNamespace(id=entity[0], name=entity[1])] if entity else []),
        )
        monkeypatch.setattr(portal, "User", fake_model(list(users)))
        monkeypatch.setattr(sub_store, "payer_for_entity", lambda _e: payer_id)
        if members is not None:
            monkeypatch.setattr(
                portal, "_admin_candidates", lambda _e: [portal._person(m) for m in members]
            )
        return portal

    return _install


def test_only_the_payer_can_read_who_else_could_be_billed(app, subscriber_options):
    """The first payer-portal read with an entity id IN the request, so it is the first
    that could be pointed somewhere. Being the payer is the same test that gates changing
    the subscription at all."""
    portal = subscriber_options(payer_id="someone-else", users=[], members=[])

    with app.app_context():
        assert portal.build_subscriber_options("u1", "e1") is None


def test_an_entity_that_nobody_pays_for_is_not_readable(app, subscriber_options):
    portal = subscriber_options(payer_id=None, members=[])

    with app.app_context():
        assert portal.build_subscriber_options("u1", "e1") is None


def test_an_unknown_entity_is_not_readable(app, subscriber_options):
    portal = subscriber_options()

    with app.app_context():
        assert portal.build_subscriber_options("u1", "nope") is None


def test_the_current_payer_leads_the_list_and_the_rest_are_alphabetical(
    app, subscriber_options
):
    harry = _member("u1", "Harry", "Kim", "harry.kim@oliveandvine.com")
    others = [
        _member("u2", "Rebecca", "Park", "rebecca.park@oliveandvine.com"),
        _member("u3", "Jiwon", "Kim", "jiwon.kim@oliveandvine.com"),
        _member("u4", "Daniel", "Park", "daniel.park@oliveandvine.com"),
    ]
    portal = subscriber_options(users=[harry], members=[*others, harry])

    with app.app_context():
        result = portal.build_subscriber_options("u1", "e1")

    assert result["entity"] == {"entity_id": "e1", "entity_name": "NVIDIA"}
    assert result["current"]["name"] == "Harry Kim"
    assert [c["name"] for c in result["candidates"]] == [
        "Harry Kim",
        "Daniel Park",
        "Jiwon Kim",
        "Rebecca Park",
    ]
    assert [c["is_current"] for c in result["candidates"]] == [True, False, False, False]


def test_the_current_payer_is_listed_even_when_they_are_no_longer_an_admin(
    app, subscriber_options
):
    """A demoted payer is still the payer. Omitting them would contradict the banner
    above the list, which names them."""
    harry = _member("u1", "Harry", "Kim", "harry.kim@oliveandvine.com")
    portal = subscriber_options(
        users=[harry],
        members=[_member("u2", "Rebecca", "Park", "rebecca.park@oliveandvine.com")],
    )

    with app.app_context():
        result = portal.build_subscriber_options("u1", "e1")

    assert [c["name"] for c in result["candidates"]] == ["Harry Kim", "Rebecca Park"]
    assert result["candidates"][0]["is_current"] is True


def test_only_admins_are_offered(app, monkeypatch):
    """The business rule the screen states: a cashier cannot be made responsible for a
    company's bill, so offering one would be offering a choice that must be refused.
    ``super_admin`` outranks admin and is kept — dropping someone for being MORE senior
    than the bar reads as a missing user."""
    from billing.services import portal

    rows = [
        (_member("u1", "Harry", "Kim", "harry.kim@x.com"), "admin"),
        (_member("u2", "Rebecca", "Park", "rebecca.park@x.com"), "cashier"),
        (_member("u3", "Jiwon", "Kim", "jiwon.kim@x.com"), "accountant"),
        (_member("u4", "Daniel", "Park", "daniel.park@x.com"), "super_admin"),
        (_member("u5", "Sam", "Lee", "sam.lee@x.com"), "shop_manager"),
    ]

    import shared_models.models as models_db

    # The service reads the memberships (user_id, role) then the users by id.
    monkeypatch.setattr(
        models_db,
        "UserEntity",
        fake_model([
            SimpleNamespace(entity_id="e1", user_id=member.id, role=role, approved=True)
            for member, role in rows
        ]),
    )
    monkeypatch.setattr(portal, "User", fake_model([member for member, _role in rows]))

    with app.app_context():
        assert [c["email"] for c in portal._admin_candidates("e1")] == [
            "harry.kim@x.com",
            "daniel.park@x.com",
        ]


def test_losing_the_member_query_costs_the_list_and_not_the_page(app, monkeypatch):
    """Same posture as the rest of the portal's optional reads: answer nothing rather than
    fail the page. (Flask's copy also asserted a session rollback; autocommit needs none.)"""
    import shared_models.models as models_db
    from billing.services import portal

    class _Boom:
        objects = property(lambda self: (_ for _ in ()).throw(RuntimeError("the database is having a moment")))

    monkeypatch.setattr(models_db, "UserEntity", _Boom())

    with app.app_context():
        assert portal._admin_candidates("e1") == []


def test_sorting_by_next_billing_mixes_dated_and_undated_rows(app):
    """A row with no next date must sort beside rows that have one.

    This is the crash the SQLite-backed suite cannot otherwise see. The dates behind
    ``_next_date`` come from ``trial_end`` / ``paid_through`` / ``app_access_until``,
    all declared ``DateTime(timezone=True)`` — so Postgres hands them back AWARE while
    SQLite hands them back naive. The undated rows are parked on a ``_NO_DATE``
    sentinel, and while that sentinel was ``datetime.max`` (naive) the comparison blew
    up with "can't compare offset-naive and offset-aware datetimes" the moment one
    company had nothing scheduled and another did — a 500 on
    ``GET /api/me/subscriptions?sort=next_billing`` in production only.

    Aware datetimes are used here deliberately: they are what the real query returns.
    """
    from billing.services import portal

    dated = {"_next_date": datetime(2026, 9, 1, tzinfo=UTC)}
    undated = {"_next_date": None}
    rows = [undated, dated]

    rows.sort(key=portal._sort_key("next_billing"))

    assert rows == [dated, undated], "undated rows sort last, not first"
