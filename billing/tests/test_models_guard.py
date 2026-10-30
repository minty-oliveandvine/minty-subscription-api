"""The three rules of the mirrors, as tests.

1. This service ships no DDL: no ``migrations/`` directory, and minty-db's migrations are
   switched off here (``MIGRATION_MODULES``) - they run by hand, never from a service.
2. The models are minty-db's (since 2026-10-30), which has every table; the code imports only
   the fourteen subscription tables' models and the read-only rows the engine needs (``user``, ``user_entity``, ``entities``,
   ``entity_function``, ``entity_function_map``, ``country_info``, ``currency_info``,
   ``invitation``) - nothing else. A new table here is a plan amendment, not a commit.
3. ``stripe`` is imported in exactly one module (``billing/services/stripe_client.py``,
   Part 2 step 2) - the single Stripe writer, cross-cutting rule 9. Today: nowhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.apps import apps

ROOT = Path(__file__).resolve().parents[2]

SUBSCRIPTION_TABLES = {
    "billing_plan",
    "billing_policy",
    "payer_billing_group",
    "billing_account_payment_method",
    "entity_billing_group",
    "entity_billing_consent",
    "entity_module_subscription",
    "user_stripe_customer",
    "subscription_invoice",
    "subscription_invoice_line",
    "subscription_invoice_counter",  # invoice numbers, owner 2026-10-23
    "subscription_transfer",
    "subscription_audit_log",
    "subscription_email_log",
}
READ_ONLY_TABLES = {
    "user",
    "user_entity",
    "entities",
    "entity_function",
    "entity_function_map",
    "country_info",
    "currency_info",
    "invitation",
}


def _imported_models() -> set[str]:
    """Every minty_db model the service's code (not its tests) names."""
    names = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if ".venv" in rel or rel.startswith(("billing/tests/", "e2e/")):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom) and node.module == "minty_db.models":
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.Attribute) and ast.unparse(node.value) in ("minty_db.models", "models_db"):
                names.add(node.attr)
    return names


def test_no_migrations_directory_and_minty_db_never_migrates_here():
    from django.conf import settings

    assert not list(ROOT.glob("*/migrations")), "this service ships no DDL"
    assert settings.MIGRATION_MODULES.get("minty_db", "unset") is None


def test_the_code_uses_only_the_domain_tables_and_the_read_only_rows():
    allowed = {m.__name__ for m in apps.get_app_config("minty_db").get_models()
               if m._meta.db_table in SUBSCRIPTION_TABLES | READ_ONLY_TABLES}
    assert len(allowed) == len(SUBSCRIPTION_TABLES | READ_ONLY_TABLES), "a listed table has no minty_db model"
    assert _imported_models() <= allowed, sorted(_imported_models() - allowed)


def test_stripe_is_imported_in_one_module_at_most():
    hits = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if ".venv" in rel or rel.startswith(("billing/tests/", "e2e/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "stripe" or n.startswith("stripe.") for n in names):
                hits.append(rel)
    assert set(hits) <= {"billing/services/stripe_client.py"}, hits
