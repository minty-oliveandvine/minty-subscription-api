"""``replay_scenarios.prune_orphans`` deletes only the audit rows whose company is GONE.

Ids are uuid.UUID since minty-db: the set of companies still alive and the ids checked against it
must be one type, or every row looks dangling and the prune deletes history it must keep.
"""

from __future__ import annotations

import pytest
from minty_db.models import SubscriptionAuditLog

pytestmark = pytest.mark.django_db


def test_an_audit_row_whose_company_still_exists_survives_the_prune(user, entity):
    from billing.management.commands.replay_scenarios import prune_orphans

    row = SubscriptionAuditLog.objects.create(
        entity_id=entity.id, function_code="PETTY_CASH", payer_user_id=user.id,
        action="cancel", outcome="succeeded",
    )

    prune_orphans({"user_id": str(user.id)})  # its Stripe half is refused under test and skipped

    assert SubscriptionAuditLog.objects.filter(id=row.id).exists(), "a live company's history was deleted"
