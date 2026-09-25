"""The billing-account routes (08-A / 08-B / 08-C): the HTTP half of
``billing/tests/engine/test_portal_billing_accounts.py``.

The rules live in the engine tests. What is pinned HERE is what the routes add or could
lose on the way to the page:

* **Every route is the caller's own.** Each takes an account id (and some a company) from
  the browser; someone else's answers exactly like one that does not exist.
* **The refusals reach the page as sentences**, through the wallet's shell.
* **The confirm route checks an account BEFORE the card is attached** — the service only
  reaches the account fields after Stripe holds the card, and a refusal there would leave a
  card saved against no account.
* **The new query and body fields are carried through**, not dropped (``account`` on the
  invoices and on a removal).
"""

from __future__ import annotations

import pytest

from billing.tests.api.conftest import ORIGIN, bearer, post_json, preflight
from billing.tests.engine.test_billing_payment_methods import _card, wallet  # noqa: F401

pytestmark = pytest.mark.django_db

ACCOUNTS = "/api/me/billing/accounts"
CONFIRM = "/api/me/billing/payment-methods/confirm"
REMOVE = "/api/me/billing/payment-methods/remove"


def _open(user, pm_id, company="Acme Ltd"):
    from billing.services import store

    return store.create_billing_account(user.id, pm_id, billing_company=company)


def test_every_account_route_needs_a_token(client):
    assert client.get(ACCOUNTS).status_code == 401
    for action in ("update", "default-card", "move"):
        assert post_json(client, f"{ACCOUNTS}/{action}", {}).status_code == 401


def test_preflight_answers_without_a_token(client):
    response = preflight(client, f"{ACCOUNTS}/move")
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == ORIGIN


def test_the_accounts_are_the_tokens_own(client, user, other_user, wallet):  # noqa: F811
    mine = _open(user, "pm_mine")
    _open(other_user, "pm_theirs", company="Not Yours Ltd")
    wallet["methods"] = [_card("pm_mine")]

    response = client.get(ACCOUNTS, **bearer(user))

    assert response.status_code == 200
    body = response.json()
    assert [a["id"] for a in body["accounts"]] == [str(mine.id)]
    assert body["accounts"][0]["name"] == "Acme Ltd"
    assert "countries" not in body


def test_the_country_list_is_opt_in(client, user, countries, wallet):  # noqa: F811
    wallet["methods"] = []

    body = client.get(f"{ACCOUNTS}?countries=1", **bearer(user)).json()

    assert {"code": "HK", "name": "Hong Kong"} in body["countries"]
    assert body["publishable_key"] == "pk_test_123"


@pytest.mark.parametrize(
    ("action", "payload", "words"),
    [
        ("update", {"billing_company": "X"}, "No billing account was given."),
        ("default-card", {"payment_method": "pm_mine"}, "No billing account was given."),
        ("move", {"account": "a1"}, "No company was given."),
    ],
)
def test_a_missing_id_is_a_400_that_says_which(client, user, wallet, action, payload, words):  # noqa: F811
    wallet["methods"] = [_card("pm_mine")]

    response = post_json(client, f"{ACCOUNTS}/{action}", payload, **bearer(user))

    assert response.status_code == 400
    assert response.json()["error"] == words


@pytest.mark.parametrize("action", ["update", "default-card", "move"])
def test_someone_elses_account_is_not_found_on_every_write(
    client, user, other_user, entity, wallet, action  # noqa: F811
):
    from billing.services import store

    theirs = _open(other_user, "pm_theirs", company="Not Yours Ltd")
    mine = _open(user, "pm_mine")
    store.upsert_module_row(entity.id, "PETTY_CASH", user.id, phase="active")
    store.nominate_group_for_entity(entity.id, user.id, mine.id)
    wallet["methods"] = [_card("pm_mine")]
    payload = {
        "update": {"account": str(theirs.id), "billing_company": "Hijacked Ltd"},
        "default-card": {"account": str(theirs.id), "payment_method": "pm_mine"},
        "move": {"account": str(theirs.id), "entity": str(entity.id)},
    }[action]

    response = post_json(client, f"{ACCOUNTS}/{action}", payload, **bearer(user))

    assert response.status_code == 404
    assert response.json()["error"] == "That billing account couldn't be found."
    assert store.billing_group(theirs.id).billing_company == "Not Yours Ltd"


def test_a_refused_move_reaches_the_page_as_its_sentence(client, user, entity, wallet):  # noqa: F811
    from billing.services import store

    acme = _open(user, "pm_a")
    beta = _open(user, "pm_b", company="Beta Ltd")
    store.upsert_module_row(entity.id, "PETTY_CASH", user.id, phase="past_due")
    store.nominate_group_for_entity(entity.id, user.id, acme.id)
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]

    response = post_json(
        client, f"{ACCOUNTS}/move",
        {"entity": str(entity.id), "account": str(beta.id)}, **bearer(user),
    )

    assert response.status_code == 409
    assert response.json()["error"] == (
        "Payer Trading Co's last payment on Acme Ltd didn't go through. "
        "Settle it there first, then move the company."
    )


def test_a_move_answers_the_accounts_and_what_went_where(client, user, entity, wallet):  # noqa: F811
    from billing.services import store

    acme = _open(user, "pm_a")
    beta = _open(user, "pm_b", company="Beta Ltd")
    store.upsert_module_row(entity.id, "PETTY_CASH", user.id, phase="active")
    store.nominate_group_for_entity(entity.id, user.id, acme.id)
    wallet["methods"] = [_card("pm_a"), _card("pm_b", last4="1111")]

    response = post_json(
        client, f"{ACCOUNTS}/move",
        {"entity": str(entity.id), "account": str(beta.id)}, **bearer(user),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["moved"]["to_account"] == {"id": str(beta.id), "name": "Beta Ltd"}
    assert {a["id"]: len(a["companies"]) for a in body["accounts"]} == {
        str(acme.id): 0, str(beta.id): 1,
    }


# --- confirm: the account is checked before the card is attached ----------------------


@pytest.fixture
def confirm_spy(monkeypatch):
    """Records ``confirm_setup`` calls instead of reaching Stripe."""
    from billing.services import payment_methods

    calls = []

    def _spy(uid, setup_intent, **kwargs):
        calls.append((str(uid), setup_intent, kwargs))
        return {"has_account": True, "default_id": None, "methods": [], "total": 0}

    monkeypatch.setattr(payment_methods, "confirm_setup", _spy)
    return calls


def test_a_new_account_carries_its_name_through(client, user, confirm_spy):
    response = post_json(
        client, CONFIRM,
        {"setup_intent": "seti_1", "billing_company": " Vine Consulting ",
         "billing_email": "ap@vine.test"},
        **bearer(user),
    )

    assert response.status_code == 200
    (_uid, intent, kwargs), = confirm_spy
    assert intent == "seti_1"
    assert (kwargs["billing_company"], kwargs["billing_email"]) == ("Vine Consulting", "ap@vine.test")
    assert kwargs["billing_group_id"] is None


def test_a_new_account_without_both_fields_is_refused_before_stripe(client, user, confirm_spy):
    response = post_json(
        client, CONFIRM, {"setup_intent": "seti_1", "billing_company": "Vine Consulting"},
        **bearer(user),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "Enter the email address invoices should go to."
    assert confirm_spy == []


def test_a_card_for_someone_elses_account_is_refused_before_stripe(
    client, user, other_user, confirm_spy
):
    theirs = _open(other_user, "pm_theirs")

    response = post_json(
        client, CONFIRM, {"setup_intent": "seti_1", "billing_group_id": str(theirs.id)},
        **bearer(user),
    )

    assert response.status_code == 404
    assert confirm_spy == []


def test_a_card_for_the_callers_own_account_goes_through(client, user, confirm_spy):
    mine = _open(user, "pm_mine")

    response = post_json(
        client, CONFIRM, {"setup_intent": "seti_1", "billing_group_id": str(mine.id)},
        **bearer(user),
    )

    assert response.status_code == 200
    assert confirm_spy[0][2]["billing_group_id"] == str(mine.id)


# --- the fields the existing routes now carry -----------------------------------------


def test_the_invoices_carry_the_account_filter(client, user, monkeypatch):
    from billing.services import portal

    seen = {}

    def _spy(uid, **kwargs):
        seen.update(kwargs)
        return {"invoices": [], "entity_options": [], "entity_id": None,
                "account_id": kwargs.get("account_id"), "total": 0, "page": 1, "pages": 1,
                "per_page": 10}

    monkeypatch.setattr(portal, "build_payer_invoices", _spy)

    response = client.get("/api/me/invoices?account=a1&per_page=10", **bearer(user))

    assert response.status_code == 200
    assert seen["account_id"] == "a1"


def test_a_removal_carries_the_account_whose_page_asked(client, user, monkeypatch):
    from billing.services import payment_methods

    seen = {}

    def _spy(uid, pm, **kwargs):
        seen.update(kwargs, pm=pm)
        return {"has_account": True, "default_id": None, "methods": [], "total": 0}

    monkeypatch.setattr(payment_methods, "remove", _spy)

    response = post_json(
        client, REMOVE, {"payment_method": "pm_b", "account": "a1"}, **bearer(user)
    )

    assert response.status_code == 200
    assert seen == {"account_id": "a1", "pm": "pm_b"}


def test_a_surprise_is_the_shells_500_with_cors(client, user, monkeypatch):
    from billing.services import portal

    def _boom(*_a, **_k):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(portal, "build_billing_accounts", _boom)

    response = client.get(ACCOUNTS, HTTP_ORIGIN=ORIGIN, **bearer(user))

    assert response.status_code == 500
    assert "down" not in response.json()["error"]
    assert response["Access-Control-Allow-Origin"] == ORIGIN
