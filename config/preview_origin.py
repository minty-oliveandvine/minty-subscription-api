"""PREVIEW_ORIGIN_REGEX: the browser origins of a branch's preview set.

A branch's previews live on ``https://pr-<slug>-<app>.dailyminty.com`` (the ``.github`` repo's
``vercel-preview-set.yml``). minty-infra ``services.tf`` writes this regex, narrowed to the apps
that call each service, onto ONE environment's services (``var.preview_backend``) and nowhere
else; unset turns the feature off. It becomes ``CORS_ALLOWED_ORIGIN_REGEXES``, which
django-cors-headers applies with ``re.match``.

A value that could let in any origin outside that shape refuses to boot: a typo must never
open CORS. Copied verbatim in minty-subscription-api, minty-payment-request-api and
minty-onboarding-api (there is no shared Python package); change the three together.
"""

import re

from django.core.exceptions import ImproperlyConfigured

_PREFIX = "^https://pr-"
_SUFFIX = r"\.dailyminty\.com$"


def _has_top_level_bar(value: str) -> bool:
    """A ``|`` outside every group and class splits the anchors off into separate branches."""
    depth, in_class, i = 0, False, 0
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            return True
        i += 1
    return False


def preview_origin_regexes(raw: str | None) -> list[str]:
    value = (raw or "").strip()
    if not value:
        return []
    if not (value.startswith(_PREFIX) and value.endswith(_SUFFIX)) or _has_top_level_bar(value):
        raise ImproperlyConfigured(
            "PREVIEW_ORIGIN_REGEX must be one pattern from ^https://pr- to \\.dailyminty\\.com$ "
            "with no top-level |."
        )
    try:
        re.compile(value)
    except re.error as exc:
        raise ImproperlyConfigured(f"PREVIEW_ORIGIN_REGEX does not compile: {exc}") from None
    return [value]
