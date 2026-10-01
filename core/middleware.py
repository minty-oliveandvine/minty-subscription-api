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


class ServiceScopeMiddleware:
    """Open one ``billing.services._context`` scope per request - what Flask's app context
    was to ``g``. The engine memoises the trusted clock, a currency's decimal places, the
    billing policy row, the plan catalogue and a customer's default card on it, for the life
    of the request and no longer."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from billing.services import _context

        with _context.scope():
            return self.get_response(request)
