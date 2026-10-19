"""§3/§7's booking-write rules: the approval binds one payload, and one key is one appointment.

Scheduling is the only agent with write authority, and §3 puts two forbidden
capabilities on it, each with a test here:

* **"cannot write without a valid approval"** — a write is refused unless a live
  approval is bound to the exact payload being written (the hash), to the
  server-side state version (the version), to the present (the expiry) and to a
  named human (the grantor). Each of those is a separate case below, because each is
  a way the check could be weakened by one `if`.
* **"on a timeout it cannot create a duplicate under a new key"** — a transport fault
  on the write path becomes `BookingOutcomeUnknown`, never the retryable failure, and
  a key that disagrees with the key the *turn* carries is refused
  (`DuplicateBookingKeyRefused`). The model supplies an approval object and a key
  string; the authority for "may this be written" is the hash, the version, the clock
  and the turn's own key.

The hash is the same function the mock API uses (`services/mock-api/src/domain/ids.ts`:
keys sorted, JSON minified, sha256 hex), and one fixed vector pins it, because a drift
between the two sides would make every approval unverifiable in one direction and
silently valid in the other. `canonical.py` claims the two agree; this is the test
that makes the claim checkable.

The guards are called the way `forbidden.py` calls them — through a hand-built
subject that satisfies `tools.call.WriteGuardSubject` and nothing more. That is
deliberate: the boundary belongs to the guard, and proving it needs neither a budget,
a lease, nor a transport.

Two findings are pinned here, both found by running the code:

* `UpstreamRejected` cannot be raised without a `status_code` (the runtime requires a
  keyword), so the delegation failure path had a `TypeError` hiding in it until a
  test constructed one;
* a write that fails with a status *outside* the code's set (a 429, say) keeps its own
  classification and is not re-labelled `outcome_unknown`, because "we were
  rate-limited" does establish that nothing was written.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from agents_fixtures import (
    COMMAND_ID,
    HOSPITAL_ID,
    PATIENT_ID,
    PROVIDER_ID,
    SLOT_ID,
    WORKFLOW_ID,
    AllowAllPolicy,
    StubApi,
    envelope,
    toolset_for,
)
from pydantic import ValidationError

from referraleasy_agent_runtime import (
    AgentRole,
    UpstreamRejected,
    UpstreamTransient,
    project_failure,
)
from referraleasy_agents.canonical import canonical_json, payload_hash
from referraleasy_agents.errors import (
    BookingOutcomeUnknown,
    DuplicateBookingKeyRefused,
    NoApprovalRefused,
)
from referraleasy_agents.tools.scheduling import (
    MIN_IDEMPOTENCY_KEY_LENGTH,
    check_approval,
    enforce_idempotency_key,
)
from referraleasy_contracts.generated.booking import BookingPayload, BookingRequest
from referraleasy_contracts.generated.common import ResultOutcome
from referraleasy_contracts.generated.task import TaskEnvelope

pytestmark = pytest.mark.contract

#: The workflow's own booking key for the fixture turn. Long enough for the API.
TURN_KEY = "booking-key-0001"

#: A second key: what "a duplicate under a new key" would look like.
OTHER_KEY = "booking-key-0002"

#: The approval id the live-path tests expect to travel to the API.
APPROVAL_ID = "0f0f0f0f-0000-4000-8000-0000000000a1"

STATE = "BOOKING_IN_FLIGHT"

PAYLOAD: Mapping[str, Any] = {
    "patient_id": PATIENT_ID,
    "provider_id": PROVIDER_ID,
    "hospital_id": HOSPITAL_ID,
    "slot_id": SLOT_ID,
    "visit_type": "first_visit",
}

#: A far-future expiry, so only the expiry-specific tests are about the clock.
LIVE_EXPIRY = "2030-01-01T00:00:00+00:00"


@dataclass(frozen=True, slots=True)
class WriteSubject:
    """The facts a booking guard reads, and nothing else (`tools.call.WriteGuardSubject`)."""

    role: AgentRole = AgentRole.SCHEDULING
    workflow_state: str = STATE
    envelope: TaskEnvelope | None = None
    current_state_version: int | None = 7
    payload: Mapping[str, Any] = field(default_factory=lambda: {"idempotency_key": TURN_KEY})


def subject(*, envelope_: Any = ..., version: int | None = 7) -> WriteSubject:
    """A subject whose envelope carries the state and the version the guard compares."""
    return WriteSubject(
        envelope=envelope() if envelope_ is ... else envelope_,
        current_state_version=version,
    )


def canonical_payload(payload: Mapping[str, Any] = PAYLOAD) -> dict[str, Any]:
    """The payload as `create_booking` hashes it: validated, then `exclude_none`."""
    return BookingPayload.model_validate(dict(payload)).model_dump(mode="json", exclude_none=True)


def approval(**overrides: Any) -> dict[str, Any]:
    """A live approval bound to `PAYLOAD`, with one field replaceable per test."""
    body: dict[str, Any] = {
        "approval_id": APPROVAL_ID,
        "payload_hash": payload_hash(canonical_payload()),
        "state_version": 6,
        "expires_at": LIVE_EXPIRY,
        "granted_by": "patient",
    }
    body.update(overrides)
    return body


# ── the approval ─────────────────────────────────────────────────────────────────────────────


def test_a_write_with_no_envelope_has_no_version_to_bind_to() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(envelope_=None), canonical_payload(), approval())
    assert "no task envelope" in str(raised.value)


def test_a_write_with_no_approval_is_refused() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(), canonical_payload(), None)
    assert "no approval" in str(raised.value)


def test_an_approval_bound_to_another_payload_is_refused() -> None:
    changed = dict(PAYLOAD) | {"hospital_id": "0f0f0f0f-0000-4000-8000-0000000000b2"}
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(), canonical_payload(changed), approval())
    assert "payload_hash" in str(raised.value)
    assert "approval_payload_changed" in str(raised.value)


def test_an_approval_from_another_state_version_is_refused() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(), canonical_payload(), approval(state_version=5))
    assert "approval_expired" in str(raised.value)


def test_an_approval_with_no_version_at_all_is_refused() -> None:
    with pytest.raises(NoApprovalRefused):
        check_approval(subject(), canonical_payload(), approval(state_version=None))


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (None, "carries no expires_at"),
        ("", "carries no expires_at"),
        ("not a timestamp", "not a parseable timestamp"),
        ("2000-01-01T00:00:00+00:00", "expired at"),
    ],
    ids=["absent", "blank", "unparseable", "past"],
)
def test_an_approval_that_is_not_live_is_refused(expires_at: Any, expected: str) -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(), canonical_payload(), approval(expires_at=expires_at))
    assert expected in str(raised.value)


def test_a_consumed_approval_is_refused() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(), canonical_payload(), approval(consumed=True))
    assert "already been consumed" in str(raised.value)


def test_an_approval_nobody_granted_is_refused() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        check_approval(subject(), canonical_payload(), approval(granted_by=None))
    assert "who granted it" in str(raised.value)


def test_a_live_matching_approval_passes_and_names_itself() -> None:
    assert check_approval(subject(), canonical_payload(), approval()) == APPROVAL_ID


def test_an_approval_with_no_identifier_still_passes_and_names_nothing() -> None:
    """The approval *object* is what travels; an id is bookkeeping, not the binding."""
    assert check_approval(subject(), canonical_payload(), approval(approval_id=None)) is None


# ── the key ──────────────────────────────────────────────────────────────────────────────────


def test_a_write_with_no_key_is_refused() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        enforce_idempotency_key(subject(), None, tool="create_booking")
    assert "idempotency key" in str(raised.value)


def test_a_key_shorter_than_the_api_accepts_is_refused() -> None:
    with pytest.raises(NoApprovalRefused) as raised:
        enforce_idempotency_key(subject(), "short", tool="create_booking")
    assert str(MIN_IDEMPOTENCY_KEY_LENGTH) in str(raised.value)


def test_a_key_that_disagrees_with_the_turn_s_own_key_is_refused() -> None:
    """§7: a different key is a different appointment, whether the model means it or not."""
    with pytest.raises(DuplicateBookingKeyRefused) as raised:
        enforce_idempotency_key(subject(), OTHER_KEY, tool="create_booking")
    message = str(raised.value)
    assert TURN_KEY in message and OTHER_KEY in message
    assert "lookup_request" in message


def test_the_turn_s_own_key_passes() -> None:
    assert enforce_idempotency_key(subject(), TURN_KEY, tool="create_booking") == TURN_KEY


# ── the hash ─────────────────────────────────────────────────────────────────────────────────


def test_the_hash_is_the_mock_api_s_canonical_rule() -> None:
    """Same rule as `services/mock-api/src/domain/ids.ts`: sorted keys, minified, sha256 hex."""
    assert canonical_json({"b": [2, 3], "a": 1}) == '{"a":1,"b":[2,3]}'
    assert (
        payload_hash({"b": [2, 3], "a": 1})
        == "efbd0040190fb0871831e606c581f8a66db79d8e2bb836745a70051306956070"
    )
    assert payload_hash({"b": [2, 3], "a": 1}) == hashlib.sha256(b'{"a":1,"b":[2,3]}').hexdigest()


def test_a_payload_that_only_differs_by_a_null_key_hashes_the_same() -> None:
    """`exclude_none`: an absent optional field and a null one are the same payload."""
    with_none = dict(PAYLOAD) | {"contact_phone": None}
    assert payload_hash(canonical_payload(with_none)) == payload_hash(canonical_payload())


# ── the whole write path ─────────────────────────────────────────────────────────────────────


def booking_response() -> dict[str, Any]:
    """A `BookingRequest`-shaped answer, built rather than mocked."""
    request = BookingRequest.model_validate(
        {
            "booking_request_id": "0f0f0f0f-0000-4000-8000-0000000000c1",
            "workflow_id": WORKFLOW_ID,
            "command_id": COMMAND_ID,
            "idempotency_key": TURN_KEY,
            "payload_hash": payload_hash(canonical_payload()),
            "payload": canonical_payload(),
            "approval_id": APPROVAL_ID,
            "state": "REQUESTED",
            "adapter_type": "instant_confirm",
            "attempt_count": 1,
            "created_at": "2026-01-05T09:00:00+00:00",
            "updated_at": "2026-01-05T09:00:00+00:00",
        }
    )
    return {"booking": request.model_dump(mode="json"), "outcome": "requested"}


def scheduling_tools(api: Any) -> Any:
    """A started `ToolSet` for Scheduling, with the turn's own key in its payload."""
    _, tools = toolset_for(
        AgentRole.SCHEDULING,
        api=api,
        policy=AllowAllPolicy(),
        workflow_state=STATE,
        envelope=envelope(workflow_state=STATE),
        payload={"idempotency_key": TURN_KEY},
    )
    return tools


@pytest.mark.asyncio
async def test_a_live_approval_writes_the_envelope_s_identity_and_the_turn_s_key() -> None:
    api = StubApi({"/appointments": booking_response()})
    result = await scheduling_tools(api).invoke(
        "create_booking", idempotency_key=TURN_KEY, payload=dict(PAYLOAD), approval=approval()
    )

    method, path, body = api.calls[0]
    assert (method, path) == ("POST", "/appointments")
    assert body["workflow_id"] == WORKFLOW_ID, "the workflow is the envelope's, not a tool argument"
    assert body["command_id"] == COMMAND_ID
    assert body["idempotency_key"] == TURN_KEY
    assert body["approval_id"] == APPROVAL_ID
    assert result["booking"]["state"] == "REQUESTED"


@pytest.mark.asyncio
async def test_a_payload_the_contract_does_not_have_is_refused_before_any_write() -> None:
    api = StubApi({"/appointments": booking_response()})
    with pytest.raises(ValidationError):
        await scheduling_tools(api).invoke(
            "create_booking",
            idempotency_key=TURN_KEY,
            payload={**PAYLOAD, "notes": "please hurry"},
            approval=approval(),
        )
    assert api.calls == [], "a write must not be attempted with a payload the contract refuses"


class FailingApi:
    """An `Api` whose write path raises exactly what a transport failure raises."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []

    async def get(self, path: str, **_: Any) -> dict[str, Any]:  # pragma: no cover - unused here
        self.calls.append(path)
        return {}

    async def post(self, path: str, payload: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append(path)
        raise self.error


@pytest.mark.asyncio
async def test_a_write_whose_fate_is_unknown_is_outcome_unknown_and_not_retryable() -> None:
    """§7's one-way difference, through the real tool: same key, lookup next, no retry."""
    api = FailingApi(UpstreamTransient("the hospital did not answer", status_code=504))
    with pytest.raises(BookingOutcomeUnknown) as raised:
        await scheduling_tools(api).invoke(
            "create_booking", idempotency_key=TURN_KEY, payload=dict(PAYLOAD), approval=approval()
        )
    error = raised.value
    assert error.idempotency_key == TURN_KEY
    assert error.outcome is ResultOutcome.outcome_unknown
    assert "lookup_request" in str(error)
    projected = project_failure(error)
    assert projected.outcome is ResultOutcome.outcome_unknown
    assert projected.a2a_state == "failed"


@pytest.mark.asyncio
async def test_a_dropped_connection_with_no_status_is_also_unknown() -> None:
    api = FailingApi(UpstreamTransient("the connection dropped"))
    with pytest.raises(BookingOutcomeUnknown):
        await scheduling_tools(api).invoke(
            "create_booking", idempotency_key=TURN_KEY, payload=dict(PAYLOAD), approval=approval()
        )


@pytest.mark.asyncio
async def test_a_rate_limited_write_keeps_its_own_classification() -> None:
    """429 is not on the write path's "may have arrived" list: nothing was written."""
    api = FailingApi(UpstreamTransient("slow down", status_code=429))
    with pytest.raises(UpstreamTransient):
        await scheduling_tools(api).invoke(
            "create_booking", idempotency_key=TURN_KEY, payload=dict(PAYLOAD), approval=approval()
        )


@pytest.mark.asyncio
async def test_a_rejected_write_keeps_its_status_code() -> None:
    """`UpstreamRejected` requires a `status_code`; the write path must carry it through."""
    api = FailingApi(UpstreamRejected("the hospital refused this request", status_code=409))
    with pytest.raises(UpstreamRejected) as raised:
        await scheduling_tools(api).invoke(
            "create_booking", idempotency_key=TURN_KEY, payload=dict(PAYLOAD), approval=approval()
        )
    assert raised.value.status_code == 409
