"""Role and permission policy, ported from Minty's services/permission_policy.py.

WHY THIS SERVICE CARRIES IT (copied verbatim from onboarding-backend/core/policy.py, whose
reasoning below still stands): the module settings page is gated on ``MODULE_VIEW`` (any
member from cashier up may see which modules a company has) and every action on it on
``MODULE_MANAGE`` (admin and above) - the same two rules Flask applied in
``entity/routes/settings.py``. The payer portal needs none of this: ``/api/me/*`` is keyed
on the caller, and "may this person manage this subscription" is the subscription's own
rule (``store.may_manage_subscription``, the ``@require_subscription_payer`` port), not a
role.

WHY THIS IS A PORT AND NOT A SIMPLIFICATION

Onboarding looks like it needs no permission model: the person walking the wizard created
the company seconds ago and is its only member, so every check passes. That is true right
up to Step 3, where they invite somebody -- and from then on the entity can be re-entered
by an invited accountant or cashier resuming the wizard. Flask gates the invite list on
``USER_VIEW_ALL`` and the invite itself on ``USER_INVITE``, both requiring shop_manager or
above, and a cashier who resumes must see the same refusal here as there.

So this is a faithful port. Two details make it worth reading rather than skimming:

RANK, NOT A TABLE. The ``roles`` / ``permissions`` / ``role_permissions`` tables exist in
this schema, but this policy does not read them -- they belong to the user_management
admin screens. Authorisation is decided in code from a rank ladder and a rules table.
Mirroring those tables here would have been the obvious wrong move.

THE FULL RULES TABLE IS PORTED, not just the handful Group B needs. A partial port is the
kind of thing that drifts: the next group adds a permission, someone copies a neighbouring
rule for it, and the two services disagree about who may create a sales method. The table
is thirty lines; drift costs more.

SUPERUSER READ-ONLY. A system superuser reaches any entity without a ``user_entity`` row,
but only for the view permissions in READONLY_ALLOWED_PERMISSIONS. Every write is refused
even though their effective role resolves to super_admin. Dropping that distinction would
let a superuser browsing a customer's half-finished onboarding change it.
"""

from __future__ import annotations

from typing import Any

from minty_db.enums import SystemRole
from minty_db.models import User, UserEntity

# The rules themselves - roles, permissions, the rank ladder, the superuser's read-only list and the
# strict role-management rule - are minty_shared.policy (v0.5, owner 2026-11-12: one copy, not five).
# This module keeps only how this service finds a person's membership, and the gate built on them.
# Re-exported: callers import them from here.
from minty_shared.policy import (  # noqa: F401
    PERMISSION_RULES,
    READONLY_ALLOWED_PERMISSIONS,
    ROLE_ALIASES,
    ROLE_RANK,
    Permission,
    PermissionRule,
    Role,
    assignable_roles,
    can_manage_role_assignment,
    normalize_role,
    role_at_least,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
# The words are the database's (minty_db.enums.SystemRole, generated from the ``system_role``
# enum in Minty's docs/schema/01_schema_rebased.sql). The constant keeps its historical name.
SYSTEM_ROLE_NORMAL = SystemRole.NORMAL.value
SYSTEM_ROLE_ADMIN = SystemRole.ADMIN.value
SYSTEM_ROLE_SUPERUSER = SystemRole.SUPERADMIN.value
SYSTEM_ROLE_VALUES = tuple(SystemRole.values)

#: Pre-split ``role`` values that meant the system super admin. "superuser" is NOT here:
#: the pre-split column never held it (test_policy pins that); the pre-rename *system_role*
#: spelling is handled in normalize_system_role instead.
LEGACY_SUPERUSER_ROLES = frozenset({"admin", "super_admin"})


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normalize_system_role(system_role: Any) -> str:
    if system_role is None:
        return SYSTEM_ROLE_NORMAL
    normalized = str(system_role).strip().lower()
    if normalized == "superuser":  # pre-rename spelling, e.g. in an old JWT
        return SYSTEM_ROLE_SUPERUSER
    return normalized if normalized in SYSTEM_ROLE_VALUES else SYSTEM_ROLE_NORMAL


def legacy_role_to_system_role(role: Any) -> str:
    if role is None:
        return SYSTEM_ROLE_NORMAL
    normalized = str(role).strip().lower().replace(" ", "_").replace("-", "_")
    return (
        SYSTEM_ROLE_SUPERUSER
        if normalized in LEGACY_SUPERUSER_ROLES
        else SYSTEM_ROLE_NORMAL
    )


# ---------------------------------------------------------------------------
# Membership and role resolution
# ---------------------------------------------------------------------------
def is_superuser(user: Any) -> bool:
    """Reads ``system_role``, falling back to the legacy ``role`` column.

    The fallback matters: ``system_role`` was split out of ``role``, and the check is
    case-insensitive because rows exist with non-canonical casing.
    """
    if not user:
        return False
    system_role = getattr(user, "system_role", None)
    if system_role is not None:
        return normalize_system_role(system_role) == SYSTEM_ROLE_SUPERUSER
    return legacy_role_to_system_role(getattr(user, "role", None)) == SYSTEM_ROLE_SUPERUSER


def _membership_for(user_id: str, entity_id: str) -> UserEntity | None:
    return UserEntity.objects.filter(user_id=user_id, entity_id=entity_id).first()


def has_entity_membership(user: Any, entity_id: str | None) -> bool:
    """An EXPLICIT user_entity row. Does not grant superusers anything.

    Separate from :func:`has_entity_access` on purpose -- it is what distinguishes a
    superuser inside their own entity (full CRUD) from one visiting a customer's
    (read-only).
    """
    if not user or not entity_id or not getattr(user, "id", None):
        return False
    return _membership_for(str(user.id), str(entity_id)) is not None


def has_entity_access(user: Any, entity_id: str | None, require_approved: bool = True) -> bool:
    if not user or not entity_id or not getattr(user, "id", None):
        return False
    if is_superuser(user):
        return True
    membership = _membership_for(str(user.id), str(entity_id))
    if not membership:
        return False
    if require_approved and not getattr(membership, "approved", True):
        return False
    return True


def resolve_effective_role(user: Any, entity_id: str | None = None) -> str:
    if not user:
        return Role.ENTITY_BASE.value
    if is_superuser(user):
        return Role.SUPER_ADMIN.value
    if entity_id and getattr(user, "id", None):
        membership = _membership_for(str(user.id), str(entity_id))
        if membership and getattr(membership, "approved", True):
            return normalize_role(getattr(membership, "role", None))
    return Role.ENTITY_BASE.value


def is_superuser_readonly(user: Any, entity_id: str | None) -> bool:
    """A superuser viewing an entity they hold no row on: can look, cannot touch."""
    if not is_superuser(user):
        return False
    if not entity_id:
        return False
    return not has_entity_membership(user, entity_id)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def has_permission(user: Any, permission: Permission, entity_id: str | None = None) -> bool:
    rule = PERMISSION_RULES.get(permission)
    if not rule:
        # An unknown permission is refused, never allowed. A typo must fail closed.
        return False

    if rule.entity_scoped:
        if not entity_id:
            return False
        if is_superuser_readonly(user, entity_id):
            return permission in READONLY_ALLOWED_PERMISSIONS
        effective_role = resolve_effective_role(user, entity_id)
        if effective_role != Role.SUPER_ADMIN.value and not has_entity_access(
            user, entity_id, require_approved=True
        ):
            return False
        return role_at_least(effective_role, rule.min_role.value)

    if permission == Permission.ENTITY_CREATE:
        # Any authenticated person may create a company -- that is the whole premise of
        # the wizard, and there is no entity to be scoped to yet.
        return bool(user and getattr(user, "id", None))

    effective_role = resolve_effective_role(user, None)
    return role_at_least(effective_role, rule.min_role.value)


def has_permission_by_user_id(
    user_id: str, permission: Permission, entity_id: str | None = None
) -> bool:
    user = User.objects.filter(id=str(user_id)).first()
    if not user:
        return False
    return has_permission(user, permission, entity_id=entity_id)


def can_manage_role_assignment_for_entity(
    user: Any, target_role: Any, entity_id: str | None
) -> bool:
    if not user:
        return False
    return can_manage_role_assignment(resolve_effective_role(user, entity_id), target_role)
