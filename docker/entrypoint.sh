#!/usr/bin/env sh
# Container entrypoint for minty-subscription-api.
#
# This service is a TENANT of the schema Flask owns: settings.py pins search_path to
# the ``?schema=`` on DATABASE_URL (default pettycashv3, config/dburl.py), and shared_models maps tables Flask's Alembic
# migrations create. So wait for both the database and that schema rather than creating
# anything ourselves.
#
# NO `manage.py migrate` HERE, EVER. This service owns zero tables and ships zero
# migrations by design (Part 2 invariant: the cutover schema gets no DDL from anyone but
# Alembic). Running migrate would be a no-op at best and, if a migration ever appeared by
# accident, a schema fight at worst - and it would leave a django_migrations table in a
# schema that must not have one.
set -eu

python - <<'PY'
import os
import time

import psycopg

from config.dburl import database_url, parse_database_url

db, schema = parse_database_url(database_url())
# Every query parameter but search_path (sslmode, ...) goes to the driver as Django's does.
params = {k: v for k, v in db["OPTIONS"].items() if k != "options"}

# Seconds, not attempts: the schema arrives only once Flask has finished its own
# migrations, which on a cold volume takes a while.
deadline = time.monotonic() + int(os.environ.get("DB_WAIT_SECONDS", "180"))
last_error = None

while time.monotonic() < deadline:
    try:
        with psycopg.connect(
            host=db["HOST"],
            port=db["PORT"],
            dbname=db["NAME"],
            user=db["USER"],
            password=db["PASSWORD"],
            **{"connect_timeout": 5, **params},
        ) as conn:
            row = conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [schema]
            ).fetchone()
            if row:
                break
            last_error = f"schema {schema} does not exist yet"
    except Exception as exc:  # noqa: BLE001 - any connection failure is a retry
        last_error = exc
    time.sleep(2)
else:
    raise RuntimeError(f"Database/schema not ready: {last_error}")
PY

echo "Starting application command: $*"
exec "$@"
