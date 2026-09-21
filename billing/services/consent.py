"""When a lapsed trial should take the subscription settings page over.

ONE definition of the condition, so the takeover, the panel and the route that charges
cannot drift. Everything here is a read; nothing writes and nothing raises.

THE TRIGGER IS THE ACCESS GATE, NOT ``trial_end``.

``trial_end`` passing does not end access. The request gate is a materialised flag on
``entity_function``, read by ``_is_module_enabled``; ``access.grants_access`` is a pure
predicate the SWEEP uses to decide what to flip, not the thing a request consults. The
flag only changes when ``close-trials`` calls ``checkout._set_module_access(..., False)``
or ``sweep-access`` repairs it, so for up to an hour (a day across a deploy, indefinitely
on an environment with no scheduler) a trial can be past its term with the customer
still working.

Keying this on the date would reproduce a bug already fixed once in
``modules.get_module_cards`` — see the ``app_trial_closing`` comment there, which records
a date-keyed reading telling a customer their trial was used up "while the gate was still
letting them work, and while a trial with a card and consent was in fact about to
CONVERT". Here it would be worse than a wrong label: this screen CHARGES, so it would
sell a subscription that was about to convert on its own. Hence the ``app_trial_closing``
exclusion below, which is the whole reason the gate is the input.

THREE MODES, because trials are per-module (``checkout.start_module_trial`` takes one
plan, so a module added later has its own ``trial_end``):

* ``takeover`` — nothing live is left. Replaces the page.
* ``panel``    — something lapsed, but another module is still TRIALING. A non-blocking
                 panel, because a full takeover would hide the very cards and controls
                 that live trial needs.
* ``None``     — nothing lapsed, or something lapsed while another module is PAID. A
                 paying entity's page is not broken; that is an upsell, and the ordinary
                 subscribe flow already covers it.
"""
from __future__ import annotations

from billing.services._log import logger

MODE_TAKEOVER = "takeover"
MODE_PANEL = "panel"

# The answer every failure path gives. A takeover is the most intrusive thing this app
# can put in front of a payer AND it charges, so anything unknown resolves to "do not".
_CLOSED: dict = {
    "mode": None,
    "lapsed": [],
    "payer_user_id": None,
    "has_card": False,
    "has_consent": False,
}


def _closed(payer_user_id=None) -> dict:
    return dict(
        _CLOSED,
        lapsed=[],
        payer_user_id=str(payer_user_id) if payer_user_id else None,
    )


def lapsed_trial_for_entity(entity_id, payer_user_id=None, *, access_state=None) -> dict:
    """The entity's lapsed-trial state.

    Returns ``{"mode", "lapsed": [{code, name, lapsed_on}], "payer_user_id", "has_card",
    "has_consent"}``. ``mode`` is ``takeover`` / ``panel`` / ``None``.

    ``access_state`` is the ``{code: bool}`` map the caller already read for the module
    cards. Pass it — re-reading it here would let this and the cards disagree about who
    has access on the same render, and the two are drawn on the same page.

    Never raises. ``has_card`` / ``has_consent`` are reported for the copy, NOT used to
    decide the mode: a lapsed entity has to buy its modules back whichever of the two it
    was missing.
    """
    # The phase constants and TRIAL_CLOSING_WINDOW moved with the classification loop to
    # ``_classify_lapsed_rows``; what is still read here is the ORDER modules are listed
    # in, the access map, and the catalog labels.
    from billing.services import clock
    from billing.services import store as sub_store
    from billing.services.entity_modules import MODULE_CODES, _enabled_state, module_display_names

    if not entity_id:
        return _closed()

    try:
        rows = sub_store.module_rows_for_entity(entity_id)
        if not rows:
            return _closed()

        payer = payer_user_id or sub_store.payer_for_entity(entity_id)
        if access_state is None:
            access_state = _enabled_state(str(entity_id))
        now = clock.now()

        lapsed, live_paid, live_trial = _classify_lapsed_rows(
            rows, access_state, now
        )

        if not lapsed:
            return _closed(payer)

        # A paying entity outranks a lapsed one: its page works, so this is an upsell.
        # Checked after the loop because an entity can be both at once.
        if live_paid:
            return _closed(payer)

        lapsed.sort(key=lambda item: MODULE_CODES.index(item["code"]))
        # Labels from the catalog, in ONE query for the set. ``function_name`` is the
        # source of truth for what a module is called (MODULE_DISPLAY holds no names),
        # so this screen and the module cards name the same thing the same way.
        names = module_display_names([item["code"] for item in lapsed])
        for item in lapsed:
            item["name"] = names.get(item["code"]) or item["code"]

        return {
            "mode": MODE_PANEL if live_trial else MODE_TAKEOVER,
            "lapsed": lapsed,
            "payer_user_id": str(payer) if payer else None,
            "has_card": _has_card(payer),
            "has_consent": bool(
                payer and sub_store.has_billing_consent(entity_id, payer)
            ),
        }
    except Exception:
        # (Flask rolled its session back here; under Django's autocommit a failed read
        # poisons nothing, so there is nothing to reset.)
        logger.exception(
            "consent: could not read lapsed-trial state for {}; answering closed",
            entity_id,
        )
        return _closed()


def _has_card(payer_user_id) -> bool:
    """Whether the payer has a default card. False on any failure — the copy then says
    "add a card", a harmless thing to tell someone who already has one."""
    if not payer_user_id:
        return False
    try:
        from billing.services import store as sub_store
        from billing.services.stripe_client import (
            customer_default_payment_method,
        )

        customer_id = sub_store.customer_id_for_user(payer_user_id)
        return bool(customer_id and customer_default_payment_method(customer_id))
    except Exception:
        logger.exception(
            "consent: could not read the default card for payer {}", payer_user_id
        )
        return False


def codes_for_restart(state: dict, requested) -> list[str]:
    """The module codes a restart may actually charge for, or ``[]``.

    THE VALIDATION for the restart route and its quote. ``requested`` arrives from the
    browser, so a code the entity has not actually lapsed — or never had — must never
    become a charge. Returns the intersection in canonical order, and an empty list for
    anything that fails: empty input, an unknown code, or one that is not in the lapsed
    set. Callers answer 422 on ``[]`` rather than pricing whatever was sent.
    """
    from billing.services.entity_modules import MODULE_CODES

    if not requested:
        return []
    if isinstance(requested, str):
        requested = [requested]

    allowed = {item["code"] for item in (state or {}).get("lapsed") or []}
    wanted = {str(code).strip().upper() for code in requested if str(code).strip()}
    if not wanted or not wanted.issubset(allowed):
        # An unknown or non-lapsed code is refused OUTRIGHT rather than filtered out.
        # Silently dropping it would charge for a different set than the one the payer
        # submitted, which on a screen that takes money is the worst of both.
        return []
    return [code for code in MODULE_CODES if code in wanted]


def _classify_lapsed_rows(rows, access_state, now):
    """Sort an entity's module rows into ``(lapsed, live_paid, live_trial)``.

    ``lapsed`` is the trials whose free period ran out with the gate already off --
    the ones the restart screen offers to buy back. The two flags are the reasons NOT
    to take the page over, and an entity can raise both at once, which is why they are
    collected here and weighed by the caller rather than short-circuiting the loop.

    Imported at call time, as the caller does: the suite patches ``MODULE_CODES`` and
    ``TRIAL_CLOSING_WINDOW`` on ``entity.services.modules``, and binding them at import
    would capture the real values and ignore the patch.
    """
    from billing.services.constants import (
        PHASE_ACTIVE,
        PHASE_CANCELLED,
        PHASE_EXPIRED,
        PHASE_PAST_DUE,
        PHASE_SCHEDULED_CANCEL,
        PHASE_TRIAL,
    )
    from billing.services.entity_modules import MODULE_CODES, TRIAL_CLOSING_WINDOW

    lapsed: list[dict] = []
    live_paid = False
    live_trial = False

    for row in rows:
        code = (getattr(row, "function_code", None) or "").upper()
        if code not in MODULE_CODES:
            continue
        phase = getattr(row, "phase", None) or ""
        trial_end = getattr(row, "trial_end", None)
        has_access = bool(access_state.get(code))

        # A deliberate cancellation is not a lapse. Cancelling is the ONLY way out of
        # a screen with no dismiss control, so taking the page over about a
        # subscription the customer themselves ended would leave them no recourse.
        if phase in (PHASE_SCHEDULED_CANCEL, PHASE_CANCELLED):
            continue

        if phase in (PHASE_ACTIVE, PHASE_PAST_DUE):
            live_paid = True
            continue

        if phase == PHASE_TRIAL and has_access:
            # Either genuinely running, or past its term with the gate still on
            # (``app_trial_closing``). Both mean the customer is working and the next
            # pass may yet convert them, so nothing may be sold about it either way.
            live_trial = True
            continue

        if phase == PHASE_EXPIRED and has_access:
            # ``expired`` with the gate still on is a repair the sweep has not made
            # yet. Side with the gate, exactly as the module card does.
            live_trial = True
            continue

        if phase not in (PHASE_TRIAL, PHASE_EXPIRED):
            continue

        if trial_end is None:
            # Not a lapsed TRIAL, so there is no free period that ran out and
            # nothing here knows what to offer.
            continue

        if phase == PHASE_TRIAL and (now - trial_end) <= TRIAL_CLOSING_WINDOW:
            # Row still says ``trial`` and the term only just ran out: the gate went
            # off before ``close-trials`` reached the row. Give the pass its window
            # to convert rather than selling something that may be about to be
            # charged anyway — the same bound the card uses for ``app_trial_closing``.
            live_trial = True
            continue

        lapsed.append({"code": code, "lapsed_on": trial_end})

    return lapsed, live_paid, live_trial
