"""Store and restore the cards for one workflow state.

Commands own transitions and consent. This module owns card identity, retirement,
submission and repair. All records use the existing control-state interface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from referraleasy_contracts.generated.common import BookingState, WorkflowState
from referraleasy_contracts.generated.sse import ComponentInstance, RenderableComponent
from referraleasy_domain import IllegalTransitionError
from referraleasy_domain.approval import PAYLOAD_HASH_KEY, canonical_booking_payload_hash
from referraleasy_gateway.control import ControlState, WorkflowRow
from referraleasy_gateway.errors import ContractViolationError
from referraleasy_gateway.surfaces import CATALOG, CardPlan, instance_id_for, instance_id_of, unwrap

SURFACE_PAYLOAD_KEY: Final = "surface_components"
_INTENT_KEY: Final = "coordination_projection"
_RETIRED_KEY: Final = "surface_retired_ids"
_VERSION_KEY: Final = "surface_state_version"


@dataclass(frozen=True, slots=True)
class SurfaceChange:
    """Persisted cards and the IDs to remove before those cards are sent."""

    components: tuple[ComponentInstance, ...]
    remove_instance_ids: tuple[UUID, ...] = ()


class WorkflowSurfaceProjection:
    """Apply the complete card lifecycle through one control-state adapter.

    Reads use stored records. A second gateway can answer a card without leaving
    a stale question in this gateway. Empty projections have a version marker.
    That marker prevents repair from opening an answered question again.
    """

    def __init__(self, control: ControlState, *, synthetic: bool = True) -> None:
        self._control = control
        self._synthetic = synthetic

    def prepare(self, row: WorkflowRow, state: WorkflowState, props: Mapping[str, Any]) -> None:
        """Validate the next card and store its intent before a transition."""
        self._validate(state, props)
        self._control.update_payload(
            row.workflow_id,
            expected_state_version=row.state_version,
            patch={_INTENT_KEY: self._intent(row, state, row.state_version + 1, props)},
        )

    def show(self, row: WorkflowRow, props: Mapping[str, Any]) -> SurfaceChange:
        """Store the current card and retire the cards it replaces."""
        row = self._fresh(row)
        state = WorkflowState(row.state)
        plan = self._validate(state, props)
        if state is WorkflowState.HUMAN_HANDOFF:
            if props.get("consent_required") is True:
                plan = CardPlan(RenderableComponent.HumanHandoffCard, "system:capture_consent")
            elif row.payload.get("handoff") and props.get("consent_required") is False:
                plan = CardPlan(RenderableComponent.HumanHandoffCard, None, slot="receipt")
        components = (self._build(row, props, plan),) if plan is not None else ()
        return self._save(row, components, props=props)

    def visible(
        self, row: WorkflowRow, *, actionable_only: bool = False
    ) -> tuple[ComponentInstance, ...]:
        """Read current cards. Include receipts unless only questions are needed."""
        current = self._control.load(row.workflow_id)
        if current is not None and current.state_version == row.state_version:
            row = current
        # A read can complete at its observed version while a worker advances.
        # Keep that row and its cards together. Only writes require a fresh version.
        return tuple(
            instance
            for instance in self._cards(row)
            if not instance.submitted
            and unwrap(instance.workflow_state_version) == row.state_version
            and (not actionable_only or instance.action is not None)
        )

    def require_action(self, row: WorkflowRow, instance_id: UUID, action: str) -> ComponentInstance:
        """Refuse actions that the current stored card does not permit."""
        row = self._fresh(row)
        instance = next(
            (card for card in self._cards(row) if instance_id_of(card) == instance_id), None
        )
        if instance_id in self._retired(row) or (
            instance is not None
            and (instance.submitted or unwrap(instance.workflow_state_version) != row.state_version)
        ):
            raise ContractViolationError(
                f"instance {instance_id} was already answered; a replayed surface must "
                "not solicit the same choice twice (§12)"
            )
        if instance is None:
            raise ContractViolationError(
                f"instance {instance_id} was never issued for workflow {row.workflow_id}; "
                "the client echoes an action the server put on an instance it holds, "
                "it never invents one (§9)"
            )
        if instance.disabled:
            raise ContractViolationError("this instance is disabled and cannot be answered")
        if unwrap(instance.action) != action:
            raise ContractViolationError(
                f"the client submitted {action!r}, but this instance issues "
                f"{unwrap(instance.action)!r}; the client echoes an action, it never invents one (§9)"
            )
        return instance

    def answer(
        self,
        row: WorkflowRow,
        instance_id: UUID,
        *,
        handoff: Mapping[str, Any] | None = None,
    ) -> SurfaceChange:
        """Store an accepted answer. Replace approved handoff consent with a receipt."""
        row = self._fresh(row)
        cards = self._cards(row)
        answered = next(card for card in cards if instance_id_of(card) == instance_id)
        if handoff is not None:
            return self.show(row, self._handoff_props(handoff))
        current = (
            answered.model_copy(update={"submitted": True}),
            *(
                card
                for card in cards
                if instance_id_of(card) != instance_id
                and not card.submitted
                and card.action is not None
            ),
        )
        return self._save(row, current)

    def repair(self, row: WorkflowRow) -> SurfaceChange | None:
        """Restore a missing projection from its intent or a recorded booking."""
        try:
            return self._repair(row)
        except IllegalTransitionError:
            current = self._control.load(row.workflow_id)
            if current is None or current.state_version != row.state_version:
                return None
            raise

    def _repair(self, row: WorkflowRow) -> SurfaceChange | None:
        row = self._fresh(row)
        cards = self._cards(row)
        handoff = row.payload.get("handoff")
        saved_intent = row.payload.get(_INTENT_KEY)
        intent = saved_intent if isinstance(saved_intent, Mapping) else {}
        if (
            row.state == WorkflowState.HUMAN_HANDOFF
            and isinstance(handoff, Mapping)
            and handoff.get("handoff_id") != intent.get("previous_handoff_id")
            and not any(
                unwrap(card.workflow_state_version) == row.state_version
                and card.props.get("consent_required") is False
                for card in cards
            )
        ):
            return self.show(row, self._handoff_props(handoff))
        if row.payload.get(_VERSION_KEY) == row.state_version or any(
            unwrap(card.workflow_state_version) == row.state_version for card in cards
        ):
            return None
        if (
            intent.get("state") == row.state
            and intent.get("state_version", row.state_version) == row.state_version
        ):
            return self.show(row, intent["props"])
        if row.state == WorkflowState.BOOKING_UNVERIFIED:
            return self.reopen_lookup(row)
        if row.state == WorkflowState.BOOKED:
            booking = next(
                (
                    item
                    for item in reversed(self._control.bookings_for(row.workflow_id))
                    if item.state is BookingState.CONFIRMED
                ),
                None,
            )
            if booking is not None:
                return self.show(
                    row,
                    {
                        "appointment": {
                            "booking_id": str(booking.booking_id),
                            "external_ref": booking.external_ref,
                            "state": "CONFIRMED",
                        }
                    },
                )
        return None

    def reopen_lookup(self, row: WorkflowRow) -> SurfaceChange:
        """Offer another status check for the original unverified booking."""
        if row.state != WorkflowState.BOOKING_UNVERIFIED:
            raise ContractViolationError(
                "Only an unverified booking can offer another status check."
            )
        return self.show(
            row,
            {
                "notice": "The hospital has not confirmed this request. Check the original request again later.",
                "can_retry": True,
                "retry_label": "Check booking status",
            },
        )

    def _handoff_props(self, handoff: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "handoff": dict(handoff),
            "consent_required": False,
            "title": "Your referral is queued for a coordinator",
            "body": handoff["patient_visible_summary"],
            "reference": handoff["ticket_ref"],
            "synthetic": self._synthetic,
        }

    @staticmethod
    def _intent(
        row: WorkflowRow, state: WorkflowState, version: int, props: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        intent: dict[str, Any] = {
            "state": state.value,
            "state_version": version,
            "props": dict(props),
        }
        if state is WorkflowState.HUMAN_HANDOFF:
            saved = row.payload.get(_INTENT_KEY)
            previous = saved if isinstance(saved, Mapping) else {}
            if previous.get("state_version") == version and "previous_handoff_id" in previous:
                intent["previous_handoff_id"] = previous["previous_handoff_id"]
            else:
                handoff = row.payload.get("handoff")
                intent["previous_handoff_id"] = (
                    handoff.get("handoff_id") if isinstance(handoff, Mapping) else None
                )
        return intent

    @staticmethod
    def _validate(state: WorkflowState, props: Mapping[str, Any]) -> CardPlan | None:
        plan = CATALOG.get(state)
        if plan is not None:
            missing = [key for key in plan.required_props if key not in props]
            if missing:
                raise ContractViolationError(
                    f"{plan.component.value} needs {missing}; a card without them cannot be rendered "
                    "and is not forwarded"
                )
        return plan

    @staticmethod
    def _build(row: WorkflowRow, props: Mapping[str, Any], plan: CardPlan) -> ComponentInstance:
        return ComponentInstance(
            instance_id=instance_id_for(row.workflow_id, plan.component, plan.slot),
            component=plan.component,
            props=dict(props),
            workflow_state_version=row.state_version,
            action=plan.action,
            submitted=False,
            disabled=plan.slot == "receipt",
            notice=None,
        )

    def _fresh(self, row: WorkflowRow) -> WorkflowRow:
        current = self._control.load(row.workflow_id)
        if current is None or current.state_version != row.state_version:
            raise IllegalTransitionError("The workflow changed before the card operation.")
        return current

    @staticmethod
    def _cards(row: WorkflowRow) -> tuple[ComponentInstance, ...]:
        saved = row.payload.get(SURFACE_PAYLOAD_KEY)
        if not isinstance(saved, list):
            return ()
        return tuple(ComponentInstance.model_validate(item) for item in saved)

    @staticmethod
    def _retired(row: WorkflowRow) -> set[UUID]:
        return {UUID(value) for value in row.payload.get(_RETIRED_KEY, [])}

    def _save(
        self,
        row: WorkflowRow,
        components: Sequence[ComponentInstance],
        *,
        props: Mapping[str, Any] | None = None,
    ) -> SurfaceChange:
        kept = {instance_id_of(card) for card in components}
        removed = tuple(
            instance_id_of(card) for card in self._cards(row) if instance_id_of(card) not in kept
        )
        retired = (self._retired(row) | set(removed)) - kept
        patch: dict[str, Any] = {
            SURFACE_PAYLOAD_KEY: [card.model_dump(mode="json") for card in components],
            _RETIRED_KEY: sorted(map(str, retired)),
            _VERSION_KEY: row.state_version,
        }
        if props is not None:
            patch[_INTENT_KEY] = self._intent(
                row, WorkflowState(row.state), row.state_version, props
            )
            if row.state == WorkflowState.AWAITING_FINAL_APPROVAL:
                reviewed = props.get("booking_payload")
                if isinstance(reviewed, Mapping):
                    patch["booking_payload"] = dict(reviewed)
                    patch[PAYLOAD_HASH_KEY] = canonical_booking_payload_hash(reviewed)
        self._control.update_payload(
            row.workflow_id, expected_state_version=row.state_version, patch=patch
        )
        return SurfaceChange(tuple(components), removed)
