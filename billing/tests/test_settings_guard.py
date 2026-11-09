"""config/settings refuses to boot with the placeholder SECRET_KEY outside development.

Regression test for the 2026-09-14 production incident: the service ran with no
SECRET_KEY set, fell back to the in-repo placeholder, and 401'd every authenticated call.
"""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

#: The two senders this service requires (owner, 2026-11-05: no fallback, no MAIL_FROM).
SENDERS = {"SUBSCRIPTION_FROM": "subscription@minty.example", "ONBOARDING_FROM": "onboarding@minty.example"}


def _load_settings(monkeypatch, env, *, senders=SENDERS):
    for k in ("SECRET_KEY", "APP_ENV", "ACCOUNTS_API_URL", "ACCOUNTS_API_PUBLIC_URL", "MAIL_FROM", "SUBSCRIPTION_EMAIL", "ONBOARDING_EMAIL", *SENDERS):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**senders, **env}.items():
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


#: What production needs besides the senders (ACCOUNTS_API_URL has no default there).
PROD = {"APP_ENV": "production", "SECRET_KEY": "x" * 64, "ACCOUNTS_API_URL": "https://accounts-api.example"}


def test_a_real_key_boots_in_production(monkeypatch):
    mod = _load_settings(monkeypatch, PROD)
    assert mod.SECRET_KEY == "x" * 64
    assert mod.DEBUG is False


def test_accounts_api_url_is_required_outside_development(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="ACCOUNTS_API_URL is required"):
        _load_settings(monkeypatch, {**PROD, "ACCOUNTS_API_URL": ""})


def test_accounts_api_url_defaults_to_local_accounts_api_in_development(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "development"})
    assert mod.ACCOUNTS_API_URL == mod.ACCOUNTS_API_PUBLIC_URL == "http://localhost:8002"


def test_urls_and_senders_come_from_the_canonical_names(monkeypatch):
    for k in ("ACCOUNTS_API_PUBLIC_URL", "CORS_ALLOWED_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    mod = _load_settings(
        monkeypatch,
        {
            "APP_ENV": "development",
            "ACCOUNTS_API_URL": "http://minty:8010/",
            "MINTY_WEB_URL": "https://hub.example/",
            "PAYMENT_REQUEST_WEB_URL": "https://pay.example",
            "PETTY_CASH_WEB_URL": "https://pettycash.example/",
            "SMTP_URL": "smtps://u:p@smtp.example",
            "DATABASE_URL": "postgres://u@db/minty?schema=pettycash_alt",
        },
    )
    assert mod.ACCOUNTS_API_URL == "http://minty:8010"
    assert mod.ACCOUNTS_API_PUBLIC_URL == "http://minty:8010"
    assert mod.CORS_ALLOWED_ORIGINS == [
        "https://hub.example",
        "https://pay.example",
        "https://pettycash.example",
        "http://minty:8010",
    ]
    assert mod.SUBSCRIPTION_FROM == mod.DEFAULT_FROM_EMAIL == "subscription@minty.example"
    assert mod.ONBOARDING_FROM == "onboarding@minty.example"
    assert (mod.EMAIL_HOST, mod.EMAIL_PORT, mod.EMAIL_USE_SSL) == ("smtp.example", 465, True)
    assert mod.DB_SCHEMA == "pettycash_alt"
    assert mod.DATABASES["default"]["NAME"] == "minty"



def test_preview_origins_are_off_unless_set(monkeypatch):
    monkeypatch.delenv("PREVIEW_ORIGIN_REGEX", raising=False)
    assert _load_settings(monkeypatch, {"APP_ENV": "development"}).CORS_ALLOWED_ORIGIN_REGEXES == []
    regex = r"^https://pr-[a-z0-9-]+-(?:my|payment-request)\.dailyminty\.com$"
    mod = _load_settings(monkeypatch, {**PROD, "PREVIEW_ORIGIN_REGEX": regex})
    assert mod.CORS_ALLOWED_ORIGIN_REGEXES == [regex]


def test_a_loose_preview_regex_refuses_to_boot(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="PREVIEW_ORIGIN_REGEX"):
        _load_settings(monkeypatch, {"APP_ENV": "development", "PREVIEW_ORIGIN_REGEX": ".*"})

@pytest.fixture(autouse=True)
def _restore_settings_module():
    yield
    # Put the test settings module back for the rest of the suite.
    sys.modules.pop("config.settings", None)
    importlib.import_module("config.settings_test")


def test_a_mail_redirect_must_be_an_email_address(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="MAIL_REDIRECT_TO"):
        _load_settings(monkeypatch, {"APP_ENV": "development", "MAIL_REDIRECT_TO": "not-an-address"})


def test_a_mail_redirect_is_read_and_trimmed(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "development", "MAIL_REDIRECT_TO": " tester@minty.test "})
    assert mod.MAIL_REDIRECT_TO == "tester@minty.test"


def test_no_mail_redirect_by_default(monkeypatch):
    monkeypatch.delenv("MAIL_REDIRECT_TO", raising=False)
    assert _load_settings(monkeypatch, {"APP_ENV": "development"}).MAIL_REDIRECT_TO == ""


@pytest.mark.parametrize("missing", sorted(SENDERS))
def test_each_sender_is_required(monkeypatch, missing):
    """No fallback (owner, 2026-11-05): a missing sender stops the service at start, rather
    than mail going out from another address or failing one notice at a time."""
    senders = {k: v for k, v in SENDERS.items() if k != missing}
    with pytest.raises(ImproperlyConfigured, match=missing):
        _load_settings(monkeypatch, {"APP_ENV": "development"}, senders=senders)


def test_the_old_sender_names_are_not_read(monkeypatch):
    """MAIL_FROM and SUBSCRIPTION_EMAIL set nothing: a host still carrying only them fails."""
    with pytest.raises(ImproperlyConfigured, match="SUBSCRIPTION_FROM"):
        _load_settings(
            monkeypatch,
            {"APP_ENV": "development", "MAIL_FROM": "a@minty.example", "SUBSCRIPTION_EMAIL": "b@minty.example"},
            senders={"ONBOARDING_FROM": "onboarding@minty.example"},
        )


def test_the_old_onboarding_name_is_not_read(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="ONBOARDING_FROM"):
        _load_settings(
            monkeypatch,
            {"APP_ENV": "development", "ONBOARDING_EMAIL": "c@minty.example"},
            senders={"SUBSCRIPTION_FROM": "subscription@minty.example"},
        )
