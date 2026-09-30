"""The dashboard subscription notice: which one popup an entity gets, and once.

Moved out of ``entity.services.modules``. Deciding that a company is past due, needs a
card, needs consent, is winding down or has a trial about to end is subscription
reasoning -- it reads phases, trial ends and billing consent -- and it was sitting in the
entity blueprint only because the module card it derives from does.

WHAT STAYS BEHIND: ``entity.services.modules`` re-exports both names, so every importer
keeps working -- and there are several import styles among them (``routes/list.py`` binds
at module level, ``routes/modules.py`` imports inside the request). Everything this module
reads back off the entity side -- ``get_module_cards``, ``TRIAL_ENDING_SOON_DAYS``,
``_NOTICE_ORDER``, ``NOTICE_SEEN_SESSION_KEY`` -- goes through the MODULE OBJECT and is
resolved at call time, never bound at import.

That is not stylistic. ``test_subscription_notice.py`` patches
``blueprints.entity.services.modules.TRIAL_ENDING_SOON_DAYS`` and
``...build_subscription_notices`` by dotted path; a ``from ... import`` here would capture
the real values at import and the patches would be silently ignored (docs/code_cleanse/CODE_CLEANSE_NOTES.md,
"dependency injection is required in this repo").
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

        # 2. The trial ALREADY lapsed and the gate is off. This state was silent: a
        #    payer could lose a module and never be told, because branch 3 below only
        #    fires while a trial is still running and there was nothing after it. The
        #    restart screen on the settings page can fix it, so the notice is the thing
        #    that gets them there.
        #
        #    Reuses ``needs_card`` rather than inventing a kind: the billing frontend
        #    types NoticeKind as a closed union and renders every kind generically, so a
        #    new one would arrive there unhandled.
        #
        #    NAMES THE MODULE, and has to. An entity can be part-lapsed with another
        #    trial still running, and a notice implying the whole company is down would
        #    be wrong for the half that is fine.
        if card.get("trial_expired") and not card.get("has_access"):
            ended = card.get("access_end_long") or card.get("trial_end_long")
            items.append(
                {
                    "kind": "needs_card",
                    "severity": "critical",
                    "module": name,
                    "module_code": code,
                    "title": f"{name} has expired",
                    "detail": (
                        (f"Its free trial ended {ended}. " if ended else "")
                        + "Restart billing to get it back."
                    ),
                    "deadline": ended,
                }
            )
            continue

        # 3. A trial that will NOT convert. Two different fixes, so two kinds — a
        #    payer with a card saved still has to authorise THIS company before it
        #    can be charged (see checkout._convert_due_trial).
        if card.get("needs_card"):
            consent_only = card.get("needs_consent_only")
            deadline = card.get("period_end_long") or card.get("period_end_short")
            items.append(
                {
                    "kind": "needs_consent" if consent_only else "needs_card",
                    "severity": "warning",
                    "module": name,
                    "module_code": code,
                    "title": (
                        f"Confirm billing to keep {name}"
                        if consent_only
                        else f"Add a payment method to keep {name}"
                    ),
                    "detail": (
                        (
                            "Your saved card is used by your other companies and won't be "
                            "charged for this one until you confirm."
                        )
                        if consent_only
                        else "Your free trial will end without converting."
                    )
                    + (f" Free trial ends {deadline}." if deadline else ""),
                    "deadline": deadline,
                }
            )
            continue

        # 4. Winding down — cancelled but still inside the paid period.
        if card.get("pending_cancel") and card.get("access_end_long"):
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
            continue

        # 5. A healthy trial that will convert. Not a problem — but the first charge
        #    is a surprise if nobody said it was coming, so it carries its date for
        #    the whole trial rather than only near the end.
        period_end = card.get("period_end")
        if (
            card.get("subscription_status") == "trialing"
            and period_end
            and not card.get("pending_cancel")
        ):
            # Still >= 0 even with no window: a trial past its end date is not
            # "ending", it has ended, and the sweep is what speaks next.
            #
            # ``trial_closing`` is the one exception, and it is not a contradiction of
            # that rule: it means the term has passed but nothing has closed the trial
            # out YET and the customer still has access. Dropping the notice there would
            # take the "first charge is coming, on this date" message away in the final
            # hour before the charge — the moment it is most worth having on screen — and
            # would make the panel visibly rearrange itself for a state nobody can act on.
            days_left = (period_end - now).days
            if (days_left >= 0 or card.get("trial_closing")) and (
                entity_modules.TRIAL_ENDING_SOON_DAYS is None
                or days_left <= entity_modules.TRIAL_ENDING_SOON_DAYS
            ):
                items.append(
                    {
                        "kind": "trial_ending",
                        "severity": "info",
                        "module": name,
                        "module_code": code,
                        # "is ending" was true when this only fired in the last week.
                        # It now runs the whole trial, and reading "ending" on day one
                        # of thirty would look like a bug — so the title states the
                        # state and the detail carries the date.
                        "title": f"{name} is on a free trial",
                        "detail": (
                            f"Your trial ends {card.get('period_end_long')} and billing starts then."
                            if card.get("period_end_long")
                            else "Billing starts when your trial ends."
                        ),
                        "deadline": card.get("period_end_long"),
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

