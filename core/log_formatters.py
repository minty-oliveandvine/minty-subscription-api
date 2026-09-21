import logging
from datetime import UTC, datetime


class CoreFormatter(logging.Formatter):
    """
    Core log format:
    2026-03-10T08:42:15Z INFO minty-api.bill_submission Bill submitted successfully
    """

    def format(self, record):
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return f"{ts} {record.levelname} {record.name} {record.getMessage()}"


class ApiFormatter(logging.Formatter):
    """
    API log format:
    2026-03-10T08:42:15Z INFO minty-api.http request_id=req_92fa user_id=128
        endpoint=/api/v1/bills method=POST Bill submitted
    """

    def format(self, record):
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        base = f"{ts} {record.levelname} {record.name}"
        extras = ""
        for attr in ("request_id", "user_id", "endpoint", "method"):
            val = getattr(record, attr, None)
            if val is not None:
                extras += f" {attr}={val}"
        return f"{base}{extras} {record.getMessage()}"
