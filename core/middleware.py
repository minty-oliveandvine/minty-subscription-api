import logging
import time
import uuid

api_logger = logging.getLogger("billing-api.http")


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = f"req_{uuid.uuid4().hex[:4]}"
        start = time.time()

        response = self.get_response(request)

        duration_ms = int((time.time() - start) * 1000)
        user_id = getattr(getattr(request, "auth_user", None), "id", "-")

        api_logger.info(
            "%s %dms",
            response.status_code,
            duration_ms,
            extra={
                "request_id": request.request_id,
                "user_id": user_id,
                "endpoint": request.path,
                "method": request.method,
            },
        )
        return response


class SubscriptionsDarkMiddleware:
    """The dark contract: while ``SUBSCRIPTION_ENABLED`` is off, every path but the health
    check and the OpenAPI document answers ``404 {"error": "not_found"}`` - the same body
    Flask's ``require_subscriptions_enabled`` gives - and it answers it WITH CORS headers.

    Why the headers matter: the browser apps read a 404 as "the feature is not there" and
    hide their doors. A 404 with no ``Access-Control-Allow-Origin`` never reaches their code
    at all; it surfaces as a CORS failure, which the apps report as an outage. So this
    middleware sits INSIDE ``corsheaders.middleware.CorsMiddleware`` in the stack (listed
    after it in ``MIDDLEWARE``), so the CORS middleware still decorates the response it
    returns, and it lets OPTIONS through untouched so the preflight itself succeeds.

    Read on every request, like Flask's flag: cheap, and a test flips the setting without
    rebuilding the app. Production is switched on by redeploying with the variable set -
    API first, then the web apps, then Minty (Part 2 step 7, 8b of the plan).
    """

    #: Paths that answer while dark. ``/healthz`` so the platform's probe keeps the service
    #: alive; the OpenAPI document so the contract can be read without switching it on.
    OPEN_PATHS = frozenset({"/healthz", "/api/openapi.json"})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        from django.http import JsonResponse

        if settings.SUBSCRIPTION_ENABLED or request.method == "OPTIONS":
            return self.get_response(request)
        if request.path in self.OPEN_PATHS or request.path.rstrip("/") in self.OPEN_PATHS:
            return self.get_response(request)
        return JsonResponse({"error": "not_found"}, status=404)
