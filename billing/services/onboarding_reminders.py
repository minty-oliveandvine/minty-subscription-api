"""Remind whoever started setting a company up, and stopped, to finish it.

The user's rules (2026-10-01):

* **Who:** a company still ``onboarding`` (``finalize`` moves it to ``connected`` /
  ``disconnected``), mailed to the person who created it - the earliest approved admin on
  ``user_entity``, since nothing records a creator - while that user is active. The email
  is about them, so it goes to them (``notify.address_for``'s default), not the company inbox.
* **When:** 1, 3 and 7 days after the last activity, ``entities.updated_at`` (a trigger moves
  it on every write, the wizard's saved step included). Each reminder has a window
  (``STAGES``), so a day the pass does not run is caught up the next day, and nothing goes
  out once a company has been quiet for ``LAST_DAY`` days - which is also why the companies
  abandoned long before this shipped are not all mailed on launch day.
* **How often:** each reminder once per quiet spell. The dedupe key carries the
  ``updated_at`` it counted from, so coming back and stopping again starts a fresh series.
* **Stops** the moment setup finishes: the company is no longer ``onboarding``.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from billing.services import clock, notify
from billing.services._log import logger

#: (reminder day, the day it is too late for it). Whole days of quiet, counted from
#: ``updated_at`` - day 1 is any time from 24 hours after the last save.
STAGES = ((1, 3), (3, 7), (7, 9))
LAST_DAY = STAGES[-1][1]


def stage_for(quiet: timedelta) -> int | None:
    """Which reminder a company quiet for ``quiet`` is due, or None."""
    for day, too_late in STAGES:
        if timedelta(days=day) <= quiet < timedelta(days=too_late):
            return day
    return None


def _creators(entity_ids: list[str]) -> dict[str, str]:
    """entity id -> the user who created it: its earliest approved admin, if active."""
    from shared_models.enums import EntityRole
    from shared_models.models import User, UserEntity

    creators: dict[str, str] = {}
    for row in (UserEntity.objects
                .filter(entity_id__in=entity_ids, role=EntityRole.ADMIN, approved=True)
                .order_by("created_at")
                .values_list("entity_id", "user_id")):
        creators.setdefault(str(row[0]), str(row[1]))
    active = {str(uid) for uid in User.objects.filter(
        id__in=list(creators.values()), is_active=True
    ).values_list("id", flat=True)}
    return {eid: uid for eid, uid in creators.items() if uid in active}


def notify_unfinished_onboarding(now: datetime | None = None,
                                 limit: int | None = None) -> dict:
    """Send the setup reminders due now. Idempotent by the email log.

    Returns ``{"reminded": [...], "skipped": [...]}`` - one entry per company that was due,
    ``skipped`` naming why (no creator, already sent, no address, mail off).
    """
    from shared_models.enums import EntityStatus
    from shared_models.models import Entity

    now = now or clock.now()
    due = list(
        Entity.objects
        .filter(status=EntityStatus.ONBOARDING,
                updated_at__gt=now - timedelta(days=LAST_DAY),
                updated_at__lte=now - timedelta(days=STAGES[0][0]))
        .order_by("updated_at")
        .values("id", "name", "onboarding_saved_step", "updated_at")
    )
    if limit is not None:
        due = due[:limit]
    creators = _creators([str(row["id"]) for row in due])

    reminded, skipped = [], []
    for row in due:
        entity_id = str(row["id"])
        stage = stage_for(now - row["updated_at"])
        entry = {"entity_id": entity_id, "stage": stage}
        user_id = creators.get(entity_id)
        if stage is None or user_id is None:
            skipped.append({**entry, "reason": "no active creator" if stage else "not due"})
            continue
        sent = notify.notify(
            user_id, notify.ONBOARDING_REMINDER,
            dedupe_key=f"{entity_id}:{user_id}:{row['updated_at'].isoformat()}:{stage}",
            context={"entity_id": entity_id, "entity_name": row["name"],
                     "saved_step": row["onboarding_saved_step"]},
        )
        (reminded if sent else skipped).append(
            {**entry, "user_id": user_id} if sent
            else {**entry, "user_id": user_id, "reason": "already sent or not delivered"}
        )
    if reminded:
        logger.info("onboarding reminders: sent {} of {} due", len(reminded), len(due))
    return {"reminded": reminded, "skipped": skipped}
