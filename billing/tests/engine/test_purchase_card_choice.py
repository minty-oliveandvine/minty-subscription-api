"""The card on the dialog that takes the money.

"Subscribe to Super Minty" is the one screen in the flow where the charge happens as the
dialog closes, and it used to be the only card surface with no say in the matter: a
read-only line reading "visa ending 5556" — a fourth way of naming a card, on the last
moment the choice is free to make.

Two things are pinned here, and only the second is cosmetic:

* NOMINATION BEFORE CHARGE. The purchase routes accept the card the dialog named and put
  the company on it before anything bills. The other order charges whatever the company
  was already on, which — on a dialog where the payer is looking at a different card — is
  a charge against a card they were not shown.
* One vocabulary. The preview's card string is built from the same helper the picker rows
  use, so the two cannot drift into "Visa •••• 5556" here and "visa ending 5556" there.
"""
from __future__ import annotations

import pytest

# --- the string -------------------------------------------------------------


@pytest.mark.parametrize(
    "brand, last4, expected",
    [
        ("visa", "5556", "Visa •••• 5556"),
        ("mastercard", "7068", "Mastercard •••• 7068"),
        ("amex", "0005", "Amex •••• 0005"),
        # A wallet Stripe hands back with no card object still has to be nameable.
        ("visa", None, "Visa"),
        (None, "4242", "Card •••• 4242"),
    ],
)
def test_the_preview_names_a_card_the_way_every_row_does(app, monkeypatch, brand, last4, expected):
    from billing.services import checkout

    monkeypatch.setattr(checkout.store, "card_for_entity", lambda eid: "pm_1")
    monkeypatch.setattr(
        checkout, "payment_method_display", lambda pm: {"brand": brand, "last4": last4}
    )

    assert checkout._preview_card_display("u1", "e1") == expected


def test_the_preview_never_says_ending(app, monkeypatch):
    """The old wording, gone for good — it is the one that made this a fourth vocabulary."""
    from billing.services import checkout

    monkeypatch.setattr(checkout.store, "card_for_entity", lambda eid: "pm_1")
    monkeypatch.setattr(
        checkout, "payment_method_display", lambda pm: {"brand": "visa", "last4": "5556"}
    )

    assert "ending" not in checkout._preview_card_display("u1", "e1")


def test_a_stripe_hiccup_does_not_stop_the_purchase(app, monkeypatch):
    """This only decorates a dialog. A card that cannot be read is a missing line, not a
    customer who cannot subscribe."""
    from billing.services import checkout

    def _boom(_eid):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(checkout.store, "card_for_entity", _boom)

    assert checkout._preview_card_display("u1", "e1") is None


# --- nomination before charge ------------------------------------------------


