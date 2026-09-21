import logging

import jwt
from django.conf import settings
from django.core.exceptions import ValidationError
from ninja.security import HttpBearer

from shared_models.enums import EntityRole, SystemRole, is_superadmin
from shared_models.models import User, UserEntity

logger = logging.getLogger("billing-api")


def get_entity_role(user_id: str, entity_id: str) -> str | None:
    """Return the user's role for the entity, or None if no access.

    ``entity_id`` is a uuid column now: an empty or malformed id (a handoff token with no
    entity, a hand-typed header) is "no access", not a query error.
    """
    if not user_id or not entity_id:
        return None
    try:
        return (
            UserEntity.objects.filter(user_id=user_id, entity_id=entity_id)
            .values_list("role", flat=True)
            .first()
        )
    except (ValidationError, ValueError):
        return None


def _holds_super_admin_anywhere(user_id) -> bool:
    """An entity-level ``super_admin`` row on any company (the "see all" paths key on it)."""
    return UserEntity.objects.filter(user_id=user_id, role=EntityRole.SUPER_ADMIN).exists()


class BearerAuth(HttpBearer):
    """
    Validates the JWT issued by the Flask app (Module 1) during the
    cross-module handoff.  Also verifies that the caller has access to
    the entity specified in the X-Entity-Id header and loads their
    entity-level role onto the request for permission checks.
    """

    #: Whether a caller must hold a role on the resolved entity to get through the door.
    #:
    #: True for every business endpoint: a bill belongs to a company, so somebody with no
    #: role on that company has no business reaching it, and refusing up front is the
    #: simplest way to guarantee that.
    #:
    #: ``SelfBearerAuth`` sets it False for the few ``/auth/*`` endpoints that describe the
    #: PERSON rather than the company. See that class for why.
    require_entity_role = True

    def _attach_unscoped(self, request, user, jwt_system_role: str, entity_id: str = ""):
        """Authenticate as a person with NO ROLE, and return the user.

        What is emptied is the caller's standing — ``entity_role`` and
        ``is_entity_member`` — never the subject. Endpoints that need a role still refuse,
        because those two are what they read, so this widens who gets through the door and
        not what they can do once inside.

        ``entity_id`` is the company the caller ASKED about, and it is deliberately kept.
        Which modules a company has is a fact about the company, not about the asker, and
        blanking it here made ``/auth/entitlements`` answer for no company at all: Petty
        Cash vanished from the nav of a company that owns it. The token already names that
        entity and already carries those very claims — Minty put them there — so keeping
        it reveals nothing new.

        ``is_super_admin`` is still resolved, because "can this person see every entity"
        is a fact about the person and the entity list depends on it.
        """
        system_superuser = (
            is_superadmin(jwt_system_role) or self._is_system_superuser(str(user.id))
        )
        is_super_admin = system_superuser or _holds_super_admin_anywhere(user.id)

        request.auth_user = user
        request.entity_id = entity_id
        request.entity_role = ""
        request.is_super_admin = is_super_admin
        request.is_system_superuser = system_superuser
        request.is_entity_member = False
        return user

    def authenticate(self, request, token):
        try:
            import hashlib

            key = settings.SECRET_KEY
            key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
            logger.info(
                "Auth attempt: key_len=%d key_hash=%s token_len=%d",
                len(key),
                key_hash,
                len(token),
            )
            payload = jwt.decode(token, key, algorithms=["HS256"])
            user = User.objects.get(id=payload["user_id"])
            user_id = payload["user_id"]
            token_entity_id = (payload.get("entity_id") or "").strip()
            header_entity_id = (request.headers.get("X-Entity-Id") or "").strip()
            entity_id = header_entity_id or token_entity_id

            # Trust the JWT-claimed system_role first: Flask issued and signed
            # this token, and reads its own pettycashv3.user table when doing
            # so. Falling back to a DB query here causes mismatches when the
            # stored value has unexpected casing or whitespace.
            jwt_system_role = (payload.get("system_role") or "").strip().lower()

            if (
                header_entity_id
                and token_entity_id
                and header_entity_id != token_entity_id
            ):
                logger.warning(
                    "Entity ID mismatch: header=%s, token=%s, user=%s",
                    header_entity_id,
                    token_entity_id,
                    user_id,
                )

            if not entity_id:
                # Unscoped: Flask handoff with empty entity (e.g. profile from Select Company).
                if not token_entity_id and not header_entity_id:
                    logger.info(
                        "Auth: unscoped billing session user_id=%s (no entity context)",
                        user_id,
                    )
                    return self._attach_unscoped(request, user, jwt_system_role)
                logger.warning("Auth rejected: no entity_id in header or token")
                return None

            entity_role = self._get_entity_role(user.id, entity_id)

            # Resolve whether this user is a system superuser once; reuse below.
            # Trust the JWT claim first (see comment above), then fall back to
            # DB lookup so older tokens without the claim still work.
            system_superuser = (
                is_superadmin(jwt_system_role)
                or self._is_system_superuser(str(user.id))
            )
            # is_entity_member is True only when the user has an explicit
            # UserEntity row for this entity.  System superusers without a
            # row are NOT members (they get view-only access via the virtual
            # role granted below).
            is_entity_member = entity_role is not None

            # Super admins (system_role='superadmin') are allowed into any entity
            # for read-only access even without a user_entity row.  Give them a
            # virtual 'super_admin' entity role so permission checks downstream
            # still work correctly.
            if entity_role is None:
                if system_superuser:
                    entity_role = "super_admin"
                    logger.info(
                        "Auth: superuser granted virtual super_admin role "
                        "for user_id=%s entity_id=%s",
                        user.id,
                        entity_id,
                    )
                elif not self.require_entity_role:
                    # A PERSON-level endpoint reached with a company the caller has no
                    # role on. Their identity is not in doubt — the token is signed by
                    # Module 1, unexpired, and names a real user — so the honest answer is
                    # "authenticated, no company context", not 401.
                    #
                    # This is what the profile page needs. It is reached two ways: from the
                    # entity list, which hands over a deliberately unscoped token, and from
                    # INSIDE a company, which scopes the token to that company. On the
                    # second path a caller with no `user_entity` row was rejected outright,
                    # and the frontend reported it as "our session timed out" and bounced
                    # them to a login that re-minted the same token — an endless loop over
                    # a screen that never needed the company in the first place.
                    logger.info(
                        "Auth: user_id=%s has no role on entity_id=%s — continuing "
                        "without a role (person-level endpoint)",
                        user.id,
                        entity_id,
                    )
                    return self._attach_unscoped(
                        request, user, jwt_system_role, entity_id
                    )
                else:
                    logger.warning(
                        "Auth rejected: no role for user_id=%s entity_id=%s",
                        user.id,
                        entity_id,
                    )
                    return None

            # is_super_admin is True for entity-level super_admin users AND for
            # system superusers (system_role='superadmin') so that the entity
            # list and other "see all" paths work correctly for both groups.
            is_super_admin = system_superuser or _holds_super_admin_anywhere(user.id)

            request.auth_user = user
            request.entity_id = entity_id
            request.entity_role = entity_role
            request.is_super_admin = is_super_admin
            request.is_system_superuser = system_superuser
            request.is_entity_member = is_entity_member
            return user
        except jwt.ExpiredSignatureError:
            logger.warning("Auth rejected: token expired")
            return None
        except (jwt.DecodeError, jwt.InvalidTokenError) as exc:
            logger.warning("Auth rejected: invalid token — %s", exc)
            return None
        except KeyError as exc:
            logger.warning("Auth rejected: missing claim in token — %s", exc)
            return None
        except User.DoesNotExist:
            logger.warning("Auth rejected: user not found in DB")
            return None

    # Kept as a staticmethod so `self._get_entity_role(...)` call sites and any
    # subclass override keep working; the implementation lives at module level.
    _get_entity_role = staticmethod(get_entity_role)

    @staticmethod
    def _is_system_superuser(user_id: str) -> bool:
        """Return True if the user's stored system_role is the super admin.

        The column is the ``system_role`` enum (``superadmin``); a comparison, not a
        case-insensitive match - the type has one spelling.
        """
        return User.objects.filter(id=str(user_id), system_role=SystemRole.SUPERADMIN).exists()


class SelfBearerAuth(BearerAuth):
    """Auth for the endpoints that describe the PERSON, not the company - here the whole
    ``/api/me/*`` payer portal (billing-backend uses it for ``/auth/*``).

    Identical to ``BearerAuth`` in everything that matters — same signature check, same
    user lookup, same entity resolution, and the same populated role when the caller DOES
    hold one on the entity in play. The single difference is what happens when they do
    not: instead of 401, the request continues with no company context.

    WHY THAT IS SAFE HERE: every ``/api/me/*`` route is keyed on ``request.auth_user`` -
    the payer's own subscriptions, invoices, cards and transfers. None reads a company's
    data through the company; the rows are found by ``payer_user_id``, and Flask's
    ``routes/portal.py`` applied exactly the same rule (``_user_id_from_bearer`` and
    nothing about the entity). The portal is reached from Minty's entity list with a
    deliberately UNSCOPED token, and from inside a company with a scoped one; both must
    work, which is what ``require_entity_role = False`` buys.

    WHAT IT MUST NOT BE USED FOR: the module settings page. ``/api/entities/{id}/modules``
    and its actions belong to a company, and their gate is that you hold a role on it
    (``BearerAuth`` + ``MODULE_VIEW`` / ``MODULE_MANAGE`` from ``core.policy``). ``BearerAuth``
    stays the default for the whole API (``config/urls.py``); this is opted into by the
    ``me`` router only.
    """

    require_entity_role = False


class EntityBearerAuth(BearerAuth):
    """Auth for the routes that belong to a COMPANY: the module settings page and the notice.

    ``BearerAuth`` (billing-backend's) lets a token with no company anywhere - neither an
    ``entity_id`` claim nor an ``X-Entity-Id`` header - through as an unscoped person, because
    billing-backend has person-level endpoints on the same router and each one checks the
    role it needs. Here the person-level routes have their own class (``SelfBearerAuth``, the
    ``me`` router), so a company route reached with no company is simply refused at the door:
    there is nothing it could answer about, and 401 is the shape the clients already handle
    (``lib/apiClient.ts`` sends the browser back through Flask's re-handoff, which re-mints the
    token FOR the company). The onboarding router keeps plain ``BearerAuth``: the wizard names
    its company in the body, as onboarding-backend does.
    """

    def _attach_unscoped(self, request, user, jwt_system_role: str, entity_id: str = ""):
        logger.warning(
            "Auth rejected: company route %s reached with no entity (user_id=%s)",
            request.path,
            user.id,
        )
        return None
