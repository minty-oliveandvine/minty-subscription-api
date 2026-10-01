import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Shared with the Flask app (Minty), billing-backend and onboarding-backend. Flask MINTS
# the JWTs this service verifies (``core/auth.py``); this service never mints one, and a
# mismatch here 401s every request rather than failing loudly at boot. All four services
# must read it from one source (Part 3's ``minty-infra`` makes that one place).
_DEFAULT_SECRET_KEY = "change-me-in-production"
SECRET_KEY = os.environ.get("SECRET_KEY", _DEFAULT_SECRET_KEY)
DEBUG = os.environ.get("DEBUG", "True").lower() in ("true", "1", "yes")

# REFUSE TO BOOT WITH THE PLACEHOLDER KEY OUTSIDE DEBUG. On 2026-09-14 onboarding-backend
# ran in production without SECRET_KEY set: every authenticated call answered 401 while
# the public endpoints kept working, and nothing said why. Worse than broken, it was
# forgeable - the fallback string is in the repo. A crash at startup is the loud failure.
if not DEBUG and SECRET_KEY == _DEFAULT_SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. It must be the same value the Flask app mints tokens "
        "with; without it every request is refused with 401. Refusing to start."
    )
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

# Deliberately no django.contrib.contenttypes / django.contrib.auth. Authentication is our
# own bearer scheme (core.auth) and authorisation our own membership checks (core.policy).
# There is no admin, no sessions, no ContentType lookups. Installing them would make
# `migrate` want to CREATE TABLE django_content_type / auth_* inside the schema Alembic
# owns - and this service never runs `migrate` at all (docker/entrypoint.sh).
INSTALLED_APPS = [
    "corsheaders",
    "shared_models",
    "billing",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "core.middleware.ServiceScopeMiddleware",
    "core.middleware.RequestLoggingMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Jinja2 for the notification template (templates/email/subscription_notice.html), so it
# moved over from Flask verbatim in Part 2 step 2. No Django template engine: this service
# renders no pages.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.jinja2.Jinja2",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {"autoescape": True},
    },
]


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# The scheduler switch
#
# Subscriptions are always on: every route answers. (The feature-wide dark switch,
# SUBSCRIPTION_ENABLED, was removed 2026-10-01 once the service ran on a test site.)
#
# SUBSCRIPTION_SCHEDULER_ENABLED - the in-process timer (billing/scheduler.py), and the only
# thing that decides whether it starts. Off by default so importing the app in a test or a
# shell bills nobody; on in the deployed web service only, until Part 3's Terraform moves the
# pass to a Render Cron Job running ``manage.py subscriptions tick``.
# ---------------------------------------------------------------------------
SUBSCRIPTION_SCHEDULER_ENABLED = _flag("SUBSCRIPTION_SCHEDULER_ENABLED", False)
SUBSCRIPTION_SCHEDULER_LIGHT = _flag("SUBSCRIPTION_SCHEDULER_LIGHT", True)
SUBSCRIPTION_SCHEDULER_TZ = os.environ.get("SUBSCRIPTION_SCHEDULER_TZ") or "Asia/Hong_Kong"
SUBSCRIPTION_SCHEDULER_FULL_HOUR = int(os.environ.get("SUBSCRIPTION_SCHEDULER_FULL_HOUR") or 5) % 24

# ---------------------------------------------------------------------------
# Cross-service: the Flask app. Identity and the company are Flask's until Part 3, so
# the portal's invite-admin forwards there (core/flask_client.py - the only module that
# calls Flask), and the links this service puts in emails point at minty-web via Flask's
# login-gated re-handoff (``{FLASK_APP_URL}/handoff/minty-web?next=...``).
# ---------------------------------------------------------------------------
FLASK_APP_URL = os.environ.get("FLASK_APP_URL", "http://localhost:5001").rstrip("/")
# The address a PERSON reaches Minty at - the links in emails (Flask's PUBLIC_URL). Distinct
# from FLASK_APP_URL, which in the docker stack is the internal service name; defaults to it
# so a single-host setup needs one variable.
MINTY_PUBLIC_URL = os.environ.get("MINTY_PUBLIC_URL", FLASK_APP_URL).rstrip("/")
FLASK_PROXY_TIMEOUT = int(os.environ.get("FLASK_PROXY_TIMEOUT", "20"))

# ---------------------------------------------------------------------------
# CORS - three browser apps call this API: minty-web (the payer portal and the module
# settings page), and - since the sidebar with My Profile was copied into them on
# 2026-09-30 - billing-frontend and Flask's own pages, whose My Profile shows the
# Subscriptions Overview from ``/api/me/subscriptions``. Flask (``MINTY_PUBLIC_URL``, the
# address a person's browser reaches it at) and onboarding-backend also call it server-side,
# which needs no CORS.
#
# ``x-entity-id`` IS advertised, unlike onboarding-backend: the module settings page
# reached from the portal carries an unscoped token and names the company in this header,
# exactly as billing-frontend does with billing-backend.
# ---------------------------------------------------------------------------
MINTY_WEB_URL = os.environ.get("MINTY_WEB_URL", "http://localhost:3002").rstrip("/")
PAYMENTS_WEB_URL = os.environ.get("PAYMENTS_WEB_URL", "http://localhost:3000").rstrip("/")
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS", f"{MINTY_WEB_URL},{PAYMENTS_WEB_URL},{MINTY_PUBLIC_URL}"
    ).split(",")
    if origin.strip()
]
CORS_ALLOW_HEADERS = [
    "authorization",
    "content-type",
    "accept",
    "origin",
    "x-entity-id",
]

# ---------------------------------------------------------------------------
# Database - the same database and schema the Flask app owns
#
# This service is a TENANT of the schema, never its owner. Every model is managed = False
# and this repo ships no migrations: Alembic in Minty is the owner-of-record for all DDL.
# The schema name is a setting shared with Minty (blueprints/shared/schema.py reads the
# SAME variable with the same default); every db_table is unqualified and resolves through
# search_path. `MINTY_DB_SCHEMA=pettycash_alt pytest` proves nothing spells it.
# ---------------------------------------------------------------------------
DB_SCHEMA = os.environ.get("MINTY_DB_SCHEMA", "pettycashv3")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "postgres"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "admin"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "OPTIONS": {"options": f"-c search_path={DB_SCHEMA},public"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
USE_I18N = False
# Every stamp in section K is TIMESTAMPTZ and the engine's clock is the DATABASE's now()
# (``billing/services/clock.py`` in step 2). USE_TZ stays True; a naive datetime anywhere
# in this service is a bug.
USE_TZ = True
TIME_ZONE = "UTC"

# ---------------------------------------------------------------------------
# Stripe - THIS SERVICE IS THE ONLY HOLDER OF THE KEYS (cross-cutting rule 9).
#
# Flask's copy goes in Part 2 step 5 and a guard test there keeps it out; onboarding-
# backend proxies its card routes here. ``billing/services/stripe_client.py`` is the one
# module that imports ``stripe``, and ``billing_gateway.py`` the one that charges.
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

# ---------------------------------------------------------------------------
# Mail - the eight subscription notices, on the same Brevo SMTP Minty uses. Without
# EMAIL_HOST every send is logged and skipped rather than failing the pass that raised it
# (Flask's rule, kept). The sender is SUBSCRIPTION_EMAIL, as in Flask.
# ---------------------------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend"
    if os.environ.get("EMAIL_HOST")
    else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _flag("EMAIL_USE_TLS", True)
# Seconds each SMTP step may take. Django's default is None - block forever - and the notices
# go out inside the billing pass, which holds the scheduler lock while it waits.
EMAIL_TIMEOUT = float(os.environ.get("EMAIL_TIMEOUT") or 10)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL") or os.environ.get(
    "SUBSCRIPTION_EMAIL", "noreply@example.com"
)
SUBSCRIPTION_EMAIL = os.environ.get("SUBSCRIPTION_EMAIL", DEFAULT_FROM_EMAIL)

# ---------------------------------------------------------------------------
# Logging - core + API formatters (same shape as the other two Django services so the
# logs read side by side)
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "core": {"()": "core.log_formatters.CoreFormatter"},
        "api": {"()": "core.log_formatters.ApiFormatter"},
    },
    "handlers": {
        "console_core": {"class": "logging.StreamHandler", "formatter": "core"},
        "console_api": {"class": "logging.StreamHandler", "formatter": "api"},
        "file_core": {
            "class": "logging.FileHandler",
            "filename": str(LOG_DIR / "core.log"),
            "formatter": "core",
        },
        "file_api": {
            "class": "logging.FileHandler",
            "filename": str(LOG_DIR / "api.log"),
            "formatter": "api",
        },
    },
    "loggers": {
        "billing-api": {
            "handlers": ["console_core", "file_core"],
            "level": os.environ.get("LOG_LEVEL", "INFO"),
        },
        "billing-api.http": {
            "handlers": ["console_api", "file_api"],
            "level": os.environ.get("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
