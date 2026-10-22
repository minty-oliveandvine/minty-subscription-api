"""This service's own middleware. The trace and the access line are every API's:
``minty_shared.django.middleware.RequestLoggingMiddleware`` (config/settings.py)."""


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
