"""What a "retry the payment now" answers, in words - one set for every button that asks.

Two routes collect an outstanding invoice at the payer's request through the same engine call
(``dunning.retry_now``): the module page's ``retry-payment`` (a company's card) and the payer
portal's ``POST /api/me/invoices/{id}/retry`` (08-B's invoice row). They share these words, so
a customer is told the same thing whichever button they pressed.
"""

from __future__ import annotations

RETRY_MESSAGES = {
    "paid": "Payment received — your subscription is active again.",
    "no_card": "There's no card on file to charge. Add a payment method, then try again.",
    "gave_up": "This subscription is past its payment deadline and has been closed.",
    "nothing_owed": "Nothing is outstanding — your subscription is up to date.",
    # Deliberately not "nothing is outstanding": something is, and the customer can see it
    # sitting Unpaid on the Invoices tab. It is simply not this period's (``dunning.retry_now``).
    "older_debt_only": (
        "There's nothing due for the current period. An earlier unpaid invoice is "
        "still outstanding — contact us and we'll sort it out with you."
    ),
    # The row asked for one invoice and the rules would charge another: the page is older
    # than the account's state. Nothing was charged.
    "not_this_invoice": (
        "That invoice can't be retried from here any more. Refresh the page to see what's "
        "outstanding now."
    ),
    # The payment PROCESSOR failed, not the card: not a decline (no "check your card"), and
    # nothing was charged.
    "unavailable": (
        "We couldn't reach the payment provider. Nothing was charged — please try again "
        "shortly."
    ),
}


DECLINED = "That card was declined. Try a different payment method."
_GENERIC_DECLINE = "your card was declined"


def declined_message(reason: str | None) -> str:
    """A decline, in the processor's words where they add something: "insufficient funds" and
    "card expired" need different things from the customer.

    Stripe's generic "Your card was declined." adds nothing to our own sentence - prefixed, it
    read "That card was declined: Your card was declined." (the user, 2026-09-28: don't repeat
    it). So only what Stripe says AFTER that phrase is kept, and with nothing after it the
    customer gets the next step instead.
    """
    reason = (reason or "").strip()
    if reason.lower().startswith(_GENERIC_DECLINE):
        rest = reason[len(_GENERIC_DECLINE):].lstrip(" .:;,-")
        return f"That card was declined. {rest}" if rest else DECLINED
    return f"That card was declined: {reason}" if reason else DECLINED


def retry_answer(result: dict) -> dict:
    """``{"ok", "status", "message"}`` for a ``retry_now`` result."""
    status = result["status"]
    if status == "failed":
        message = declined_message(result.get("reason"))
    else:
        message = RETRY_MESSAGES.get(status, "Payment could not be completed.")
    return {"ok": status in ("paid", "nothing_owed"), "status": status, "message": message}
