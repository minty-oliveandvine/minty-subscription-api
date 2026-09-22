# minty-billing-api smoke tests

```bash
pytest e2e                       # against a service that is already running
E2E_SUBSCRIPTIONS=0 pytest e2e   # against one running dark
```

HTTP level, no browser, **nothing is started here**: the service under test is already up
(`manage.py runserver 8004`, the docker stack, or a deployment). Every test skips with a
reason when it is not answering, and the token tests skip without the credentials.

| Variable | What |
|---|---|
| `E2E_BASE_URL` | default `http://127.0.0.1:8004` (not `localhost`: Windows resolves it to `::1` first and every request stalls ~2 s); a Render URL for a run against a deployment |
| `E2E_WEB_ORIGIN` | the minty-web origin the CORS assertions use, default `http://localhost:3002` |
| `E2E_PAYMENTS_ORIGIN` | the payment-request UI's origin (the notice's caller), default `http://localhost:3000` |
| `E2E_SUBSCRIPTIONS` | `0` when the service runs with `SUBSCRIPTION_ENABLED=0` (dark, the cutover state) |
| `E2E_JWT_SECRET` | the `SECRET_KEY` shared with Minty; the tests mint the same token Flask does |
| `E2E_MINTY_USER` / `E2E_MINTY_ENTITY` | the identity `Minty/scripts/e2e_seed.py --print` creates |

Never commit any of these values.

## What it pins

| Mode | Checks |
|---|---|
| both | `/healthz` 200 · `/api/openapi.json` readable · the CORS preflight from minty-web succeeds (with `x-entity-id`) |
| dark | the portal, the module page, the notice and an onboarding route answer `404 {"error":"not_found"}` **with** `Access-Control-Allow-Origin`; a valid token changes nothing |
| live | no token → `401 {"error":"Unauthorized"}`; a forged token → 401; a Flask-shaped token gets the portal's table (200, `entities` + `total`, CORS); the module page refuses an unscoped token without `X-Entity-Id` and answers its page model with it (`entity_id`, `cards`, `can_manage_modules`, `viewer`); the notice answers a member with `items` and `settings_path`, CORS for the payment UI's origin |

These are the checks Part 2 step 7's runbook runs after each deploy. The unit suite
(`pytest`, which reads `testpaths = billing`) never collects this folder; the two share the
settings module but nothing else.

## Later (Part 2 steps 4–6)

The live journeys — start a card-free trial → Flask's gate opens → `manage.py subscriptions
run-daily --mode full` past `trial_end` → module off → restart quote → cancel — are browser
journeys and live in `minty-web/features/subscription/e2e/`; this folder stays the API's
own contract check.
