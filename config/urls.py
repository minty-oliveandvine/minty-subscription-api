"""URL mounting.

THE PATHS ARE FLASK'S, RE-HOMED UNDER ONE API.

* ``/api/me/*`` - the payer portal: the fifteen paths of Flask's ``routes/portal.py`` plus
  this service's eight (``transfer/seen``, the four ``billing/accounts`` routes, and an
  invoice's ``breakdown``, ``retry`` and ``pdf``), Flask's paths and JSON unchanged, so
  ``payerPortal.ts`` moved to minty-web with a new base URL and no path edits.
* ``/api/entities/{id}/modules`` and ``/api/entities/{id}/modules/{action}`` - the module
  settings page. In Flask these were the Jinja page plus nineteen ``POST
  /entity/settings/module/<org_id>/<action>`` routes; here they are one page model and
  one action endpoint, because the page is minty-web's now and there is no Jinja to serve.
* ``/api/entities/{id}/subscription-notice`` - the notice the payment module's landing page
  shows (Flask: ``/api/entity/<id>/subscription-notice``; the plural is the one path change,
  made in billing-frontend's ``lib/subscriptionNotice.ts`` together with the base URL).
* ``/api/onboarding/*`` - the nine card and billing-account routes the wizard's backend
  proxies here, plus the new ``POST /api/onboarding/trials/start`` that finalize calls.

AUTH DEFAULTS TO ON. ``NinjaAPI(auth=BearerAuth())`` makes every endpoint token-gated
unless its router says otherwise, so forgetting the decorator on a new endpoint fails
closed. The ``me`` router opts into ``SelfBearerAuth`` (person, not company); nothing is
public but ``/healthz`` and the OpenAPI document.

Every router is live (Part 2 step 3); ``billing/tests/test_contract.py`` pins the paths.
"""

from django.http import JsonResponse
from django.urls import path
from ninja import NinjaAPI

from core.auth import BearerAuth
from core.exceptions import register_exception_handlers

api = NinjaAPI(
    title="Minty Billing API",
    version="0.1.0",
    description="The subscription engine, extracted from Minty (Part 2 of the modernisation plan).",
    auth=BearerAuth(),
    # The document is public (with /healthz) so the contract can be read without a token.
    # Interactive docs stay off the public surface; open them locally with DEBUG if wanted.
    openapi_url="/openapi.json",
    docs_url="/_docs",
)

register_exception_handlers(api)

from billing.api.me import me_router  # noqa: E402
from billing.api.modules import modules_router  # noqa: E402
from billing.api.notice import notice_router  # noqa: E402
from billing.api.onboarding import onboarding_router  # noqa: E402

api.add_router("/me", me_router, tags=["Payer portal"])
api.add_router("/entities", modules_router, tags=["Module settings"])
api.add_router("/entities", notice_router, tags=["Notice"])
api.add_router("/onboarding", onboarding_router, tags=["Onboarding"])


def healthz(request):
    """Liveness only - does not touch the database, and needs no token.

    Deliberately not a readiness check: the container entrypoint already waits for the
    database and the schema before starting, so a health endpoint that also queried would
    report unhealthy for a transient database blip and get the container killed mid-request.
    """
    return JsonResponse({"status": "ok", "service": "minty-billing-api"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("api/", api.urls),
]
