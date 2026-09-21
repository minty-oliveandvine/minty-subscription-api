"""``scheduler.run_pass_now`` - the timer's entry into the engine.

It runs on APScheduler's worker thread, so it has to bring its own request scope (the memos
Flask's ``app.app_context()`` gave the timer) and tidy its own database connection. Both are
asserted through what ``daily.run_daily`` sees when the pass reaches it.
"""

from __future__ import annotations

import threading

import pytest

from billing import scheduler
from billing.services import _context, daily

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_pass_runs_inside_a_fresh_scope_on_its_own_thread(monkeypatch):
    seen = {}

    def _run_daily(now, *, issue, mode):
        seen["active"] = _context.active()
        seen["issue"] = issue
        seen["mode"] = mode
        return {"ok": True, "jobs": [], "behind": []}

    monkeypatch.setattr(daily, "run_daily", _run_daily)
    result = {}

    def worker():
        # A new thread starts with an empty ContextVar context, as APScheduler's does.
        result["out"] = scheduler.run_pass_now(mode="full")

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)

    assert result["out"] == {"ok": True, "jobs": [], "behind": []}
    assert seen == {"active": True, "issue": True, "mode": "full"}
    assert _context.active() is False, "the scope is the pass's, not the process's"


def test_the_pass_never_raises(monkeypatch):
    def _boom(now, *, issue, mode):
        raise RuntimeError("the pass fell over")

    monkeypatch.setattr(daily, "run_daily", _boom)
    assert scheduler.run_pass_now(mode="light") is None


def test_a_held_lock_skips_the_pass(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _held():
        yield False

    called = []
    monkeypatch.setattr(daily, "daily_lock", _held)
    monkeypatch.setattr(daily, "run_daily", lambda *a, **k: called.append(1))
    assert scheduler.run_pass_now(mode="full") is None
    assert called == []


def test_dark_runs_nothing(settings, monkeypatch):
    settings.SUBSCRIPTION_ENABLED = False
    called = []
    monkeypatch.setattr(daily, "run_daily", lambda *a, **k: called.append(1))
    assert scheduler.run_pass_now(mode="full") is None
    assert called == []
