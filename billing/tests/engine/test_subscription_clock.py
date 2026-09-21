"""Trusted-clock tests: subscription access/grace decisions use Stripe's server
time (captured from the Date header of the customer and payment-method reads)
instead of the host wall clock, falling back to the database clock and then the
process clock when no Stripe response was seen."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_parse_http_date():
    from billing.services import clock

    dt = clock._parse_http_date("Wed, 01 Jul 2026 12:00:00 GMT")
    assert dt == datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    assert clock._parse_http_date(None) is None
    assert clock._parse_http_date("not a date") is None


def test_now_defaults_to_host_clock_without_record(app):
    from billing.services import clock

    with app.app_context():
        before = datetime.now(UTC)
        val = clock.now()
    # No server time recorded → within a second of the process clock.
    assert abs((val - before).total_seconds()) < 2


def test_now_returns_recorded_server_time(app):
    from billing.services import clock

    with app.app_context():
        clock.record_http_date("Wed, 01 Jul 2026 12:00:00 GMT")
        val = clock.now()
    assert val == datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def test_bad_header_leaves_host_clock(app):
    from billing.services import clock

    with app.app_context():
        clock.record_http_date("garbage")
        val = clock.now()
    assert abs((val - datetime.now(UTC)).total_seconds()) < 2


def test_grants_access_uses_trusted_clock(app):
    """With Stripe's server time pinned ahead of the host clock, access is decided by
    that clock, not the host clock.

    ``access.grants_access`` takes ``now`` as an argument rather than reading a clock
    itself, so what this pins is the pairing: every caller feeds it ``clock.now()``, and
    the trusted value is what decides. See ``test_the_access_sweep_reads_the_trusted_clock``
    for the caller side."""
    from datetime import timedelta

    from billing.services import access, clock

    now_real = datetime.now(UTC)
    paid_through = now_real + timedelta(days=5)  # host-now: grants access

    with app.app_context():
        # Pin trusted time 10 days ahead → the paid period has "ended" → no access.
        future = (now_real + timedelta(days=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        clock.record_http_date(future)
        assert access.grants_access(
            clock.now(),
            phase="active",
            trial_end=None,
            app_access_until=None,
            period_end=paid_through,
        ) is False
        # ...and the same row still grants against the host clock, so the assertion
        # above is about the clock and not about the dates.
        assert access.grants_access(
            now_real,
            phase="active",
            trial_end=None,
            app_access_until=None,
            period_end=paid_through,
        ) is True


# NOT covered here: that ``modules.sweep_expired_module_access`` feeds ``clock.now()``
# into ``grants_access`` rather than the host clock. It discovers its own entities from
# the database, so pinning it needs entity/module fixtures — that belongs with the
# sweep's own tests, not the clock's.


_STRIPE_DATE = "Wed, 01 Jul 2026 12:00:00 GMT"
_STRIPE_MOMENT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


class _Dated(dict):
    """A Stripe result: a mapping that also carries ``last_response``, as the SDK's do."""

    class last_response:  # noqa: N801 - mirrors the SDK attribute, not a class name
        headers = {"Date": _STRIPE_DATE}

    def auto_paging_iter(self):
        return iter(self.get("data", []))


def _fake_stripe():
    class _Stripe:
        class Customer:
            @staticmethod
            def retrieve(customer_id):
                return _Dated({"id": customer_id})

            @staticmethod
            def search(query, limit):
                return _Dated({"data": [{"id": "cus_1"}]})

        class PaymentMethod:
            @staticmethod
            def list(customer, limit):
                return _Dated({"data": [{"id": "pm_1"}]})

    return _Stripe()


@pytest.mark.parametrize(
    "read",
    [
        lambda sc: sc.retrieve_customer("cus_1"),
        lambda sc: sc.list_payment_methods("cus_1"),
        lambda sc: sc.find_customer_by_user("u1"),
    ],
    ids=["retrieve_customer", "list_payment_methods", "find_customer_by_user"],
)
def test_every_capturing_stripe_read_records_the_date_header(app, monkeypatch, read):
    """EACH of the three reads must pin the clock on its own.

    Source 1 is a side effect of calls made for other reasons, so it dies quietly when
    its host call is deleted -- which is exactly what happened when the in-house billing
    cutover removed ``list_customer_subscriptions``, the single capture point at the
    time. Parametrised rather than written once against whichever read is convenient:
    deleting any one capture point must fail here, not degrade the clock in silence.
    """
    from billing.services import clock

    stripe_client = pytest.importorskip("billing.services.stripe_client")  # slice B
    monkeypatch.setattr(stripe_client, "get_stripe", _fake_stripe)

    with app.app_context():
        read(stripe_client)
        assert clock.now() == _STRIPE_MOMENT
