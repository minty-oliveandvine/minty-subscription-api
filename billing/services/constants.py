"""Shared subscription status vocabulary.

Kept dependency-free (no models / no Stripe imports) so both the live Stripe
layer and the optional cache model can import it without creating an import
cycle through ``models.db``.
"""

# --- App-tracked lifecycle phases (entity_module_subscription.phase) ----------
PHASE_TRIAL = "trial"
PHASE_ACTIVE = "active"
PHASE_PAST_DUE = "past_due"
PHASE_SCHEDULED_CANCEL = "scheduled_cancel"
PHASE_CANCELLED = "cancelled"
PHASE_EXPIRED = "expired"

# --- Cancel-extension lifecycle (entity_module_subscription.extension_state) ---
# The extension covers the access days AFTER the billing anchor, so it bills ON the
# anchor invoice: a PENDING invoice item that Stripe sweeps onto the payer's next
# invoice. It is only charged up-front when there is no next invoice to ride (the
# payer's last line).
#   pending  -> queued as a pending invoice item; undo = delete it (no money moved)
#   invoiced -> collected (swept onto the anchor invoice, or charged up-front)
#   deleted / credited / refunded -> terminal undo outcomes
EXT_PENDING = "pending"
EXT_INVOICED = "invoiced"

# --- Audit actions (subscription_audit_log.action) ----------------------------
AUDIT_CANCEL = "cancel"
AUDIT_UNCANCEL = "uncancel"
# The subscription ENDED, as opposed to being cancelled: the days a cancellation bought,
# or the grace a debt was allowed, finally ran out. Nobody clicks this one — it is the
# access sweep recording a date passing (see ``checkout.terminate_lapsed_module``).
AUDIT_TERMINATE = "terminate"
# The entity's bill changed hands. Unlike the three above, these are not about what an
# entity is subscribed to — they are about WHO PAYS for it, which is why the log carries
# ``payer_before`` / ``payer_after`` alongside them. One row per module code, because
# ``function_code`` is NOT NULL and the payer sits on every row of the entity.
AUDIT_TRANSFER_OFFERED = "transfer_offered"      # 16 chars
AUDIT_TRANSFER_ACCEPTED = "transfer_accepted"    # 17
AUDIT_TRANSFER_DECLINED = "transfer_declined"    # 17
AUDIT_TRANSFER_CANCELLED = "transfer_cancelled"  # 18
# All four fit the model's String(20). Counted rather than assumed, because the shipped
# column is VARCHAR(40) and the model is narrower — the model is the binding constraint.

# --- Subscriber transfer lifecycle (subscription_transfer.status) -------------
# ``charging`` and ``charged`` are the two NON-TERMINAL states: an accept that got
# part-way. They exist because accepting cannot be one transaction — the charge and the
# payer flip commit separately — so the row has to record how far it got, and the
# recovery paths resolve exactly these two.
TRANSFER_PENDING = "pending"
TRANSFER_CHARGING = "charging"
TRANSFER_CHARGED = "charged"
TRANSFER_ACCEPTED = "accepted"
TRANSFER_DECLINED = "declined"
TRANSFER_CANCELLED = "cancelled"
TRANSFER_EXPIRED = "expired"

#: Statuses holding a live claim on the entity — no second offer may open against it.
#: Covers the in-flight states, not just ``pending``, or a second accept could start
#: while the first is mid-charge and both would move the same pointer.
TRANSFER_OPEN_STATUSES = (TRANSFER_PENDING, TRANSFER_CHARGING, TRANSFER_CHARGED)

#: An accept that stopped part-way. The repair step's work list; normally empty.
TRANSFER_STRANDED_STATUSES = (TRANSFER_CHARGING, TRANSFER_CHARGED)

# --- Audit outcomes (subscription_audit_log.outcome) --------------------------
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_ABORTED = "aborted"
