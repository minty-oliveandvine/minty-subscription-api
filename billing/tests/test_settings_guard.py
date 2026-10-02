"""config/settings refuses to boot with the placeholder SECRET_KEY outside development.

Regression test for the 2026-09-14 production incident: the service ran with no
SECRET_KEY set, fell back to the in-repo placeholder, and 401'd every authenticated call.
"""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured


def _load_settings(monkeypatch, env):
    for k in ("SECRET_KEY", "APP_ENV"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # load_dotenv() must not put a real key back from .env for this test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    sys.modules.pop("config.settings", None)
    return importlib.import_module("config.settings")


def test_placeholder_key_is_refused_in_production(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {"APP_ENV": "production"})


def test_production_is_the_default(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {})


def test_an_unknown_app_env_is_production(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {"APP_ENV": "staging"})


def test_placeholder_key_is_tolerated_in_development(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "development"})
    assert mod.SECRET_KEY == "change-me-in-production"
    assert mod.DEBUG is True


def test_a_real_key_boots_in_production(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "production", "SECRET_KEY": "x" * 64})
    assert mod.SECRET_KEY == "x" * 64
    assert mod.DEBUG is False


def test_urls_and_senders_come_from_the_canonical_names(monkeypatch):
    for k in ("PETTY_CASH_PUBLIC_URL", "SUBSCRIPTION_EMAIL", "ONBOARDING_EMAIL", "CORS_ALLOWED_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    mod = _load_settings(
        monkeypatch,
        {
            "APP_ENV": "development",
            "PETTY_CASH_URL": "http://minty:8010/",
            "MINTY_WEB_URL": "https://hub.example/",
            "PAYMENT_REQUEST_WEB_URL": "https://pay.example",
            "MAIL_FROM": "noreply@minty.example",
            "SMTP_URL": "smtps://u:p@smtp.example",
            "DATABASE_URL": "postgres://u@db/minty?schema=pettycash_alt",
        },
    )
    assert mod.PETTY_CASH_URL == "http://minty:8010"
    assert mod.PETTY_CASH_PUBLIC_URL == "http://minty:8010"
    assert mod.CORS_ALLOWED_ORIGINS == ["https://hub.example", "https://pay.example", "http://minty:8010"]
    assert mod.DEFAULT_FROM_EMAIL == mod.SUBSCRIPTION_EMAIL == mod.ONBOARDING_EMAIL == "noreply@minty.example"
    assert (mod.EMAIL_HOST, mod.EMAIL_PORT, mod.EMAIL_USE_SSL) == ("smtp.example", 465, True)
    assert mod.DB_SCHEMA == "pettycash_alt"
    assert mod.DATABASES["default"]["NAME"] == "minty"


@pytest.fixture(autouse=True)
def _restore_settings_module():
    yield
    # Put the test settings module back for the rest of the suite.
    sys.modules.pop("config.settings", None)
    importlib.import_module("config.settings_test")
