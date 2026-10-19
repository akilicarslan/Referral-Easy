"""What one tool call is given: authority, budget, transport, provenance.

A tool handler needs five things it must never be tempted to take from the model:

* **the role and the workflow state** — they decide whether the call is permitted
  at all, and a model that could name its own state would be choosing its own
  permissions;
* **the lease** — the milliseconds this call may consume, already narrowed by
  what remains of the turn;
* **the transport** — one client per turn, so one retry policy and one deadline;
* **the envelope** — the §5 task envelope the call arrived in, which carries the
  workflow's `current_state_version` and its `allowed_actions`;
* **the ledger** — where provenance-tagged results go.

None of those appear in the model-visible parameter list of any tool
(`tools/specs.py` declares that list, and `test_tool_surface.py` asserts the ADK
declaration matches it exactly). They arrive here instead, because the caller
built them from the envelope it received rather than from anything a model said.

The same rule covers **whose records a call is about**. `workflow_id`,
`command_id` and `patient_id` are read through :func:`require_workflow_id`,
:func:`require_command_id` and :func:`require_patient_id` — never accepted as tool
arguments — so a model cannot attribute a booking write, a document check or a
constraint to a workflow or a patient other than the one whose task it is
answering. Every parameter a tool *does* declare is therefore a fact about the
subject of the call (a specialty, a hospital, a slot, a document), not about the
turn it is running inside.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from referraleasy_agent_runtime import AgentRole, CallLease, PolicyBackend, TurnBudget
from referraleasy_agents.canonical import id_text
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.evidence import EvidenceLedger
from referraleasy_contracts.generated.task import TaskEnvelope

__all__ = [
    "Api",
    "DelegationPort",
    "ToolCall",
    "WriteGuardSubject",
    "require_command_id",
    "require_patient_id",
    "require_workflow_id",
]


class Api(Protocol):
    """The transport a tool handler may use. Narrow on purpose: two methods."""

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        budget: TurnBudget | None = None,
        lease: CallLease | None = None,
    ) -> dict[str, Any]: ...

    async def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        budget: TurnBudget | None = None,
        lease: CallLease | None = None,
    ) -> dict[str, Any]: ...


class DelegationPort(Protocol):
    """How a delegation reaches a specialist.

    A port rather than a URL built inside the tool: the transport (A2A over HTTP in
    a deployment, an in-process call in a test) is the service's business, and the
    tool's job is the envelope and the budget. The runtime's `JsonServiceClient` and
    `a2a_client_config` are what the HTTP implementation is built from.
    """

    async def send(
        self,
        *,
        target_role: AgentRole,
        request: Mapping[str, Any],
        budget: TurnBudget,
    ) -> Mapping[str, Any]:
        """Send the child request and return its response body."""
        ...


class WriteGuardSubject(Protocol):
    """What a booking write's guards need to know about the call.

    The two guards in `tools/scheduling.py` — the approval check and the
    idempotency-key check — read five facts and nothing else: who is writing, in
    which state, what the server-side state version is, whether an envelope
    was supplied at all, and the turn's own idempotency key. Naming that here
    rather than requiring a whole :class:`ToolCall` is what lets `forbidden.py` call
    the *same* guards with a hand-built subject and prove §3's booking rules without
    a live API, a budget or a transport. A boundary that can only be tested through
    four layers of machinery is a boundary that stops being tested.
    """

    @property
    def role(self) -> AgentRole: ...

    @property
    def workflow_state(self) -> str: ...

    @property
    def envelope(self) -> TaskEnvelope | None: ...

    @property
    def current_state_version(self) -> int | None: ...

    @property
    def payload(self) -> Mapping[str, Any]:
        """The turn's own structured input.

        Read for exactly one fact — ``idempotency_key``, the key the *workflow*
        minted for this booking request. A model that supplies a different key is
        asking for a second appointment, and §7's rule is only enforceable because
        the turn's own key can be read here.
        """
        ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One authorised tool call, with everything it needs and nothing it may not have."""

    role: AgentRole
    tool: str
    workflow_state: str
    api: Api
    budget: TurnBudget
    lease: CallLease
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    envelope: TaskEnvelope | None = None
    #: The authority that owns the role x tool x state table, when the caller has
    #: one. `classify_error` needs it to say which actions the state machine still
    #: permits, and asking the authority is the only way to say that without
    #: keeping a second copy of the table (§4).
    policy: PolicyBackend | None = None
    #: The transport that carries a delegation to a specialist. Only the coordinator
    #: has a tool that uses it; every other role leaves it None, and the tool that
    #: needs it refuses loudly rather than reporting a delegation it never made.
    delegation: DelegationPort | None = None
    #: The caller's own structured input for this turn, already schema-validated.
    #: A tool handler reads facts from here (a plan id, a visit type) that the
    #: caller supplied; it never reads an *authority* from here.
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def workflow_id(self) -> str | None:
        """The workflow this call belongs to, from the envelope."""
        return None if self.envelope is None else id_text(self.envelope.workflow_id)

    @property
    def command_id(self) -> str | None:
        """The command this call belongs to, from the envelope (§7 deduplication)."""
        return None if self.envelope is None else id_text(self.envelope.command_id)

    @property
    def current_state_version(self) -> int | None:
        """The workflow's state version as the envelope stated it, server-side.

        ``.root`` and not ``int(...)``: `StateVersion` is a `RootModel`, so it is not
        itself an ``int``, and ``int(StateVersion(...))`` is a `TypeError` at the
        moment the booking guard reads it.
        """
        return None if self.envelope is None else int(self.envelope.current_state_version.root)

    @property
    def patient_details_id(self) -> str | None:
        """The §6 indirection, when the envelope carried one."""
        if self.envelope is None or self.envelope.patient_details_id is None:
            return None
        return id_text(self.envelope.patient_details_id)


# ── identity a tool must not take from the model ───────────────────────────────────────────────
#
# Three functions, one rule: *whose* records these are is the service's fact, not the
# model's choice. A tool that needs one of them calls the function; a tool declaration
# cannot name one (`tools/specs.py` has no such parameter, and `test_tool_surface.py`
# asserts that over every tool). Before these existed, four tools took a workflow id, a
# command id or a patient id as a model argument — which would have let a model attribute
# a booking write, a document check or a constraint to a workflow or a patient other than
# the one whose task it was answering.


def require_workflow_id(call: ToolCall) -> str:
    """The workflow this call belongs to, from the task envelope it arrived in.

    Default-deny: no envelope means no identity, and the call is refused rather than
    attributed by guesswork.
    """
    if call.envelope is None:
        raise UnknownDataError(
            f"{call.tool} has no task envelope, so there is no workflow to attribute this call to. "
            "The envelope is the §5 task's identity; a call that cannot name the workflow it "
            "belongs to would be writing into an unknown patient's record."
        )
    return id_text(call.envelope.workflow_id)


def require_command_id(call: ToolCall) -> str:
    """The command this call belongs to, from the envelope (§7 deduplication)."""
    if call.envelope is None:
        raise UnknownDataError(
            f"{call.tool} has no task envelope, so it carries no command id, and a write keyed by "
            "nothing cannot be recognised as a redelivery (§7)."
        )
    return id_text(call.envelope.command_id)


def require_patient_id(call: ToolCall) -> str:
    """The patient this call is about, from the turn's own validated input (§6).

    Two service-supplied sources, in order: the role's input schema (``patient_id``,
    which the workflow put there and the schema has already validated) and the
    envelope's ``patient_details_id``. The model describes *what* to do; the service
    knows *whose* records these are.
    """
    value = call.payload.get("patient_id")
    if value is None:
        value = call.patient_details_id
    if value is None:
        raise UnknownDataError(
            f"{call.tool} was given no patient: neither this role's own input nor the envelope's "
            "`patient_details_id` names one, so there is no record to act on. Default-deny rather "
            "than a patient id invented in the tool."
        )
    return id_text(value)
