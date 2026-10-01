"""The in-app subscription notice: what an entity's landing page interrupts someone with.

The port of Flask's ``blueprints/subscription/services/notices.py``. Deciding that a company
is past due or winding down is subscription reasoning -- it reads phases, the dunning stamp
and access ends -- derived from the same module cards the settings page renders.

TWO KINDS, and only two (the user's decision, 2026-10-01): ``past_due`` (a renewal failed and
the customer has been told) and ``pending_cancel`` (a PAID module cancelled and winding down).
Every trial notice -- the trial ending, a trial that will not convert for want of a card or of
consent, a lapsed trial, a cancelled trial -- was removed. The trial-ending EMAIL
(``notify.py``, ``trial_ending``) is what still tells a payer about their trial.

Everything this module reads back off the entity side -- ``get_module_cards``,
``_NOTICE_ORDER``, ``NOTICE_SEEN_SESSION_KEY`` -- goes through the MODULE OBJECT
(``billing.services.entity_modules``) and is resolved at call time, never bound at import, so
a test that patches ``billing.services.entity_modules.get_module_cards`` by dotted path is
honoured; a ``from ... import`` here would capture the real value and the patch would be
silently ignored.
"""
from __future__ import annotations

from billing.services import entity_modules


def build_subscription_notices(entity_id: str, user_id) -> dict:
    """Everything worth interrupting someone with when they enter an entity.

    ENTITY-WIDE, not per-module: billing is per payer and the anchor is shared, so a
    declined card affects every module the entity holds. Both landing pages (Petty
    Cash here, Payment in the billing frontend) show the same list, each item naming
    the module it is about.

    Deliberately reuses ``get_module_cards`` rather than re-deriving state. Every
    condition below is already a field on those cards, computed against the
    subscription rows that the billing engine itself reads — so the modal, the
    settings page and the invoice cannot disagree about what is wrong.

    Returns ``items: []`` when there is nothing to say; callers treat that as
    "render nothing" rather than rendering an empty modal.
    """
    from billing.services import clock, dunning
    from billing.services import store as sub_store
    from core.policy import Permission, has_permission_by_user_id
    from shared_models.models import User

    cards = entity_modules.get_module_cards(entity_id)
    now = clock.now()
    items: list[dict] = []

    for card in cards:
        name = card.get("name") or card.get("code")
        code = card.get("code")

        # 1. Money already failed. The most urgent thing that can be true: access
        #    ends on a date the payer can still act before. Only once the customer has
        #    been told - past due because the PROCESSOR failed is not a failed payment.
        if card.get("subscription_status") == "past_due" and dunning.told_of_failure(
            entity_id, now
        ):
            items.append(
                {
                    "kind": "past_due",
                    "severity": "critical",
                    "module": name,
                    "module_code": code,
                    "title": f"{name} payment failed",
                    "detail": (
                        f"Pay by {card['access_end_long']} to keep access."
                        if card.get("access_end_long")
                        else "Update your payment method to keep access."
                    ),
                    "deadline": card.get("access_end_long"),
                }
            )
            continue

        # 2. Winding down — a PAID module cancelled but still inside its paid period.
        #    A cancelled free trial also reads ``pending_cancel``; that is a trial, and
        #    trials get no notice.
        if (
            card.get("pending_cancel")
            and card.get("access_end_long")
            and not card.get("trial_cancelled")
        ):
            items.append(
                {
                    "kind": "pending_cancel",
                    "severity": "warning",
                    "module": name,
                    "module_code": code,
                    "title": f"{name} is ending",
                    "detail": (
                        f"Access until {card['access_end_long']}. It won't be billed again."
                    ),
                    "deadline": card.get("access_end_long"),
                }
            )

    items.sort(key=lambda i: entity_modules._NOTICE_ORDER.index(i["kind"]))

    # Who may actually act. Permission says who administers the entity; the payer is
    # whose card the buttons spend — @require_subscription_payer refuses anyone else
    # server-side, so offering them an action would produce a button that fails.
    can_manage = bool(
        user_id
        and has_permission_by_user_id(
            str(user_id), Permission.MODULE_MANAGE, entity_id
        )
        and sub_store.may_manage_subscription(entity_id, user_id)
    )

    payer = None
    payer_id = sub_store.payer_for_entity(entity_id)
    if payer_id and str(payer_id) != str(user_id):
        payer_user = sub_store._by_pk(User, payer_id)
        if payer_user:
            payer = {
                "name": " ".join(
                    p for p in (payer_user.first_name, payer_user.last_name) if p
                ).strip(),
                "email": payer_user.email or "",
            }

    return {
        "items": items,
        "can_manage": can_manage,
        "payer": payer,
        "severity": items[0]["severity"] if items else None,
    }


def claim_subscription_notice(session, entity_id: str) -> bool:
    """Whether to show the notice now — and if so, mark it shown for this session.

    "Always appear when first logged in to the entity": once per entity per login,
    not once per page view and not once forever. A user who fixes the problem and
    comes back tomorrow should be told if it is still broken.

    Consumes rather than merely reads, because that is what makes the cost bearable:
    ``build_subscription_notices`` is only ever called when this returns True, so the
    subscription queries run once per entity per session instead of on every dashboard
    load. A previous per-page-view billing read was removed for exactly that reason
    (see the comment in ``routes.modules.module_selection``).

    Takes the session as a parameter rather than importing ``flask.session`` so it is
    testable with a plain dict, and so the JSON endpoint — which is stateless and
    always returns the notice — can simply not call it. THIS SERVICE HAS NO SESSION: the
    API is stateless and never calls this; Flask's copy keeps doing the once-per-session
    gating until step 5. Kept for parity with a dict, as the docstring above says.
    """
    if not entity_id:
        return False
    seen = session.get(entity_modules.NOTICE_SEEN_SESSION_KEY) or []
    if str(entity_id) in seen:
        return False
    # Reassign rather than mutate in place: Flask's session only marks itself dirty
    # on __setitem__, so appending to the existing list would not persist.
    session[entity_modules.NOTICE_SEEN_SESSION_KEY] = [*seen, str(entity_id)]
    return True

