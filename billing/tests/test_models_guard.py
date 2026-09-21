"""The three rules of the mirrors, as tests.

1. Every model is ``managed = False`` and this repo has no ``migrations/`` directory: Alembic
   in Minty owns the DDL, and ``docker/entrypoint.sh`` runs no ``migrate``.
2. The thirteen subscription tables are all here, under their schema names, and so are the
   read-only rows the engine needs (``user``, ``user_entity``, ``entities``,
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


def _models():
    return [m for m in apps.get_models() if m._meta.app_label in ("shared_models", "billing")]


def test_every_model_is_unmanaged_in_production():
    # Under test SHARED_MODELS_MANAGED_FOR_TESTING may have flipped the runtime flag (SQLite
    # builds the tables from the models); the SOURCE must still say managed = False.
    source = (ROOT / "shared_models" / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name != "Meta"]
    for cls in classes:
        metas = [n for n in cls.body if isinstance(n, ast.ClassDef) and n.name == "Meta"]
        assert metas, f"{cls.name} has no Meta"
        meta = {
            n.targets[0].id: n.value.value
            for n in metas[0].body
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        }
        if meta.get("abstract") is True:
            continue  # a mixin, not a table
        assert meta.get("managed") is False, f"{cls.name} must be managed = False"


def test_no_migrations_directory_anywhere():
    assert not list(ROOT.glob("*/migrations")), "this service ships no DDL"


def test_the_tables_are_exactly_the_domain_plus_the_read_only_rows():
    tables = {m._meta.db_table for m in _models()}
    assert tables == SUBSCRIPTION_TABLES | READ_ONLY_TABLES


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
