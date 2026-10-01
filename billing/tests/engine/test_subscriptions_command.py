"""``manage.py subscriptions <job>`` - one test per job, plus the contracts around them.

Each job reaches its service and prints its ASCII report; the Stripe-touching ones, run with
no key, report the missing key as THEIR failure and take nothing else down with them.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from billing.services import _context
from shared_models.models import EntityModuleSubscription

from .conftest import make_entity, make_user, seed_modules, seed_plans

pytestmark = pytest.mark.django_db


def run(*args):
    out = StringIO()
    call_command("subscriptions", *args, stdout=out)
    return out.getvalue()


# --- the jobs ---------------------------------------------------------------------------------


def test_jobs_run_inside_a_request_scope(monkeypatch):
    """Flask's commands ran under ``app.app_context()``; ours open ``_context.scope()`` so the
    clock, policy and catalog memos work the way they do in a request."""
    seen = []

    def _sweep():
        seen.append(_context.active())
        return {"disabled": [], "restored": []}

    monkeypatch.setattr("billing.services.access_sweep.sweep_expired_module_access", _sweep)
    out = run("sweep-access")
    assert seen == [True]
    assert "Disabled 0 module(s)" in out and "Restored 0 module(s)" in out


def test_close_trials_reports_what_it_closed(monkeypatch):
    monkeypatch.setattr(
        "billing.services.checkout.convert_or_expire_due_trials",
        lambda limit=None: {"converted": [{"entity_id": "e1", "code": "PETTY_CASH"}], "expired": []},
    )
    out = run("close-trials", "--limit", "5")
    assert "Converted 1 trial(s) to paid; expired 0." in out
    assert "converted: PETTY_CASH (entity e1)" in out


def test_run_renewals_is_dry_by_default_and_scopes_to_the_named_payers(monkeypatch):
    from datetime import UTC, datetime

    calls = []

    def _run(now, *, scope, issue, limit=None):
        calls.append((scope, issue, limit))
        return {"planned": [{"user_id": "u1", "total": 28000,
                             "period_start": datetime(2026, 9, 1, tzinfo=UTC),
                             "period_end": datetime(2026, 10, 1, tzinfo=UTC)}],
                "issued": [], "failed": [], "skipped": []}

    monkeypatch.setattr("billing.services.renewals.run_renewals", _run)
    out = run("run-renewals", "--user", "u1", "--user", "u2")
    assert calls == [(["u1", "u2"], False, None)]
    assert "DRY RUN - nothing charged. 1 payer(s) due:" in out
    assert "would bill 280.00 to u1 (01 Sep - 01 Oct 2026)" in out


def test_run_renewals_everyone_is_typed(monkeypatch):
    from billing.services import renewals

    calls = []
    monkeypatch.setattr(
        renewals, "run_renewals",
        lambda now, *, scope, issue, limit=None: calls.append(scope) or {"planned": [], "issued": [], "failed": [], "skipped": []},
    )
    run("run-renewals", "--issue")
    assert calls == [renewals.ALL_PAYERS]


def test_retry_dunning_reports(monkeypatch):
    monkeypatch.setattr(
        "billing.services.dunning.collect_due",
        lambda now, limit=None: {"retried": [{"user_id": "u1"}], "recovered": [{"user_id": "u1"}],
                                  "given_up": [{"user_id": "u2", "attempts": 13}]},
    )
    out = run("retry-dunning")
    assert "Retried 1; recovered 1; gave up on 1." in out
    assert "gave up:   u2 after 13 attempt(s)" in out


def test_notify_trial_ending_defaults_to_three_days(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "billing.services.checkout.notify_trials_ending",
        lambda days_before, limit=None: seen.append(days_before) or {"warned": [], "skipped": []},
    )
    run("notify-trial-ending")
    run("notify-trial-ending", "--days-before", "7")
    assert seen == [3, 7]


def test_revoke_ungranted_dry_lists_and_apply_writes():
    fns = seed_modules()
    owner = make_user("owner@test.com")
    bare = make_entity(owner, name="Bare Ltd", modules=("PETTY_CASH",))

    out = run("revoke-ungranted")
    assert "Would switch off 1 module grant(s)" in out and f"PETTY_CASH (entity {bare.id})" in out

    out = run("revoke-ungranted", "--apply")
    assert "Switched off 1 module grant(s)" in out
    from shared_models.models import EntityFunctionMap

    assert EntityFunctionMap.objects.get(entity_id=bare.id, entity_function_id=fns["PETTY_CASH"].id).is_enabled is False


def test_reconcile_customers_without_a_stripe_key_reports_the_key(monkeypatch):
    """What the real ``get_stripe`` raises with no key (the engine conftest replaces it with
    an assertion for every other test, so the message is restored here)."""

    def _unconfigured():
        raise RuntimeError("STRIPE_SECRET_KEY is not configured; cannot make Stripe API calls.")

    monkeypatch.setattr("billing.services.stripe_client.get_stripe", _unconfigured)
    with pytest.raises(CommandError, match="STRIPE_SECRET_KEY"):
        run("reconcile-customers")


def test_run_daily_without_a_key_reports_per_job_and_finishes(monkeypatch):
    """The pass catches per job: with no Stripe key the jobs that reach Stripe fail with the
    key's message, the others run, and the command still prints a complete report."""
    seed_plans()
    seed_modules()
    owner = make_user("owner@test.com")
    entity = make_entity(owner, modules=("PETTY_CASH",))
    from datetime import UTC, datetime

    EntityModuleSubscription.objects.create(
        entity_id=entity.id, function_code="PETTY_CASH", payer_user_id=owner.id, phase="trial",
        trial_end=datetime(2020, 1, 1, tzinfo=UTC),  # long due: close-trials will ask Stripe
    )
    out = run("run-daily", "--mode", "full")
    assert "No --issue" in out
    assert "Daily pass" in out
    # nothing raised out of the command; every job has a line
    for job in ("notify-trial-ending", "close-trials", "repair-transfers",
                "collect-transfers", "run-renewals", "retry-dunning"):
        assert job in out, out
