"""ReferralEasy contract bindings.

``packages/contracts/schemas/*.json`` is the single source of truth. Nothing in this package is
hand-written except the thin, explicitly-marked modules below — everything under ``generated/`` is
produced by ``packages/contracts/generate.sh`` (``pnpm gen:contracts``). Do not hand-edit it.

Layout
------
``schemas``      Locate, load and validate against the JSON Schema source files.
``generated``    Pydantic v2 models generated from those schemas.
``a2a``          Application result -> A2A protocol state adapter (§1.1, §5).
``constraints``  Cross-field rules JSON Schema expresses but generated Pydantic does not.
"""

from referraleasy_contracts.a2a import (
    A2A_TASK_STATE_BY_EVENT,
    A2A_TASK_STATE_BY_OUTCOME,
    TERMINAL_A2A_STATES,
    AgentTaskRequest,
    AgentTaskResult,
    ProtocolEvent,
    project_event,
    project_outcome,
)
from referraleasy_contracts.constraints import validate_result_envelope
from referraleasy_contracts.schemas import (
    CONTRACT_VERSION,
    SCHEMA_DIR,
    SCHEMA_FILES,
    load_schema,
    load_schemas,
    validate_against,
    validate_def,
)

__all__ = [
    "A2A_TASK_STATE_BY_EVENT",
    "A2A_TASK_STATE_BY_OUTCOME",
    "CONTRACT_VERSION",
    "SCHEMA_DIR",
    "SCHEMA_FILES",
    "TERMINAL_A2A_STATES",
    "AgentTaskRequest",
    "AgentTaskResult",
    "ProtocolEvent",
    "load_schema",
    "load_schemas",
    "project_event",
    "project_outcome",
    "validate_against",
    "validate_def",
    "validate_result_envelope",
]
