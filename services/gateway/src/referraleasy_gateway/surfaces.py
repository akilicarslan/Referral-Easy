"""Define the card catalog, stable IDs and complete agent decisions.

The catalog contains the eight contract components. WorkflowSurfaceProjection
owns their lifecycle. Partial agent output cannot become a committed decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import RootModel

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_contracts.generated.sse import ComponentInstance, RenderableComponent

__all__ = [
    "CATALOG",
    "HELP_CARD_COMPONENT",
    "SURFACE_PROPS_KEY",
    "AgentDecision",
    "CardPlan",
    "PartialBuffer",
    "PartialOutputError",
    "instance_id_of",
    "instance_id_of_like",
    "surface_id_for",
    "unwrap",
    "workflow_state_of",
]


def unwrap(value: Any) -> Any:
    """The innermost value of a contract `RootModel`.

    The generated bindings wrap scalars — `Uuid`, `CommandId`, `StateVersion`, `ActionRef` are all
    `RootModel`s, and `CommandId`'s root is itself a `Uuid`. Those wrappers are **unhashable** and
    compare **unequal** to the plain values they carry, so a wrapped id used as a dict key, put in a
    `set`, or compared against a token's `patient_ref` fails — loudly for the unhashable-half (a
    `TypeError` deep inside a set) and *silently* for the equality half.

    Everything on this side of the boundary works with `UUID`, `str` and `int`, and these two
    functions are where the conversion happens — once per value, rather than at each use. The module
    that owns the contract types owns the conversion, which is why this lives here and `commands`
    imports it.
    """
    while isinstance(value, RootModel):
        value = value.root
    return value


def instance_id_of(instance: ComponentInstance) -> UUID:
    """Read a card ID as a plain UUID."""
    return instance_id_of_like(instance.instance_id)


def instance_id_of_like(value: Any) -> UUID:
    """An id from either side of the boundary, as a plain `UUID`.

    Called for contract ids (`Uuid` RootModels) and for plain ones, because
    `ComponentInstance.instance_id` is wrapped and projection IDs are not. Unwrapping a plain
    `UUID` is a no-op, so a caller never has to know which side it is holding.
    """
    unwrapped = unwrap(value)
    return unwrapped if isinstance(unwrapped, UUID) else UUID(str(unwrapped))


#: Where a coordinator leaves the props for a card. The gateway forwards *this* and nothing else, so
#: a payload that is not under this key can never reach a browser (§2.1: an unvalidated agent payload
#: is never forwarded).
SURFACE_PROPS_KEY: Final = "surface_props"


@dataclass(frozen=True, slots=True)
class CardPlan:
    """What card a state shows, the action it submits, and the props it must have.

    `required_props` is the shape check this phase can honestly make: the contract types `props` as
    an open object, so the gateway enforces the keys the card cannot render without and refuses to
    forward a card missing them. The full per-component schemas belong to the A2UI catalog in
    `apps/web` (Phase 2.2); when they exist, they replace this tuple rather than being duplicated by
    it.
    """

    component: RenderableComponent
    action: str | None
    required_props: tuple[str, ...] = ()
    #: Which card instance this state re-uses. Two states that show the same card for different
    #: reasons share a slot only when they mean the same thing to the patient.
    slot: str = "primary"


#: State -> card. The action on each card is the one the policy table permits *in that state*; the
#: consistency test is `tests/gateway/test_surfaces.py::test_every_planned_action_is_permitted_where_it_is_issued`.
CATALOG: Final[Mapping[WorkflowState, CardPlan]] = {
    WorkflowState.AWAITING_HOSPITAL_CHOICE: CardPlan(
        RenderableComponent.HospitalPicker, "system:await_patient_choice", ("options",)
    ),
    WorkflowState.AWAITING_DOCTOR_CHOICE: CardPlan(
        RenderableComponent.DoctorPicker, "system:await_patient_choice", ("options",)
    ),
    WorkflowState.AWAITING_SLOT_CHOICE: CardPlan(
        RenderableComponent.SlotPicker, "system:await_patient_choice", ("options",)
    ),
    WorkflowState.AWAITING_FINAL_APPROVAL: CardPlan(
        RenderableComponent.BookingReview, "system:capture_consent", ("booking_payload",)
    ),
    WorkflowState.BOOKED: CardPlan(RenderableComponent.AppointmentReceipt, None, ("appointment",)),
    WorkflowState.BOOKING_UNVERIFIED: CardPlan(
        RenderableComponent.DirectoryFallbackCard, "scheduling:lookup_request", ("notice",)
    ),
    WorkflowState.RECOVERY: CardPlan(RenderableComponent.DirectoryFallbackCard, None, ("notice",)),
    WorkflowState.HUMAN_HANDOFF: CardPlan(RenderableComponent.HumanHandoffCard, None, ("handoff",)),
}

#: The static card §9 keeps available when the guardrail dependency is off
#: ("the static guide/help card remains available"). It is not in the catalog because it is not a
#: state
#: — it is what the patient can always reach, including when nothing else works.
HELP_CARD_COMPONENT: Final = RenderableComponent.HumanHandoffCard


def surface_id_for(workflow_id: UUID) -> UUID:
    """One surface per workflow, stable across reconnects."""
    return uuid5(NAMESPACE_URL, f"referraleasy/surface/{workflow_id}")


def instance_id_for(workflow_id: UUID, component: RenderableComponent, slot: str) -> UUID:
    """The instance id for a card slot. Deterministic, so a replay re-renders instead of adding."""
    return uuid5(NAMESPACE_URL, f"referraleasy/surface/{workflow_id}/{component.value}/{slot}")


def workflow_state_of(component: RenderableComponent) -> WorkflowState | None:
    """The state a card belongs to, read back out of the catalog. For tests and for the contract."""
    for state, plan in CATALOG.items():
        if plan.component is component:
            return state
    return None


class PartialOutputError(RuntimeError):
    """An unfinished decision was treated as a result (§12: partial output is never final state)."""


@dataclass
class PartialBuffer:
    """Streamed model output, before anyone may call it an answer.

    The buffer exists so that "still streaming" is a state the code can *be in*, rather than a
    convention about not reading a string too early. `commit` is the only reader that returns a
    result, and it refuses while the producer has not signalled completion.
    """

    chunks: list[str] = field(default_factory=list)
    complete: bool = False

    def add(self, chunk: str) -> None:
        self.chunks.append(chunk)

    def finish(self) -> None:
        self.complete = True

    @property
    def text(self) -> str:
        """Everything accumulated so far. Safe to *display*; never safe to persist as a result."""
        return "".join(self.chunks)

    def commit(self) -> str:
        """The finished text, or a refusal.

        Raises :class:`PartialOutputError` — the exception a caller gets for trying to persist
        something that is still arriving. It is not a policy or version refusal, so it is not in the
        HTTP mapping: it must never be reachable from a client request.
        """
        if not self.complete:
            raise PartialOutputError(
                "the decision is still streaming; partial agent output is never committed as final "
                "state (§12)"
            )
        return self.text


@dataclass(frozen=True, slots=True)
class AgentDecision:
    """What a coordinator (or, today, a test) decided, with its completeness attached.

    `complete=False` is the shape a streaming decision has: it may be *shown* (as `partial_text`) and
    it may not be *committed*. Keeping completeness on the value rather than in a caller's head is
    what lets `commit` refuse it.
    """

    complete: bool
    to_state: str | None = None
    component: RenderableComponent | None = None
    props: Mapping[str, Any] = field(default_factory=dict)
    notice: str | None = None
    text: str = ""

    def commit(self) -> AgentDecision:
        """This decision, if it is finished. The only way a decision becomes state."""
        if not self.complete:
            raise PartialOutputError(
                "a decision that is still streaming cannot be committed: §12 forbids committing "
                "partial agent output as final state"
            )
        return self
