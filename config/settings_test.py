import os

from config.settings import *  # noqa: F401, F403

# A fixed key so tests can mint a JWT the service will accept. Nothing about the value
# matters except that signing and verifying use the same one - which is the whole contract
# with Flask in production, too.
SECRET_KEY = "test-secret-key-shared-with-flask"

# Never a timer in a test process.
SUBSCRIPTION_SCHEDULER_ENABLED = False

# Two test databases, chosen by MINTY_TEST_PG_URI:
#
#   unset  -> SQLite in memory, tables built FROM THE MODELS (SHARED_MODELS_MANAGED_FOR_TESTING).
#             Proves this service's logic. Cannot see whether the models match the real schema.
#   set    -> PostgreSQL, a database built FROM docs/schema/01_schema_rebased.sql in the Minty
#             repo by tests/pg_harness.py (loaded by conftest.py at the repo root). Nothing is
#             created from the models; a mirror column the schema lacks fails on the SELECT,
#             which is the point. Same knobs as Minty: MINTY_TEST_PG_DBNAME (minty_test),
#             MINTY_TEST_PG_KEEP=1, PG_BIN, MINTY_REPO (C:\Github\Minty).
_PG_URI = os.environ.get("MINTY_TEST_PG_URI")
if _PG_URI:
    from urllib.parse import urlsplit as _urlsplit

    _u = _urlsplit(_PG_URI)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test"),
            "USER": _u.username or "postgres",
            "PASSWORD": _u.password or "",
            "HOST": _u.hostname or "localhost",
            "PORT": str(_u.port or 5432),
            "OPTIONS": {"options": f"-c search_path={DB_SCHEMA},public"},  # noqa: F405
            # Never let pytest-django create/destroy a database of its own here; the root
            # conftest overrides django_db_setup and hands it the harness's build.
            "TEST": {"NAME": os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

LOGGING["handlers"]["file_core"] = {"class": "logging.NullHandler"}  # noqa: F405
LOGGING["handlers"]["file_api"] = {"class": "logging.NullHandler"}  # noqa: F405

# Build the shared tables in the test database. In production they are Alembic's.
SHARED_MODELS_MANAGED_FOR_TESTING = not _PG_URI  # tables come from the schema file on Postgres

# No test may reach the real Flask app or Stripe. billing/tests/conftest.py blocks the
# transport; these values exist so an un-stubbed call fails fast against an obviously fake
# host instead of quietly hitting a developer localhost or a live account.
FLASK_APP_URL = "http://flask.invalid"
ONBOARDING_WEB_URL = "http://onboarding.invalid"
STRIPE_SECRET_KEY = ""  # empty, like Flask's test env: an unstubbed get_stripe() raises
STRIPE_PUBLISHABLE_KEY = ""
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
