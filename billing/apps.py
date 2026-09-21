import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger("billing-api")


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"
    verbose_name = "Subscriptions"

    def ready(self):
        """Start the in-process timer - in the WEB process only.

        ``ready()`` runs in every process that sets up Django: gunicorn's workers (the
        ones we want), but also ``manage.py`` commands, the test runner and the dev
        server's autoreloader parent. ``billing.scheduler.start_scheduler`` makes the
        env-gate decision (both switches must be on); this method only rules out the
        processes that are not a web server, because a timer inside ``manage.py
        subscriptions run-daily`` would run the pass twice, and one inside pytest would
        bill somebody.
        """
        if _is_management_command() or _is_autoreloader_parent():
            return
        from billing.scheduler import start_scheduler

        start_scheduler()


def _is_management_command() -> bool:
    """``manage.py <anything>`` except ``runserver`` - the dev server IS a web process."""
    argv = sys.argv
    if not argv or not argv[0].endswith("manage.py"):
        return False  # gunicorn / uvicorn / the wsgi module
    return len(argv) < 2 or argv[1] != "runserver"


def _is_autoreloader_parent() -> bool:
    """The dev server's supervisor imports the app and then forks the real one. Without
    this the supervisor gets a scheduler too, and every code edit leaves another behind
    (Flask's scheduler skips its Werkzeug equivalent for the same reason)."""
    return "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true"
