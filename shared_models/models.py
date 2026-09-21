"""Django mirrors of tables the Flask app's Alembic migrations own.

EVERY MODEL HERE IS ``managed = False`` AND THIS REPO SHIPS NO MIGRATIONS.

Alembic in Minty is the owner-of-record for all of ``pettycashv3`` (the schema name is a
setting, ``config.settings.DB_SCHEMA``). A new column means a revision there first, then a
hand-edit here - and a plan amendment, because Part 2 promises the cutover schema gets no
DDL. Minty's ``docs/schema/generators/audit_models.py`` diffs this file against a live
build of ``01_schema_rebased.sql`` and must report 0 findings for this repo, as it does
for billing-backend and onboarding-backend.

WHAT THIS SERVICE MAY WRITE

Writable - the subscription domain, thirteen tables, this service the ONLY writer once
subscriptions are live: ``billing_plan``, ``billing_policy``, ``payer_billing_group``,
``billing_account_payment_method``, ``entity_billing_group``, ``entity_billing_consent``,
``entity_module_subscription``, ``user_stripe_customer``, ``subscription_invoice``,
``subscription_invoice_line``, ``subscription_transfer``, ``subscription_audit_log``,
``subscription_email_log``. Flask keeps its SQLAlchemy models of them for Alembic and reads
five things through ``blueprints/subscription/services/store_ro.py``; it writes none.

Write-restricted - ``entity_function_map``: this service writes ``is_enabled``,
``enabled_at`` and ``disabled_at`` - the projection of subscription state that Flask's
per-request module gate reads (``blueprints/entity/routes/modules.py::_is_module_enabled``)
- and nothing else on the row (never ``created_by``, never ``settings_json``). The writer is
``billing/services/entity_modules.py``, the Django copy of Flask's
``entity/services/modules._write_pairs``, and its rows are byte-identical to Flask's. The
access SWEEP exempts companies still in the wizard (``entities.status = onboarding``); the
writer itself has no such check, exactly like Flask's. While subscriptions are dark this
service writes the table not at all: Flask's toggle, the wizard's step 2 and ``flask modules
set`` are the writers then.

Read-only: ``user``, ``user_entity``, ``entities``, ``entity_function``, ``country_info``,
``currency_info``, ``invitation``. Identity and the company are Flask's until Part 3.

IDS ARE STRINGS

Every uuid column is a ``MintyUUIDField`` (``shared_models/fields.py``): the database type
is ``uuid``, the Python value is the hyphenated lowercase ``str`` - which is what Flask's
``MintyUuid(as_uuid=False)`` gives and what the whole engine compares against. A ForeignKey
resolves its converters through the TARGET field, so the read-only mirrors' primary keys are
``MintyUUIDField`` too; ``row.payer_user_id`` is a ``str`` on every backend. A value assigned
in memory is not converted (``obj.id = uuid4()`` stays a ``UUID`` until read back), so code
and fixtures pass ``str(uuid.uuid4())``; the domain tables' primary keys default to it.

DEFAULTS FLASK APPLIES IN PYTHON

SQLAlchemy distinguishes ``default=`` (applied in PYTHON on insert) from ``server_default=``
(a real DDL default). The schema carries the server defaults (``db_default=Now()`` on the
stamps, ``gen_random_uuid()`` on ids); the Python-side ones Flask relies on are declared here
so a Django insert matches a Flask insert: every domain primary key (``new_id``),
``BillingPolicy.id = 1``, ``BillingPlan.interval_months = 1`` / ``is_active = True``,
``SubscriptionEmailLog.status = "failed"``, ``SubscriptionTransfer.status = "pending"``,
``EntityFunction.description = ""`` / ``display_order = 999``. And SQLAlchemy's
``onupdate=func.now()`` on ``updated_at`` (client-side, fired on ORM saves AND bulk updates)
is ``UpdatedAtMixin``: ``save()`` sets ``updated_at`` to the database's ``now()`` on every
update, and every bulk ``.update()`` in ``billing/services`` passes ``updated_at=Now()``.

THE PARTIAL UNIQUE INDEXES

``uq_bapm_one_default`` (one default card per account) and ``idx_st_entity_open`` (one open
transfer offer per company) are partial unique indexes in the schema and are load-bearing:
the engine relies on the second raising ``IntegrityError``. They are declared as conditional
``UniqueConstraint``s here so the SQLite test database - built FROM these models - enforces
them too; with ``managed = False`` nothing here ever reaches production DDL.
"""

from django.db import models
from django.db.models import Q
from django.db.models.expressions import Combinable
from django.db.models.functions import Now

from shared_models.enums import (
    AuditOutcome,
    EntityRole,
    EntityStatus,
    ExtensionState,
    InvitationStatus,
    ModuleCode,
    SubscriptionPhase,
    SystemRole,
    TransferStatus,
)
from shared_models.fields import CharNField, MintyUUIDField, PgEnumField, new_id


class UpdatedAtMixin(models.Model):
    """``updated_at`` that the DATABASE moves on every update.

    In ``01_schema_rebased.sql`` section 3 these tables carry ``trg_<table>_updated``, a
    BEFORE UPDATE trigger that sets ``NEW.updated_at = now()`` unconditionally - whatever the
    application wrote, including an explicit stamp. SQLAlchemy's ``onupdate=func.now()`` was
    redundant with it on Postgres. This mixin is that trigger's mirror for the SQLite test
    database, which has no triggers: ``save()`` on an update writes ``Now()`` over anything the
    caller set (as the trigger will in production) and refreshes the attribute from the row,
    so callers never see the expression object and the in-memory value is the database's.

    Two consequences worth knowing. ``entity_modules._write_pairs`` stamps ``updated_at``
    with the process clock exactly as Flask's does - and on Postgres neither writer's stamp
    lands on an UPDATE; only the INSERT stamps are the application's. And inside a Postgres
    test transaction ``now()`` is the transaction START, so a test may assert that the stamp
    moved, never that it moved forward. Bulk ``.update()`` calls pass ``updated_at=Now()``
    themselves (the ORM does not route them through ``save()``).
    """

    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self.updated_at = Now()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "updated_at" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "updated_at"]
        super().save(*args, **kwargs)
        if isinstance(self.updated_at, Combinable):
            self.refresh_from_db(fields=["updated_at"])


# ---------------------------------------------------------------------------
# People and companies - read-only
# ---------------------------------------------------------------------------


class User(models.Model):
    """Read-only mirror of pettycashv3.user, managed by the Flask app.

    This service never writes a user: Flask owns registration, the email-OTP login and the
    handoff token. This exists so a verified JWT's ``user_id`` can be resolved to a real
    person - the payer - and so a notice can name and reach them (``notify.recipient_for``
    reads ``email``, falling back to ``xero_email``).

    No Xero token columns and no ``signed_in_at`` / ``last_seen_at``: nothing here may read
    a Xero token (Flask is the sole refresher) and presence is Flask's. A column that must
    not be read is better absent than present.
    """

    id = MintyUUIDField(primary_key=True)
    email = models.CharField(max_length=254, unique=True, null=True, blank=True)
    # Present so a test-mode insert satisfies the NOT NULL; never read here (Flask checks it).
    password = models.CharField(max_length=255)
    first_name = models.CharField(max_length=150, default="")
    last_name = models.CharField(max_length=150, default="")
    username = models.CharField(max_length=150, unique=True)
    system_role = PgEnumField("system_role", choices=SystemRole.choices, default=SystemRole.NORMAL)
    is_active = models.BooleanField(default=True)
    approved = models.BooleanField(default=False)
    # The address Xero knows the person by; the notification fallback when ``email`` is empty.
    xero_email = models.CharField(max_length=100, null=True, blank=True)
    # NOT NULL DEFAULT now() in the schema; db_default lets an insert leave them to Postgres.
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "user"

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"


class Entity(models.Model):
    """Read-only mirror of pettycashv3.entities. Flask and onboarding-backend write it.

    The engine reads ``name`` (invoice lines), ``status`` (an entity still ``onboarding`` is
    exempt from the access sweep), ``currency_id`` and ``country_code`` (the plan currency
    a company is quoted in). It writes none of them.
    """

    id = MintyUUIDField(primary_key=True)
    name = models.CharField(max_length=100)
    # FKs into the registries, as plain columns: country_code is the ISO alpha-2
    # country_info PK; currency_id is a uuid into currency_info(id).
    country_code = CharNField(max_length=2, null=True, blank=True)
    currency_id = MintyUUIDField(null=True, blank=True)
    contact_phone = models.CharField(max_length=36, null=True, blank=True)
    business_email = models.CharField(max_length=100, null=True, blank=True)
    xero_org_id = models.CharField(max_length=36, null=True, blank=True)
    xero_tenant_name = models.CharField(max_length=255, null=True, blank=True)
    currency_format = models.CharField(max_length=30, null=True, blank=True)
    timezone = models.CharField(max_length=30, null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    status = PgEnumField("entity_status", choices=EntityStatus.choices, default=EntityStatus.ONBOARDING)
    onboarding_saved_step = models.IntegerField(null=True, blank=True)
    financial_year_end_day = models.SmallIntegerField(null=True, blank=True)
    financial_year_end_month = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())
    last_connected_at = models.DateTimeField(null=True, blank=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    last_accessed_by_user_id = MintyUUIDField(null=True, blank=True)
    connected_by_user_id = MintyUUIDField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "entities"

    def __str__(self):
        return self.name


class UserEntity(models.Model):
    """Read-only mirror of pettycashv3.user_entity - membership and the role within it.

    COMPOSITE PRIMARY KEY, modelled as one (Django 5.2 ``CompositePrimaryKey``), as
    onboarding-backend does: a user holds a row per company. ``core.auth`` reads it to
    resolve the caller's role on the entity a request names; ``core.policy`` for
    ``MODULE_VIEW`` / ``MODULE_MANAGE``.
    """

    pk = models.CompositePrimaryKey("user_id", "entity_id")
    user_id = MintyUUIDField()
    entity_id = MintyUUIDField()
    role = PgEnumField("entity_role", choices=EntityRole.choices)
    approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(db_default=Now())
    joined_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "user_entity"

    def __str__(self):
        return f"{self.user_id}@{self.entity_id} ({self.role})"


class Invitation(models.Model):
    """Read-only mirror of pettycashv3.invitation.

    The portal's ``invite-admin`` does not write this: it forwards to Flask
    (``core.flask_client``), which owns the row, the email and the accept link. Mirrored so
    ``subscriber-options`` can list the pending invitations of a company the payer pays for.
    """

    id = MintyUUIDField(primary_key=True)
    entity_id = MintyUUIDField()
    email = models.CharField(max_length=150)
    role = PgEnumField("entity_role", choices=EntityRole.choices)
    first_name = models.CharField(max_length=100, null=True, blank=True)
    last_name = models.CharField(max_length=100, null=True, blank=True)
    token = models.CharField(max_length=64, unique=True)
    status = PgEnumField("invitation_status", choices=InvitationStatus.choices, default=InvitationStatus.PENDING)
    invited_by = MintyUUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "invitation"

    def __str__(self):
        return f"invite {self.email} -> {self.entity_id} ({self.status})"


# ---------------------------------------------------------------------------
# Reference registries - read-only, seeded by Flask
# ---------------------------------------------------------------------------


class CountryInfo(models.Model):
    """Read-only. ISO 3166-1 alpha-2 ``country_code`` is the primary key. Here so the
    ``entities.country_code`` FK can be satisfied in tests and a company's plan currency
    resolved from its country."""

    country_code = CharNField(max_length=2, primary_key=True)
    alpha3_code = CharNField(max_length=3)
    country_name_en = models.CharField(max_length=100)
    currency_id = MintyUUIDField(null=True, blank=True)
    phone_code = models.CharField(max_length=10, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=999)

    class Meta:
        managed = False
        db_table = "country_info"

    def __str__(self):
        return f"{self.country_code} {self.country_name_en}"


class CurrencyInfo(models.Model):
    """Read-only. ``id`` is a uuid PK; ``currency_code`` is the unique ISO 4217 code that
    ``billing_plan.currency``, ``user_stripe_customer.currency`` and
    ``subscription_invoice.currency`` reference. ``decimal_places`` is what money formatting
    resolves against - never a hardcoded 100."""

    id = MintyUUIDField(primary_key=True)
    currency_code = CharNField(max_length=3, unique=True)
    currency_name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=10, default="")
    decimal_places = models.SmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "currency_info"

    def __str__(self):
        return f"{self.currency_code} ({self.currency_name})"


# ---------------------------------------------------------------------------
# Module catalog and gate
# ---------------------------------------------------------------------------


class EntityFunction(models.Model):
    """Read-only. The module catalog: which modules exist at all.

    ``is_active`` says whether a module is OFFERED, never who may use it. Flask deleted a
    fallback that read it as permission, because falling back to it hands a module to every
    entity that never subscribed.
    """

    id = MintyUUIDField(primary_key=True)
    function_code = PgEnumField("module_code", choices=ModuleCode.choices, unique=True)
    function_name = models.CharField(max_length=150)
    description = models.TextField(default="", db_default="")
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=999, db_default=999)

    class Meta:
        managed = False
        db_table = "entity_function"

    def __str__(self):
        return self.function_code


class EntityFunctionMap(UpdatedAtMixin):
    """Per-entity module on/off - the projection Flask's module gate reads.

    THE ONE WRITE OUTSIDE THE DOMAIN. When subscriptions are live this service is the
    writer of ``is_enabled`` / ``enabled_at`` / ``disabled_at`` (a trial starting, a
    renewal failing past its window, a cancellation running out - each ends in a row here),
    through ``billing/services/entity_modules.py`` only; the stamps and ``created_by`` are
    written exactly as Flask's ``_write_pairs`` writes them. Dark, it does not write the table.

    The table is in ``01``'s updated_at-trigger list, hence the mixin (see it for what that
    means for the explicit stamp on an UPDATE).
    """

    # Keyed by (entity_id, entity_function_id) - the schema has no surrogate id.
    pk = models.CompositePrimaryKey("entity_id", "entity_function_id")
    entity_id = MintyUUIDField(db_index=True)
    entity_function_id = MintyUUIDField()
    # DANGER: the DATABASE default for this column is `true`. A row inserted without naming
    # is_enabled GRANTS the module, so every write sets it explicitly, and the model default
    # is the safer one on purpose: a caller that forgets the keyword fails closed.
    is_enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    # The person who first wrote the row (uuid FK to user); NULL for a job or the CLI.
    created_by = MintyUUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_function_map"

    def __str__(self):
        return f"{self.entity_id} -> {self.entity_function_id} ({self.is_enabled})"


# ---------------------------------------------------------------------------
# The subscription domain - thirteen tables, this service the writer when live.
# Column order and types follow section K of 01_schema_rebased.sql; amounts are
# integer minor units ("cents"); every stamp is TIMESTAMPTZ (USE_TZ = True). The
# ForeignKeys mirror the schema's constraints (so the SQLite test database enforces
# them too); each FK is named so its attname is the column name Flask's code reads
# (``payer_user`` -> ``payer_user_id``).
# ---------------------------------------------------------------------------


class BillingPlan(UpdatedAtMixin):
    """The price catalog. ``code`` is a module code or the bundle key ``BILL+PETTY_CASH``
    (so VARCHAR, not the ``module_code`` enum); ``amount`` is in minor units of
    ``currency``. Written by ``manage.py plans`` only."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    code = models.CharField(max_length=100, unique=True)
    display_name = models.CharField(max_length=255)
    amount = models.IntegerField()
    currency = CharNField(max_length=3)
    interval_months = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "billing_plan"

    def __str__(self):
        return f"{self.code} {self.amount} {self.currency}"


class BillingPolicy(UpdatedAtMixin):
    """The singleton (``CHECK (id = 1)``) of tunable windows: trial length, the access
    kept after a paid cancellation, the past-due window and the dunning retry offsets
    (``"1,2,...,13"`` - retries 1..13 by choice). The engine's ``policy`` module reads it
    once per scope."""

    id = models.IntegerField(primary_key=True, default=1)
    trial_days = models.IntegerField(default=30)
    paid_cancel_access_days = models.IntegerField(default=30)
    past_due_window_days = models.IntegerField(default=15)
    retry_offsets_days = models.CharField(max_length=100, default="1,2,3,4,5,6,7,8,9,10,11,12,13")
    updated_by = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "billing_policy"


class PayerBillingGroup(UpdatedAtMixin):
    """A billing ACCOUNT: one payer, one default card, one cycle. ``stripe_payment_method_id``
    is the card the account charges; the account's other cards are
    ``BillingAccountPaymentMethod`` rows. ``paid_through`` / ``dunning_*`` are per account
    because one invoice is raised per account."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    payer_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="payer_user_id", related_name="billing_groups"
    )
    stripe_payment_method_id = models.CharField(max_length=255)
    billing_email = models.CharField(max_length=255, null=True, blank=True)
    billing_company = models.CharField(max_length=255, null=True, blank=True)
    paid_through = models.DateTimeField(null=True, blank=True)
    dunning_started_at = models.DateTimeField(null=True, blank=True)
    dunning_attempts = models.IntegerField(default=0)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "payer_billing_group"


class BillingAccountPaymentMethod(UpdatedAtMixin):
    """The cards on a billing account; ``is_default`` marks the one the account charges
    (kept in step with ``PayerBillingGroup.stripe_payment_method_id``). Cascades with the
    account - it is the account's list, not a fact of its own."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    billing_group = models.ForeignKey(
        PayerBillingGroup, on_delete=models.CASCADE, db_column="billing_group_id",
        related_name="payment_methods",
    )
    stripe_payment_method_id = models.CharField(max_length=255)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "billing_account_payment_method"
        unique_together = (("billing_group", "stripe_payment_method_id"),)
        constraints = [
            # One default card per account - partial, so every non-default row is free.
            models.UniqueConstraint(
                fields=["billing_group"], condition=Q(is_default=True), name="uq_bapm_one_default"
            ),
        ]


class EntityBillingGroup(UpdatedAtMixin):
    """Which billing account pays for a company. ``UNIQUE (entity_id, payer_user_id)`` is
    the one-payer-per-entity rule; ``source`` records how the link was made (``capture``,
    ``chosen``, ``backfill``, ``confirmed`` - Flask's vocabulary, VARCHAR by choice)."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity = models.ForeignKey(
        Entity, on_delete=models.CASCADE, db_column="entity_id", related_name="billing_groups"
    )
    payer_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="payer_user_id", related_name="+"
    )
    billing_group = models.ForeignKey(
        PayerBillingGroup, on_delete=models.DO_NOTHING, db_column="billing_group_id",
        related_name="entities",
    )
    source = models.CharField(max_length=20)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_billing_group"
        unique_together = (("entity", "payer_user"),)


class EntityBillingConsent(models.Model):
    """A member's consent to be billed for a company (``source`` = ``card`` or
    ``confirmed``; the consent-takeover flow reads and writes it). One row per member per
    company."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity = models.ForeignKey(
        Entity, on_delete=models.CASCADE, db_column="entity_id", related_name="billing_consents"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, db_column="user_id", related_name="billing_consents"
    )
    source = models.CharField(max_length=20)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_billing_consent"
        unique_together = (("entity", "user"),)


class EntityModuleSubscription(UpdatedAtMixin):
    """The subscription itself: one row per company per module, one payer across them
    all. ``phase`` is the life of it (``subscription_phase``); ``app_access_until`` is
    what the access sweep projects into ``entity_function_map.is_enabled``."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity = models.ForeignKey(
        Entity, on_delete=models.CASCADE, db_column="entity_id", related_name="module_subscriptions"
    )
    function_code = PgEnumField("module_code", choices=ModuleCode.choices)
    payer_user = models.ForeignKey(
        User, on_delete=models.CASCADE, db_column="payer_user_id", related_name="paid_subscriptions"
    )
    phase = PgEnumField("subscription_phase", choices=SubscriptionPhase.choices)
    app_access_until = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    first_billed_at = models.DateTimeField(null=True, blank=True)
    billed_through = models.DateTimeField(null=True, blank=True)
    extension_amount = models.IntegerField(null=True, blank=True)
    extension_state = PgEnumField(
        "extension_state", choices=ExtensionState.choices, null=True, blank=True
    )
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_module_subscription"
        unique_together = (("entity", "function_code"),)

    def __str__(self):
        return f"{self.entity_id}/{self.function_code} {self.phase} paid by {self.payer_user_id}"


class UserStripeCustomer(UpdatedAtMixin):
    """A person's Stripe customer, one per user. ``anchor_at`` is the billing anchor every
    renewal of theirs is aligned to (per payer, not per entity); ``currency`` the
    customer's Stripe currency, fixed once set."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, db_column="user_id", related_name="stripe_customer"
    )
    stripe_customer_id = models.CharField(max_length=255, unique=True)
    anchor_at = models.DateTimeField(null=True, blank=True)
    currency = CharNField(max_length=3, null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "user_stripe_customer"


class SubscriptionInvoice(UpdatedAtMixin):
    """An invoice raised on a billing account. ``status`` mirrors Stripe's vocabulary
    (VARCHAR on purpose - someone else's value set). ``idempotency_key`` is the DOUBLE-
    CHARGE GUARD: ``billing_gateway`` claims it BEFORE the charge and relies on the unique
    index (``idx_si_idempotency_key``) to refuse a second claim."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    payer_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="payer_user_id", related_name="subscription_invoices"
    )
    billing_group = models.ForeignKey(
        PayerBillingGroup, on_delete=models.SET_NULL, db_column="billing_group_id",
        null=True, blank=True, related_name="invoices",
    )
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    currency = CharNField(max_length=3)
    total = models.IntegerField(default=0)
    status = models.CharField(max_length=20)
    memo = models.CharField(max_length=500, null=True, blank=True)
    payment_method = models.CharField(max_length=100, null=True, blank=True)
    hosted_invoice_url = models.CharField(max_length=500, null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True, unique=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "subscription_invoice"


class SubscriptionInvoiceLine(models.Model):
    """One company's share of an invoice. ``kind`` is ``full`` / ``remaining`` / ``unused`` /
    ``credit`` (Flask's words); ``entity_name`` and ``product_name`` are captured at issue so
    a renamed company keeps its old name on old invoices. Ordered by ``created_at`` like
    Flask's ``lines`` relationship."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    invoice = models.ForeignKey(
        SubscriptionInvoice, on_delete=models.CASCADE, db_column="invoice_id", related_name="lines"
    )
    entity = models.ForeignKey(
        Entity, on_delete=models.DO_NOTHING, db_column="entity_id", related_name="+"
    )
    entity_name = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255)
    amount = models.IntegerField()
    kind = models.CharField(max_length=20, default="full")
    at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "subscription_invoice_line"
        ordering = ("created_at",)


class SubscriptionTransfer(models.Model):
    """A change-of-subscriber request: ``from_user`` offers a company's billing to
    ``to_user``, who accepts (and is charged the quoted amount - ``charge_key`` is the
    idempotency key of that charge) or declines before ``expires_at``. At most one OPEN
    offer per company (``idx_st_entity_open``)."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity = models.ForeignKey(
        Entity, on_delete=models.CASCADE, db_column="entity_id", related_name="subscription_transfers"
    )
    from_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="from_user_id", related_name="transfers_offered"
    )
    to_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="to_user_id", related_name="transfers_received"
    )
    status = PgEnumField("transfer_status", choices=TransferStatus.choices, default=TransferStatus.PENDING)
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    accepted_billed_through = models.DateTimeField(null=True, blank=True)
    accepted_anchor_at = models.DateTimeField(null=True, blank=True)
    quoted_amount = models.IntegerField(null=True, blank=True)
    quoted_currency = CharNField(max_length=3, null=True, blank=True)
    charge_attempt = models.IntegerField(default=0)
    charge_key = models.CharField(max_length=120, null=True, blank=True)
    charge_invoice_id = models.CharField(max_length=64, null=True, blank=True)
    note = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "subscription_transfer"
        constraints = [
            # One open offer per company - partial on the three live-claim states.
            models.UniqueConstraint(
                fields=["entity"],
                condition=Q(status__in=[
                    TransferStatus.PENDING, TransferStatus.CHARGING, TransferStatus.CHARGED
                ]),
                name="idx_st_entity_open",
            ),
        ]


class SubscriptionAuditLog(models.Model):
    """Every state change the engine makes, with the phase before and after and who
    did it (``actor_user`` NULL for the scheduler). Append-only; ``outcome`` says whether
    the action went through or was aborted by a rule."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity = models.ForeignKey(
        Entity, on_delete=models.CASCADE, db_column="entity_id", related_name="subscription_audit"
    )
    function_code = PgEnumField("module_code", choices=ModuleCode.choices)
    payer_user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, db_column="payer_user_id", related_name="+"
    )
    actor_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, db_column="actor_user_id",
        null=True, blank=True, related_name="+",
    )
    action = models.CharField(max_length=40)
    phase_before = PgEnumField(
        "subscription_phase", choices=SubscriptionPhase.choices, null=True, blank=True
    )
    phase_after = PgEnumField(
        "subscription_phase", choices=SubscriptionPhase.choices, null=True, blank=True
    )
    app_access_until = models.DateTimeField(null=True, blank=True)
    extension_amount = models.IntegerField(null=True, blank=True)
    extension_state = PgEnumField(
        "extension_state", choices=ExtensionState.choices, null=True, blank=True
    )
    outcome = PgEnumField("audit_outcome", choices=AuditOutcome.choices)
    cancel_reason = models.CharField(max_length=500, null=True, blank=True)
    note = models.CharField(max_length=500, null=True, blank=True)
    payer_before = MintyUUIDField(null=True, blank=True)
    payer_after = MintyUUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "subscription_audit_log"


class SubscriptionEmailLog(models.Model):
    """The dedup ledger of the ten notification emails: ``UNIQUE (event, dedupe_key)`` is
    what stops a re-run of the daily pass sending the same notice twice. ``status`` is
    ``failed`` until the send succeeds, then ``sent`` (Flask's ``STATUS_FAILED`` default);
    ``error`` the reason when it failed."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, db_column="user_id", related_name="subscription_emails"
    )
    event = models.CharField(max_length=40)
    dedupe_key = models.CharField(max_length=200)
    recipient = models.CharField(max_length=200, null=True, blank=True)
    status = models.CharField(max_length=20, default="failed")
    error = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "subscription_email_log"
        unique_together = (("event", "dedupe_key"),)
