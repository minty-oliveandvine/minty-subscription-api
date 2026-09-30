"""The daily access reconciler: close windows that simply elapsed.

Moved here from ``entity.services.modules``, where it sat among 2,600 lines of module-card
rendering despite being neither about the entity blueprint nor about rendering. It is
subscription work: it reads ``paid_through``, ``app_access_until`` and the phase, and it
is driven by ``daily.run_daily``, ``dunning`` and the transfer accept -- all of which live
here.

THE PORT (Part 2 step 2): here ``billing.services.entity_modules`` is the entity side, and
this module is the ONLY exporter of ``sweep_expired_module_access`` - Flask's
``entity.services.modules`` re-export is not reproduced, importers name this module. The two
entity-side dependencies -- ``_enabled_state`` and ``set_entity_module`` -- are still reached
through the MODULE OBJECT and read at call time, not bound at import: the ported suite
patches those names on ``entity_modules``, and a ``from ... import`` here would capture the
real ones at import and silently ignore the patch.

The lazy imports inside the functions are the existing house pattern (they close the
``checkout`` cycle).

``revoke_ungranted_module_access`` is the ORM where Flask's was two raw statements with the
schema name interpolated: same rows (an enabled map row for a canonical module, no
subscription row, entity not mid-onboarding), same update, and it runs on SQLite too.
"""
from __future__ import annotations

from django.db.models import Exists, OuterRef
from django.db.models.functions import Now

from billing.services._log import logger
from shared_models.enums import ModuleCode
from shared_models.models import Entity, EntityFunction, EntityFunctionMap, EntityModuleSubscription


def revoke_ungranted_module_access(*, dry_run: bool = True) -> list[dict]:
    """Switch off every module grant that no ``entity_module_subscription`` row backs.

    The launch-day counterpart of migration ``m1a01``, which the cutover skipped because
    subscriptions were dark (blueprints/shared/feature_flags.py): once the feature is on,
    access is a projection of the subscription row, and a map row switched on with no row
    behind it offers a module the card would then invite the company to start a trial
    for. Same boundary as the migration - "has no row at all", never a date comparison
    (that is the sweep's job) - and the same exemption for mid-wizard entities.

    Returns the rows it would (dry run) or did switch off, ``{entity_id, code}`` each,
    ordered by entity then code so two runs (or the two implementations) compare.
    Nothing is ever granted here; the switch itself grants nothing either.
    """
    codes = tuple(ModuleCode.values)
    functions = {fn.id: fn.function_code for fn in EntityFunction.objects.filter(function_code__in=codes)}
    if not functions:
        return []

    # Flask's SQL had two NOT EXISTS clauses. The subscription one matches on the function
    # CODE while the map row carries the function ID, so it is asked per code here (two
    # codes, two queries) rather than through a join the mirrors do not declare.
    found: list[dict] = []
    for fn_id, code in functions.items():
        backed = EntityModuleSubscription.objects.filter(entity_id=OuterRef("entity_id"), function_code=code)
        onboarding = Entity.objects.filter(id=OuterRef("entity_id"), status="onboarding")
        rows = (
            EntityFunctionMap.objects.filter(entity_function_id=fn_id, is_enabled=True)
            .annotate(backed=Exists(backed), onboarding=Exists(onboarding))
            .filter(backed=False, onboarding=False)
            .values_list("entity_id", flat=True)
        )
        found.extend({"entity_id": str(entity_id), "code": code} for entity_id in rows)
    found.sort(key=lambda hit: (hit["entity_id"], hit["code"]))
    if dry_run or not found:
        return found

    switched = 0
    for fn_id, code in functions.items():
        entity_ids = [hit["entity_id"] for hit in found if hit["code"] == code]
        if not entity_ids:
            continue
        switched += EntityFunctionMap.objects.filter(
            entity_function_id=fn_id, entity_id__in=entity_ids, is_enabled=True
        ).update(is_enabled=False, disabled_at=Now(), updated_at=Now())
    logger.info("revoke-ungranted: switched off {} module grant(s) with no subscription row", switched)
    return found


def sweep_expired_module_access(payer_user_id=None) -> dict:
    """Disable modules whose access has lapsed past its grace, and END the ones that are
    over.

    ``payer_user_id`` narrows the whole pass to one billing account. The daily job runs
    unscoped; a caller that has just changed one account's entitlement — dunning, on the
    payment that clears an episode — passes its payer so the customer's access comes back
    with the payment rather than at the next nightly run.

    Nothing fires at a grace boundary — not the past-due window measured from
    ``paid_through``, and not ``app_access_until`` (a cancelled module's paid
    extension). So the access map goes stale the moment a window simply elapses, and
    this reconciles it.

    Two things go stale at that boundary, not one. Access is the obvious one. The other
    is the PHASE: ``scheduled_cancel`` and ``past_due`` describe a subscription on its
    way out, and once the date they hang on has passed it is out — so they are moved to
    ``cancelled``, which is terminal and, unlike either of them, lets the customer buy
    the module again. Without that the row reads as mid-cancellation or in-arrears
    forever and the panel keeps offering Renew or Pay now for something already ended.
    See ``checkout.terminate_lapsed_module``; trials are excluded there, because the
    trial-end job owns that transition.

    Runs the SAME sync the webhooks do, per entity, rather than reimplementing "who should
    have access" — the copy that used to live here had drifted into three bugs: it read
    the payer's views without filtering to this entity (granting siblings' modules), it
    iterated only codes that HAD a Stripe view (so a module cancelled out of a bundle —
    whose line is gone, which is the exact case this exists for — was never evaluated),
    and it ignored app-granted access (revoking live trials).

    A module with NO subscription row is revoked, not skipped. The subscription row is
    the record of truth and ``is_enabled`` only projects it, so "switched on with no row
    behind it" is precisely the state this job exists to erase — it is how an entity ends
    up inside a module whose card still offers "Start free trial". This used to `continue`
    on the grounds that a hand-enabled module was nobody's business, which meant the one
    inconsistency nothing else could repair was the one thing deliberately left alone.

    Entities still mid-onboarding are exempt: the wizard records its Step 2 selection in
    the map and only starts the trials at finalize, so between those two calls an enabled
    module with no row is expected rather than broken.

    Reconciles in BOTH directions. A module whose entitlement has come BACK — a past-due
    account that paid, a dunning episode that recovered — is switched on again, because
    nothing else does it either. Revocation used to be one-way: the sweep only looked at
    modules that were currently on, so one it turned off left its candidate set for good
    and the customer stayed locked out of a subscription still being charged for. See the
    restore branch for why that direction is deliberately narrower than this one.

    Intended to run daily (``flask subscriptions sweep-access``). Returns
    ``{"disabled": [{"entity_id", "code"}, ...], "restored": [...]}``.
    """
    from billing.services import access, checkout, clock, entity_modules, policy
    from billing.services import store as sub_store

    code_set = set(entity_modules.MODULE_CODES)
    disabled: list[dict] = []
    restored: list[dict] = []
    now = clock.now()
    # One window for the whole sweep. Reading it per entity would let a mid-run edit
    # revoke access for the tail of the batch under a rule the head never saw.
    grace_days = policy.current().past_due_window_days

    entity_ids, onboarding_ids = _sweep_scope(payer_user_id, sub_store)

    for entity_id in entity_ids:
        if entity_id in onboarding_ids:
            continue
        try:
            rows = {
                row.function_code.upper(): row
                for row in sub_store.module_rows_for_entity(entity_id)
            }
            # The date access is measured against lives on the CARD this company is
            # billed on — not on the rows, which drift apart between entities, and no
            # longer on the account, which cannot answer for two cards at once.
            payer_id = next(
                (r.payer_user_id for r in rows.values() if r.payer_user_id), None
            )
            paid_through = (
                sub_store.paid_through_for_entity(entity_id) if payer_id else None
            )
            enabled = entity_modules._enabled_state(entity_id)
            for code in code_set:
                row = rows.get(code)
                if not enabled.get(code):
                    # Switched off while the subscription still entitles it. Restoring
                    # is deliberately narrower than revoking: only a BILLED module, and
                    # only on the same ``grants_access`` predicate that took it away.
                    #
                    # Requiring a row keeps the guarantee that access is a projection of
                    # a subscription — a flag with nothing behind it is still revoked
                    # above and is never invented here. Requiring the module to be BILLED
                    # is what keeps this from fighting the customer: a paid module cannot
                    # be switched off by hand at all (``set_entity_module`` refuses it),
                    # so an off flag on one can only have come from this sweep. A trial
                    # IS freely toggleable, so re-enabling one would silently overturn a
                    # deliberate choice — and a live trial never loses access this way in
                    # the first place, since its date does not depend on the billing
                    # cycle. Terminal phases grant nothing and so are never restored.
                    if row is not None and access.is_paid_module(
                        phase=row.phase,
                        has_been_billed=row.first_billed_at is not None,
                    ) and access.grants_access(
                        now,
                        phase=row.phase,
                        trial_end=row.trial_end,
                        app_access_until=row.app_access_until,
                        period_end=paid_through,
                        past_due_grace_days=grace_days,
                    ):
                        entity_modules.set_entity_module(
                            entity_id, code, True, actor="subscription"
                        )
                        restored.append({"entity_id": entity_id, "code": code})
                    continue
                # Switched on with nothing behind it — never subscribed, or a row
                # deleted out from under the flag. Access is a projection; with no
                # row to project, it comes off.
                if row is None:
                    entity_modules.set_entity_module(
                        entity_id, code, False, actor="subscription"
                    )
                    disabled.append(
                        {"entity_id": entity_id, "code": code}
                    )
                    continue
                if access.grants_access(
                    now,
                    phase=row.phase,
                    trial_end=row.trial_end,
                    app_access_until=row.app_access_until,
                    period_end=paid_through,
                    past_due_grace_days=grace_days,
                ):
                    continue
                # Access has lapsed — a trial that ended, a cancellation past its
                # extension, or a past-due account past its grace. Nothing else closes
                # the gate: the boundary is a DATE, and no event fires when a date passes.
                entity_modules.set_entity_module(
                    entity_id, code, False, actor="subscription"
                )
                # The same date ENDS the subscription, so the phase has to say so too.
                # Revoking access while leaving the row on scheduled_cancel / past_due
                # left a module nobody could use still offering Renew or Pay now for
                # something already over. A trial is left alone — the trial-end job owns
                # that transition (see checkout.terminate_lapsed_module).
                checkout.terminate_lapsed_module(row)
                disabled.append(
                    {"entity_id": entity_id, "code": code}
                )
        except Exception:
            logger.exception("modules: access sweep failed for entity {}", entity_id)
            continue

    # THIS SWEEP MAILS NOTHING, in either direction.
    #
    # Restorations never did: the customer is told by the thing that caused them — the
    # dunning "you're all settled" notice — and a second "your access is back" for the same
    # event reads as a system talking to itself. (The receipt that used to confirm any
    # other clearing payment is retired, 2026-09-30, so that case is now silent.)
    #
    # Revocations used to, and no longer do (2026-09, by decision). Worth knowing what
    # that means, because nothing else covers it: when this switches a module off, the
    # customer is not told. The dunning notices warn that suspension is coming, so they
    # are not unwarned — but nothing confirms it happened, and a lapse from plain
    # cancellation is not announced at all. If "the customer discovered it by hitting an
    # Access Denied page" ever comes back as a complaint, this is the line that explains
    # why.
    return {"disabled": disabled, "restored": restored}


def _sweep_scope(payer_user_id, sub_store):
    """Which entities this sweep looks at: ``(entity_ids, onboarding_ids)``.

    Kept together because the two populations only make sense as a pair -- see the
    comment below for why a one-way candidate set turned the sweep into a ratchet that
    could revoke access and never give it back.

    Imports at call time like the caller does: the suite patches ``MODULE_CODES`` on
    ``entity_modules``, and binding it at import would ignore that.
    """
    from billing.services import entity_modules

    # Two populations, because this reconciles in BOTH directions.
    #
    # Entities with a module switched on are the only ones that could need switching
    # off. On its own that set made the sweep a one-way ratchet: a module revoked here
    # left the candidate set permanently, so nothing could ever switch it back on — and
    # nothing else does. An account that went past due, then paid, stayed locked out of
    # a subscription it was being charged for, which is precisely the recovery dunning
    # exists to deliver. So entities holding a BILLED module are candidates too, however
    # their access flag currently reads.
    module_fn_ids = list(
        EntityFunction.objects.filter(function_code__in=entity_modules.MODULE_CODES)
        .values_list("id", flat=True)
    )
    entity_ids = (
        set(
            EntityFunctionMap.objects.filter(
                entity_function_id__in=module_fn_ids, is_enabled=True
            ).values_list("entity_id", flat=True)
        )
        if module_fn_ids
        else set()
    )
    if payer_user_id is None:
        entity_ids |= sub_store.entity_ids_with_billed_modules()
    else:
        mine = {
            str(row.entity_id)
            for row in sub_store.module_rows_for_payer(payer_user_id)
        }
        entity_ids = (entity_ids & mine) | sub_store.entity_ids_with_billed_modules(
            payer_user_id
        )

    # Mid-onboarding entities are exempt (see docstring). Resolved in ONE query up
    # front rather than per entity, so a long sweep can't straddle a finalize and
    # judge the head of the batch by a different rule than the tail. Unfiltered by
    # entity_ids on purpose: the set of in-flight onboardings is small, and an IN
    # clause over every enabled entity is the part that would not scale.
    onboarding_ids = set(
        Entity.objects.filter(status="onboarding").values_list("id", flat=True)
    )
    return entity_ids, onboarding_ids
