"""The system super admin, as this service reads it - its own behaviour over minty-db's enum."""

from minty_db.enums import SystemRole

#: The pre-rename spelling of the system super admin; JWTs minted before C1 carry it.
#: Not ``admin``: that is a distinct, lesser member of the enum now.
LEGACY_SUPERUSER = frozenset({"superuser"})


def is_superadmin(system_role) -> bool:
    """Is this ``system_role`` value (a row's or a JWT claim's) the system super admin?"""
    word = (str(system_role or "")).strip().lower()
    return word == SystemRole.SUPERADMIN or word in LEGACY_SUPERUSER
