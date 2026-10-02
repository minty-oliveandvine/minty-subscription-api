# minty-subscription-api smoke tests

```bash
pytest e2e                       # against a service that is already running
```

HTTP level, no browser, **nothing is started here**: the service under test is already up
(`manage.py runserver 8000`, the docker stack, or a deployment). Every test skips with a
reason when it is not answering, and the token tests skip without the credentials.

| Variable | What |
|---|---|
| `E2E_BASE_URL` | default `http://127.0.0.1:8000` (not `localhost`: Windows resolves it to `::1` first and every request stalls ~2 s); a Render URL for a run against a deployment |
| `E2E_MINTY_WEB_URL` | the minty-web origin the CORS assertions use, default `http://localhost:3000` |
| `E2E_PAYMENT_REQUEST_WEB_URL` | the payment-request UI's origin (minty-payment-request-web, the notice's caller), default `http://localhost:3020` |
| `E2E_JWT_SECRET` | the `SECRET_KEY` shared with Minty; the tests mint the same token Flask does |
| `E2E_MINTY_USER` / `E2E_MINTY_ENTITY` | the identity `Minty/scripts/e2e_seed.py --print` creates |

Never commit any of these values.

## What it pins

| Group | Checks |
|---|---|
| public | `/healthz` 200 · `/api/openapi.json` readable · the CORS preflight from minty-web succeeds (with `x-entity-id`) |
| `TestLive` | no token → `401 {"error":"Unauthorized"}`; a forged token → 401; a Flask-shaped token gets the portal's table (200, `entities` + `total`, CORS); the module page refuses an unscoped token without `X-Entity-Id` and answers its page model with it (`entity_id`, `cards`, `can_manage_modules`, `viewer`); the notice answers a member with `items` and `settings_path` (Flask's `/handoff/minty-web?...` hand-over), CORS for the payment UI's origin |

These are the checks the runbook runs after each deploy. (The dark cases went with the dark
switch, 2026-10-01: subscriptions are always on.) The unit suite
(`pytest`, which reads `testpaths = billing`) never collects this folder; the two share the
settings module but nothing else.

## Later (Part 2 steps 4–6)

The live journeys — start a card-free trial → Flask's gate opens → `manage.py subscriptions
run-daily --mode full` past `trial_end` → module off → restart quote → cancel — are browser
journeys and live in `minty-web/features/subscription/e2e/`; this folder stays the API's
own contract check.
