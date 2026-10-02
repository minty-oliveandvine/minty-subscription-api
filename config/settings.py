import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from config.dburl import database_url, parse_database_url
from config.smtpurl import parse_smtp_url

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Shared with the Flask app (Minty), minty-payment-request-api and minty-onboarding-api.
# Flask MINTS the JWTs this service verifies (``core/auth.py``); this service never mints
# one, and a mismatch here 401s every request rather than failing loudly at boot. All four
# services must read it from one source (Part 3's ``minty-infra`` makes that one place).
_DEFAULT_SECRET_KEY = "change-me-in-production"
SECRET_KEY = os.environ.get("SECRET_KEY", _DEFAULT_SECRET_KEY)
# APP_ENV is ``development`` or ``production`` (the default); anything else is production.
APP_ENV = os.environ.get("APP_ENV", "production").strip().lower()
DEBUG = APP_ENV == "development"

# REFUSE TO BOOT WITH THE PLACEHOLDER KEY OUTSIDE DEVELOPMENT. On 2026-09-14 the onboarding
# API ran in production without SECRET_KEY set: every authenticated call answered 401 while
# the public endpoints kept working, and nothing said why. Worse than broken, it was
# forgeable - the fallback string is in the repo. A crash at startup is the loud failure.
if not DEBUG and SECRET_KEY == _DEFAULT_SECRET_KEY:
    raise ImproperlyConfigured(
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
# login-gated re-handoff (``{PETTY_CASH_PUBLIC_URL}/handoff/minty-web?next=...``).
# ---------------------------------------------------------------------------
PETTY_CASH_URL = (os.environ.get("PETTY_CASH_URL") or "http://localhost:8010").rstrip("/")
# The address a PERSON reaches Minty at - the links in emails. Distinct from PETTY_CASH_URL,
# which in the docker stack is the internal service name; defaults to it so a single-host
# setup needs one variable.
PETTY_CASH_PUBLIC_URL = (os.environ.get("PETTY_CASH_PUBLIC_URL") or PETTY_CASH_URL).rstrip("/")
# Seconds a forwarded call to Flask may take (core/flask_client.py).
FLASK_PROXY_TIMEOUT = 20

# ---------------------------------------------------------------------------
# CORS - three browser apps call this API: minty-web (the payer portal and the module
# settings page), and - since the sidebar with My Profile was copied into them on
# 2026-09-30 - minty-payment-request-web and Flask's own pages, whose My Profile shows the
# Subscriptions Overview from ``/api/me/subscriptions``. Flask (``PETTY_CASH_PUBLIC_URL``, the
# address a person's browser reaches it at) and minty-onboarding-api also call it server-side,
# which needs no CORS.
#
# ``x-entity-id`` IS advertised, unlike minty-onboarding-api: the module settings page
# reached from the portal carries an unscoped token and names the company in this header,
# exactly as minty-payment-request-web does with minty-payment-request-api.
# ---------------------------------------------------------------------------
MINTY_WEB_URL = (os.environ.get("MINTY_WEB_URL") or "http://localhost:3000").rstrip("/")
PAYMENT_REQUEST_WEB_URL = (
    os.environ.get("PAYMENT_REQUEST_WEB_URL") or "http://localhost:3020"
).rstrip("/")
CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in (
        os.environ.get("CORS_ALLOWED_ORIGINS")
        or f"{MINTY_WEB_URL},{PAYMENT_REQUEST_WEB_URL},{PETTY_CASH_PUBLIC_URL}"
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
# The schema name is a setting shared with Minty: ``?schema=`` on DATABASE_URL (default
# pettycashv3, config/dburl.py), the same URL Minty reads. Every db_table is unqualified and
# resolves through search_path. `DATABASE_URL=...?schema=pettycash_alt pytest` proves nothing
# spells it.
# ---------------------------------------------------------------------------
_DEFAULT_DB, DB_SCHEMA = parse_database_url(database_url())

DATABASES = {"default": _DEFAULT_DB}

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
# Mail - the eight subscription notices and the setup reminder, on the same Brevo SMTP
# Minty uses: SMTP_URL (config/smtpurl.py). Without it every send is logged and skipped
# rather than failing the pass that raised it (Flask's rule, kept). MAIL_FROM is the default
# sender; the billing sender is SUBSCRIPTION_EMAIL, as in Flask, and the setup reminder's
# ONBOARDING_EMAIL - both default to MAIL_FROM.
# ---------------------------------------------------------------------------
_SMTP = parse_smtp_url(os.environ.get("SMTP_URL"))
EMAIL_BACKEND = _SMTP["EMAIL_BACKEND"]
EMAIL_HOST = _SMTP["EMAIL_HOST"]
EMAIL_PORT = _SMTP["EMAIL_PORT"]
EMAIL_HOST_USER = _SMTP["EMAIL_HOST_USER"]
EMAIL_HOST_PASSWORD = _SMTP["EMAIL_HOST_PASSWORD"]
EMAIL_USE_TLS = _SMTP["EMAIL_USE_TLS"]
EMAIL_USE_SSL = _SMTP["EMAIL_USE_SSL"]
# Seconds each SMTP step may take (``?timeout=``, default 10). Django's default is None -
# block forever - and the notices go out inside the billing pass, which holds the scheduler
# lock while it waits.
EMAIL_TIMEOUT = _SMTP["EMAIL_TIMEOUT"]
DEFAULT_FROM_EMAIL = os.environ.get("MAIL_FROM") or "noreply@example.com"
SUBSCRIPTION_EMAIL = os.environ.get("SUBSCRIPTION_EMAIL") or DEFAULT_FROM_EMAIL
# The "finish setting up your company" reminder's sender (notify.onboarding_sender).
ONBOARDING_EMAIL = os.environ.get("ONBOARDING_EMAIL") or DEFAULT_FROM_EMAIL

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
