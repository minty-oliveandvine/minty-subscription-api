"""The payer portal's read model — one payer, every entity they pay for.

The rest of the subscription UI is ENTITY-scoped: you pick a company, then look at its
modules. The model underneath is not. A payer has one billing account, one anchor and
one paid-through, and it spans every entity they own (see ``store.module_rows_for_payer``
and the module docstring in ``blueprints.subscription``). "Manage subscriptions" on the
profile is the first screen that reads the model the way it is actually shaped, so the
query starts from the PAYER and fans out to entities rather than the other way round.

Deliberately NOT built on ``entity.services.modules.get_module_cards``. That function
answers a much richer question for one entity — prices, trial eligibility, conversion
forecasts — and pays for it with a Stripe call (``customer_default_payment_method``) and
a catalog read per entity. Running it across thirty-odd entities to fill a table with
six columns would be dozens of network round trips for data this screen never shows.

What it does share is the STATUS VOCABULARY. The rules for "is this module live", and
which date to print beside it, are read from ``access.py`` here exactly as they are
there, so the row in this table and the card on the entity's own settings page cannot
disagree about what the customer holds.

Nothing here writes. Cancelling, subscribing and resuming stay on the entity settings
page behind ``@require_subscription_payer`` — this module is the index, not a second
place money can move from.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from billing.services import display
from billing.services._log import logger
from billing.services.constants import (
    PHASE_ACTIVE,
    PHASE_PAST_DUE,
    PHASE_SCHEDULED_CANCEL,
    PHASE_TRIAL,
)
from billing.services.entity_modules import MODULE_CODES
from billing.services.store import _by_pk
from shared_models.models import CountryInfo, Entity, EntityFunction, User

# --- Status vocabulary -------------------------------------------------------
#
# Seven values, and each says something the others cannot. The badge colours in the
# design cover four; the three extra are splits the design's grey "not subscribed"
# would otherwise flatten into a lie:
#
#   past_due      — live, and the money is owed. Never merged into "active": the whole
#                   point of the row is that something needs fixing.
#   ended         — this entity PAID, and it ran out. Rendering that as "not subscribed"
#                   tells a customer they never bought something they did.
#   trial_expired — the free days ran out and nothing was ever charged. Distinct from
#                   ``ended`` for the mirror-image reason: telling someone who paid that
#                   their "trial expired" denies the purchase, and the two are told apart
#                   by ``first_billed_at`` exactly as ``get_module_cards`` tells its own
#                   "free trial expired" from its "access ended".
#
STATUS_ACTIVE = "active"
STATUS_TRIALING = "trialing"
STATUS_CANCELLED = "cancelled"
STATUS_PAST_DUE = "past_due"
STATUS_ENDED = "ended"
STATUS_TRIAL_EXPIRED = "trial_expired"
STATUS_NOT_SUBSCRIBED = "not_subscribed"

# What the badge reads. Lower case on purpose — it matches the design and, more
# usefully, keeps a status out of the way of the entity name beside it.
STATUS_LABELS = {
    STATUS_ACTIVE: "active",
    STATUS_TRIALING: "free trial",
    STATUS_CANCELLED: "cancelled",
    STATUS_PAST_DUE: "payment due",
    STATUS_ENDED: "ended",
    STATUS_TRIAL_EXPIRED: "trial expired",
    STATUS_NOT_SUBSCRIBED: "not subscribed",
}

# Sort order for the Status column, worst first. Ascending therefore surfaces the rows
# that need a decision — an overdue card, a subscription winding down — above the ones
# quietly working, which is the only ordering of a status column anyone wants.
#
# The three dormant states are ordered by how much was invested: a lapsed PAID module is
# the one most likely to be worth reviving, an expired trial less so, and never-held
# least of all.
_STATUS_RANK = {
    STATUS_PAST_DUE: 0,
    STATUS_CANCELLED: 1,
    STATUS_TRIALING: 2,
    STATUS_ACTIVE: 3,
    STATUS_ENDED: 4,
    STATUS_TRIAL_EXPIRED: 5,
    STATUS_NOT_SUBSCRIBED: 6,
}

SORT_FIELDS = ("entity", "subscriber", "country", "modules", "status", "next_billing")
DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 100

# A date far past anything real, used to park rows with no date at the END of an
# ascending sort. ``None`` cannot be compared against a datetime, and mapping it to
# ``datetime.min`` would sort "nothing scheduled" above "due tomorrow".
# Aware, because every date it is sorted against is: the columns behind ``_next_date``
# are all ``DateTime(timezone=True)``, so Postgres hands them back with a tzinfo and a
# naive sentinel raises "can't compare offset-naive and offset-aware datetimes" the
# moment one row has no date and another does. SQLite returns them naive, which is why
# the suite never saw it.
_NO_DATE = datetime.max.replace(tzinfo=UTC)


def _fmt(moment) -> str | None:
    """'15 Aug 2026' — the PADDED form; this grid sets dates in a column. See
    ``display.day_padded``, which ``payment_methods`` prints through too."""
    return display.day_padded(moment)


def _iso(moment) -> str | None:
    return moment.isoformat() if moment else None


# --- The payer's next billing date ---------------------------------------------
#
# ONE DATE FOR THE PAYER, whichever account is on screen. Every billing account renews on
# the payer's anchor ("one cycle billed as several invoices" — the billing-groups note in
# ``store``), so the accounts share it.


def next_billing_from(anchor, now) -> datetime | None:
    """The end of the anchor period ``now`` is inside — the next renewal boundary.

    NOT the anchor itself. ``anchor_at`` is the payer's FIRST charge and never moves, so
    printed as "Next Billing Date" it reads as a date in the past from the second month
    on — which is what the portal's landing printed until this existed. Projected through
    ``period_containing``, the arithmetic the renewal runner bills on, month-end clamp
    included.

    None when there is no cycle (nothing has ever been charged), or when the anchor cannot
    be projected: a date on a card is never worth failing the page it sits on.
    """
    from billing.services.billing import period_containing

    if not anchor:
        return None
    try:
        return period_containing(anchor, now).end
    except Exception:
        logger.exception("portal: could not project the next billing date from {}", anchor)
        return None


def next_billing_at(user_id) -> datetime | None:
    """``next_billing_from`` for one payer, now. Also what a company's settings page falls
    back to (``panel.get_next_payment_date``), so the two cannot name different days."""
    from billing.services import clock
    from billing.services import store as sub_store

    anchor, _currency = sub_store.billing_cycle_for_user(user_id)
    return next_billing_from(anchor, clock.now())


def next_bill_for_account(user_id, group_id, anchor, when) -> dict | None:
    """What ONE billing account's next renewal will charge - 08-B's "Amount (estimated)".

    Priced by the renewal runner's own ``build_renewal`` for the period that starts on the
    payer's next billing date, so the estimate and the invoice cannot disagree about what
    is charged or at what price: the account's companies billing forward, each on its own
    plan line (the bundle when it carries both modules), its trials that will have
    converted by then (``converting_by`` - the settings panel's rule), and any
    cancellation extension riding the invoice. ESTIMATED because what changes before the
    date - a trial lapsing, a module cancelled or added - changes the bill.

    ``{"amount": "HKD 400.00", "amount_minor": 40000, "currency": "HKD"}``, or None when
    there is no cycle yet, nothing to bill, or it cannot be priced (logged: a figure on a
    page is never worth failing the page).
    """
    from billing.services import renewals
    from billing.services.billing import period_containing

    if not anchor or when is None:
        return None
    try:
        period = period_containing(anchor, when)
        invoice = renewals.build_renewal(
            user_id, period, group_id=group_id, converting_by=period.start
        )
    except Exception:
        logger.exception("portal: could not estimate the next bill of account {}", group_id)
        return None
    if invoice is None or invoice.total <= 0:
        return None
    currency = (invoice.currency or "").upper()
    return {
        "amount": _money(invoice.total, currency),
        "amount_minor": invoice.total,
        "currency": currency,
    }


def _module_names() -> dict[str, str]:
    """Display name per canonical module code, from the catalog.

    Falls back to the code itself, which is what ``get_module_cards`` does — a catalog
    row can be missing in a half-seeded environment and a table with a blank column is
    worse than one reading "PAYMENT_REQUEST".
    """
    names = {code: code for code in MODULE_CODES}
    try:
        for fn in EntityFunction.objects.filter(function_code__in=MODULE_CODES):
            if fn.function_name:
                names[fn.function_code] = fn.function_name
    except Exception:
        # Same posture as the reads in get_module_cards: never fail the page over a
        # label. (Flask rolled its session back here; autocommit needs no reset.)
        logger.exception("portal: could not read module catalog names")
    return names


def _country_names(codes) -> dict[str, str]:
    """Country display name per ISO code. Missing rows fall back to the code."""
    wanted = {c for c in codes if c}
    if not wanted:
        return {}
    try:
        return {
            row.country_code: (row.country_name_en or row.country_code)
            for row in CountryInfo.objects.filter(country_code__in=wanted)
        }
    except Exception:
        logger.exception("portal: could not read country names")
        return {}


def _module_state(row, *, now, paid_through, grace_days) -> dict:
    """One module's status and the date printed beside it.

    The branches mirror ``get_module_cards`` exactly, including the two subtleties that
    took a while to settle there:

    * ``granted`` gates every live branch, not the phase. A phase stays ``active`` until
      something writes it, while access ends on a date that passes unattended — so
      reading the phase alone badges a lapsed module "active" next to a date in the past.
    * a CANCELLED trial is still a trial. Cancelling one does not end it, it only stops
      it converting, so the free days keep running and the date to print is the trial's
      own end rather than a paid-through it never had.
    """
    from billing.services import access

    if row is None:
        return {
            "status": STATUS_NOT_SUBSCRIBED,
            "date": None,
            "date_label": None,
        }

    phase = getattr(row, "phase", None) or ""
    trial_end = getattr(row, "trial_end", None)
    app_access_until = getattr(row, "app_access_until", None)
    never_billed = getattr(row, "first_billed_at", None) is None

    granted = access.grants_access(
        now,
        phase=phase,
        trial_end=trial_end,
        app_access_until=app_access_until,
        period_end=paid_through,
        past_due_grace_days=grace_days,
    )
    ends = access.access_end(
        phase=phase,
        trial_end=trial_end,
        app_access_until=app_access_until,
        period_end=paid_through,
        past_due_grace_days=grace_days,
    )

    if granted and phase == PHASE_TRIAL:
        return {"status": STATUS_TRIALING, "date": trial_end, "date_label": "Trial ends"}

    if granted and phase == PHASE_SCHEDULED_CANCEL:
        # Cancelled while still free vs cancelled after paying are the same phase and
        # very different sentences: one keeps its free days, the other keeps days it was
        # charged for. ``never_billed`` is what separates them.
        if never_billed and trial_end is not None:
            return {
                "status": STATUS_CANCELLED,
                "date": trial_end,
                "date_label": "Trial ends",
            }
        return {"status": STATUS_CANCELLED, "date": ends, "date_label": "Expires"}

    if granted and phase == PHASE_PAST_DUE:
        return {"status": STATUS_PAST_DUE, "date": ends, "date_label": "Access ends"}

    if granted and phase == PHASE_ACTIVE:
        # "Next billing" is the payer's paid-through: the renewal run bills at that
        # point, for every entity on the account at once.
        return {
            "status": STATUS_ACTIVE,
            "date": paid_through,
            "date_label": "Next billing",
        }

    # A row with no access. Two different sentences, and ``first_billed_at`` is the whole
    # difference: free days that ran out, or a subscription that was paid for and lapsed.
    #
    # Neither states a verdict on WHY, only the date it stopped. The entity card learned
    # that the hard way — a lapsed paid period keeps phase=active until a sweep writes it,
    # so "cancelled" would be a claim nobody made.
    if never_billed and trial_end is not None:
        return {
            "status": STATUS_TRIAL_EXPIRED,
            "date": trial_end,
            "date_label": "Trial ended",
        }

    # The date it STOPPED, and only that: the row's own end (its access end, its trial's),
    # else when its access ran out - a paid period that lapsed - once that has passed. Never a
    # date still ahead. The old last resort was ``paid_through``, the billing ACCOUNT's, which
    # keeps moving while the account renews for its other companies: a module terminated on
    # the spot (access cut, nothing of its own kept) read "Ended 18 Oct 2026" - a date to come,
    # under a word that says it is past. With no date of its own it now prints none.
    stopped = app_access_until or trial_end or ends
    if stopped is not None and stopped > now:
        stopped = None
    return {
        "status": STATUS_ENDED,
        "date": stopped,
        "date_label": "Ended" if stopped is not None else None,
    }


def _next_date(modules) -> datetime | None:
    """The soonest date the entity has anything happening on, for the sort column."""
    dates = [m["date"] for m in modules if m.get("date")]
    return min(dates) if dates else None


def _matches(row: dict, needle: str) -> bool:
    """Free-text search over what the table actually shows.

    Includes the status LABEL, so "cancelled" and "free trial" find rows — the words a
    customer sees on screen are the words they will type.
    """
    haystack = " ".join(
        [
            row["entity_name"] or "",
            row["country"] or "",
            row["subscriber"]["name"] or "",
            row["subscriber"]["email"] or "",
        ]
        + [f"{m['name']} {m['status_label']}" for m in row["modules"]]
    ).lower()
    return needle in haystack


def _sort_key(field: str):
    if field == "subscriber":
        return lambda r: (r["subscriber"]["name"] or "").lower()
    if field == "country":
        return lambda r: (r["country"] or "").lower()
    if field == "modules":
        # How many modules this entity actually holds. Sorting a multi-line cell
        # alphabetically would order by "Bill Payment" on every row and mean nothing;
        # the question the column can answer is "how much is switched on here".
        return lambda r: sum(
            1
            for m in r["modules"]
            if m["status"] in (STATUS_ACTIVE, STATUS_TRIALING, STATUS_PAST_DUE)
        )
    if field == "status":
        return lambda r: min(
            (_STATUS_RANK.get(m["status"], 9) for m in r["modules"]), default=9
        )
    if field == "next_billing":
        return lambda r: r["_next_date"] or _NO_DATE
    return lambda r: (r["entity_name"] or "").lower()


def _person(user, user_id=None) -> dict:
    """``{id, name, email}`` for a user row that may be missing.

    ``email`` falls back to ``username`` because the two are the same address for
    everyone signed up through the invite flow, and the settings user list prints
    ``username`` — a person shown there by one address and here by a blank is the same
    person twice.
    """
    return {
        "id": str(getattr(user, "id", None) or user_id or ""),
        "name": " ".join(
            p
            for p in [
                (getattr(user, "first_name", "") or "").strip(),
                (getattr(user, "last_name", "") or "").strip(),
            ]
            if p
        ),
        "email": (
            (getattr(user, "email", "") or "").strip()
            or (getattr(user, "username", "") or "").strip()
        ),
    }


def build_payer_subscriptions(
    user_id,
    *,
    query: str = "",
    sort: str = "entity",
    direction: str = "asc",
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> dict:
    """Every entity ``user_id`` is the payer for, with each module's status and date.

    Membership is not the test here — being the PAYER is. An admin of ten companies who
    pays for none of them gets an empty list, which is correct: this screen is the
    billing relationship, and the entity's own settings page is where a non-payer admin
    reads the same state (and is told who to ask).

    That also makes the endpoint above safe by construction. It filters on
    ``payer_user_id``, so there is no entity id to tamper with and no way to widen the
    result to somebody else's companies.

    Searching, sorting and paging are done in Python rather than SQL. The set is one
    payer's entities — tens, not thousands — and the sort keys ("worst status first",
    "how many modules are live") are computed from the access rules above, which SQL
    cannot express without duplicating them.
    """
    from billing.services import clock, policy
    from billing.services import store as sub_store

    payer = _by_pk(User, user_id)
    rows = sub_store.module_rows_for_payer(user_id)

    now = clock.now()
    grace_days = policy.current().past_due_window_days
    anchor_at, currency = sub_store.billing_cycle_for_user(user_id)

    by_entity: dict[str, dict] = {}
    for row in rows:
        by_entity.setdefault(str(row.entity_id), {})[
            (row.function_code or "").upper()
        ] = row

    entities = list(Entity.objects.filter(id__in=list(by_entity))) if by_entity else []
    countries = _country_names(getattr(e, "country_code", None) for e in entities)
    names = _module_names()

    subscriber = _person(payer, user_id)
    # The account-level figure for the summary block: the earliest date anything on this
    # payer's account is paid through. (F5: the summary used to read the LAST company's
    # per-card ``paid_through`` from the loop below - undefined for a payer with no
    # companies, so their page answered 500.)
    account_paid_through = sub_store.paid_through_for_user(user_id)

    items: list[dict] = []
    for entity in entities:
        entity_rows = by_entity.get(str(entity.id), {})
        # PER COMPANY, not per payer. Each is billed on the card it was nominated onto,
        # and each card buys its own periods — so one row of this table can be past due
        # while the one under it is paid up, which is exactly what the screen has to show.
        paid_through = sub_store.paid_through_for_entity(str(entity.id))
        modules = []
        for code in MODULE_CODES:
            state = _module_state(
                entity_rows.get(code),
                now=now,
                paid_through=paid_through,
                grace_days=grace_days,
            )
            modules.append(
                {
                    "code": code,
                    "name": names.get(code, code),
                    "status": state["status"],
                    "status_label": STATUS_LABELS[state["status"]],
                    "date_label": state["date_label"],
                    "date": _fmt(state["date"]),
                    "date_iso": _iso(state["date"]),
                    # Kept unformatted for the sort below and stripped before the
                    # response is returned.
                    "_date": state["date"],
                }
            )
        items.append(
            {
                "entity_id": str(entity.id),
                "entity_name": entity.name or "",
                "country_code": getattr(entity, "country_code", None),
                "country": countries.get(
                    getattr(entity, "country_code", None) or "",
                    getattr(entity, "country_code", None) or "",
                ),
                "subscriber": subscriber,
                "modules": modules,
                # A Minty PATH, not a URL. The frontend hands its token back through
                # ``buildMintyEnterUrl`` to get a session; a bare origin would land the
                # user on the login form. Same contract as the notice endpoint's
                # ``settings_path``.
                "settings_path": f"/entity/settings/module/{entity.id}",
                # Step 3 addition over Flask's answer (minty-web's Manage Subscriptions list
                # is "newest company first" by design and sorts on this; ISO like
                # ``date_iso``, since it is for computing on, not reading).
                "created_at": _iso(getattr(entity, "created_at", None)),
                "_next_date": None,
            }
        )

    for item in items:
        item["_next_date"] = _next_date(
            [{"date": m["_date"]} for m in item["modules"]]
        )

    needle = (query or "").strip().lower()
    if needle:
        items = [i for i in items if _matches(i, needle)]

    total = len(items)
    field = sort if sort in SORT_FIELDS else "entity"
    items.sort(key=_sort_key(field), reverse=(direction == "desc"))

    per_page = max(1, min(int(per_page or DEFAULT_PER_PAGE), MAX_PER_PAGE))
    pages = max(1, -(-total // per_page))
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * per_page
    window = items[start:start + per_page]

    for item in window:
        item.pop("_next_date", None)
        for module in item["modules"]:
            module.pop("_date", None)

    # HOW THIS PAYER'S OWN OFFERS ENDED, where they have not been told yet - the one place
    # a finished handover is visible. Advisory: a failure here leaves the list empty and the
    # billing page still renders, exactly as the pending read below does. A modal is not
    # worth taking a page down for.
    try:
        from billing.services import transfers

        outcomes = transfers.unseen_outcomes(user_id)
    except Exception:
        logger.exception("portal: could not read finished handovers for %s", user_id)
        outcomes = []

    next_billing = next_billing_from(anchor_at, now)
    return {
        "payer": subscriber,
        # Empty list rather than absent when there is nothing, the shape rule the rest of
        # this payload follows.
        "transfer_outcomes": outcomes,
        "billing": {
            # The cycle's START, kept for the port's fidelity. Not a date to print as
            # "next": that is ``next_billing``.
            "anchor": _fmt(anchor_at),
            "anchor_iso": _iso(anchor_at),
            "paid_through": _fmt(account_paid_through),
            "paid_through_iso": _iso(account_paid_through),
            # Added over Flask's answer: the date the payer is next billed.
            "next_billing": _fmt(next_billing),
            "next_billing_iso": _iso(next_billing),
            "currency": currency,
        },
        "entities": window,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "sort": field,
        "direction": "desc" if direction == "desc" else "asc",
        "query": query or "",
    }


# --- Change subscriber (READ ONLY) -------------------------------------------
#
# Who an entity's bill COULD be handed to. Nothing here moves it: handing an entity to a
# different payer moves an established financial relationship — their card, their cycle,
# their invoice — and the write has no route, no proration preview and no audit entry
# today. This answers the list the screen renders and stops there.
#
# ADMINS ONLY, and that is a business rule rather than a permission check. A cashier
# cannot be made responsible for a company's bill, so offering one would be offering a
# choice that must then be refused. It mirrors the Settings user list — same membership
# table, same approved-only filter — narrowed to the role that can hold a subscription.


def _admin_candidates(entity_id) -> list[dict]:
    """Approved members of ``entity_id`` whose role can hold the bill.

    ``role_at_least(..., admin)`` rather than ``== "admin"``: ``super_admin`` outranks
    admin on the same ladder (``permission_policy.ROLE_RANK``), and dropping someone for
    being MORE senior than the bar is the kind of filter that reads as a missing user.
    """
    from core.policy import Role, role_at_least
    from shared_models.models import UserEntity

    try:
        # Same filter as the Settings user list: an unapproved row is a request to join,
        # not a member.
        memberships = list(
            UserEntity.objects.filter(entity_id=str(entity_id), approved=True)
            .values_list("user_id", "role")
        )
        # And the ACCOUNT has to be live, not just the membership. A deactivated user
        # keeps their ``user_entity`` row and so kept appearing here — an offer to them
        # can never be accepted, and it freezes the payer's own exit behind an inbox
        # nobody can open.
        people = User.objects.in_bulk([uid for uid, _role in memberships])
        rows = [
            (people[uid], role)
            for uid, role in memberships
            if uid in people and people[uid].approved is True
        ]
    except Exception:
        logger.exception("portal: could not read members of entity %s", entity_id)
        return []

    return [
        _person(user)
        for user, role in rows
        if role_at_least(role, Role.ADMIN.value)
    ]


def build_subscriber_options(user_id, entity_id) -> dict | None:
    """The Change-subscriber screen: one entity, its current payer, and the admins.

    Returns None when ``user_id`` is not the payer for that entity — which the route
    turns into a 404. This is the first payer-portal read with an entity id IN the
    request, so it is also the first that can be pointed somewhere; every other endpoint
    is safe by construction because it filters on the token's user. Being the payer is
    the same test ``store.may_manage_subscription`` applies before anything about a
    subscription can be changed, so the list of who could take it over is not readable by
    someone who could not hand it over.
    """
    from billing.services import store as sub_store

    entity = _by_pk(Entity, entity_id) if entity_id else None
    if entity is None:
        return None

    payer_id = sub_store.payer_for_entity(entity.id)
    if payer_id is None or str(payer_id) != str(user_id):
        return None

    current = _person(_by_pk(User, payer_id), payer_id)

    # The day this company's money runs to - the footer's "paid up until ...". Advisory, like
    # the pending read below: the screen renders without it (the sentence is simply left off),
    # so a company whose billing cannot be read still offers the handover.
    try:
        paid_through = sub_store.paid_through_for_entity(entity.id)
    except Exception:
        logger.exception("portal: could not read what %s is paid through", entity.id)
        paid_through = None

    # The current payer LEADS the list and is included whether or not the membership
    # query produced them — they may have been demoted since, or hold no row on this
    # entity at all. A list missing the person the banner says is being billed
    # contradicts the banner.
    rest = sorted(
        (c for c in _admin_candidates(entity.id) if c["id"] != current["id"]),
        key=lambda c: (c["name"] or c["email"] or "").lower(),
    )

    # Why the button may be unavailable, and what it would cost — both computed here so
    # the screen can SAY so before the click. Without them the only way to learn that a
    # handover is refused is to attempt one, which is a poor way to find out that a trial
    # is running or a debt is outstanding.
    from billing.services import transfers

    try:
        pending = transfers.pending_transfer_for_entity(entity.id)
    except Exception:
        # Advisory, not load-bearing: the screen still renders and the POST still
        # refuses on its own checks.
        logger.exception("portal: could not read a pending handover for %s", entity.id)
        pending = None

    blockers: list[str] = []
    quotes: dict[str, dict] = {}
    inherited: dict[str, list] = {}
    if pending is None and rest:
        try:
            # ENTITY-level refusals, asked once. A candidate has to be named to ask, but
            # only the answers true of the COMPANY are kept — a trial still running, a
            # debt outstanding — because those hold whoever accepts.
            blockers = [
                reason
                for reason in transfers.transfer_blockers(
                    entity.id, from_user_id=payer_id, to_user_id=rest[0]["id"]
                )
                # Refusals about a PARTICULAR CANDIDATE are dropped: they say nothing
                # about the entity, and "that person needs to be an admin" above a list of
                # five people reads as though none of them could take it. They are also
                # computed against ``rest[0]`` alone, so they would be one candidate's
                # answer shown over everybody. The remaining ones are true of whoever
                # accepts.
                if not reason.startswith("That person")
            # ONE reason, the most blocking. ``transfer_blockers`` is ordered by priority
            # and the refusal paths already answer with the first, so sending the whole
            # list would let the screen say more than the POST will — and four problems at
            # once is a wall the reader has to triage rather than an instruction.
            ][:1]
            if not blockers:
                # PER CANDIDATE, and the earlier version of this was simply wrong. The
                # quote is NOT a fact about the entity: its window ends at the
                # RECIPIENT's period end, derived from their own billing anchor. Two
                # admins whose cycles turn over on different days are charged different
                # amounts for the same handover, so a single figure labelled "whoever
                # accepts" is correct only for the one person it was priced against.
                #
                # Affordable done properly: pricing reads the recipient's cycle and the
                # plan, both local, and does not touch the processor.
                for candidate in rest:
                    priced = transfers.quote_transfer(
                        entity.id, to_user_id=candidate["id"]
                    )
                    if priced is not None:
                        quotes[candidate["id"]] = priced
                    # Trials the candidate would INHERIT — free days now, a charge on
                    # their card later. Per candidate for the same reason the quote is:
                    # the conversion is priced against their own cycle.
                    inherited[candidate["id"]] = transfers.trial_disclosure(
                        entity.id, candidate["id"]
                    )
        except Exception:
            logger.exception("portal: could not price a handover for %s", entity.id)
            blockers, quotes, inherited = [], {}, {}

    return {
        "entity": {"entity_id": str(entity.id), "entity_name": entity.name or ""},
        # The day the outgoing payer's money stops covering the company. It is a fact about
        # the ENTITY, so it is answered here and not left to be read off a quote: quotes are
        # priced per candidate and only when there IS one - a company whose payer is its only
        # admin has none, and the screen's footer was losing "paid up until ..." entirely.
        "paid_through": paid_through,
        "current": current,
        "candidates": [
            {**current, "is_current": True},
            # Each carries ITS OWN price, because the amount depends on the recipient's
            # billing anchor rather than on the entity.
            *(
                {
                    **c,
                    "is_current": False,
                    "quote": quotes.get(c["id"]),
                    "trials": inherited.get(c["id"]) or [],
                }
                for c in rest
            ),
        ],
        "blockers": blockers,
        "pending_transfer": (
            {
                "id": pending.id,
                "to_user_id": pending.to_user_id,
                "status": pending.status,
                "since": pending.created_at,
            }
            if pending is not None
            else None
        ),
    }


def invite_admin_to_entity(user_id, entity_id, email: str, *, send) -> tuple[bool, str]:
    """Invite ``email`` into the entity as an ADMIN. Returns ``(ok, message)``.

    This is the "Invite someone new" half of Change subscriber, and it is ONLY the first
    half. It gets a person into the company; it does not hand them the bill. Handing over
    the bill needs a transfer that survives the period already paid for — see the note on
    the Change-subscriber screen — and nothing here moves a payer.

    The role is fixed at ADMIN and not a parameter. The screen exists to widen the list of
    people who could take the subscription over, and only an admin can: every money route
    carries ``Permission.MODULE_MANAGE`` on top of being the payer. Inviting a cashier
    from here would add somebody who can never appear in the list they were invited to
    join.

    Two gates, and they answer different questions:

    * the caller must be the PAYER for this entity — the same test that lets them read the
      candidate list at all, so the screen and its action agree about who may use it;
    * and must hold ``USER_INVITE`` on it, because this adds a MEMBER to a company. Being
      the payer is a billing relationship and does not by itself confer the right to give
      somebody access.

    THE INVITATION ITSELF IS FLASK'S. Membership, the invite row, its token, expiry and
    the email are the identity system's until Part 3, so the write is delegated to
    ``send`` — ``send(body) -> (payload, status)`` — which the route supplies as a forward
    of the caller's bearer to Flask's ``POST /api/onboarding/invite`` (``core.flask_client``;
    the session-cookie ``/minty/api/invitation/send`` cannot take a bearer); a test passes a
    lambda. Flask's answer is read here: a 2xx with ``email_sent`` (top level, as the
    onboarding endpoint puts it, or under ``invitation`` as the session one does), or an
    error sentence under ``error`` / ``message`` that is already customer-facing ("already
    a member", "an invitation is already pending") and passes through.
    """
    from billing.services import store as sub_store
    from core.policy import Permission, has_permission

    address = (email or "").strip()
    if not address or " " in address or address.count("@") != 1 or not all(address.split("@")):
        return False, "That doesn't look like an email address."

    entity = _by_pk(Entity, entity_id) if entity_id else None
    if entity is None:
        return False, "That company isn't on your billing account."

    payer_id = sub_store.payer_for_entity(entity.id)
    if payer_id is None or str(payer_id) != str(user_id):
        # Same answer as an unknown entity. Telling the two apart would confirm an id to
        # someone who should not be asking.
        return False, "That company isn't on your billing account."

    user = _by_pk(User, user_id)
    if not has_permission(user, Permission.USER_INVITE, str(entity.id)):
        return False, "You don't have permission to invite people to this company."

    payload, status = send({"entity_id": str(entity.id), "email": address, "role": "admin"})
    payload = payload if isinstance(payload, dict) else {}
    if not 200 <= int(status or 0) < 300:
        return False, (
            payload.get("error") or payload.get("message") or "That invitation couldn't be created."
        )

    # The row is the invitation; the email is how it is delivered. A send failure leaves a
    # valid pending invite that can be resent from the entity's Users page, so it is
    # reported rather than undone — deleting it would throw away a good record because
    # SMTP hiccuped.
    email_sent = payload.get("email_sent")
    if email_sent is None:
        email_sent = (payload.get("invitation") or {}).get("email_sent", True)
    if not email_sent:
        return True, (
            f"Invited {address} as an admin, but the email didn't send. "
            "You can resend it from the company's Users page."
        )
    return True, f"Invited {address} as an admin of {entity.name}."


# --- The Invoices tab --------------------------------------------------------
#
# One invoice per payer per period, with a LINE per entity — so "invoices for an entity"
# is a filter over lines, not a separate set. Filtering therefore changes two things and
# not just the row count: the description becomes that entity's products, and the amount
# becomes that entity's share rather than the invoice total. Showing the whole total
# against one company would overstate what it cost by however many other companies rode
# the same invoice.
#
# `entity_name` and `product_name` are SNAPSHOTS on the line (see the model docstring):
# an entity renamed next year must not rewrite what last year's invoice said. So the
# filter list is built from the LINES, not from the entity table — a company you have
# since stopped paying for still has invoices, and still belongs in the dropdown.

# Stripe's vocabulary, kept as-is in the column, turned into words here.
INVOICE_STATUS_LABELS = {
    "paid": "Paid",
    "open": "Unpaid",
    "draft": "Draft",
    "uncollectible": "Failed",
    "void": "Void",
}


def _money(amount_minor, currency_code) -> str:
    """"HK$400.00", or "HKD 400.00" — symbol from ``currency_info``, never hardcoded.

    The symbol lookup and the code-vs-glyph spacing both live in ``services.money`` now;
    this used to hold its own copy of each, as did ``modules._fmt_money``.
    """
    from billing.services import money

    return money.format_with_symbol(amount_minor, currency_code)


def _reference(invoice) -> str:
    """What to print in the Invoice # column.

    There is no sequential invoice NUMBER in the schema — the processor's id is the only
    external reference we hold, and a locally invented counter would be a second identity
    for the same document that support could not look up anywhere. So: the processor's id
    when we have one, and a short form of the local id when the row was reserved but
    never sent (``external_id`` is NULL — see ``store.reserve_invoice``).
    """
    if invoice.external_id:
        return invoice.external_id
    return f"#{str(invoice.id)[:8].upper()}"


def _issued(invoice):
    """When an invoice was issued: finalized, else created. The list's date and the PDF's
    Bill Date both read this, so the two can never name different days."""
    return invoice.issued_at or invoice.created_at


#: The statuses an invoice is a document in: finalized and sent, and not withdrawn.
DOCUMENT_STATUSES = frozenset({"paid", "open", "uncollectible"})


def has_document(invoice) -> bool:
    """Whether the invoice has a PDF (``GET /api/me/invoices/{id}/pdf``).

    It must have reached the processor (``external_id``) and been finalized — paid, open or
    uncollectible. A draft was never issued; a void one was withdrawn (a refused purchase, or
    the dead half of a refresh); and a reservation the processor never saw is no document at
    all. The list's ``has_pdf`` and the route's 409 both ask this, so the button is only ever
    offered where the download works.
    """
    return bool(invoice.external_id) and (invoice.status or "").lower() in DOCUMENT_STATUSES


def invoice_account_id(invoice, account_ids) -> str | None:
    """The billing account an invoice belongs to: the one that raised it — or, for an
    invoice raised before accounts existed, the payer's OLDEST (``account_ids`` is oldest
    first), the attribution dunning makes when it collects them
    (``dunning._invoices_for_group``). None when the payer has no account at all."""
    own = getattr(invoice, "billing_group_id", None)
    if own:
        return str(own)
    return str(account_ids[0]) if account_ids else None


def _products(lines) -> list[str]:
    """The plan names to print in the Description column: what was BOUGHT.

    Two rules, both about lines that are not purchases:

    CREDITS ARE NOT PRODUCTS. An upgrade invoice carries both halves of the change —
    the old plan credited (negative, ``kind="unused"``) and the new one charged. Listing
    every line's product read "Petty Cash · Super Minty", which describes the transition
    rather than the thing bought and looks like two subscriptions on one entity. Only
    lines that charge name the invoice. A wholly negative invoice keeps its names, since
    a credit note with an empty description says nothing at all.

    NO PARENTHETICALS. ``renewals`` and the cancel path suffix the snapshot with
    "(access extension)" / "(access after cancellation)" so the LINE explains itself.
    Summarised into one cell they turn a renewal into "Petty Cash · Petty Cash (access
    after cancellation)" — the same product twice, and dedupe cannot see it. The suffix
    is left on the line and on the memo, where there is room to explain it.

    Deduped but ORDER-PRESERVING: "Super Minty" twice is two entities on the same plan,
    not something to print twice.
    """
    charged = [ln for ln in lines if (ln.amount or 0) > 0] or list(lines)
    products: list[str] = []
    for line in charged:
        name = _plan_name(line.product_name)
        if name and name not in products:
            products.append(name)
    return products


def _plan_name(product_name: str | None) -> str:
    """``"Petty Cash (access extension)"`` -> ``"Petty Cash"``."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", product_name or "").strip()


def _is_extension(line) -> bool:
    """A line billing access that continues past a cancellation.

    Identified by the parenthetical the biller writes into the snapshot — "(access
    extension)" from ``cancellation_invoice``, "(access after cancellation)" from the
    renewal runner. There is no column for it: ``kind`` says how the amount was measured
    (whole period / prorated / credited), not what the charge is FOR.
    """
    return "(access" in (getattr(line, "product_name", "") or "").lower()


# What an invoice IS. Derived from the line kinds rather than stored, because the kinds
# are already the record of how each amount was arrived at and a second column saying the
# same thing is a second column that can disagree.
#
#   renewal    the monthly bill — at least one whole-period line
#   upgrade    a mid-period change: the old plan credited, the new one charged
#   start      a module beginning mid-period, prorated, with nothing to credit
#   extension  access bought past a cancellation, and nothing else
#   credit     nothing positive on it at all
EVENT_LABELS = {
    "renewal": "Renewal",
    "upgrade": "Upgrade",
    "start": "New subscription",
    "extension": "Access extension",
    "credit": "Credit",
    "charge": "Charge",
}


def _kind(line) -> str:
    return (getattr(line, "kind", "") or "full").lower()


def _event(lines) -> str:
    kinds = {_kind(ln) for ln in lines}
    charged = [ln for ln in lines if (ln.amount or 0) > 0]

    if not charged:
        return "credit"
    if "unused" in kinds and "remaining" in kinds:
        return "upgrade"
    # A renewal that also carries extensions is still a renewal: the whole-period lines
    # are the reason the invoice exists and the extension rides along. Checked BEFORE
    # the extension case for exactly that reason.
    #
    # It takes a whole-period line that is NOT itself an extension. The renewal runner
    # writes a cancelled company's extension as kind "full" too (``Line.kind``'s default,
    # ``renewals._pending_extension_lines``), so a card renewing only to collect the
    # extension of its last company read as a "Renewal" of a module nobody still has.
    if any(_kind(ln) == "full" and not _is_extension(ln) for ln in lines):
        return "renewal"
    if all(_is_extension(ln) for ln in charged):
        return "extension"
    if "remaining" in kinds:
        return "start"
    return "charge"


def _period_span(start, end) -> str | None:
    """``"28 Jul – 28 Aug 2026"``, or ``"20 Dec 2026 – 20 Jan 2027"`` across a year.

    The year is printed once when both ends share it. Matching ``billing._span`` and the
    memos, days are NOT zero-padded here — this reads as a sentence, unlike the Date
    column beside it.
    """
    if not start or not end:
        return None
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def _describe(invoice, lines) -> tuple[str, str]:
    """``(headline, detail)`` for the Description column.

    The plan name alone answered "what am I subscribed to", which the customer already
    knows — it could not tell a renewal from an upgrade from the extension charged when
    something was cancelled, and three invoices in one month all read "Petty Cash". The
    headline says what HAPPENED; the detail says over which days and for which company.

    The stored ``memo`` explains the arithmetic in full and travels beside these rather
    than being parsed into them: it is a sentence the biller wrote when it knew the
    figures, and re-deriving it from the columns is how the two start disagreeing.
    """
    event = _event(lines)
    products = _products(lines)

    if event == "upgrade":
        old = next(
            (_plan_name(ln.product_name) for ln in lines
             if (ln.kind or "") == "unused"),
            None,
        )
        new = next(
            (_plan_name(ln.product_name) for ln in lines
             if (ln.amount or 0) > 0),
            None,
        )
        headline = (
            f"Upgrade · {old} → {new}"
            if old and new
            else f"Upgrade · {', '.join(products)}"
        )
    else:
        label = EVENT_LABELS.get(event, EVENT_LABELS["charge"])
        headline = f"{label} · {', '.join(products)}" if products else label

    parts = []
    span = _period_span(invoice.period_start, invoice.period_end)
    if span:
        parts.append(span)

    # One company gets NAMED; several get counted. A payer with six entities on one
    # renewal would otherwise turn this cell into a paragraph, and the Entity filter
    # above the table is the way to ask about one of them.
    names = sorted({ln.entity_name for ln in lines if ln.entity_name})
    if len(names) == 1:
        parts.append(names[0])
    elif names:
        parts.append(f"{len(names)} entities")

    # Called out separately because a cancellation charge is the one line on a renewal a
    # customer is not expecting — the same reason ``renewal_memo`` says it.
    extensions = sum(1 for ln in lines if _is_extension(ln) and (ln.amount or 0) > 0)
    if extensions and event != "extension":
        parts.append(
            f"{extensions} access extension{'' if extensions == 1 else 's'}"
        )

    return headline, " · ".join(parts)


#: An invoice whose charge was made and declined. Nothing leaves one ``open`` without trying:
#: an invoice issued without collecting stays a DRAFT (``billing_gateway.issue_invoice``). The
#: one brief exception is a RE-ISSUE (``billing_gateway.refresh_invoice``): the replacement is
#: open a moment before it is charged, in the same attempt - and if that attempt dies between
#: the two, the next one charges it.
FAILED_INVOICE_STATUSES = ("open", "uncollectible")


def retryable_invoice_ids(user_id, failed=None) -> set[str]:
    """The invoices 08-B's *Retry payment* is offered on: per card, the ONE open invoice
    ``dunning.retry_now`` would charge right now. Read from our own rows - no Stripe call, and
    nothing written (``retry_now``'s own context closes spent episodes; a list must not).

    ``failed`` is the payer's failed invoices when the caller has read them already (the
    invoice list has: all of them, before any narrowing); left out, they are read here. No
    failed invoice, nothing else is read - most payers, most of the time.

    The engine's rule decides, called rather than restated (``dunning._manual_target``): the
    current period's renewal, else an open mid-period charge, never an abandoned give-up bill -
    and nothing on a card past its give-up deadline, or whose access has already run out. That
    last holds with or without a dunning stamp: an episode closed by giving up leaves
    ``paid_through`` where it stopped, so its abandoned renewal IS "the current period" by
    key, and the business does not chase those (2026-08-11). A renewal's ``idempotency_key`` IS
    its ``renewal_key`` (``renewals.period_key`` feeds both), so a row answers what Stripe's
    metadata would. A failed invoice not in this set is shown failed, with no button that could
    only refuse.
    """
    from datetime import timedelta

    from billing.services import clock, dunning, policy
    from billing.services import store as sub_store
    from shared_models.models import SubscriptionInvoice

    if failed is None:
        failed = SubscriptionInvoice.objects.filter(
            payer_user_id=str(user_id), status__in=FAILED_INVOICE_STATUSES
        )
    # Oldest first, as ``_manual_target``'s fallback wants them; a reservation Stripe never
    # confirmed has no invoice to charge.
    open_rows = sorted(
        (
            row
            for row in failed
            if (row.status or "").lower() in FAILED_INVOICE_STATUSES and row.external_id
        ),
        key=lambda row: row.created_at,
    )
    if not open_rows:
        return set()
    account = sub_store.customer_mapping_for_user(user_id)
    groups = sub_store.billing_groups_for_payer(user_id)
    if account is None or not groups:
        return set()

    first = str(groups[0].id)  # an invoice from before accounts is the oldest one's
    by_card: dict[str, list[dict]] = {}
    for row in open_rows:
        key = row.idempotency_key or ""
        # What ``_manual_target`` reads off a Stripe invoice: a renewal's idempotency key IS
        # its renewal key; any other charge has none.
        by_card.setdefault(str(row.billing_group_id or first), []).append(
            {"id": str(row.id),
             "metadata": {"renewal_key": key if key.startswith("renewal-") else None}}
        )

    now = clock.now()
    window = policy.current().past_due_window_days
    retryable: set[str] = set()
    for group in groups:
        mine = by_card.get(str(group.id))
        if not mine:
            continue
        started = group.dunning_started_at
        access_ends_at = (
            group.paid_through + timedelta(days=window) if group.paid_through else None
        )
        if started is not None and dunning.should_give_up(now, started, window, access_ends_at):
            continue
        if access_ends_at is not None and now >= access_ends_at:
            continue  # access has run out: a lapsed account's bill, not one to chase
        target = dunning._manual_target(mine, dunning._current_period_key(account, group))
        if target is not None:
            retryable.add(target["id"])
    return retryable


def build_payer_invoices(
    user_id,
    *,
    entity_id: str | None = None,
    account_id: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """The payer's invoices, newest first, optionally narrowed to one entity or one
    billing account.

    Filtered on ``payer_user_id``, so — like the rest of the portal — there is no id in
    the request that could reach another payer's history. ``entity_id`` narrows within
    that; an entity the caller does not pay for simply matches nothing.

    ``account_id`` narrows to the invoices ONE billing account raised (08-B is one
    account's page). An invoice raised before accounts existed carries no account, and
    belongs to the payer's OLDEST — the attribution dunning already makes when it collects
    them (``dunning._invoices_for_group``), so the page and the collection agree on whose
    debt it is. An account that is not the caller's matches nothing. Narrowed in Python,
    like the entity filter, over a set already fixed to the payer.
    """
    from shared_models.models import SubscriptionInvoice

    invoices = list(
        SubscriptionInvoice.objects.filter(payer_user_id=str(user_id))
        .order_by("-period_start", "-created_at")
        .prefetch_related("lines")
    )
    # Which row *Retry payment* sits on, judged per card over ALL the payer's failed invoices
    # (as the engine judges), so before the narrowing below. None failed: nothing more is read.
    failed = [inv for inv in invoices if (inv.status or "").lower() in FAILED_INVOICE_STATUSES]
    retryable = retryable_invoice_ids(user_id, failed) if failed else set()

    wanted_account = str(account_id) if account_id else None
    if wanted_account:
        from billing.services import store as sub_store

        owned = [str(g.id) for g in sub_store.billing_groups_for_payer(user_id)]
        if wanted_account not in owned:
            invoices = []
        else:
            invoices = [
                inv for inv in invoices if invoice_account_id(inv, owned) == wanted_account
            ]

    # The dropdown, from history rather than from current subscriptions.
    seen: dict[str, str] = {}
    for invoice in invoices:
        for line in invoice.lines.all():
            seen.setdefault(str(line.entity_id), line.entity_name or "")
    entity_options = sorted(
        ({"id": eid, "name": name} for eid, name in seen.items()),
        key=lambda e: (e["name"] or "").lower(),
    )

    wanted = str(entity_id) if entity_id else None
    rows: list[dict] = []
    for invoice in invoices:
        lines = list(invoice.lines.all())
        if wanted:
            lines = [ln for ln in lines if str(ln.entity_id) == wanted]
            if not lines:
                continue

        headline, detail = _describe(invoice, lines)

        # Filtered: this entity's share. Unfiltered: what was actually SENT, which is
        # `total` and not a sum of lines — the gateway drops zero-amount lines.
        amount = sum(ln.amount or 0 for ln in lines) if wanted else (invoice.total or 0)
        status = (invoice.status or "").lower()
        issued = _issued(invoice)

        rows.append(
            {
                "id": str(invoice.id),
                "reference": _reference(invoice),
                "date": _fmt(issued),
                "date_iso": _iso(issued),
                # WHEN IT WAS PAID, which is not when it was issued. Null while an
                # invoice is open and on one that failed, and the grid prints "—"
                # rather than borrowing `date` — a settlement date that is really an
                # issue date is wrong quietly, which is the worst way to be wrong
                # about money.
                "paid": _fmt(invoice.paid_at),
                "paid_iso": _iso(invoice.paid_at),
                "period_start": _fmt(invoice.period_start),
                "period_end": _fmt(invoice.period_end),
                # What happened, then over which days and for which company. The memo
                # rides along in full — it is the only place the arithmetic behind a
                # prorated figure is written down, and it is too long for the cell.
                "description": headline,
                "description_detail": detail,
                "memo": getattr(invoice, "memo", None),
                "amount": _money(amount, invoice.currency),
                "amount_minor": amount,
                "currency": (invoice.currency or "").upper(),
                "status": status,
                "status_label": INVOICE_STATUS_LABELS.get(
                    status, (status or "unknown").title()
                ),
                # The one invoice per card that *Retry payment* would charge right now
                # (``retryable_invoice_ids``); every other failed one is shown failed only.
                "retryable": str(invoice.id) in retryable,
                # A SNAPSHOT taken when the charge settled — never the account's current
                # default, which is a different card the moment anyone updates one, and
                # the invoice it would be wrong about first is a failed one. Null for
                # invoices raised before the column existed, and the UI says "not
                # recorded" rather than guessing.
                "payment_method": getattr(invoice, "payment_method", None),
                # Stripe's hosted page (while open, a way to pay). Null until finalized.
                # minty-web no longer links it: its "Invoice PDF" is our own document.
                "hosted_invoice_url": getattr(invoice, "hosted_invoice_url", None),
                # Whether ``/invoices/{id}/pdf`` has a document to serve - the same rule
                # the route refuses on (``has_document``), so the button never 409s.
                "has_pdf": has_document(invoice),
                "entities": sorted({ln.entity_name for ln in lines if ln.entity_name}),
            }
        )

    total = len(rows)
    per_page = max(1, min(int(per_page or 10), MAX_PER_PAGE))
    pages = max(1, -(-total // per_page))
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * per_page

    return {
        "invoices": rows[start:start + per_page],
        "entity_options": entity_options,
        "entity_id": wanted,
        "account_id": wanted_account,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }


# --- One invoice, company by company (08-B's "Billing Breakdown") ----------------------

#: How a cancellation extension's product is named on its line (``renewals._pending_extension_lines``).
EXTENSION_SUFFIX = " (access after cancellation)"


def _payers_invoice(user_id, invoice_id):
    """One of THIS payer's invoices, its lines prefetched — or None for a malformed id,
    another payer's invoice, or no invoice at all (one answer: someone who should not be
    asking learns nothing). The breakdown and the PDF both come through here, so no id in a
    request reaches another payer's history by either route."""
    import uuid

    from shared_models.models import SubscriptionInvoice

    try:
        wanted = str(uuid.UUID(str(invoice_id)))
    except ValueError:
        return None
    return (
        SubscriptionInvoice.objects.filter(id=wanted, payer_user_id=str(user_id))
        .prefetch_related("lines")
        .first()
    )


def build_invoice_breakdown(user_id, invoice_id) -> dict | None:
    """One invoice, company by company - 08-B's "Billing Breakdown · Download csv".

    A row per line the invoice charged (``subscription_invoice_line``, in its order): the
    company, the subscription, what it costs a month, the days the line paid for and what
    was charged for them (a credit is negative). The web writes the CSV. None when the
    invoice is not this payer's - no id in the request reaches another payer's history.

    WHAT A LINE CARRIES. The company, the product and the charge, captured at issue - and,
    since 2026-09-25 (schema item 23), what the line PAID FOR: its days and the price per
    period they were charged at, written by whatever priced it (``billing.Line``). Those are
    read as they are. One of them can be missing on a recorded line: an access extension
    whose rate stepped part-way (a bundle winding down) had no single rate, and its row
    shows the rate that makes it add up over the days it recorded.

    A LINE ISSUED BEFORE THEN recorded neither, and they are read back from how each kind of
    line is priced (``billing.prorate`` / ``billing.extension_charge``):

    * a renewal (``full``) pays for the invoice's whole period - its rate IS its charge;
    * a module started or upgraded mid-period (``remaining``), and the credit for the plan
      it replaced (``unused``), cover ``at`` to the period's end, prorated by the second - so
      the rate is the charge scaled back up to the whole period, to the nearest whole unit
      (every plan is priced in whole units);
    * a cancellation extension (``… (access after cancellation)``) pays for the days of
      access past the paid period: from the invoice's start to the module's access end, priced
      against the period BEFORE the invoice (the one the paid days belonged to). The end is
      the module row's ``app_access_until`` while it still says so; the rate is the charge
      scaled back up over that period - so a module leaving a bundle shows the MARGINAL rate
      it was actually charged at (Super Minty less the module kept), not the catalogue's
      price, and the row adds up. A module resumed since has cleared its access end: the day
      is then left out rather than guessed, and the rate falls back to the catalogue's -
      the gap the recorded columns exist to close.

    A zero line is left out: the gateway never sent it, so it is on no invoice anyone saw.
    """
    from datetime import timedelta

    from billing.services import money
    from billing.services import store as sub_store
    from billing.services.billing import period_containing
    from billing.services.renewals import _extension_product
    from shared_models.models import BillingPlan

    invoice = _payers_invoice(user_id, invoice_id)
    if invoice is None:
        return None

    start, end = invoice.period_start, invoice.period_end
    period_seconds = (end - start).total_seconds()
    unit = 10 ** money.decimal_places(invoice.currency)

    def rate(amount: int, whole: float, part: float) -> int | None:
        """The monthly rate a prorated charge was priced at, to the nearest whole unit."""
        return round(abs(amount) * whole / part / unit) * unit if part > 0 else None

    def recorded(line) -> bool:
        return line.period_start is not None and line.period_end is not None

    lines = [line for line in invoice.lines.all() if int(line.amount or 0) != 0]
    # Only an extension that recorded nothing, or no single rate, needs the period before
    # the invoice (and the first kind the catalogue and its module's row too).
    derived = [
        ln for ln in lines
        if (ln.product_name or "").strip().endswith(EXTENSION_SUFFIX)
        and not (recorded(ln) and ln.unit_amount is not None)
    ]
    catalogue: dict[str, int] = {}
    module_for: dict[str, str] = {}
    before_seconds = period_seconds
    if derived:
        catalogue = {
            (plan.display_name or "").strip().lower(): plan.amount
            for plan in BillingPlan.objects.all()
        }
        module_for = {_extension_product(code).lower(): code for code in MODULE_CODES}
        # The period an extension was priced against: the one the paid days belonged to.
        anchor, _currency = sub_store.billing_cycle_for_user(user_id)
        if anchor is not None:
            before = period_containing(anchor, start - timedelta(seconds=1))
            before_seconds = (before.end - before.start).total_seconds()

    rows = []
    for line in lines:
        amount = int(line.amount)
        product = (line.product_name or "").strip()
        extension = product.endswith(EXTENSION_SUFFIX)
        name = product[: -len(EXTENSION_SUFFIX)] if extension else product
        kind = "extension" if extension else (line.kind or "full").lower()

        if recorded(line):
            # As the line was priced, recorded at issue (schema item 23).
            line_start, line_end, monthly = line.period_start, line.period_end, line.unit_amount
            if monthly is None:
                # No single rate (an extension whose rate stepped part-way): the one that
                # makes the row add up over its days, against the period they were priced on.
                whole = before_seconds if extension else period_seconds
                monthly = rate(amount, whole, (line_end - line_start).total_seconds())
        else:
            # Issued before lines recorded their days: read back from the kind (above).
            line_start, line_end, monthly = start, end, abs(amount)
            if extension:
                code = module_for.get(name.lower())
                row = sub_store.module_row(line.entity_id, code) if code else None
                until = getattr(row, "app_access_until", None)
                if until is not None and until > start:
                    line_end = until
                    monthly = rate(amount, before_seconds, (until - start).total_seconds())
                else:
                    line_end, monthly = None, catalogue.get(name.lower())
            elif kind in ("remaining", "unused") and line.at is not None:
                line_start = line.at
                monthly = rate(amount, period_seconds, (end - line_start).total_seconds())

        rows.append(
            {
                "entity_id": str(line.entity_id),
                "entity_name": line.entity_name or "",
                "subscription": name,
                "kind": kind,
                "monthly_minor": monthly,
                "period_start": _iso(line_start),
                "period_end": _iso(line_end),
                "charged_minor": amount,
            }
        )

    return {
        "invoice": {
            "id": str(invoice.id),
            "reference": _reference(invoice),
            "currency": (invoice.currency or "").upper(),
            "period_start": _iso(start),
            "period_end": _iso(end),
            "total_minor": invoice.total or 0,
        },
        "rows": rows,
    }


# --- Billing accounts (08-A / 08-B / 08-C) ---------------------------------------------
#
# A BILLING ACCOUNT is a ``payer_billing_group`` row: the name it bills under ("Bill to"),
# a billing email, the cards on it, the ONE card it charges, the companies it pays for and
# its own dunning clock. The portal shows one at a time — 08-A the chosen one, 08-B its
# profile — and every account carries what those pages print, so neither page has to join
# anything itself.


def account_name(group, payer: dict) -> str:
    """What "Bill to" reads for one account: the company the payer named it after, else
    the payer — which is what every account opened before accounts had names renders as,
    and what the Stripe customer is called for them (``checkout._payer_identity``)."""
    company = (getattr(group, "billing_company", None) or "").strip()
    return company or payer.get("name") or payer.get("email") or ""


def card_address(card: dict | None, country_names: dict) -> dict | None:
    """An account's address: the billing address of the card it CHARGES (the account holds
    none of its own - the user's decision; 08-C writes the card's), with the country named
    from the registry. None when Stripe no longer holds that card. ``card`` is a
    ``payment_methods._view`` row."""
    if card is None:
        return None
    address = dict(card.get("address") or {})
    code = address.get("country")
    address["country_name"] = country_names.get(code or "", code) if code else None
    return address


def address_lines(address: dict | None) -> list[str]:
    """The address as 08-B prints it: line 1, line 2, then the place - city, region, postal
    code and country on one line, blanks and repeats dropped ("Hong Kong, Hong Kong" says it
    once). A port of minty-web's ``lib/billingAccounts.ts::addressLines``, so the invoice PDF
    and the page print one address the same way; change both together."""
    if not address:
        return []
    seen: set[str] = set()
    place: list[str] = []
    for part in (address.get("city"), address.get("state"), address.get("postal_code"),
                 address.get("country_name")):
        text = (part or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            place.append(text)
    lines = (address.get("line1"), address.get("line2"), ", ".join(place))
    return [text for text in ((line or "").strip() for line in lines) if text]


def _countries() -> list[dict]:
    """Every country in the registry, in its display order — the 08-C Country list.

    NOT filtered on ``is_active``: that flag says where Minty operates, and a card can be
    billed from anywhere.
    """
    return [
        {"code": row.country_code, "name": row.country_name_en or row.country_code}
        for row in CountryInfo.objects.order_by("display_order", "country_name_en")
    ]


def build_billing_accounts(user_id, *, countries: bool = False) -> dict:
    """The payer's billing accounts, oldest first — the first is the one shown by default.

    ONE STRIPE READ for the page (``payment_methods._accounts_with_cards``) and one query
    each for the nominations, the module rows and the company names.

    Per account:

    * ``name`` — ``account_name``; ``billing_company`` / ``billing_email`` stay raw so a
      form can tell "unnamed" from "named after the payer";
    * ``bill_to_email`` — the address "Bill to" prints, which is where the account's money
      emails go and what its invoices name: ``store.account_email`` (the billing email,
      else the business email every company on it shares), else the payer's;
    * ``card`` — the card the account CHARGES, or null when Stripe no longer holds it (a
      detached card is an account that cannot pay, and the page says so);
    * ``cards`` — the shelf, default first, with ``is_default`` marking THIS account's card.
      The shared card views mark the Stripe CUSTOMER default instead, so each is copied
      and re-marked here — two accounts on one card would otherwise disagree about it;
    * ``address`` — the charged card's billing address. The account holds none of its own
      (the user's decision): the address IS the card's, which 08-C writes;
    * ``companies`` — the companies nominated onto it that this payer still pays for (a
      nomination outlives a handover as history, and history is not shown);
    * ``in_dunning`` / ``past_due`` — the account's collection clock, and whether anything
      on it is owed (the clock, or any of its companies past due);
    * ``next_bill`` — what its next renewal will charge, estimated (``next_bill_for_account``).

    ``next_billing`` is the payer's, once: every account renews on the same anchor.
    ``countries`` (the registry) and ``publishable_key`` only when asked — 08-C is the one
    screen that needs them: its address form is Stripe's own (the AddressElement), limited to
    the registry's countries and mounted with the key.
    """
    from billing.services import clock, payment_methods
    from billing.services import store as sub_store

    anchor, _currency = sub_store.billing_cycle_for_user(user_id)
    when = next_billing_from(anchor, clock.now())
    wallet, groups = payment_methods._accounts_with_cards(user_id)
    live = {m["id"]: m for m in wallet["methods"]}
    payer = _person(_by_pk(User, user_id), user_id)

    phases: dict[str, set[str]] = {}
    for row in sub_store.module_rows_for_payer(user_id):
        phases.setdefault(str(row.entity_id), set()).add(getattr(row, "phase", None) or "")

    on_account: dict[str, list[str]] = {}
    for nomination in sub_store.nominations_for_payer(user_id):
        entity_id = str(nomination.entity_id)
        if entity_id in phases:
            on_account.setdefault(str(nomination.billing_group_id), []).append(entity_id)

    named = {entity_id for ids in on_account.values() for entity_id in ids}
    companies_read = list(Entity.objects.filter(id__in=sorted(named))) if named else []
    names = {str(e.id): (e.name or "") for e in companies_read}
    # Read in the same query, for ``bill_to_email``.
    business_emails = {str(e.id): e.business_email for e in companies_read}

    charged_cards = {
        str(group.id): live.get(group.stripe_payment_method_id) for group, _ in groups
    }
    country_names = _country_names(
        ((card or {}).get("address") or {}).get("country")
        for card in charged_cards.values()
    )

    accounts = []
    for group, cards in groups:
        charging = group.stripe_payment_method_id
        charged = charged_cards[str(group.id)]
        shelf = [dict(card, is_default=card["id"] == charging) for card in cards]
        if charged is not None and not any(card["id"] == charging for card in shelf):
            # A charged card missing from its own shelf is the divergence
            # ``billing_account_payment_method`` exists to prevent; show what is charged.
            shelf.insert(0, dict(charged, is_default=True))
        shelf.sort(key=lambda card: 0 if card["is_default"] else 1)

        address = card_address(charged, country_names)

        companies = sorted(
            (
                {
                    "entity_id": entity_id,
                    "entity_name": names.get(entity_id, ""),
                    "past_due": PHASE_PAST_DUE in phases.get(entity_id, set()),
                }
                for entity_id in on_account.get(str(group.id), [])
            ),
            key=lambda company: (company["entity_name"] or "").lower(),
        )
        in_dunning = group.dunning_started_at is not None
        accounts.append(
            {
                "id": str(group.id),
                "name": account_name(group, payer),
                "billing_company": group.billing_company,
                "billing_email": group.billing_email,
                "bill_to_email": sub_store.account_email(
                    group,
                    [business_emails.get(eid) for eid in on_account.get(str(group.id), [])],
                ) or payer.get("email"),
                "default_id": charging,
                "card": dict(charged, is_default=True) if charged is not None else None,
                "cards": shelf,
                "total": len(shelf),
                "address": address,
                "companies": companies,
                "in_dunning": in_dunning,
                "past_due": in_dunning or any(c["past_due"] for c in companies),
                "next_bill": next_bill_for_account(user_id, group.id, anchor, when),
            }
        )

    payload = {
        "has_account": wallet["has_account"],
        "payer": payer,
        "next_billing": _fmt(when),
        "next_billing_iso": _iso(when),
        "accounts": accounts,
        "total": len(accounts),
        # The flat wallet as well: a card on no account is still the payer's, and the
        # Stripe customer default still decides what pickers offer first.
        "methods": wallet["methods"],
        "default_id": wallet["default_id"],
    }
    if countries:
        payload["countries"] = _countries()
        # Public by definition (it is what the browser loads Stripe.js with). None when this
        # environment has no Stripe: the form then says the address cannot be changed here,
        # and the name and email still can.
        payload["publishable_key"] = payment_methods.get_publishable_key() or None
    return payload
