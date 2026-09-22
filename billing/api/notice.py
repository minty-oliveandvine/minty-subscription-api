"""``GET /api/entities/{id}/subscription-notice`` - the notice the payment module's landing
page shows (trial ending, past due, cancelled...). Flask served it as
``/api/entity/<id>/subscription-notice`` in ``entity/routes/modules.py``
(``subscription_notice_api``); the plural is the one path change. The JSON keeps
``settings_path`` so billing-frontend's ``buildMintyEnterUrl`` is untouched. Flask's own
dashboard fetches the same notice server-side with a five-minute self-minted assertion (Part 2
decision) - any failure there means no notice, never an error.

Same data the Petty Cash dashboard modal renders, over JSON. The billing frontend talks to its
own backend for everything else; subscription state lives only here, so this is the one
endpoint it calls on this origin. Stateless by design: it does NOT consume the "show once per
session" claim (``notices.claim_subscription_notice`` stays Flask's, session-bound); the
frontend keeps its own per-tab flag.

The gate is Flask's, in ``NoticeBearerAuth``: the token must be Flask's (signed, unexpired,
naming a real user); a token that names a company is held to it (403 ``entity_mismatch`` when
the path names another); a token that names NO company - the refresh path mints through the
payment module's backend and need not preserve the claim - falls back to the caller's
membership of the company in the PATH, which is what authorises the read in any case. That
fallback is the one thing ``EntityBearerAuth`` does not do (it refuses a company-less token at
the door), and it matters here because billing-frontend sends only the bearer, never
``X-Entity-Id``.
"""

from __future__ import annotations

from ninja import Router

from billing.api._json import respond
from billing.services._log import logger
from core.auth import EntityBearerAuth, get_entity_role
from shared_models.enums import is_superadmin


class NoticeBearerAuth(EntityBearerAuth):
    """``EntityBearerAuth`` plus Flask's fallback for a token that names no company."""

    def _attach_unscoped(self, request, user, jwt_system_role: str, entity_id: str = ""):
        path_entity = str((getattr(request.resolver_match, "kwargs", None) or {}).get("entity_id") or "")
        role = get_entity_role(str(user.id), path_entity) if path_entity else None
        system_superuser = is_superadmin(jwt_system_role) or self._is_system_superuser(str(user.id))
        if role is None and not system_superuser:
            logger.info("Notice API: user {} has no access to {}", user.id, path_entity)
            return None
        request.auth_user = user
        request.entity_id = path_entity
        request.entity_role = role or "super_admin"
        request.is_super_admin = system_superuser
        request.is_system_superuser = system_superuser
        request.is_entity_member = role is not None
        return user


notice_router = Router(auth=NoticeBearerAuth())


@notice_router.get("/{entity_id}/subscription-notice", summary="The dashboard notice")
def subscription_notice(request, entity_id: str):
    """``{"items": [...], "can_manage", "payer", "severity", "settings_path"}``. A notice must
    never break the landing page: a builder failure answers ``{"items": []}`` with 200."""
    from billing.services import entity_modules

    # The claim scopes the token to one company; a path naming another is refused. The auth
    # class already proved membership of ``request.entity_id`` (header, claim, or the path).
    if str(getattr(request, "entity_id", "") or "") != str(entity_id):
        logger.info("Notice API: token scoped to {}, asked for {}", request.entity_id, entity_id)
        return respond({"error": "entity_mismatch"}, 403)

    user_id = str(request.auth_user.id)
    try:
        notice = entity_modules.build_subscription_notices(str(entity_id), user_id)
    except Exception:
        logger.exception("Subscription notice API failed for {}", entity_id)
        return respond({"items": []})

    # A Minty PATH, not a URL: the frontend's buildMintyEnterUrl() hands its token back for
    # a session. Returning a bare origin here would land on the login form.
    notice["settings_path"] = f"/entity/settings/module/{entity_id}"
    return respond(notice)
