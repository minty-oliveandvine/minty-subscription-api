"""``GET /api/entities/{id}/subscription-notice`` - the notice the payment module's landing
page shows (trial ending, past due, cancelled...). Flask served it as
``/api/entity/<id>/subscription-notice`` in ``entity/routes/modules.py``; the plural is the
one path change. The JSON keeps ``settings_path`` so billing-frontend's ``buildMintyEnterUrl``
is untouched. Flask's own dashboard fetches the same notice server-side with a five-minute
self-minted assertion (Part 2 decision) - any failure there means no notice, never an error.
"""

from ninja import Router

from billing.api._stub import not_implemented
from core.auth import EntityBearerAuth

notice_router = Router(auth=EntityBearerAuth())


@notice_router.get("/{entity_id}/subscription-notice", summary="The dashboard notice (stub)")
def subscription_notice(request, entity_id: str):
    return not_implemented(request)
