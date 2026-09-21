"""``billing/services/constants.py`` is Flask's file verbatim; the Postgres enum types those
words are written into are spelled once more in ``shared_models/enums.py``. The two must
agree, or an engine write is refused by the type at the one moment nothing else checks it -
Minty's ``column_types._enum`` assertion, ported. The audit ``action`` column is plain text
(VARCHAR(40)), so its constants are only checked for width.
"""

from __future__ import annotations

from billing.services import constants
from billing.services.entity_modules import MODULE_CODES
from shared_models.enums import (
    AuditOutcome,
    ExtensionState,
    ModuleCode,
    SubscriptionPhase,
    TransferStatus,
)
from shared_models.models import SubscriptionAuditLog


def test_phases_are_exactly_the_enum_members():
    assert {
        constants.PHASE_TRIAL,
        constants.PHASE_ACTIVE,
        constants.PHASE_PAST_DUE,
        constants.PHASE_SCHEDULED_CANCEL,
        constants.PHASE_CANCELLED,
        constants.PHASE_EXPIRED,
    } == set(SubscriptionPhase.values)


def test_extension_states_are_enum_members():
    # The engine writes two of the five states; the others are bookkeeping written by hand.
    assert {constants.EXT_PENDING, constants.EXT_INVOICED} <= set(ExtensionState.values)


def test_transfer_statuses_are_exactly_the_enum_members():
    assert {
        constants.TRANSFER_PENDING,
        constants.TRANSFER_CHARGING,
        constants.TRANSFER_CHARGED,
        constants.TRANSFER_ACCEPTED,
        constants.TRANSFER_DECLINED,
        constants.TRANSFER_CANCELLED,
        constants.TRANSFER_EXPIRED,
    } == set(TransferStatus.values)
    assert set(constants.TRANSFER_OPEN_STATUSES) == {"pending", "charging", "charged"}
    assert set(constants.TRANSFER_STRANDED_STATUSES) == {"charging", "charged"}


def test_outcomes_are_exactly_the_enum_members():
    assert {constants.OUTCOME_SUCCEEDED, constants.OUTCOME_ABORTED} == set(AuditOutcome.values)


def test_module_codes_are_exactly_the_enum_members():
    assert set(MODULE_CODES) == set(ModuleCode.values)


def test_audit_actions_fit_the_column():
    width = SubscriptionAuditLog._meta.get_field("action").max_length
    for name in dir(constants):
        if name.startswith("AUDIT_"):
            assert len(getattr(constants, name)) <= width, name
