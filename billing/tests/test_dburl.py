"""config/dburl.py: DATABASE_URL -> DATABASES["default"] and the schema."""

import pytest

from config.dburl import DEFAULT_DATABASE_URL, database_url, parse_database_url


def test_the_schema_is_popped_into_search_path():
    db, schema = parse_database_url("postgresql://u:p@db.example:6543/minty?schema=pettycash_alt")
    assert schema == "pettycash_alt"
    assert db == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "minty",
        "USER": "u",
        "PASSWORD": "p",
        "HOST": "db.example",
        "PORT": "6543",
        "OPTIONS": {"options": "-c search_path=pettycash_alt,public"},
    }


def test_no_schema_means_the_default_and_port_5432():
    db, schema = parse_database_url("postgresql://u@localhost/postgres")
    assert schema == "pettycashv3"
    assert db["OPTIONS"] == {"options": "-c search_path=pettycashv3,public"}
    assert db["PORT"] == "5432"
    assert db["PASSWORD"] == ""


def test_other_query_params_are_kept_for_the_driver():
    db, schema = parse_database_url(
        "postgresql://u:p@h/d?sslmode=require&schema=s1&connect_timeout=5"
    )
    assert schema == "s1"
    assert db["OPTIONS"] == {
        "sslmode": "require",
        "connect_timeout": "5",
        "options": "-c search_path=s1,public",
    }


def test_user_password_and_name_are_percent_decoded():
    db, _ = parse_database_url("postgresql://us%40er:p%40ss%3Aw%2Ford@h:5432/my%20db")
    assert (db["USER"], db["PASSWORD"], db["NAME"]) == ("us@er", "p@ss:w/ord", "my db")


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"])
def test_every_postgres_scheme_is_accepted(scheme):
    db, _ = parse_database_url(f"{scheme}://u:p@h/d")
    assert db["ENGINE"] == "django.db.backends.postgresql"
    assert db["HOST"] == "h"


def test_another_scheme_is_refused():
    with pytest.raises(ValueError):
        parse_database_url("mysql://u:p@h/d")


def test_database_url_reads_the_environment(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() == DEFAULT_DATABASE_URL == "postgresql://postgres@localhost:5432/postgres"
    monkeypatch.setenv("DATABASE_URL", "postgres://x@y/z")
    assert database_url() == "postgres://x@y/z"
