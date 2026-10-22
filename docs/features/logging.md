# Logging

Every line this service writes, like every other Minty backend's (Flask included), has one format,
defined once in [`minty-shared-py`](https://github.com/minty-oliveandvine/minty-shared-py)
(`minty_shared.logctx`, pinned in `requirements.txt`; since 2026-10-22):

```
2026-10-22T08:42:15.123Z INFO [trace=a1b2c3d4] [user=<uuid>|entity=<uuid>|step=subscriptions.run-renewals] message
```

`-` stands for anything not known. `config/settings.py` builds the `LOGGING` dict with
`logging_config`:

| Logger | Writes | File |
|---|---|---|
| `billing-api` | everything the service logs | `logs/core.log` |
| `billing-api.http` | one access line per request, `METHOD path status ms` (does not propagate) | `logs/api.log` |

Both also go to stderr, which is what Render keeps.

- **trace**: `minty_shared.django.middleware.RequestLoggingMiddleware` (settings `MINTY_HTTP_LOGGER`) opens one per request. An incoming
  `X-Request-ID` of 8-32 lowercase hex digits is kept; anything else (missing, uppercase, a
  newline, a forged `[user=...]`) is replaced with 8 fresh digits. The id is echoed on the
  response as `X-Request-ID` and kept on `request.request_id`.
- **user / entity**: bound only once they are verified. `core.auth.BearerAuth` binds the user and the company once the caller's role on it is settled; a person-level route reached with a company they hold no role on binds the user only (`_attach_unscoped`).
- **step**: the route's URL name (ninja names every operation) unless code names one with
  `with logctx.log_step("area.action"):` or `@logctx.log_step(...)`. Each job of the daily pass is `subscriptions.<job>` (`billing/services/daily.py`); a timer pass runs under its own trace and `scheduler.<mode>` (`billing/scheduler.py`), and `manage.py subscriptions <job>` under `command.subscriptions.<job>`.
- **Calls to Flask** send the trace (`logctx.outgoing_headers()`), so Flask's lines for the same
  request share it: `core/flask_client.py::forward`. Calls to Xero and Stripe never do.
- **Context is cleared** at the end of each request (`logctx.reset()`), so a reused worker never
  prints the previous caller.

Tests: `billing/tests/test_logging.py`. When you add a log call, keep ids the line already carries out of the message;
a row id (`bill=…`, `link=…`) still belongs in it.
