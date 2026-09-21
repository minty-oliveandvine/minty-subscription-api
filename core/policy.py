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

from dataclasses import dataclass
from enum import Enum
from typing import Any

from shared_models.enums import SystemRole
from shared_models.models import User, UserEntity

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
# The words are the database's (shared_models/enums.SystemRole, mirroring the ``system_role``
# enum in Minty's docs/schema/01_schema_rebased.sql). The constant keeps its historical name.
SYSTEM_ROLE_NORMAL = SystemRole.NORMAL.value
SYSTEM_ROLE_ADMIN = SystemRole.ADMIN.value
SYSTEM_ROLE_SUPERUSER = SystemRole.SUPERADMIN.value
SYSTEM_ROLE_VALUES = tuple(SystemRole.values)

#: Pre-split ``role`` values that meant the system super admin. "superuser" is NOT here:
#: the pre-split column never held it (test_policy pins that); the pre-rename *system_role*
#: spelling is handled in normalize_system_role instead.
LEGACY_SUPERUSER_ROLES = frozenset({"admin", "super_admin"})


class Role(str, Enum):
    ENTITY_BASE = "entity_base"
    CASHIER = "cashier"
    SHOP_MANAGER = "shop_manager"
    ACCOUNTANT = "accountant"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class Permission(str, Enum):
    USER_INVITE = "user_invite"
    USER_VIEW_ALL = "user_view_all"
    USER_ROLE_ASSIGN = "user_role_assign"
    USER_ROLE_DELETE = "user_role_delete"
    ENTITY_CREATE = "entity_create"
    ENTITY_VIEW = "entity_view"
    ENTITY_UPDATE = "entity_update"
    ENTITY_RENAME = "entity_rename"
    ENTITY_DELETE = "entity_delete"
    MODULE_VIEW = "module_view"
    MODULE_MANAGE = "module_manage"
    SALES_METHOD_VIEW = "sales_method_view"
    SALES_METHOD_CREATE = "sales_method_create"
    SALES_METHOD_UPDATE = "sales_method_update"
    SALES_METHOD_DELETE = "sales_method_delete"
    SALES_METHOD_REORDER = "sales_method_reorder"
    COA_VIEW = "coa_view"
    COA_CREATE = "coa_create"
    COA_UPDATE = "coa_update"
    COA_DELETE = "coa_delete"
    XERO_SETTINGS_VIEW = "xero_settings_view"
    XERO_SETTINGS_UPDATE = "xero_settings_update"
    REPORT_VIEW_OWN = "report_view_own"
    REPORT_VIEW_ENTITY = "report_view_entity"
    REPORT_EDIT_OWN = "report_edit_own"
    REPORT_EDIT_ENTITY = "report_edit_entity"
    REPORT_DELETE_OWN = "report_delete_own"
    REPORT_DELETE_ENTITY = "report_delete_entity"
    REPORT_PUBLISH = "report_publish"
    CONTACT_CREATE = "contact_create"


ROLE_RANK: dict[str, int] = {
    Role.ENTITY_BASE.value: 0,
    Role.CASHIER.value: 1,
    Role.SHOP_MANAGER.value: 2,
    Role.ACCOUNTANT.value: 3,
    Role.ADMIN.value: 4,
    Role.SUPER_ADMIN.value: 5,
}

#: Spellings seen in real rows. An unrecognised role normalises to ENTITY_BASE
#: (rank 0), never to a guess -- an unknown value must not be granted anything.
ROLE_ALIASES: dict[str, str] = {
    "user": Role.ENTITY_BASE.value,
    "no_role": Role.ENTITY_BASE.value,
    "none": Role.ENTITY_BASE.value,
    "entity base": Role.ENTITY_BASE.value,
    "entity-base": Role.ENTITY_BASE.value,
    "client": Role.CASHIER.value,
    "shop manager": Role.SHOP_MANAGER.value,
    "shop-manager": Role.SHOP_MANAGER.value,
    "super admin": Role.SUPER_ADMIN.value,
    "super-admin": Role.SUPER_ADMIN.value,
}


@dataclass(frozen=True)
class PermissionRule:
    min_role: Role
    entity_scoped: bool = True


PERMISSION_RULES: dict[Permission, PermissionRule] = {
    Permission.USER_INVITE: PermissionRule(Role.SHOP_MANAGER),
    Permission.USER_VIEW_ALL: PermissionRule(Role.SHOP_MANAGER),
    Permission.USER_ROLE_ASSIGN: PermissionRule(Role.SHOP_MANAGER),
    Permission.USER_ROLE_DELETE: PermissionRule(Role.ACCOUNTANT),
    Permission.ENTITY_CREATE: PermissionRule(Role.ENTITY_BASE, entity_scoped=False),
    Permission.ENTITY_VIEW: PermissionRule(Role.CASHIER),
    Permission.ENTITY_UPDATE: PermissionRule(Role.ACCOUNTANT),
    Permission.ENTITY_RENAME: PermissionRule(Role.ADMIN),
    Permission.ENTITY_DELETE: PermissionRule(Role.ADMIN),
    # Seeing which modules an entity subscribes to is read-only information every
    # entity member needs; changing them is admin-only.
    Permission.MODULE_VIEW: PermissionRule(Role.CASHIER),
    Permission.MODULE_MANAGE: PermissionRule(Role.ADMIN),
    Permission.SALES_METHOD_VIEW: PermissionRule(Role.CASHIER),
    Permission.SALES_METHOD_CREATE: PermissionRule(Role.ACCOUNTANT),
    Permission.SALES_METHOD_UPDATE: PermissionRule(Role.ACCOUNTANT),
    Permission.SALES_METHOD_DELETE: PermissionRule(Role.ACCOUNTANT),
    Permission.SALES_METHOD_REORDER: PermissionRule(Role.ACCOUNTANT),
    Permission.COA_VIEW: PermissionRule(Role.CASHIER),
    Permission.COA_CREATE: PermissionRule(Role.ACCOUNTANT),
    Permission.COA_UPDATE: PermissionRule(Role.ACCOUNTANT),
    Permission.COA_DELETE: PermissionRule(Role.ACCOUNTANT),
    Permission.XERO_SETTINGS_VIEW: PermissionRule(Role.CASHIER),
    Permission.XERO_SETTINGS_UPDATE: PermissionRule(Role.ACCOUNTANT),
    Permission.REPORT_VIEW_OWN: PermissionRule(Role.CASHIER),
    Permission.REPORT_VIEW_ENTITY: PermissionRule(Role.CASHIER),
    Permission.REPORT_EDIT_OWN: PermissionRule(Role.CASHIER),
    Permission.REPORT_EDIT_ENTITY: PermissionRule(Role.SHOP_MANAGER),
    Permission.REPORT_DELETE_OWN: PermissionRule(Role.CASHIER),
    Permission.REPORT_DELETE_ENTITY: PermissionRule(Role.SHOP_MANAGER),
    Permission.REPORT_PUBLISH: PermissionRule(Role.ACCOUNTANT),
    Permission.CONTACT_CREATE: PermissionRule(Role.CASHIER),
}

#: What a superuser may do on an entity they hold no membership on. Views only --
#: every write is refused even though their effective role resolves to super_admin.
READONLY_ALLOWED_PERMISSIONS: frozenset[Permission] = frozenset({
    Permission.USER_VIEW_ALL,
    Permission.ENTITY_VIEW,
    Permission.MODULE_VIEW,
    Permission.SALES_METHOD_VIEW,
    Permission.COA_VIEW,
    Permission.XERO_SETTINGS_VIEW,
    Permission.REPORT_VIEW_OWN,
    Permission.REPORT_VIEW_ENTITY,
})


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normalize_role(role: Any) -> str:
    if role is None:
        return Role.ENTITY_BASE.value
    role_text = str(role).strip().lower()
    if role_text in ROLE_ALIASES:
        return ROLE_ALIASES[role_text]
    if role_text in ROLE_RANK:
        return role_text
    return Role.ENTITY_BASE.value


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


def role_at_least(role: Any, minimum_role: Any) -> bool:
    return ROLE_RANK.get(normalize_role(role), 0) >= ROLE_RANK.get(
        normalize_role(minimum_role), 0
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


def can_manage_role_assignment(actor_role: Any, target_role: Any) -> bool:
    """You may only act on a role at or below your own, and never below shop_manager.

    Group E needs this to refuse cancelling an invite for a role above the canceller's.
    """
    actor = normalize_role(actor_role)
    target = normalize_role(target_role)
    if not role_at_least(actor, Role.SHOP_MANAGER.value):
        return False
    return ROLE_RANK.get(actor, 0) >= ROLE_RANK.get(target, 0)


def can_manage_role_assignment_for_entity(
    user: Any, target_role: Any, entity_id: str | None
) -> bool:
    if not user:
        return False
    return can_manage_role_assignment(resolve_effective_role(user, entity_id), target_role)
