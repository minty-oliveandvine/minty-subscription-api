"""Every route exists from day one; none does anything until Part 2 step 3 fills it.

The stubs are here rather than left out so the contract - paths, methods, auth class - is
pinned by tests and by the OpenAPI document before a line of the engine is ported, and so
the dark test can walk every path and see the 404 in front of it.
"""

from django.http import JsonResponse

NOT_IMPLEMENTED = {"error": "not_implemented"}


def not_implemented(request, **_):
    """``501`` with the house body shape (``error``, see core/exceptions.py)."""
    return JsonResponse(NOT_IMPLEMENTED, status=501)
