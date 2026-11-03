"""The module price catalog, from Minty's own ``billing_plan`` table.

Replaces the half of ``stripe_state`` that read Stripe Products and Prices. The prices a
customer is quoted and the prices they are charged now come from the same place —
``billing_plan`` — which is the point: while the catalog lived in Stripe and the
arithmetic lived here, the two could disagree and nothing would notice.

SHAPES ARE DELIBERATELY UNCHANGED. ``PlanView`` and ``BundlePlanView`` keep the field
names their callers and templates already use, so this is a change of SOURCE rather than
a change of interface. The two Stripe-only fields are gone:

* ``stripe_price_id`` / ``stripe_product_id`` — there is no Stripe object to point at.
  ``id`` now returns the plan code, which is what a template needs it for (a stable key),
  and no caller ever sent it back to Stripe: the gateway builds invoices from amounts,
  never from a stored price id.

``billing_plan`` is keyed by the sorted SET of module codes — ``BILL``, ``PETTY_CASH``,
``BILL+PETTY_CASH`` (the plan words; the module code is PAYMENT_REQUEST, see
``billing.plan_code``). A single-module plan is one code; a bundle is two or more. That is
the whole distinction between the two views below.
"""
from __future__ import annotations

from dataclasses import dataclass

from billing.services import _context
from billing.services._log import logger
from billing.services.billing import plan_modules

# Per-request memo for ``available_plans``. Same shape and lifetime as ``policy.current``
# and ``money.decimal_places`` — cached on the request scope, so it is per-request and thread-safe.
#
# Building the list costs TWO queries (``EntityFunction.query.all()`` plus
# ``active_billing_plans``), and ``plan_for_module`` rebuilt the whole thing to answer for
# one code — inside a per-row loop in the trial-conversion job, among others. The catalog
# is a price list: it does not change while a request is in flight, and a daily pass runs
# against one snapshot of it by design rather than by accident.
_G_PLANS_KEY = "_subscription_available_plans"


@dataclass(frozen=True)
class PlanView:
    """One subscribable module and its price."""

    entity_function_id: str
    function_code: str
    display_name: str
    amount: int              # smallest currency unit
    currency_code: str
    billing_interval: str
    billing_interval_count: int
    is_available_for_subscription: bool = True

    @property
    def id(self) -> str:
        """Stable key for templates. Was the Stripe price id; now the plan code."""
        return self.function_code


@dataclass(frozen=True)
class BundlePlanView:
    """The multi-module plan: ONE price covering several modules.

    ``amount`` is the whole line price (400) — never split across ``function_codes``.
    There is no per-module share of a bundle, which is exactly why the catalog is keyed
    by the code SET rather than by module.
    """

    function_codes: tuple[str, ...]
    display_name: str
    amount: int
    currency_code: str
    billing_interval: str
    billing_interval_count: int

    @property
    def id(self) -> str:
        return "+".join(self.function_codes)

    def covers(self, codes) -> bool:
        """True if this bundle is exactly the set of modules in ``codes``."""
        return set(self.function_codes) == {
            str(c).strip().upper() for c in codes if str(c).strip()
        }


def _interval(plan) -> tuple[str, int]:
    """``billing_plan`` stores months; the views speak Stripe's interval vocabulary.

    Kept as a translation rather than a stored string so a future yearly plan is a number
    in the table, not a new column.
    """
    months = int(getattr(plan, "interval_months", 1) or 1)
    if months % 12 == 0:
        return "year", months // 12
    return "month", months


def _function_ids() -> dict[str, str]:
    """{FUNCTION_CODE: entity_function.id}. Empty if the catalog table is unreadable.

    Callers use this id to link a plan to the module gate. A missing row is not fatal —
    the code is the real identity — so this degrades to an empty string rather than
    dropping the plan and making a module unsellable.
    """
    try:
        from shared_models.models import EntityFunction

        return {
            f.function_code.upper(): str(f.id)
            for f in EntityFunction.objects.all()
            if f.function_code
        }
    except Exception:
        logger.exception("catalog: could not read entity_function; plans will lack ids")
        return {}


def _single_plans():
    from billing.services import store

    return [p for p in store.active_billing_plans() if "+" not in (p.code or "")]


def available_plans() -> list[PlanView]:
    """Every subscribable single-module plan, cheapest first.

    Bundles are excluded on purpose: a bundle is not something a customer picks off a
    list, it is what two modules cost together. ``bundle_plan`` answers that separately.

    Memoized for the request — see ``_G_PLANS_KEY``. The returned list is SHARED, so
    callers must not mutate it; every one of them either iterates it or builds a dict
    from it, and ``sorted`` below already hands back a fresh list each build.
    """
    if _context.active():
        cached = _context.get(_G_PLANS_KEY)
        if cached is not None:
            return cached

    plans = _build_available_plans()
    _context.set(_G_PLANS_KEY, plans)
    return plans


def _build_available_plans() -> list[PlanView]:
    """The uncached build. Split out so the memo above stays one readable branch."""
    ids = _function_ids()
    plans = []
    for plan in _single_plans():
        # the MODULE code (PAYMENT_REQUEST), whatever word the plan row uses (BILL)
        (code,) = plan_modules(plan.code) or ("",)
        interval, count = _interval(plan)
        plans.append(
            PlanView(
                entity_function_id=ids.get(code, ""),
                function_code=code,
                display_name=plan.display_name,
                amount=int(plan.amount),
                currency_code=(plan.currency or "").upper(),
                billing_interval=interval,
                billing_interval_count=count,
            )
        )
    return sorted(plans, key=lambda p: (p.amount, p.function_code))


def plan_for_module(function_code: str) -> PlanView | None:
    """The plan for one module code, or None if it is not sellable."""
    if not function_code:
        return None
    wanted = str(function_code).strip().upper()
    return next((p for p in available_plans() if p.function_code == wanted), None)


def bundle_plan() -> BundlePlanView | None:
    """The multi-module plan, or None if the catalog has none.

    Takes the LARGEST multi-code plan, so a future three-module bundle wins over a
    two-module one rather than depending on row order.
    """
    from billing.services import store

    multi = [p for p in store.active_billing_plans() if "+" in (p.code or "")]
    if not multi:
        return None
    plan = max(multi, key=lambda p: len(p.code.split("+")))
    interval, count = _interval(plan)
    return BundlePlanView(
        function_codes=plan_modules(plan.code),
        display_name=plan.display_name,
        amount=int(plan.amount),
        currency_code=(plan.currency or "").upper(),
        billing_interval=interval,
        billing_interval_count=count,
    )
