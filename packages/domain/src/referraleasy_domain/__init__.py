"""ReferralEasy domain layer.

A typed client over the PostgreSQL control-state primitives (§7). The database owns the *rules* —
state transitions, ownership, approvals, command dedup and the policy table are enforced by
constraints, triggers and functions in `migrations/`. This package does not re-implement them; it
gives the gateway and the workers a typed way to invoke them, and translates their refusals into
exceptions the caller can act on.

Re-implementing a rule here would create a second source of truth that could drift from the SQL
that actually enforces it — so anything that must be true is enforced there, and tested there.

Modules
-------
``errors``    Typed exceptions for the SQLSTATE classes the migrations raise.
``approval``  Approval binding, including the canonical booking-payload hash (§9).
``policy``    The state -> agent/tool table (§4) as seen by the application.
``workflow``  Workflow claim / advance / release (§7), and the booking gate (§9).
``commands``  `command_id` dedup on replay (§7).
``outbox``    Notification delivery with a bounded number of attempts (§7).
``handoff``   The operator queue, and the suspension of automation while a human holds it (§11).

Two conventions worth knowing before reading them.

**Refusals are the database's.** Nothing in this package decides whether an action is legal. It
calls a function, and if the function refuses, the SQLSTATE is translated into the exception that
names the rule (`errors.translate`). A check written here would be a copy of a rule that is already
enforced, and the copy is the one that would be wrong.

**A convention the modules share.** `ConnectionLike` is a structural type, so these stores accept a
psycopg connection, a pool checkout or a test double without importing a driver. That is also why
the driver is imported lazily, in the two places that must send a `jsonb` parameter: importing this
package never requires it.
"""

from referraleasy_domain.approval import (
    CANONICAL_ENCODING,
    PAYLOAD_HASH_KEY,
    PAYLOAD_HASH_PATTERN,
    canonical_booking_payload_hash,
)
from referraleasy_domain.commands import (
    REQUEST_HASH_PATTERN,
    CommandDecision,
    CommandDisposition,
    CommandLedger,
    CommandRecord,
    canonical_request_hash,
)
from referraleasy_domain.errors import (
    SQLSTATE_MAP,
    AppendOnlyViolationError,
    ApprovalRefusedError,
    CommandConflictError,
    DomainError,
    HumanOwnedError,
    IllegalTransitionError,
    OutboxClaimError,
    PolicyDeniedError,
    StaleOwnershipError,
    translate,
)
from referraleasy_domain.handoff import (
    DEFAULT_RESOLUTION_STATE,
    Handoff,
    HandoffStore,
)
from referraleasy_domain.outbox import (
    DrainReport,
    OutboxMessage,
    OutboxStatus,
    OutboxStore,
)
from referraleasy_domain.policy import (
    ACTION_REF_PATTERN,
    MalformedActionRefError,
    PolicyTable,
    parse_action_ref,
)
from referraleasy_domain.workflow import (
    BookingRequest,
    Workflow,
    WorkflowStore,
)

__all__ = [
    "ACTION_REF_PATTERN",
    "CANONICAL_ENCODING",
    "DEFAULT_RESOLUTION_STATE",
    "PAYLOAD_HASH_KEY",
    "PAYLOAD_HASH_PATTERN",
    "REQUEST_HASH_PATTERN",
    "SQLSTATE_MAP",
    "AppendOnlyViolationError",
    "ApprovalRefusedError",
    "BookingRequest",
    "CommandConflictError",
    "CommandDecision",
    "CommandDisposition",
    "CommandLedger",
    "CommandRecord",
    "DomainError",
    "DrainReport",
    "Handoff",
    "HandoffStore",
    "HumanOwnedError",
    "IllegalTransitionError",
    "MalformedActionRefError",
    "OutboxClaimError",
    "OutboxMessage",
    "OutboxStatus",
    "OutboxStore",
    "PolicyDeniedError",
    "PolicyTable",
    "StaleOwnershipError",
    "Workflow",
    "WorkflowStore",
    "canonical_booking_payload_hash",
    "canonical_request_hash",
    "parse_action_ref",
    "translate",
]
