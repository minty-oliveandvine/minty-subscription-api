"""config/settings refuses to boot with the placeholder SECRET_KEY outside DEBUG.

Regression test for the 2026-09-14 production incident: the service ran with no
SECRET_KEY set, fell back to the in-repo placeholder, and 401'd every authenticated call.
"""

import importlib
import sys

import pytest


def _load_settings(monkeypatch, env):
    for k in ("SECRET_KEY", "DEBUG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # load_dotenv() must not put a real key back from .env for this test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    sys.modules.pop("config.settings", None)
    return importlib.import_module("config.settings")


def test_placeholder_key_is_refused_when_debug_is_off(monkeypatch):
    with pytest.raises(RuntimeError, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {"DEBUG": "false"})


def test_placeholder_key_is_tolerated_in_debug(monkeypatch):
    mod = _load_settings(monkeypatch, {"DEBUG": "true"})
    assert mod.SECRET_KEY == "change-me-in-production"


def test_a_real_key_boots_with_debug_off(monkeypatch):
    mod = _load_settings(monkeypatch, {"DEBUG": "false", "SECRET_KEY": "x" * 64})
    assert mod.SECRET_KEY == "x" * 64


@pytest.fixture(autouse=True)
def _restore_settings_module():
    yield
    # Put the test settings module back for the rest of the suite.
    sys.modules.pop("config.settings", None)
    importlib.import_module("config.settings_test")
