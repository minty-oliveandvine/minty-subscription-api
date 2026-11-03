"""The module GATE's write side, and the constants the engine reads through it.

The Django copy of Flask's ``blueprints/entity/services/modules.py`` (Part 2 step 2). The
catalog (``entity_function``) defines which modules exist; per-entity on/off lives in
``entity_function_map``, and Flask's per-request gate
(``blueprints.entity.routes.modules._is_module_enabled``) reads it. This module is the
single place in THIS service that writes those rows, so the daily pass and the subscription
lifecycle produce rows shaped exactly as Flask's ``_write_pairs`` produces them (id columns,
audit columns, enabled/disabled timestamps) - the projection has two writers during Part 2
(Flask and this service) and their rows must be indistinguishable.

Two helpers:
  * ``apply_module_selection(s)`` / ``apply_default_modules`` - write the FULL state at once
    (Flask's onboarding step 2 and entity create use these; kept for parity and tests).
  * ``set_entity_module`` - flip ONE module without touching the others. The subscription
    lifecycle calls it with ``actor="subscription"``, which is the only actor allowed to
    switch a PAID module off.

Everything here that the engine reads is read off the module object at call time
(``entity_modules._enabled_state``, ``entity_modules.MODULE_CODES`` ...), so tests can
monkeypatch it exactly as the Flask suite did.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from billing.services._log import logger
from shared_models.enums import ModuleCode
from shared_models.models import EntityFunction, EntityFunctionMap

# Canonical module codes: the ``module_code`` enum (schema item 20). The Payment Request
# module's code is PAYMENT_REQUEST; ``MODULE_BILL`` keeps its historical name so the call
# sites read as they did in Flask. ``billing_plan.code`` still says ``BILL`` by decision -
# ``billing.services.billing.plan_code`` maps it.
MODULE_PETTY_CASH = ModuleCode.PETTY_CASH.value
MODULE_BILL = ModuleCode.PAYMENT_REQUEST.value
MODULE_CODES: tuple[str, ...] = (MODULE_PETTY_CASH, MODULE_BILL)

# How long after ``trial_end`` a trial the subscription pass has not closed out yet still
# presents as a trial being finalised rather than as one that expired.
#
# Six hours against an hourly pass: wide enough to cover a missed run, a deploy, or a host
# that was busy, and far too narrow to cover an environment where nothing runs at all.
TRIAL_CLOSING_WINDOW = timedelta(hours=6)

# What the multi-module plan is called on screen. The catalog row carries its own
# display_name and that wins; this is the fallback for a catalog that has no bundle
# row yet, and the one place the name is written down.
BUNDLE_DISPLAY_NAME = "Super Minty"

# Why a module row is being written. Not stored (entity_function_map.created_by is the
# person); the paid-subscription guard in ``set_entity_module`` keys on
# ``actor == ACTOR_SUBSCRIPTION`` and the callers still say who they are.
ACTOR_ONBOARDING = "onboarding"
ACTOR_CLI = "cli"
ACTOR_ENTITY_CREATE = "entity_create"
ACTOR_SUBSCRIPTION = "subscription"

# Default entitlement for a freshly created entity: nothing. Access is a projection of the
# module's entity_module_subscription row, so a brand new entity holds no module until a
# trial or a subscription starts one.
DEFAULT_MODULE_STATE: dict[str, bool] = {
    MODULE_PETTY_CASH: False,
    MODULE_BILL: False,
}

# Per-module presentation metadata that has no source of truth elsewhere: the card
# illustration and the "Learn more" link. Prices and labels are NOT here - amounts come
# from billing_plan (services.catalog) and labels from entity_function.function_name.
MODULE_DISPLAY: dict[str, dict] = {
    MODULE_PETTY_CASH: {
        "image": "img/cash_reg.webp",
        "learn_more": "https://youtu.be/kMs85hnvwFw?si=4Yt6WHeFIYVkNLPK",
    },
    MODULE_BILL: {
        "image": "img/payment-icon.png",
        "learn_more": "https://youtu.be/v7G6gaGO0V0?si=P7-aWHPa9p4jDlHS",
    },
}

# Ordering for the notice list, most severe first. The modal shows every item that
# applies rather than picking one. It is also the list of every kind ``notices`` may
# emit: a kind missing here makes the sort raise. Trial kinds were removed 2026-10-01.
_NOTICE_ORDER = ("past_due", "pending_cancel")

#: Session key holding the entity ids whose notice has already been shown this login
#: (Flask's dashboard; kept as a name because ``notices`` spells it).
NOTICE_SEEN_SESSION_KEY = "subscription_notice_seen"
LOGIN_SID_SESSION_KEY = "login_sid"


def _entity_customer_id(entity_id) -> str | None:
    """The entity's Stripe customer id from local tables (None if it has no customer).

    LOCAL READS ONLY, deliberately - do NOT swap this for ``checkout._resolve_customer_id``.
    That helper falls back to a Stripe Customer Search when the mapping row is missing,
    which is right on the billing paths but wrong here: this feeds the settings-page card
    render, so a payer who genuinely has no customer would fire a search on EVERY page load
    and always find nothing.
    """
    if not entity_id:
        return None
    # entity -> payer -> customer. The customer belongs to the PAYER (a user), not to
    # the entity, because one payer's card covers every entity they own.
    from billing.services import store as sub_store

    payer_id = sub_store.payer_for_entity(entity_id)
    return sub_store.customer_id_for_user(payer_id) if payer_id else None


def _enabled_state(entity_id: str) -> dict[str, bool]:
    """Resolve each canonical module's on/off state for an entity.

    Mirrors Flask's ``_is_module_enabled`` exactly, including its fail-closed default:
    no map row -> OFF. Returns {code: bool} for every code in MODULE_CODES so callers get
    a complete, stable picture.
    """
    catalog_by_code = {
        fn.function_code: fn
        for fn in EntityFunction.objects.filter(function_code__in=MODULE_CODES)
    }
    fn_ids = [fn.id for fn in catalog_by_code.values()]
    maps_by_fn_id = {}
    if fn_ids:
        maps_by_fn_id = {
            row.entity_function_id: row
            for row in EntityFunctionMap.objects.filter(
                entity_id=str(entity_id), entity_function_id__in=fn_ids
            )
        }

    state: dict[str, bool] = {}
    for code in MODULE_CODES:
        fn = catalog_by_code.get(code)
        row = maps_by_fn_id.get(fn.id) if fn else None
        if row is not None:
            state[code] = bool(row.is_enabled)
        else:
            # No explicit grant - and the catalog's is_active says whether a module
            # is offered at all, never who may use it. Deny.
            state[code] = False
    return state


def get_enabled_modules_for_entities(entity_ids: list[str]) -> dict[str, set[str]]:
    """Map each entity id to the set of module codes that are enabled for it.

    Built on ``_enabled_state`` so it shares the same fail-closed default.
    """
    result: dict[str, set[str]] = {}
    for entity_id in entity_ids:
        state = _enabled_state(entity_id)
        result[entity_id] = {code for code, on in state.items() if on}
    return result


def module_display_names(codes) -> dict[str, str]:
    """Human labels for module codes, from the catalog - ``{code: name}``.

    ``entity_function.function_name`` is the source of truth for what a module is
    called in the app (MODULE_DISPLAY deliberately holds no names), so the label is
    read rather than hardcoded. Codes with no catalog row fall back to the code.
    """
    codes = {(c or "").upper() for c in codes if c}
    if not codes:
        return {}
    try:
        rows = list(EntityFunction.objects.filter(function_code__in=codes))
    except Exception:
        logger.exception("modules: could not read module display names")
        return {code: code for code in codes}
    by_code = {
        (r.function_code or "").upper(): (r.function_name or "").strip()
        for r in rows
    }
    return {code: (by_code.get(code) or code) for code in codes}


# The module cards live in ``billing.services.cards``; the notice builder reaches them
# through THIS module at call time, as Flask's did, so a test's patch on
# ``entity_modules.get_module_cards`` still bites.
def get_module_cards(entity_id: str) -> list[dict]:
    from billing.services.cards import get_module_cards as _cards

    return _cards(entity_id)


def _forecast_conversion_charges(entity_id, payer_id, billed_now, cards):
    from billing.services.cards import _forecast_conversion_charges as _f

    return _f(entity_id, payer_id, billed_now, cards)


def get_subscription_summary(entity_id: str) -> dict:
    from billing.services.panel import get_subscription_summary as _f

    return _f(entity_id)


def get_billing_anchor(entity_id: str) -> str | None:
    from billing.services.panel import get_billing_anchor as _f

    return _f(entity_id)


def next_payment_from_panel(panel: dict | None) -> str | None:
    from billing.services.panel import next_payment_from_panel as _f

    return _f(panel)


def get_next_payment_date(entity_id: str) -> str | None:
    from billing.services.panel import get_next_payment_date as _f

    return _f(entity_id)


def build_consent_takeover(entity_id, user_id, *, can_manage: bool, access_state=None):
    from billing.services.panel import build_consent_takeover as _f

    return _f(entity_id, user_id, can_manage=can_manage, access_state=access_state)


def get_module_plan_catalog() -> dict:
    from billing.services.cards import get_module_plan_catalog as _f

    return _f()


def get_trial_modules_for_entities(entity_ids: list[str]) -> dict[str, set[str]]:
    from billing.services.cards import get_trial_modules_for_entities as _f

    return _f(entity_ids)


def build_subscription_panel(cards, summary, anchor_display):
    from billing.services.panel import build_subscription_panel as _panel

    return _panel(cards, summary, anchor_display)


def build_subscription_notices(entity_id: str, user_id) -> dict:
    from billing.services.notices import build_subscription_notices as _build

    return _build(entity_id, user_id)


def apply_module_selection(
    entity_id: str, selected_code: str, *, actor: str, user_id: str | None = None
) -> tuple[dict, int]:
    """Set this entity's modules from a single-select choice.

    Selected -> is_enabled=True, every other registered module -> is_enabled=False.
    Returns ({"modules": {code: bool}}, status).
    """
    if not entity_id:
        return {"error": "entity_id is required"}, 400
    if selected_code not in MODULE_CODES:
        return (
            {"error": f"Unknown module code: {selected_code!r}. Expected one of {list(MODULE_CODES)}."},
            400,
        )

    pairs = {code: (code == selected_code) for code in MODULE_CODES}
    return _write_pairs(entity_id, pairs, actor=actor, user_id=user_id)


def apply_module_selections(
    entity_id: str, selected_codes, *, actor: str, user_id: str | None = None
) -> tuple[dict, int]:
    """Set this entity's modules from a multi-select choice.

    Each code in ``selected_codes`` -> is_enabled=True; every other registered
    module -> is_enabled=False.
    """
    if not entity_id:
        return {"error": "entity_id is required"}, 400

    selected = {str(c).strip().upper() for c in (selected_codes or []) if c}
    if not selected:
        return {"error": "Select at least one module."}, 400

    unknown = sorted(selected - set(MODULE_CODES))
    if unknown:
        return (
            {"error": f"Unknown module code(s): {unknown}. Expected one of {list(MODULE_CODES)}."},
            400,
        )

    pairs = {code: (code in selected) for code in MODULE_CODES}
    return _write_pairs(entity_id, pairs, actor=actor, user_id=user_id)


def apply_default_modules(
    entity_id: str, *, actor: str = ACTOR_ENTITY_CREATE, user_id: str | None = None
) -> tuple[dict, int]:
    """Seed a new entity's module entitlements with the default state - every module OFF."""
    if not entity_id:
        return {"error": "entity_id is required"}, 400

    return _write_pairs(entity_id, dict(DEFAULT_MODULE_STATE), actor=actor, user_id=user_id)


def _module_has_paid_subscription(entity_id: str, code: str) -> bool:
    """True if the module is BILLED, so it must be cancelled through billing rather
    than switched off here - otherwise access and payment fall out of step.

    Trialing modules are intentionally NOT counted: a card-free trial can be toggled
    off freely, because nothing is being charged for it. A PAST-DUE module counts as
    paid, and so does a cancelled module still inside its paid extension; a cancelled
    TRIAL shares that phase but has never been charged (see ``access.is_paid_module``).
    """
    from billing.services import access, store

    row = store.module_row(entity_id, (code or "").strip().upper())
    if row is None:
        return False
    return access.is_paid_module(
        phase=row.phase,
        has_been_billed=row.first_billed_at is not None,
    )


def set_entity_module(
    entity_id: str, code: str, enabled: bool, *, actor: str, user_id: str | None = None
) -> tuple[dict, int]:
    """Flip one module on/off for an entity without touching the others.

    The subscription lifecycle (``actor="subscription"``) is authoritative on access
    and bypasses the paid-subscription guard so a cancellation/lapse can still turn a
    module off.
    """
    if not entity_id:
        return {"error": "entity_id is required"}, 400
    if code not in MODULE_CODES:
        return (
            {"error": f"Unknown module code: {code!r}. Expected one of {list(MODULE_CODES)}."},
            400,
        )

    # A module with a paid subscription can't be disabled by hand - access follows
    # billing, so it is cancelled through billing, which then revokes access.
    if (
        not enabled
        and actor != ACTOR_SUBSCRIPTION
        and _module_has_paid_subscription(entity_id, code)
    ):
        return (
            {
                "error": "This module has an active subscription. Cancel it from "
                "billing before disabling the module."
            },
            409,
        )

    return _write_pairs(entity_id, {code: bool(enabled)}, actor=actor, user_id=user_id)


def _write_pairs(
    entity_id: str, pairs: Mapping[str, bool], *, actor: str, user_id: str | None = None
) -> tuple[dict, int]:
    """Upsert one row per (entity_id, function_code) in ``pairs``.

    Existing rows are mutated in place so audit history (created_at / created_by) is
    preserved; only enabled_at or disabled_at is bumped on actual state changes, and
    updated_at is bumped every write. The stamps are this process's UTC clock, written
    explicitly - exactly as Flask's ``_write_pairs`` writes them, so a row from either
    writer reads the same. (On an UPDATE the database owns ``updated_at`` - the
    ``trg_entity_function_map_updated`` trigger in production, ``UpdatedAtMixin`` on
    SQLite - so the explicit value lands only on the INSERT, for both writers.)

    ``user_id`` is the person doing it and lands in ``created_by`` on a NEW row; None
    (the pass, a command, a seed) leaves it NULL. ``actor`` is the reason and is not
    stored - see ACTOR_*.
    """
    del actor  # behaviour is keyed on it by the callers; the row does not record it
    now = datetime.now(UTC)
    codes = list(pairs.keys())

    catalog = list(EntityFunction.objects.filter(function_code__in=codes))
    catalog_by_code = {fn.function_code: fn for fn in catalog}
    missing = [c for c in codes if c not in catalog_by_code]
    if missing:
        # Catalog hasn't been seeded yet - the migration that seeds it is the
        # prerequisite, not silent self-healing here.
        return (
            {"error": f"Module catalog missing rows for: {missing}. Run the seed migration."},
            500,
        )

    fn_ids = [fn.id for fn in catalog]
    existing = list(
        EntityFunctionMap.objects.filter(entity_id=str(entity_id), entity_function_id__in=fn_ids)
    )
    by_fn_id = {row.entity_function_id: row for row in existing}

    for code, enabled in pairs.items():
        fn = catalog_by_code[code]
        row = by_fn_id.get(fn.id)
        if row is None:
            EntityFunctionMap(
                entity_id=str(entity_id),
                entity_function_id=fn.id,
                is_enabled=enabled,
                enabled_at=now if enabled else None,
                disabled_at=None if enabled else now,
                created_by=str(user_id) if user_id else None,
                created_at=now,
                updated_at=now,
            ).save(force_insert=True)
        else:
            changed = ["updated_at"]
            if row.is_enabled != enabled:
                if enabled:
                    row.enabled_at = now
                    changed.append("enabled_at")
                else:
                    row.disabled_at = now
                    changed.append("disabled_at")
                row.is_enabled = enabled
                changed.append("is_enabled")
            row.updated_at = now
            row.save(update_fields=changed)

    # Return the full canonical-module state so callers (and clients) get a
    # single consistent shape regardless of which helper they called.
    canonical = list(EntityFunction.objects.filter(function_code__in=MODULE_CODES))
    code_by_fn_id = {fn.id: fn.function_code for fn in canonical}
    final_rows = EntityFunctionMap.objects.filter(
        entity_id=str(entity_id), entity_function_id__in=list(code_by_fn_id)
    )
    state = {code: False for code in MODULE_CODES}
    for row in final_rows:
        code = code_by_fn_id.get(row.entity_function_id)
        if code:
            state[code] = bool(row.is_enabled)

    return {"modules": state}, 200
