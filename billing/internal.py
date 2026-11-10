"""What other Minty backends may ask about a company's subscription - read-only, for
minty-accounts-api's hub (Phase 5 of the Flask retirement, 2026-11-06; the owner's choice: the
hub asks this service rather than keeping Flask's read-only copy, ``store_ro.py``).

Three plain Django views, outside the ninja API, so they are no part of its public contract (and
its OpenAPI snapshot). Each needs a backend's 60-second assertion (HS256, the shared
``SECRET_KEY``), scoped ``subscription-facts``; anything else is 401.

- ``POST /api/internal/subscriptions/attention`` ``{entity_ids: [..]}`` -> ``{attention: {id:
  {trials: {code: ISO end}, payment_failed: bool}}}``: the entity list's badges. Trials by this
  service's rule (``cards.get_trial_modules_for_entities``), payment failed as the module card
  says it (past due, access still granted, the customer told), from the database alone - the
  card itself also reads Stripe, too much for a list.
- ``GET /api/internal/subscriptions/entities/<id>`` -> ``{payer, payers, pending_transfer_to,
  billed_modules}``: who pays (``store.payer_for_entity``), every payer any row names (the
  removal guard fails closed on a split), an open handover's nominee, and the modules that are
  billed (which locks the manual module switch).
- ``GET /api/internal/subscriptions/payers/<user_id>/entities`` -> ``{entities: [{id, name}]}``:
  every company whose bill this person carries, in any phase (may they leave?).

Nothing here writes.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import jwt
from django.conf import settings
from django.http import HttpRequest, HttpResponseNotAllowed, JsonResponse
from minty_db.models import Entity, EntityModuleSubscription
from minty_shared import logctx, tokens

from billing.services import clock, dunning, policy, store, transfers
from billing.services._log import logger
from billing.services.cards import get_trial_modules_for_entities
from billing.services.constants import (
    PHASE_ACTIVE,
    PHASE_PAST_DUE,
    PHASE_SCHEDULED_CANCEL,
    PHASE_TRIAL,
)

SCOPE = "subscription-facts"
MAX_ENTITIES = 500
UNAUTHORIZED = {"error": "Unauthorized"}


def _authenticated(request: HttpRequest) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    try:
        claims = tokens.decode_bearer(header[7:].strip(), settings.SECRET_KEY,
                                      options={"require": ["exp", "scope"]})
    except jwt.PyJWTError as exc:
        logger.warning("internal subscription facts: refused caller ({})", type(exc).__name__)
        return False
    if claims.get("scope") != SCOPE:
        logger.warning("internal subscription facts: refused caller (wrong scope)")
        return False
    logctx.bind(user=claims.get("user_id"), entity=claims.get("entity_id"))
    return True


def _uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# ---- attention -------------------------------------------------------------------------------


def _trial_ends(entity_ids: list[str]) -> dict[str, dict[str, str]]:
    """Each company's trial modules (``get_trial_modules_for_entities``' rule) with when each ends."""
    on_trial = {str(k): v for k, v in get_trial_modules_for_entities(entity_ids).items()}
    if not on_trial:
        return {}
    ends: dict[str, dict[str, str]] = {}
    rows = EntityModuleSubscription.objects.filter(
        entity_id__in=list(on_trial), first_billed_at__isnull=True, trial_end__isnull=False,
        phase__in=(PHASE_TRIAL, PHASE_SCHEDULED_CANCEL))
    for row in rows:
        code = (row.function_code or "").upper()
        if code in on_trial.get(str(row.entity_id), set()):
            ends.setdefault(str(row.entity_id), {})[code] = _iso(row.app_access_until or row.trial_end)
    return ends


def _payment_failed(entity_ids: list[str], now) -> set[str]:
    """The companies with a module the card calls ``past_due``: past due, access still granted
    (until ``app_access_until`` when promised, else the company's paid-through plus the past-due
    window) and the customer told (``dunning.told_of_failure``). Flask's ``store_ro`` rule, which
    restates this service's card; here it uses the engine's own reads."""
    past_due: dict[str, list] = {}
    for row in EntityModuleSubscription.objects.filter(entity_id__in=entity_ids, phase=PHASE_PAST_DUE):
        past_due.setdefault(str(row.entity_id), []).append(row)
    if not past_due:
        return set()
    window = timedelta(days=policy.current().past_due_window_days)
    failed = set()
    for entity_id, rows in past_due.items():
        if not dunning.told_of_failure(entity_id, now):
            continue
        paid_through = store.paid_through_for_entity(entity_id)
        for row in rows:
            ends_at = row.app_access_until or (paid_through + window if paid_through is not None else None)
            if ends_at is not None and ends_at > now:
                failed.add(entity_id)
                break
    return failed


def attention(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _authenticated(request):
        return JsonResponse(UNAUTHORIZED, status=401)
    try:
        body = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    raw = body.get("entity_ids") if isinstance(body, dict) else None
    if not isinstance(raw, list) or len(raw) > MAX_ENTITIES or any(_uuid(i) is None for i in raw):
        return JsonResponse({"error": f"Send entity_ids: a list of at most {MAX_ENTITIES} company ids."}, status=422)
    entity_ids = [_uuid(i) for i in raw]
    now = clock.now()
    trials = _trial_ends(entity_ids)
    failed = _payment_failed(entity_ids, now)
    known = set(trials) | failed | {str(e) for e in EntityModuleSubscription.objects.filter(
        entity_id__in=entity_ids).values_list("entity_id", flat=True)}
    return JsonResponse({"attention": {
        entity_id: {"trials": trials.get(entity_id, {}), "payment_failed": entity_id in failed}
        for entity_id in sorted(known)
    }})


# ---- one company, one payer -----------------------------------------------------------------


def _billed(row) -> bool:
    """Billed modules lock the manual switch: active and past due; a scheduled cancel only when it
    was ever billed (a cancelled trial never was)."""
    if row.phase in (PHASE_ACTIVE, PHASE_PAST_DUE):
        return True
    return row.phase == PHASE_SCHEDULED_CANCEL and row.first_billed_at is not None


def entity_facts(request: HttpRequest, entity_id: str) -> JsonResponse:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not _authenticated(request):
        return JsonResponse(UNAUTHORIZED, status=401)
    entity_id = _uuid(entity_id)
    if entity_id is None:
        return JsonResponse({"error": "That isn't a company id."}, status=422)
    rows = list(EntityModuleSubscription.objects.filter(entity_id=entity_id))
    offer = transfers.pending_transfer_for_entity(entity_id)
    return JsonResponse({
        "payer": str(payer) if (payer := store.payer_for_entity(entity_id)) else None,
        "payers": sorted({str(r.payer_user_id) for r in rows if r.payer_user_id}),
        "pending_transfer_to": str(offer.to_user_id) if offer is not None and offer.to_user_id else None,
        "billed_modules": sorted({(r.function_code or "").upper() for r in rows if _billed(r)}),
        # Paid and running (active or past due): minty-accounts-api refuses to archive the company.
        "paid_modules": sorted({(r.function_code or "").upper() for r in rows
                                if r.phase in (PHASE_ACTIVE, PHASE_PAST_DUE)}),
    })


def payer_entities(request: HttpRequest, user_id: str) -> JsonResponse:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not _authenticated(request):
        return JsonResponse(UNAUTHORIZED, status=401)
    user_id = _uuid(user_id)
    if user_id is None:
        return JsonResponse({"error": "That isn't a person's id."}, status=422)
    ids = {str(e) for e in EntityModuleSubscription.objects.filter(payer_user_id=user_id)
           .values_list("entity_id", flat=True)}
    named = {str(e.id): (e.name or "").strip() or str(e.id) for e in Entity.objects.filter(id__in=list(ids))}
    # A missing company still counts: it is the SUBSCRIPTION that would strand.
    return JsonResponse({"entities": [{"id": i, "name": named.get(i, i)} for i in sorted(ids)]})

