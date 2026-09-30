"""``logger`` for the engine - loguru's call shape over the standard library.

Flask's services log with loguru: ``logger.info("renewals: {} issued for {}", n, user_id)`` -
``str.format`` placeholders filled from the positional arguments, plus ``logger.exception``
inside an ``except``. This service logs through ``logging.getLogger("billing-api")`` like the
other two Django services, so the 157 ported call sites keep their text and this adapter does
the formatting: ``{}`` messages are ``str.format``-ed, and the three portal lines that were
written with ``%s`` (and never interpolated under loguru) are ``%``-formatted. Formatting
happens eagerly only when the level is enabled, as loguru did.
"""

from __future__ import annotations

import logging
from typing import Any

_std = logging.getLogger("billing-api")


class _BraceLogger:
    def __init__(self, std: logging.Logger):
        self._std = std

    @staticmethod
    def _render(message: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        text = str(message)
        if not args and not kwargs:
            return text
        if "{" in text:
            try:
                return text.format(*args, **kwargs)
            except (IndexError, KeyError, ValueError):
                return text + " " + " ".join(repr(a) for a in args)
        try:
            return text % args
        except (TypeError, ValueError):
            return text + " " + " ".join(repr(a) for a in args)

    def _log(self, level: int, message: Any, args: tuple[Any, ...], kwargs: dict[str, Any], *,
             exc_info: bool = False) -> None:
        if not self._std.isEnabledFor(level):
            return
        self._std.log(level, self._render(message, args, kwargs), exc_info=exc_info, stacklevel=3)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, message, args, kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, message, args, kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, message, args, kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, message, args, kwargs)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, message, args, kwargs, exc_info=True)

    def critical(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, message, args, kwargs)


logger = _BraceLogger(_std)
