"""The replay catalogue held to the design it reproduces — no database, no Stripe.

``manage.py replay_scenarios``'s catalogue is Figma 05·A: one company per module-status
combination the Subscription Summary draws, and a written reason for each frame it cannot
live. The command checks its own scripts at import (``_check_scripts``); these tests pin
that it does, and that every way a script can be wrong is REFUSED. Until 2026-09-28 a
mistyped event kind was skipped without a word and the scenario quietly rendered as
something else — nothing tested the harness at all.
"""

from __future__ import annotations

import pytest

from billing.management.commands import replay_scenarios as rs


def _without(frame: str) -> list:
    return [entry for entry in rs.CATALOGUE if not entry[0].startswith(f"{frame} ")]


def test_every_05a_frame_is_either_lived_or_explained():
    lived = [name.split(" ", 1)[0] for name, _script in rs.CATALOGUE]
    assert len(rs.FRAMES_05A) == 48
    assert len(lived) == len(set(lived)) == 38
    assert set(lived).isdisjoint(rs.UNREACHABLE_05A)
    assert set(lived) | set(rs.UNREACHABLE_05A) == set(rs.FRAMES_05A)


def test_the_real_catalogue_and_runs_pass_their_own_checks():
    rs._check_scripts()


@pytest.mark.parametrize(
    ("event", "refusal"),
    [
        (("trail", rs.PC, rs.RUNNING), "unknown event kind"),
        (("trial", "BILL", rs.RUNNING), "takes"),
        (("consent", rs.PC, rs.CONSENTED), "takes"),
        (("nominate", "a-card", rs.FAILING), "card spec"),
        (("buy", rs.PR, 3), "count back from 0"),
        (("rename", "  ", rs.FAILING), "1-255 characters"),
        (("rename", "x" * 256, rs.FAILING), "1-255 characters"),
    ],
)
def test_a_bad_event_is_refused(event, refusal):
    with pytest.raises(SystemExit, match=refusal):
        rs._check_script("t", [("M14 Lantern Bay Limited", [event])])


def test_a_frame_seeded_twice_is_refused():
    with pytest.raises(SystemExit, match="seeded twice"):
        rs._check_catalogue([*rs.CATALOGUE, rs.CATALOGUE[0]], rs.UNREACHABLE_05A)


def test_a_frame_dropped_without_a_reason_is_refused():
    with pytest.raises(SystemExit, match=r"neither \['M44'\]"):
        rs._check_catalogue(_without("M44"), rs.UNREACHABLE_05A)


def test_a_frame_both_lived_and_unreachable_is_refused():
    with pytest.raises(SystemExit, match=r"seeded AND unreachable \['M44'\]"):
        rs._check_catalogue(rs.CATALOGUE, {**rs.UNREACHABLE_05A, "M44": "no"})


def test_a_name_without_its_frame_code_is_refused():
    with pytest.raises(SystemExit, match="frame code"):
        rs._check_catalogue([("Scenario 8 - Both Active", [])], {})


def test_a_charge_before_the_anchor_is_refused():
    early = ("M14 Lantern Bay Limited", [("buy", rs.PR, rs.ANCHOR_BUY - 1)])
    with pytest.raises(SystemExit, match="before ANCHOR_BUY"):
        rs._check_catalogue([*_without("M14"), early], rs.UNREACHABLE_05A)


def test_a_card_change_before_any_purchase_is_refused():
    # Nothing carries onto the declining card, so it is never charged and never declines:
    # the company would render ACTIVE instead of SUSPENDED.
    bare = ("M61 Kingsmead Limited", [("nominate", rs.FAILING_CARD, rs.FAILING)])
    with pytest.raises(SystemExit, match="before buying"):
        rs._check_catalogue([*_without("M61"), bare], rs.UNREACHABLE_05A)


def test_both_billing_accounts_are_named_for_what_happens_to_them():
    # Unnamed, an account reads as its payer, so the two read the same in the 08-A picker.
    renames = {
        name.split(" ", 1)[0]: code
        for name, script in rs.CATALOGUE
        for kind, code, _when in script
        if kind == "rename"
    }
    assert renames == {"M44": rs.WORKING_ACCOUNT, "M66": rs.FAILING_ACCOUNT}
    assert (rs.WORKING_ACCOUNT, rs.FAILING_ACCOUNT) == ("Success", "Failed")


def test_a_rename_before_the_company_moves_is_refused():
    # Renamed at its buy, M66 would name the WORKING account "Failed" and then leave it.
    early = ("M66 Halcyon Labs Limited", [
        ("buy", rs.PC, rs.BOUGHT), ("buy", rs.PR, rs.BOUGHT),
        ("rename", rs.FAILING_ACCOUNT, rs.BOUGHT),
        ("nominate", rs.FAILING_CARD, rs.FAILING),
    ])
    with pytest.raises(SystemExit, match="before its last move"):
        rs._check_catalogue([*_without("M66"), early], rs.UNREACHABLE_05A)


def test_offsets_that_put_the_renewal_before_the_card_change_are_refused(monkeypatch):
    # R lands 28-31 days after ANCHOR_BUY; a card change on or after it misses the decline.
    monkeypatch.setattr(rs, "FAILING", rs.ANCHOR_BUY + 28)
    with pytest.raises(SystemExit, match="R falls after FAILING"):
        rs._check_catalogue(rs.CATALOGUE, rs.UNREACHABLE_05A)


def test_a_consent_before_the_lapsed_trials_end_is_refused(monkeypatch):
    # Consent is per entity: given before -20 it would CONVERT the lapsed sibling.
    monkeypatch.setattr(rs, "AFTER_LAPSE", rs.LAPSED + 29)
    with pytest.raises(SystemExit, match="nothing consents before"):
        rs._check_catalogue(rs.CATALOGUE, rs.UNREACHABLE_05A)


def test_a_declining_account_default_is_refused():
    # The payer's DEFAULT card charges nobody since the per-entity cards: scripted this way
    # the lifecycle's and L1's failures silently never happened.
    with pytest.raises(SystemExit, match="declines nothing"):
        rs._check_script("t", [("L1 Doomed Co", [("card", rs.CARD_FAIL, -1)])])


@pytest.mark.parametrize(
    "shape", ["LIFECYCLE", "LIFECYCLE_SPLIT", "L1_GIVES_UP", "CATALOGUE"],
)
def test_every_shape_meant_to_fail_puts_a_declining_card_under_a_group(shape):
    script = [event for _name, events in getattr(rs, shape) for event in events]
    assert any(
        kind in ("recard", "nominate") and rs._declines(code) for kind, code, _when in script
    ), f"{shape} no longer puts a declining card under any company"


@pytest.mark.parametrize(
    ("spec", "declines"),
    [(rs.CARD_FAIL, True), (rs.FAILING_CARD, True), (rs.CARD_GOOD, False), (rs.CARD_B, False)],
)
def test_a_tagged_failing_card_is_still_a_failing_card(spec, declines):
    assert rs._declines(spec) is declines
