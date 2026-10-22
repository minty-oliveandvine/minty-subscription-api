import logging
import time

from minty_shared import logctx

api_logger = logging.getLogger("billing-api.http")


class RequestLoggingMiddleware:
    """One trace per request (``minty_shared.logctx``): an incoming ``X-Request-ID`` is kept when
    well formed, echoed on the response, and carried by every line written meanwhile - the auth
    layer adds the user and entity, the route name is the fallback step. Then one access line."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = logctx.start_trace(request.headers.get(logctx.TRACE_HEADER))
        start = time.monotonic()
        try:
            response = self.get_response(request)
            duration_ms = int((time.monotonic() - start) * 1000)
            response[logctx.TRACE_HEADER] = request.request_id
            api_logger.info(
                "%s %s %s %dms",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
                extra={"endpoint": request.path, "method": request.method},
            )
            return response
        finally:
            logctx.reset()

    def process_view(self, request, view_func, view_args, view_kwargs):
        logctx.set_default_step(_route_name(request, view_func))
        return None


def _route_name(request, view_func) -> str | None:
    """The step a route gives its request: the ninja operation for THIS method - one URL pattern
    serves every method of a path, under the FIRST operation's URL name, so ``GET /bills/`` would
    read as the create - else the URL name (a plain Django view, or a ninja that changed shape;
    ``test_logging`` pins the ninja case)."""
    for cell in getattr(view_func, "__closure__", None) or ():
        try:
            operations = getattr(cell.cell_contents, "operations", None) or ()
        except ValueError:  # an empty cell
            continue
        for operation in operations:
            if request.method in operation.methods:
                return operation.view_func.__name__
    match = request.resolver_match
    return match.url_name if match else None


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
