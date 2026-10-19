# ruff: noqa: RUF003  # Turkish quoted from §9 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The HTTP surface: routes, request bodies, and the one error envelope (§9, §12).

Everything in this module is transport. It reads a request, calls one method of
:class:`~referraleasy_gateway.commands.CommandService`, and renders the answer. No rule lives here —
which is why the module is readable as a table: each route names the session kind it accepts, the
scope it checks, and the service method it delegates to.

Three decisions are worth stating because they are not obvious:

**`/healthz` is unauthenticated.** The compose healthcheck curls it without a token, and a health
probe that needs a session is a probe that reports "unhealthy" whenever the session fixture is the
thing that is broken. It reports configuration facts (`control_backend`, whether each guardrail layer
has something behind it) and no secret, and it does not touch the database.

**The stream accepts a token in the query string, and only in the demo profile.** `EventSource`
cannot set request headers, so a browser stream cannot carry `Authorization` at all. The trade is
made explicitly (`build_router` calls the verifier with `token=`, which `_bearer` only consults when
the caller asked for it) and it is confined to the demo profile by `settings.is_demo`. A token in a
URL must be assumed to reach logs; the Cloud Run profile's replacement is a short-lived stream ticket
or a same-origin cookie, and that is a change to this route and not to the verifier.

**Errors are rendered once, by type.** `register_error_handlers` maps `GatewayError` and
`DomainError` through `to_envelope`, and a `RequestValidationError` becomes the same envelope with
code `contract_violation`. A client therefore has one failure shape to parse, and the `sqlstate` in it
names the exact database rule that refused — which is how a test can assert "refused by RE002" rather
than "refused with 409".

**The session dependencies travel as argument defaults, and each site says so.** `B008` is disabled
per line below rather than repo-wide, because the alternative is broken here for the reason
`packages/agent-runtime` documents at the same pattern: this module uses `from __future__ import
annotations`, so FastAPI resolves a parameter annotation against *module* globals — and
`require_patient`/`require_admin` are closures built inside `build_router`. An
`Annotated[Session, Depends(require_patient)]` is therefore not resolvable and silently degrades into
a required query parameter, which is a 422 where a 401 belongs. The marker has to be in the default,
where FastAPI can see the real object.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from referraleasy_contracts import CONTRACT_VERSION
from referraleasy_contracts.generated.common import ReasonCode, WorkflowState
from referraleasy_contracts.generated.sse import ClientAction, StreamEventType
from referraleasy_domain import DomainError
from referraleasy_gateway.admin import (
    TelemetryReads,
    Window,
    filters_from,
    load_alert_rules,
    load_scenarios,
    unavailable,
)
from referraleasy_gateway.approval import ApprovalService
from referraleasy_gateway.auth import (
    TRACE_ID_HEADER,
    JwtIssuer,
    JwtVerifier,
    Session,
    SessionKind,
    admin_session_dependency,
    patient_session_dependency,
)
from referraleasy_gateway.commands import CommandReceipt, CommandService, SubmittedAction
from referraleasy_gateway.config import OFFLINE_PROFILE_LABEL, GatewaySettings
from referraleasy_gateway.control import ControlState
from referraleasy_gateway.errors import (
    ERROR_ENVELOPE_OPENAPI,
    BookingHaltedError,
    ContractViolationError,
    GatewayError,
    NotFoundError,
    to_envelope,
)
from referraleasy_gateway.events import EventLog, frames
from referraleasy_gateway.guardrails import GuardrailPipeline, layer_report
from referraleasy_gateway.human_queue import open_tickets
from referraleasy_gateway.killswitch import KillSwitch
from referraleasy_gateway.policy import ActionPolicy
from referraleasy_gateway.timing import Clock, LatencyLedger
from referraleasy_telemetry import Filters, TelemetryQueryError, TimeRange

__all__ = [
    "GatewayDeps",
    "build_router",
    "register_error_handlers",
]

#: The response header that identifies one request through the logs. Correlation only — nothing in
#: the gateway reads it as an identity (§9).
REQUEST_ID_HEADER: Final = "X-Request-Id"

#: How often the stream looks for new events while holding the connection open. Short enough that a
#: demo feels live, long enough not to spin.
_STREAM_POLL_SECONDS: Final = 0.05

#: A comment frame, sent while nothing is happening. `EventSource` ignores it and an intermediary
#: proxy sees traffic, which is what stops an idle stream being reaped at 60 seconds.
HEARTBEAT_FRAME: Final = ": keep-alive\n\n"

#: Why an admin read has no data. One string rather than four, so the admin app can tell "this
#: deployment has no telemetry cluster" from "the cluster answered and there is nothing in the
#: window" without parsing a sentence it has to keep in step with this file.
NO_TELEMETRY_SOURCE: Final = (
    "OPENSEARCH_URL is unset, so this deployment has no telemetry cluster to read. The gateway "
    "serves its patient surface without one (§18's core profile), and the admin pages report the "
    "absence rather than an empty chart."
)


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    """Everything a route may reach. Built once by `app.create_app` and captured in the closures."""

    settings: GatewaySettings
    clock: Clock
    control: ControlState
    log: EventLog
    approvals: ApprovalService
    policy: ActionPolicy
    guardrails: GuardrailPipeline
    commands: CommandService
    latencies: LatencyLedger
    patient_verifier: JwtVerifier
    admin_verifier: JwtVerifier
    patient_issuer: JwtIssuer | None = None
    admin_issuer: JwtIssuer | None = None
    #: §12's admin pages read the telemetry indices through this (ADR-0039). `None` when
    #: `OPENSEARCH_URL` is unset, which is a supported profile and not a misconfiguration: the
    #: routes answer "no source" rather than the gateway refusing to start.
    telemetry: TelemetryReads | None = None
    #: §16's kill switch. Defaulted rather than required, like `telemetry`, so a test that builds
    #: deps by hand gets a released one instead of a `TypeError`. Process-local, and the module's
    #: docstring says what that costs a multi-replica deployment.
    kill_switch: KillSwitch = field(default_factory=KillSwitch)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SessionRequestBody(BaseModel):
    """The demo fixture's session request. Not a login: there is no password for it to check."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)
    patient_ref: str | None = None
    ttl_seconds: int | None = Field(default=None, gt=0, le=24 * 3600)


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1)
    workflow_state_version: int = Field(ge=1)
    text: str = Field(min_length=1)


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1)
    expected_state_version: int = Field(ge=1)
    to_state: WorkflowState
    reason_code: ReasonCode = ReasonCode.ok
    #: `false` means the coordinator's decision is still streaming. The gateway then shows the text
    #: and commits nothing (§12).
    complete: bool = True
    props: dict[str, Any] | None = None
    partial_text: str = ""


class ControlSwitchBody(BaseModel):
    """§16's kill switch. `engage: false` releases it."""

    model_config = ConfigDict(extra="forbid")

    engage: bool
    reason: str = Field(min_length=1)
    by: str = Field(min_length=1)


class BookingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1)
    approval_id: UUID
    expected_state_version: int = Field(ge=1)
    booking_payload: dict[str, Any]
    #: The input this booking came from. Required, because a booking whose input nobody screened is a
    #: booking §9 forbids: "Guardrail dependency kapalıyken booking yazılamaz".
    screening_text: str = Field(min_length=1)
    #: The scheduler's report: `confirmed` or `unverified`. Absent means "not said", and then the
    #: reference below decides it — a hospital's reference is a confirmation, nothing is
    #: `unverified` (`commands._outcome_for`, the one place that inference is made). An explicit
    #: `confirmed` with no reference is refused rather than derived.
    outcome: str | None = None
    #: The hospital's own reference for the appointment. Required by `outcome: "confirmed"` and
    #: meaningless without it — 008's `booking_request_confirmed_has_ref` refuses a confirmed booking
    #: without one, and the gateway refuses the same request as a `422` before the write. A caller
    #: with no reference has no confirmation: it sends `"outcome": "unverified"`, and the workflow
    #: ends in `BOOKING_UNVERIFIED`, which §7 resolves by reconciliation.
    external_ref: str | None = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminQuery:
    """§12's window and filter set, as the admin routes consume them.

    A dependency rather than eight parameters repeated on each route: the filter set is one thing
    §12 defines once, and four copies of it is four places a new dimension has to be added.
    """

    window: Window
    resolved: TimeRange
    filters: Filters


def admin_query(
    range_: Annotated[str | None, Query(alias="range")] = None,
    timezone: Annotated[str | None, Query()] = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    agents: Annotated[list[str] | None, Query()] = None,
    agent_versions: Annotated[list[str] | None, Query()] = None,
    releases: Annotated[list[str] | None, Query()] = None,
    models: Annotated[list[str] | None, Query()] = None,
    synthetic: Annotated[str | None, Query()] = None,
) -> AdminQuery:
    """Parse §12's window and filters, or refuse (422) rather than default to something plausible.

    The zone is resolved here and not by the route, because `TimeRange` validates it — an unknown
    zone is a `TelemetryQueryError`, and converting that into the gateway's envelope is what keeps a
    typo in a dashboard URL a 422 instead of a 500.

    **`Annotated` for the query parameters and `Depends(...)` defaults for the sessions**, which is
    the opposite of what the module docstring describes, and the difference is real rather than
    stylistic. The trap there is that FastAPI resolves a postponed annotation against *module*
    globals, so a `Depends` built from a closure inside `build_router` is unresolvable. `Query` and
    `str` are module globals, so an annotation here resolves — and keeping the call out of the
    default is also what stops `B008` from firing eight times over one dependency.
    """
    window = Window.parse(range_=range_, timezone=timezone, start=start, end=end)
    try:
        resolved = window.resolve()
    except TelemetryQueryError as problem:
        raise ContractViolationError(
            f"timezone={window.timezone!r} is not usable: {problem}"
        ) from problem
    return AdminQuery(
        window=window,
        resolved=resolved,
        filters=filters_from(
            agents=agents or (),
            agent_versions=agent_versions or (),
            releases=releases or (),
            models=models or (),
            synthetic=synthetic,
        ),
    )


def _synthetic_flag(filters: Filters) -> bool | None:
    """§15's label as the boolean a span carries, or `None` for "either".

    The event vocabulary spells it `synthetic` / `live_demo`; a span maps it as a boolean. Both are
    derived from the same filter, and anything that is neither of the two known values is treated as
    unfiltered — `Filters.clauses()` has already refused it on the event path, so reaching here with
    something else means the value came from a caller that did not go through that path.
    """
    value = filters.synthetic
    if value is None:
        return None
    if str(value) == "synthetic":
        return True
    return False if str(value) == "live_demo" else None


def build_router(deps: GatewayDeps) -> APIRouter:
    """The routes, with this app's ports closed over."""
    #: Every route below answers a refusal with the same envelope, so it is declared once rather than
    #: repeated per route where the repetition would eventually disagree with the handler.
    envelope = {
        "description": "error envelope",
        "content": {"application/json": {"schema": ERROR_ENVELOPE_OPENAPI}},
    }
    router = APIRouter(
        responses={401: envelope, 403: envelope, 404: envelope, 409: envelope, 422: envelope}
    )
    require_patient = patient_session_dependency(
        deps.patient_verifier, allow_query_token=deps.settings.is_demo
    )
    require_admin = admin_session_dependency(deps.admin_verifier)

    def _receipt(receipt: CommandReceipt) -> JSONResponse:
        body = dict(receipt.body)
        body["replayed"] = receipt.replayed
        return JSONResponse(status_code=receipt.status_code, content=_jsonable(body))

    # -- health ---------------------------------------------------------------------------------

    @router.get("/healthz", summary="Liveness, unauthenticated")
    def healthz() -> dict[str, Any]:
        """Cheap, public, and honest about what it does not check.

        It does not touch the database: a probe that fails when Postgres is slow reports a gateway
        outage during a database incident, and the two are different incidents. What it does report is
        whether each guardrail layer has an implementation *right now*, because that changes what this
        process is allowed to do.
        """
        return {
            "status": "ok",
            "service": "referraleasy-gateway",
            "contract_version": CONTRACT_VERSION,
            "control_backend": deps.settings.control_backend,
            "guardrails": {
                "mode": deps.settings.guardrail_mode,
                "configured": deps.guardrails.configured,
                "layers": {
                    layer.name.value: layer.is_configured() for layer in deps.guardrails.layers
                },
            },
            "synthetic": deps.settings.synthetic,
            "offline_profile": OFFLINE_PROFILE_LABEL,
        }

    # -- demo sessions --------------------------------------------------------------------------

    if deps.settings.is_demo:

        @router.post("/v1/auth/patient-session", summary="Mint a local fixture patient token")
        def patient_session(body: SessionRequestBody) -> dict[str, Any]:
            """A local fixture, and the route says so.

            It exists because the demo has no identity provider and `apps/web` has nothing to log
            into. It is registered only when `settings.is_demo`, it validates no credential because
            there is none to validate, and the tokens it mints are verified for real by every route
            that accepts them. In the Cloud Run profile this route does not exist and §9's
            audience-checked service identity takes its place.
            """
            if deps.patient_issuer is None:  # pragma: no cover - demo profile always builds one
                raise NotFoundError("no patient token issuer is configured")
            if not body.patient_ref:
                raise ContractViolationError("a patient session needs a patient_ref")
            token = deps.patient_issuer.issue(
                body.subject, patient_ref=body.patient_ref, ttl_seconds=body.ttl_seconds
            )
            return {
                "access_token": token,
                "token_type": "Bearer",
                "kind": SessionKind.patient.value,
                "audience": deps.patient_verifier.audience,
                "synthetic": True,
                "fixture": True,
            }

        @router.post("/v1/auth/admin-session", summary="Mint a local fixture admin token")
        def admin_session(body: SessionRequestBody) -> dict[str, Any]:
            """The coordinator's fixture token. Separate audience, separate secret, separate route."""
            if deps.admin_issuer is None:  # pragma: no cover - demo profile always builds one
                raise NotFoundError("no admin token issuer is configured")
            if body.patient_ref:
                raise ContractViolationError(
                    "an admin session must not carry a patient_ref; the two kinds are separate (§9)"
                )
            token = deps.admin_issuer.issue(body.subject, ttl_seconds=body.ttl_seconds)
            return {
                "access_token": token,
                "token_type": "Bearer",
                "kind": SessionKind.admin.value,
                "audience": deps.admin_verifier.audience,
                "synthetic": True,
                "fixture": True,
            }

    # -- patient reads --------------------------------------------------------------------------

    @router.get(
        "/v1/workflows/{workflow_id}",
        summary="The patient's current surface",
        responses={404: {"description": "no such workflow **for this patient**", "content": None}},
    )
    def workflow_snapshot(
        workflow_id: UUID,
        session: Session = Depends(require_patient),  # noqa: B008
    ) -> dict[str, Any]:
        return deps.commands.snapshot(session, workflow_id)

    @router.get(
        "/v1/workflows/{workflow_id}/help",
        summary="The static help card (§9). Served while the guardrails are unavailable.",
    )
    def help_card(workflow_id: UUID, session: Session = Depends(require_patient)) -> dict[str, Any]:  # noqa: B008
        return deps.commands.help_card(session, workflow_id)

    # -- patient writes -------------------------------------------------------------------------

    @router.post(
        "/v1/workflows/{workflow_id}/actions",
        summary="Submit a card action (the client echoes a server-issued action)",
        responses={
            200: {"description": "the command was applied or replayed"},
            403: {"description": "policy refused it (RE006)", "content": None},
            409: {"description": "stale version (RE002) or a conflicting command (RE005)"},
        },
    )
    def submit_action(
        workflow_id: UUID,
        action: ClientAction,
        session: Session = Depends(require_patient),  # noqa: B008
    ) -> JSONResponse:
        """The web client's one write. Its body is the contract's `ClientAction`, unchanged.

        The response is a receipt, not an outcome: a 2xx means the gateway recorded the action, and
        what happens next arrives on the stream. That is why the receipt is small and why the client
        must not treat it as a confirmation.
        """
        receipt = deps.commands.submit_action(
            session, workflow_id, SubmittedAction.from_wire(action)
        )
        return _receipt(receipt)

    @router.post(
        "/v1/workflows/{workflow_id}/messages",
        summary="Free-text patient input, screened by the three guardrail layers",
        responses={
            200: {"description": "recorded, or handled as an emergency"},
            202: {"description": "accepted and not acted on (ambiguous input)"},
            422: {"description": "the guardrail blocked the input itself"},
        },
    )
    def submit_message(
        workflow_id: UUID,
        body: MessageBody,
        session: Session = Depends(require_patient),  # noqa: B008
    ) -> JSONResponse:
        receipt = deps.commands.submit_message(
            session,
            workflow_id,
            command_id=body.command_id,
            expected_state_version=body.workflow_state_version,
            text=body.text,
        )
        return _receipt(receipt)

    # -- the stream -----------------------------------------------------------------------------

    @router.get(
        "/v1/workflows/{workflow_id}/events",
        summary="The workflow's event stream (§12), replayable from an event id",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def stream_events(
        request: Request,
        workflow_id: UUID,
        session: Session = Depends(require_patient),  # noqa: B008
        last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
        last_event_id: str | None = Query(default=None),
        after_seq: int | None = Query(default=None, ge=0),
    ) -> StreamingResponse:
        """Replay, then live, then close. Never a second booking.

        The cursor is the contract's `event_id`: `Last-Event-ID` (which `EventSource` maintains by
        itself from the `id:` field) or the `last_event_id` query parameter, because which of the two
        a server honours is exactly the kind of detail a client cannot know. An unknown id replays
        from the beginning — a client that cannot be resumed is resumed from zero, because showing the
        sequence again is recoverable and showing nothing is not.

        The cursor decides **nothing about the work**: replaying events re-emits bytes that were
        already produced. That is what makes "replaying a stream must not create a second booking" a
        property of the design rather than of a client's discipline, and
        `tests/gateway/test_booking_once.py` asserts the row count to prove it.
        """
        # Scope before the response starts: once a frame is on the wire the status code is spent, so
        # every check that can fail has to happen here.
        deps.commands.snapshot(session, workflow_id)
        cursor = last_event_id_header or last_event_id
        return StreamingResponse(
            _stream(deps, request, workflow_id, cursor, after_seq),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- coordinator writes ---------------------------------------------------------------------

    @router.post(
        "/v1/workflows/{workflow_id}/transitions",
        summary="Move the workflow (coordinator). `complete: false` streams and commits nothing.",
    )
    def advance(
        workflow_id: UUID,
        body: TransitionBody,
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> JSONResponse:
        receipt = deps.commands.advance(
            session,
            workflow_id,
            command_id=body.command_id,
            expected_state_version=body.expected_state_version,
            to_state=body.to_state.value,
            reason_code=body.reason_code,
            complete=body.complete,
            props=body.props,
            partial_text=body.partial_text,
        )
        return _receipt(receipt)

    @router.post(
        "/v1/workflows/{workflow_id}/bookings",
        summary="The booking write. Refused while the guardrail dependency is unconfigured (§9).",
        responses={
            403: {"description": "policy (RE006) or approval (RE004) refused it"},
            503: {"description": "guardrails_not_configured, or booking_halted"},
        },
    )
    def book(
        workflow_id: UUID,
        body: BookingBody,
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> JSONResponse:
        # §16's kill switch, checked here and only here. Not on the stream, not on the help card,
        # not on the transitions a coordinator needs to finish or unwind work already in flight: the
        # spec says the switch stops booking *writes* and that read-only guidance continues, and a
        # switch that stopped everything would be an outage with a friendlier name.
        switch = deps.kill_switch.state
        if switch.engaged:
            raise BookingHaltedError(
                "booking writes are stopped by the admin kill switch (#5). Read-only guidance, the "
                "event stream and the workflow's own transitions keep working.",
                detail={
                    "reason": switch.reason,
                    "since": switch.since.isoformat() if switch.since else None,
                    "by": switch.by,
                },
            )
        receipt = deps.commands.book(
            session,
            workflow_id,
            command_id=body.command_id,
            approval_id=body.approval_id,
            expected_state_version=body.expected_state_version,
            booking_payload=body.booking_payload,
            screening_text=body.screening_text,
            outcome=body.outcome,
            external_ref=body.external_ref,
        )
        return _receipt(receipt)

    # -- admin reads ----------------------------------------------------------------------------

    @router.get("/v1/admin/workflows/{workflow_id}", summary="Coordinator view of one workflow")
    def admin_workflow(
        workflow_id: UUID,
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        return deps.commands.admin_view(session, workflow_id)

    # -- admin control (§16) ---------------------------------------------------------------------

    @router.get(
        "/v1/admin/control", summary="§16: the kill switch, and whether booking writes are on"
    )
    def admin_control(
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        """Readable without a telemetry cluster: the switch is gateway state, not a measurement."""
        return {
            "booking_writes": deps.kill_switch.state.as_dict(),
            "read_only_guidance": "available",
            "note": (
                "The switch stops booking writes and nothing else — the help card, the event stream "
                "and a workflow's own transitions keep working (§16)."
            ),
        }

    @router.post(
        "/v1/admin/control/booking-writes", summary="§16: engage or release the kill switch"
    )
    def admin_set_booking_writes(
        body: ControlSwitchBody,
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        """`engage: false` releases it. Both directions are explicit rather than a toggle.

        A toggle route would be one an operator could double-submit, and the second submission would
        silently re-open the switch an incident closed. Saying which direction you mean costs one
        field and removes that failure.
        """
        now = deps.clock.now()
        state = (
            deps.kill_switch.engage(reason=body.reason, by=body.by, now=now)
            if body.engage
            else deps.kill_switch.release(by=body.by, now=now)
        )
        return {"booking_writes": state.as_dict(), "subject": session.subject}

    @router.get("/v1/admin/metrics", summary="Measured latencies and guardrail layers")
    def metrics(session: Session = Depends(require_admin)) -> dict[str, Any]:  # noqa: B008
        """Both latencies, separately, and the guardrail layers that actually ran.

        §12: "progress latency is measured separately from final-response latency". They are two
        fields here and they are never averaged together; §9's per-layer latency comes from the
        verdicts the pipeline produced, so a layer with no implementation reports nothing rather
        than a zero that would read as instant.
        """
        return {
            "latency": deps.latencies.report(),
            "guardrails": {
                **layer_report(deps.guardrails.outcomes_seen),
                "configured": deps.guardrails.configured,
                "mode": deps.settings.guardrail_mode,
                "layer_order": [layer.name.value for layer in deps.guardrails.layers],
            },
            "config": deps.settings.describe(),
            "subject": session.subject,
            "trace_id_header": TRACE_ID_HEADER,
            "offline_profile": OFFLINE_PROFILE_LABEL,
            "synthetic": True,
        }

    # -- the admin read API (§12, ADR-0039) ------------------------------------------------------
    #
    # Every route below answers the same shape, and the shape is the point. A reader that answered
    # zeroes when it had no cluster would be indistinguishable from one that looked and found
    # nothing — §12 forbids a fabricated chart, and "we did not look" presented as "nothing
    # happened" is one. `unavailable` carries the reason instead, and every measurement inside a
    # live answer carries its own sample count, so a chart cannot show a number without how many
    # things it came from.

    def _with_window(payload: dict[str, Any], query: AdminQuery) -> dict[str, Any]:
        """Attach the resolved window to a *live* answer, and leave an unavailable one alone.

        The distinction is load-bearing. A page that could not read anything must not report which
        window it did not read — "available: false" with a window beside it is a shape a view could
        reasonably render as "we looked at today and found nothing", which is the fabricated zero
        §12 forbids, arrived at by decoration rather than by a wrong number. Doing it in one place is
        what stops four routes from disagreeing about it.
        """
        if payload.get("available") is not True:
            return payload
        return {**payload, "window": query.window.as_dict(query.resolved)}

    @router.get("/v1/admin/agents", summary="§12 Agents & Versions: the §15 dimensions in use")
    def admin_agents(
        session: Session = Depends(require_admin),  # noqa: B008
        query: AdminQuery = Depends(admin_query),  # noqa: B008
    ) -> dict[str, Any]:
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return _with_window(reads.agents(query.resolved, query.filters), query)

    @router.get("/v1/admin/traces", summary="§12 Trace Explorer: recent traces")
    def admin_traces(
        session: Session = Depends(require_admin),  # noqa: B008
        query: AdminQuery = Depends(admin_query),  # noqa: B008
    ) -> dict[str, Any]:
        """The trace list takes the window and the synthetic label, and no other §12 dimension.

        §15 stores that label at a different path on a span than on an event, so the event filter set
        does not apply to this family. Accepting `agents=` here and ignoring it would be worse than
        not offering it, and the page offers the two that do apply.
        """
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return _with_window(
            reads.traces(query.resolved, synthetic=_synthetic_flag(query.filters)), query
        )

    @router.get("/v1/admin/traces/{trace_id}", summary="§12 Trace Explorer: one trace's spans")
    def admin_trace(
        trace_id: str,
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        """No window: a trace is identified exactly, and windowing it would cut off its root span."""
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return reads.trace(trace_id)

    @router.get(
        "/v1/admin/scenarios", summary="§12 Fault Scenarios: §20's eight, and what each proves"
    )
    def admin_scenarios(
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        """Read from the fixtures, so this answers with or without a telemetry cluster.

        Every other `/v1/admin/*` route is a telemetry read and reports "no source" when there is no
        cluster. This one is not: the scenarios are a committed fixture, which is why it is
        deliberately not gated on `deps.telemetry`. A profile with no OpenSearch still has eight
        scenarios to describe, and hiding them behind a cluster that has nothing to do with them
        would be the wrong absence.
        """
        return load_scenarios()

    @router.get("/v1/admin/human-queue", summary="§11: the operator queue, oldest first")
    def admin_human_queue(
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        """Reads the handoff table directly; the *actions* live in the workers service.

        The read is `HandoffStore.open_tickets`, which the workers' `queued_handoffs` also uses, so
        the dashboard and the operator see one queue rather than two queries that happen to agree
        today. Claiming, releasing and resolving are deliberately absent: §11's races are decided by
        the database's own compare-and-set in the worker's code path, and a second implementation
        here would be a second arbiter.
        """
        if deps.settings.control_backend != "postgres":
            return unavailable(
                "this gateway is running the in-memory control profile, which has no handoff table. "
                "A handoff is a row (§11), and an empty queue would read as an idle operator rather "
                "than as a profile without the store."
            )
        return open_tickets(deps.settings.database_url)

    @router.get(
        "/v1/admin/alerts", summary="§16: the alert rules, their thresholds, and the switch"
    )
    def admin_alerts(
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        """Rules and thresholds from the alerts package; the switch from this process.

        No telemetry cluster is consulted, and none is needed. Nothing has been *evaluated*, because
        the scheduler that would do it on an interval is the remaining work in Phase 7.3 — and the
        answer says so rather than returning an empty list of firing alerts, which reads as a system
        with nothing wrong.
        """
        return {**load_alert_rules(), "kill_switch": deps.kill_switch.state.as_dict()}

    @router.get(
        "/v1/admin/overview", summary="§12 Overview: counts, outcomes, latencies, index health"
    )
    def admin_overview(
        session: Session = Depends(require_admin),  # noqa: B008
        query: AdminQuery = Depends(admin_query),  # noqa: B008
    ) -> dict[str, Any]:
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return _with_window(reads.overview(query.resolved, query.filters), query)

    @router.get("/v1/admin/guardrails", summary="§9 per-layer guardrail decisions, counted")
    def admin_guardrails(
        session: Session = Depends(require_admin),  # noqa: B008
        query: AdminQuery = Depends(admin_query),  # noqa: B008
    ) -> dict[str, Any]:
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return _with_window(reads.guardrails(query.resolved, query.filters), query)

    @router.get("/v1/admin/feedback", summary="§12 Feedback: raw count, like rate, participation")
    def admin_feedback(
        session: Session = Depends(require_admin),  # noqa: B008
        query: AdminQuery = Depends(admin_query),  # noqa: B008
    ) -> dict[str, Any]:
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return _with_window(reads.feedback(query.resolved, query.filters), query)

    @router.get("/v1/admin/evaluations", summary="§14 scores, per run and metric")
    def admin_evaluations(
        session: Session = Depends(require_admin),  # noqa: B008
        query: AdminQuery = Depends(admin_query),  # noqa: B008
    ) -> dict[str, Any]:
        reads = deps.telemetry
        if reads is None:
            return unavailable(NO_TELEMETRY_SOURCE)
        return _with_window(reads.evaluations(query.resolved, query.filters), query)

    return router


# ---------------------------------------------------------------------------
# The stream itself
# ---------------------------------------------------------------------------


async def _stream(
    deps: GatewayDeps,
    request: Request,
    workflow_id: UUID,
    cursor_event_id: str | None,
    after_seq: int | None,
) -> AsyncIterator[str]:
    """Frames: the replay, then anything new, then close.

    The hold is bounded by wall time (`time.monotonic`), not by the injected clock. The injected clock
    is a *measurement* clock — `ManualClock` only moves when something reads it — so a hold driven by
    it would be a loop whose exit condition depends on the iteration count, which is a hang waiting
    for a slow afternoon. A connection timeout is about wall time, so wall time is what decides it.

    Disconnects are detected rather than assumed, so a browser that navigates away does not leave a
    generator polling until the hold expires.
    """
    limit = deps.settings.stream_replay_limit
    replayed = deps.log.replay(
        workflow_id, after_event_id=cursor_event_id, after_seq=after_seq, limit=limit
    )
    known = deps.log.events_for(workflow_id)
    last_seq = (
        replayed[-1].seq
        if replayed
        else next(
            (event.seq for event in known if event.event_id == cursor_event_id),
            after_seq if after_seq is not None and not cursor_event_id else -1,
        )
    )
    if replayed:
        for frame in frames(replayed):
            yield frame

    hold_ms = deps.settings.stream_hold_ms
    if hold_ms <= 0 or any(
        event.type is StreamEventType.terminal and event.seq <= last_seq for event in known
    ):
        # The zero-hold profile drains and closes, which is what lets a test read a stream with a
        # plain request. A terminal event closes a real stream too: the outcome has arrived, and
        # holding the connection open afterwards leaves the client waiting for a sixth act.
        return

    started = time.monotonic()
    last_frame_at = started
    for _ in range(int(hold_ms / 1000.0 / _STREAM_POLL_SECONDS) + 2):
        if await request.is_disconnected():
            return
        pending = [event for event in deps.log.events_for(workflow_id) if event.seq > last_seq][
            :limit
        ]
        if pending:
            last_seq = pending[-1].seq
            for frame in frames(pending):
                yield frame
            last_frame_at = time.monotonic()
            if any(event.type is StreamEventType.terminal for event in pending):
                return
            continue
        if (time.monotonic() - started) * 1000 >= hold_ms:
            return
        if (time.monotonic() - last_frame_at) * 1000 >= deps.settings.stream_heartbeat_ms:
            last_frame_at = time.monotonic()
            yield HEARTBEAT_FRAME
        await asyncio.sleep(_STREAM_POLL_SECONDS)


def _jsonable(value: Any) -> Any:
    """Make a response body JSON-safe without changing its shape.

    UUIDs become strings and enums become their values; nothing is dropped and nothing is renamed, so
    a test can assert on the same keys the service produced.
    """
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):  # StrEnum
        return str(value.value)
    return value


def request_id_for(request: Request) -> str:
    """The request's correlation id: the caller's if it sent one, otherwise a fresh one."""
    existing = getattr(request.state, "request_id", None)
    if existing:
        return str(existing)
    return uuid4().hex


def register_error_handlers(app: FastAPI) -> None:
    """One envelope for every failure. Registered on the app, not per route.

    `RequestValidationError` is included on purpose: FastAPI's default 422 body has a different shape
    from ours, and a client that has to parse two failure shapes will parse one of them wrong.
    """

    def _envelope(
        request: Request, status_code: int, error: GatewayError | DomainError
    ) -> JSONResponse:
        request_id = request_id_for(request)
        content = to_envelope(error, request_id=request_id)
        headers = dict(getattr(error, "headers", {}) or {})
        headers[REQUEST_ID_HEADER] = request_id
        return JSONResponse(status_code=status_code, content=_jsonable(content), headers=headers)

    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        return _envelope(request, error.status_code, error)

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, error: DomainError) -> JSONResponse:
        from referraleasy_gateway.errors import refusal_status

        return _envelope(request, refusal_status(error), error)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request, error: RequestValidationError) -> JSONResponse:
        violation = ContractViolationError(
            "the request body is not one the frozen contract could have produced (§1.1)",
            detail={"errors": _jsonable(error.errors())},
        )
        return _envelope(request, violation.status_code, violation)
