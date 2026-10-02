"""config/smtpurl.py: SMTP_URL -> the EMAIL_* settings."""

import pytest

from config.smtpurl import CONSOLE_BACKEND, SMTP_BACKEND, parse_smtp_url


@pytest.mark.parametrize("url", [None, ""])
def test_unset_is_the_console_backend(url):
    cfg = parse_smtp_url(url)
    assert cfg["EMAIL_BACKEND"] == CONSOLE_BACKEND
    assert cfg["EMAIL_HOST"] == ""
    assert cfg["EMAIL_TIMEOUT"] == 10.0


def test_smtp_is_starttls_on_587():
    cfg = parse_smtp_url("smtp://user:secret@smtp-relay.example")
    assert cfg == {
        "EMAIL_BACKEND": SMTP_BACKEND,
        "EMAIL_HOST": "smtp-relay.example",
        "EMAIL_PORT": 587,
        "EMAIL_HOST_USER": "user",
        "EMAIL_HOST_PASSWORD": "secret",
        "EMAIL_USE_TLS": True,
        "EMAIL_USE_SSL": False,
        "EMAIL_TIMEOUT": 10.0,
    }


def test_smtps_is_implicit_ssl_on_465():
    cfg = parse_smtp_url("smtps://user:secret@smtp.example")
    assert (cfg["EMAIL_PORT"], cfg["EMAIL_USE_SSL"], cfg["EMAIL_USE_TLS"]) == (465, True, False)


def test_port_timeout_and_tls_off():
    cfg = parse_smtp_url("smtp://localhost:1025?tls=0&timeout=3")
    assert (cfg["EMAIL_PORT"], cfg["EMAIL_USE_TLS"], cfg["EMAIL_TIMEOUT"]) == (1025, False, 3.0)
    assert cfg["EMAIL_HOST_USER"] == cfg["EMAIL_HOST_PASSWORD"] == ""


def test_credentials_are_percent_decoded():
    cfg = parse_smtp_url("smtp://me%40x.com:p%40ss%2F1@h:587")
    assert (cfg["EMAIL_HOST_USER"], cfg["EMAIL_HOST_PASSWORD"]) == ("me@x.com", "p@ss/1")


def test_another_scheme_is_refused():
    with pytest.raises(ValueError):
        parse_smtp_url("http://h")
