"""``SMTP_URL`` -> Django's EMAIL_* settings.

    smtp://user:pass@host:587    STARTTLS (``?tls=0`` turns it off, for a local catcher)
    smtps://user:pass@host:465   implicit SSL

``?timeout=`` is seconds per SMTP step (default 10). User and password are percent-decoded.
Unset or empty -> the console backend: every send is written out and skipped.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlsplit

SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_TIMEOUT = 10.0
_PORTS = {"smtp": 587, "smtps": 465}


def parse_smtp_url(url: str | None) -> dict:
    """The EMAIL_* settings for ``url`` (keys are the Django setting names)."""
    if not url:
        return {
            "EMAIL_BACKEND": CONSOLE_BACKEND,
            "EMAIL_HOST": "",
            "EMAIL_PORT": _PORTS["smtp"],
            "EMAIL_HOST_USER": "",
            "EMAIL_HOST_PASSWORD": "",
            "EMAIL_USE_TLS": False,
            "EMAIL_USE_SSL": False,
            "EMAIL_TIMEOUT": DEFAULT_TIMEOUT,
        }
    parts = urlsplit(url)
    if parts.scheme not in _PORTS:
        raise ValueError(f"SMTP_URL must be smtp:// or smtps://, not {parts.scheme!r}")
    query = dict(parse_qsl(parts.query))
    ssl = parts.scheme == "smtps"
    return {
        "EMAIL_BACKEND": SMTP_BACKEND,
        "EMAIL_HOST": parts.hostname or "",
        "EMAIL_PORT": parts.port or _PORTS[parts.scheme],
        "EMAIL_HOST_USER": unquote(parts.username or ""),
        "EMAIL_HOST_PASSWORD": unquote(parts.password or ""),
        "EMAIL_USE_TLS": not ssl and query.get("tls", "1").strip().lower() not in ("0", "false", "no", "off"),
        "EMAIL_USE_SSL": ssl,
        "EMAIL_TIMEOUT": float(query.get("timeout") or DEFAULT_TIMEOUT),
    }
