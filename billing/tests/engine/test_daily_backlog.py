"""The renewal backlog report at the end of a pass (``daily._log_renewal_backlog``).

``renewals.due_renewals`` became one ``(account, group, paid_through)`` per CARD when cards got
their own periods. The report went on unpacking it as ``(account, paid_through)``, so it raised
on the very case it exists to report - any card still due after the renewal step - and it sat
outside the step's try: ``run_daily`` raised, and dunning and the access sweep were skipped
for EVERY payer, on every pass, for as long as one card stayed due (a declined renewal is
enough). The one test of it stubbed the old shape, so nothing noticed.

What is pinned: the report reads the list as it really is, counts only cards billed THIS pass
(a declined card is dunning's, not a backlog), and cannot cost the pass its later steps.
"""

from __future__ import annotations

from billing.services import daily
from billing.tests.engine.test_renewal_runner import NOW, _Record, _wire


def test_a_card_billed_and_still_due_is_reported_from_the_real_list(monkeypatch):
    renewals, _calls = _wire(monkeypatch)
    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)
    assert result["issued"]

    # The stubbed store never moves ``paid_through``, so the card is still due after being
    # billed: more than one period behind, and billed again on the next pass.
    assert daily._log_renewal_backlog(NOW, result) == ["u1"]


def test_a_card_whose_charge_failed_is_dunnings_not_a_backlog(monkeypatch):
    renewals, _calls = _wire(monkeypatch, issued={"id": "in_1", "status": "open"})
    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)
    assert result["failed"]

    assert daily._log_renewal_backlog(NOW, result) == []


def test_a_card_that_failed_on_an_earlier_pass_is_not_a_backlog_either(monkeypatch):
    """Skipped as already invoiced and unpaid - dunning's, every hour, not "behind"."""
    from billing.services import billing_gateway

    renewals, _calls = _wire(monkeypatch, existing=_Record(external_id="in_o", status="open"))
    monkeypatch.setattr(billing_gateway, "refresh_record", lambda record: "open")
    result = renewals.run_renewals(NOW, scope=["u1"], issue=True)
    assert result["skipped"][0]["reason"] == "already invoiced; unpaid"

    assert daily._log_renewal_backlog(NOW, result) == []


def test_a_report_that_fails_does_not_cost_the_pass_its_later_steps(monkeypatch, caplog):
    ran = []

    def _runner(name):
        return lambda now, **kw: ran.append(name) or {"issued": []}

    monkeypatch.setattr(daily, "_RUNNERS", {name: _runner(name) for name in daily._RUNNERS})

    def _broken(now, result):
        raise ValueError("too many values to unpack")

    monkeypatch.setattr(daily, "_log_renewal_backlog", _broken)

    result = daily.run_daily(NOW, issue=True, mode=daily.FULL)

    assert ran[-2:] == [daily.RETRY_DUNNING, daily.SWEEP_ACCESS]
    assert result["ok"] is True
    assert any(r.levelname == "ERROR"
               and "could not report the renewal backlog" in r.getMessage()
               for r in caplog.records)
