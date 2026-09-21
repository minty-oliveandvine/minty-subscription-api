"""The per-request scope the engine caches on - what ``flask.g`` was in the original.

Flask's services memoise a handful of things on ``g`` for the life of one request or one CLI
invocation: the trusted clock (``clock``), a currency's decimal places (``money``), the billing
policy row (``policy``), the plan catalogue (``catalog``) and a customer's default card
(``stripe_client``). Each guards with ``has_app_context()``: outside a request nothing is
cached and every call is fresh.

Here that is a ``ContextVar`` holding a plain dict. ``scope()`` opens one; ``active()`` says
whether one is open; ``get``/``set`` read and write it. Who opens a scope:

* ``core.middleware.ServiceScopeMiddleware`` for every HTTP request;
* the ``subscriptions`` and ``replay_scenarios`` management commands for their whole run
  (Flask's CLI commands ran inside one app context, so one clock value per invocation);
* ``billing.scheduler.run_pass_now`` for each scheduled pass - INSIDE the job thread, because
  a new thread starts with an empty context and ``convert_or_expire_due_trials`` /
  ``notify_trials_ending`` read ``clock.now()`` themselves.

A ``ContextVar`` is per thread and per async task, so two gunicorn workers' requests never
share a scope, exactly as two Flask requests never shared a ``g``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar("billing_service_scope", default=None)

_MISSING = object()


def active() -> bool:
    """Is a scope open? Flask's ``has_app_context()``."""
    return _SCOPE.get() is not None


def get(key: str, default: Any = None) -> Any:
    """Read a memoised value; ``default`` when there is no scope or no such key."""
    scope = _SCOPE.get()
    if scope is None:
        return default
    return scope.get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - mirrors flask.g's setattr
    """Memoise a value for the rest of the scope. A no-op outside one, like ``setattr(g, …)``
    guarded by ``has_app_context()``."""
    scope = _SCOPE.get()
    if scope is not None:
        scope[key] = value


def pop(key: str) -> None:
    """Forget a memoised value (``stripe_client.forget_default_payment_method``)."""
    scope = _SCOPE.get()
    if scope is not None:
        scope.pop(key, None)


@contextmanager
def scope() -> Iterator[dict[str, Any]]:
    """Open a fresh scope for the block; nested scopes see their own dict."""
    token = _SCOPE.set({})
    try:
        yield _SCOPE.get()  # type: ignore[return-value]
    finally:
        _SCOPE.reset(token)
