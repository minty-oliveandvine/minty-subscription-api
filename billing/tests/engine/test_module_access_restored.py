"""``sweep_expired_module_access`` reconciles in BOTH directions.

The sweep used to be a one-way ratchet. Its candidate set was "modules currently
switched on" — the only ones it could need to switch off — so a module it revoked left
that set for good, and nothing in the codebase ever switched one back on. An account
that went past due had its access revoked on the grace boundary, paid its balance, got
``paid_through`` advanced and its phases moved back to active by ``end_group_dunning``, and
stayed locked out of every page it was still being charged for.

These pin the restore direction and, just as importantly, its LIMITS: restoring is
narrower than revoking, because switching something on by mistake hands out access
nobody paid for.

Monkeypatched in the style of ``test_module_card_lapsed`` and for the same reason —
the conftest ``app`` fixture re-imports project modules mid-session, so the module
object captured at import time is not the one the code under test calls.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .fakes import fake_model

UTC = UTC


class _Fn:
    id = "fn_pc"
    function_code = "PETTY_CASH"


_FakeEntityFunction = fake_model([_Fn()])
# No module is switched on anywhere — the state the ratchet used to leave behind.
_FakeEntityFunctionMap = fake_model([])


# No entities are mid-onboarding.
_FakeEntity = fake_model([])


class _Row:
    entity_id = "e1"
    function_code = "PETTY_CASH"
    payer_user_id = "u1"
    trial_end = None
    app_access_until = None

    def __init__(self, phase="active", billed=True):
        self.phase = phase
        self.first_billed_at = datetime.now(UTC) - timedelta(days=60) if billed else None


def _sweep(app, monkeypatch, row, *, paid_through):
    """Run the sweep over one entity whose module is OFF, and report what it wrote."""
    import billing.services.entity_modules as modules_mod
    from billing.services import access_sweep, store

    writes: list[tuple] = []

    # The sweep binds the three models itself (``access_sweep`` module globals).
    monkeypatch.setattr(access_sweep, "EntityFunction", _FakeEntityFunction)
    monkeypatch.setattr(access_sweep, "EntityFunctionMap", _FakeEntityFunctionMap)
    monkeypatch.setattr(access_sweep, "Entity", _FakeEntity)
    monkeypatch.setattr(modules_mod, "MODULE_CODES", ("PETTY_CASH",))
    monkeypatch.setattr(modules_mod, "_enabled_state", lambda eid: {"PETTY_CASH": False})
    monkeypatch.setattr(
        modules_mod,
        "set_entity_module",
        lambda eid, code, enabled, *, actor: writes.append((eid, code, enabled, actor)),
    )
    monkeypatch.setattr(store, "entity_ids_with_billed_modules", lambda user_id=None: {"e1"})
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: [row])
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: paid_through)

    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: paid_through)
    with app.app_context():
        return access_sweep.sweep_expired_module_access(), writes


def test_a_paid_module_switched_off_is_restored_once_its_period_is_paid(app, monkeypatch):
    """The bug: past due revoked access, the balance was paid, access never came back."""
    summary, writes = _sweep(
        app, monkeypatch, _Row(), paid_through=datetime.now(UTC) + timedelta(days=24)
    )

    assert summary["restored"] == [{"entity_id": "e1", "code": "PETTY_CASH"}]
    assert summary["disabled"] == []
    assert writes == [("e1", "PETTY_CASH", True, "subscription")]


def test_a_module_off_is_visible_to_the_sweep_at_all(app, monkeypatch):
    """The ratchet, stated directly.

    Every module is switched off, so the old candidate query returns nothing and the
    sweep has no work by construction. Finding the entity anyway is what makes any
    restoration possible.
    """
    _summary, writes = _sweep(
        app, monkeypatch, _Row(), paid_through=datetime.now(UTC) + timedelta(days=1)
    )

    assert writes, "an entity with no enabled module never reached the sweep"


def test_a_paid_module_whose_period_is_over_is_not_restored(app, monkeypatch):
    """Restoring is gated on the SAME predicate that revokes, so a lapsed one stays off."""
    summary, writes = _sweep(
        app, monkeypatch, _Row(), paid_through=datetime.now(UTC) - timedelta(days=90)
    )

    assert summary["restored"] == []
    assert writes == []


def test_a_trial_switched_off_by_hand_is_left_alone(app, monkeypatch):
    """A trial is FREELY toggleable, so an off flag on one may be a deliberate choice.

    A paid module cannot be switched off by hand at all — ``set_entity_module`` refuses
    it — so an off flag there can only have come from the sweep, which is what makes
    restoring it safe. Restoring a trial would instead overturn the customer.
    """
    summary, writes = _sweep(
        app,
        monkeypatch,
        _Row(phase="trial", billed=False),
        paid_through=datetime.now(UTC) + timedelta(days=24),
    )

    assert summary["restored"] == []
    assert writes == []


def test_a_cancelled_module_is_never_restored(app, monkeypatch):
    """Terminal phases are decisions already taken; no date re-opens them."""
    summary, _writes = _sweep(
        app,
        monkeypatch,
        _Row(phase="cancelled"),
        paid_through=datetime.now(UTC) + timedelta(days=24),
    )

    assert summary["restored"] == []


def test_access_is_never_invented_without_a_subscription(app, monkeypatch):
    """No row behind the flag means no access, in both directions.

    The ``m1a01_revoke_ungranted`` migration exists because a flag with nothing behind
    it is how an entity ends up inside a module whose card still offers a free trial.
    The restore branch must not hand that back.
    """
    import billing.services.entity_modules as modules_mod
    from billing.services import access_sweep, store

    writes: list[tuple] = []
    monkeypatch.setattr(access_sweep, "EntityFunction", _FakeEntityFunction)
    monkeypatch.setattr(access_sweep, "EntityFunctionMap", _FakeEntityFunctionMap)
    monkeypatch.setattr(access_sweep, "Entity", _FakeEntity)
    monkeypatch.setattr(modules_mod, "MODULE_CODES", ("PETTY_CASH",))
    monkeypatch.setattr(modules_mod, "_enabled_state", lambda eid: {"PETTY_CASH": False})
    monkeypatch.setattr(
        modules_mod,
        "set_entity_module",
        lambda eid, code, enabled, *, actor: writes.append((eid, code, enabled, actor)),
    )
    monkeypatch.setattr(store, "entity_ids_with_billed_modules", lambda user_id=None: {"e1"})
    monkeypatch.setattr(store, "module_rows_for_entity", lambda eid: [])
    monkeypatch.setattr(store, "paid_through_for_user", lambda uid: None)

    # Same value per company: these cases describe an account with one card.
    monkeypatch.setattr(store, "paid_through_for_entity", lambda _e: None)
    with app.app_context():
        summary = access_sweep.sweep_expired_module_access()

    assert summary["restored"] == []
    assert writes == []
