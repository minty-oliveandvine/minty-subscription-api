"""How a payer-facing date is printed.

Small on purpose, and it exists because this repo had FOUR spellings of one date. Two of
them were byte-identical copies (``portal._fmt`` and ``payment_methods._fmt``); the other
two were the same unpadded format reached two different ways -- ``notify.day``, which
probed at runtime for glibc's ``%-d`` and fell back to ``"%d %b %Y".lstrip("0")`` on
Windows, and ``modules._fmt_day_month_year``, which just took the day as an int and let
strftime handle only the parts every platform agrees about.

What survives is ONE function per thing actually shown, and the difference between them is
a real typographic decision rather than an accident:

* :func:`day_padded` -- ``15 Aug 2026``. Tables. The payer portal's grid and the saved-card
  list set dates in a column, where a ragged left edge reads badly.
* :func:`day` -- ``5 Aug 2026``. Prose. Billing emails and the settings page, where nobody
  writes a zero in front of a date.
* :func:`day_month` -- ``5 Aug``. The same prose rule where the year is already obvious
  from context, as on a period that starts and ends inside one.

None of them uses ``%-d``: it is glibc-only and Windows strftime rejects it outright, and
these run under Task Scheduler on a Windows host. Formatting the day as an int sidesteps
the platform question entirely.

Every one answers ``None`` for a missing date rather than an empty string, so a caller can
tell "no date" from "a date that formatted to nothing".
"""
from __future__ import annotations


def day_padded(moment) -> str | None:
    """``15 Aug 2026`` -- zero-padded day, for dates set in a column."""
    return moment.strftime("%d %b %Y") if moment else None


def day(moment) -> str | None:
    """``5 Aug 2026`` -- unpadded day, for dates set in prose."""
    return f"{moment.day} {moment:%b %Y}" if moment else None


def day_month(moment) -> str | None:
    """``5 Aug`` -- unpadded, no year, where the year is already established."""
    return f"{moment.day} {moment:%b}" if moment else None
