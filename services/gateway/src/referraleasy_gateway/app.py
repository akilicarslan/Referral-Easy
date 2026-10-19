"""Wire-up: settings in, an ASGI app out. The only place the ports are chosen.

Everything the app does is decided here and nowhere else, so a reader can answer "what is actually
running?" by reading one file:

* **Control state** is chosen by `GATEWAY_CONTROL_BACKEND`. `memory` (the default) is
  `InMemoryControlState` — version-checked and command-deduplicated in-process, no database, which is
  what the offline demo and the unit job run on. `postgres` is `PostgresControlState`, where those
  same eight operations are performed against the schema and the *rules stay in SQL* (§7). The
  profile is verified before the app is returned rather than merely selected, so a deployment cannot
  start under a `postgres` label and discover on its first request that the DSN is wrong, or that
  the migrations were never applied.
* **The policy table** comes from `policy_seed`, transcription of migration 014's rows. It is data,
  and `tests/gateway/test_policy_table.py` re-derives it from the migration so the two cannot drift
  without a failure.
* **Guardrails** are the three-layer pipeline with layer 1 real and layers 2-3 unconfigured. That
  makes the booking write refuse while the static help card keeps working, which is §9's rule.
* **Sessions** are two verifiers over two secrets, minted for the demo by two fixture routes that
  exist only when `settings.is_demo`.

Three conveniences are demo-only and named as such: CORS (the browser client is served from another
origin in development), the fixture token routes, and `POST /v1/demo/workflows`, which exists because
the demo has no referral-creation flow to seed a workflow with (§18: a fixture, labelled as one).
None of them is registered when `APP_ENV` is not `demo`/`test`/`ci`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from referraleasy_contracts import CONTRACT_VERSION
from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_gateway.admin import TelemetryReads, build_reads
from referraleasy_gateway.api import GatewayDeps, build_router, register_error_handlers
from referraleasy_gateway.approval import (
    ApprovalLedger,
    ApprovalService,
    InMemoryApprovalLedger,
    PostgresApprovalLedger,
)
from referraleasy_gateway.auth import (
    JwtIssuer,
    Session,
    SessionKind,
    admin_session_dependency,
    build_verifiers,
)
from referraleasy_gateway.commands import CommandService
from referraleasy_gateway.config import OFFLINE_PROFILE_LABEL, ConfigurationError, GatewaySettings
from referraleasy_gateway.control import (
    InMemoryControlState,
    PostgresControlState,
    SeedableControlState,
)
from referraleasy_gateway.coordination import CoordinationService
from referraleasy_gateway.dispatch import CoordinatorDispatch
from referraleasy_gateway.errors import ContractViolationError
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.guardrails import GuardrailPipeline
from referraleasy_gateway.killswitch import KillSwitch
from referraleasy_gateway.policy import TablePolicy
from referraleasy_gateway.projection import WorkflowSurfaceProjection
from referraleasy_gateway.timing import Clock, LatencyLedger, SystemClock
from referraleasy_telemetry import Telemetry, install_server_spans

__all__ = ["APP_DESCRIPTION", "DemoWorkflowBody", "create_app"]

_log = logging.getLogger("referraleasy.gateway")

APP_DESCRIPTION = """
ReferralEasy's patient-facing gateway (§9, §12).

Four surfaces, and the split between them is the design:

* **patient** — a workflow snapshot, its severability-ordered card surface, the SSE stream, and the
  two writes a patient can cause (a card action and a free-text message).
* **coordinator (admin)** — the transitions route a decision layer drives, the booking write, and two
  read-only views for a human: one workflow, and the measured latencies.
* **`/healthz`** — unauthenticated, because a probe that needs a session reports the wrong outage.
* **`/v1/auth/*`** — local fixture tokens, present only in the demo profile.

Patient and admin sessions are separate: separate audiences, separate secrets, separate dependencies.
A patient token presented at a coordinator route is refused by the verifier with `403`, not by a
branch inside a handler.
"""


class DemoWorkflowBody(BaseModel):
    """A seeded workflow, for the demo profile only. Not a product endpoint.

    `patient_ref` is a bare string rather than a foreign key because in the memory profile there is no
    patient table to key against; the PostgreSQL profile's workflows are created by the referral
    intake flow instead, and this route does not exist there.
    """

    model_config = ConfigDict(extra="forbid")

    patient_ref: str = Field(min_length=1)
    ticket_ref: str = Field(default="T-DEMO-1", min_length=1)
    state: WorkflowState = WorkflowState.INTAKE
    payload: dict[str, Any] = Field(default_factory=dict)


#: The implementation each profile must be served by. A dict rather than an `if`, so
#: `_assert_profile_agrees` reads as the rule it is and a third profile has one obvious editing site.
_CONTROL_IMPLEMENTATIONS: dict[str, type] = {
    "memory": InMemoryControlState,
    "postgres": PostgresControlState,
}


@dataclass(frozen=True, slots=True)
class _Wiring:
    """The built graph, kept as one object so `create_app` reads as a list of steps."""

    deps: GatewayDeps
    control: SeedableControlState
    patient_issuer: JwtIssuer | None
    admin_issuer: JwtIssuer | None
    coordination: CoordinationService | None = None


def create_app(
    *,
    settings: GatewaySettings | None = None,
    env: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    control: SeedableControlState | None = None,
    guardrails: GuardrailPipeline | None = None,
    telemetry: Telemetry | None = None,
    admin_reads: TelemetryReads | None = None,
) -> FastAPI:
    """Build the app.

    `telemetry` is injected rather than configured here for the same reason `clock` is: this module
    builds the *app*, and a process-wide tracer provider is a process concern. `__main__` is what
    calls `configure_telemetry` and hands the bundle in, so a test that builds an app gets no
    provider installed and no export attempted — and the shipped image, which has
    `OTEL_EXPORTER_OTLP_ENDPOINT` set, gets one.

    `settings`, `clock`, `control` and `guardrails` are injectable so a test can pin a fixture secret,
    move time by hand, seed a workflow, and put real layers behind the guardrail seam — the last one
    is how the *write* path is exercised, since the shipped profile deliberately leaves the two model
    layers unconfigured and therefore refuses a booking. Production passes nothing and gets the
    environment, the system clock, and the unconfigured seam.

    An injected `control` overrides the profile, which is how the tests build one app over the
    memory store and one over the database without a branch anywhere else. It has to be the store the
    settings name — see `_assert_profile_agrees` — because a *half*-applied profile is the same lie
    as a `postgres` label over a dictionary, only harder to see.
    """
    resolved = settings or GatewaySettings.from_env(env)
    _assert_profile_agrees(resolved, control)
    the_clock: Clock = clock or SystemClock()
    wiring = _wire(
        resolved,
        clock=the_clock,
        control=control,
        guardrails=guardrails,
        admin_reads=admin_reads,
        telemetry=telemetry,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if wiring.coordination is not None:
            wiring.coordination.start()
        try:
            yield
        finally:
            if wiring.coordination is not None:
                wiring.coordination.close()

    app = FastAPI(
        title="ReferralEasy Gateway",
        version=CONTRACT_VERSION,
        description=APP_DESCRIPTION,
        openapi_url="/openapi.json",
        docs_url="/docs",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    # Telemetry is installed *before* the request ids so that it ends up **inside** them: Starlette
    # applies the middleware list outermost-first and `add_middleware` prepends, so the one added
    # last is the one that wraps the others. The span wants the request id on it, and that id is
    # minted by the middleware added next.
    install_server_spans(app, telemetry)
    _install_request_ids(app)
    if resolved.is_demo:
        _install_demo_cors(app)
    app.include_router(build_router(wiring.deps))
    if resolved.is_demo:
        _install_demo_routes(app, wiring)

    _log.info("gateway configured: %s", resolved.describe())
    return app


def _wire(
    settings: GatewaySettings,
    *,
    clock: Clock,
    control: SeedableControlState | None = None,
    guardrails: GuardrailPipeline | None = None,
    admin_reads: TelemetryReads | None = None,
    telemetry: Telemetry | None = None,
) -> _Wiring:
    """Build every port once, in dependency order, with nothing chosen twice."""
    events = EventLog(synthetic=settings.synthetic)
    approvals = ApprovalService(_approval_ledger(settings), clock=clock)
    latencies = LatencyLedger()
    guardrails = guardrails or GuardrailPipeline.from_settings(
        settings, clock, emitter=telemetry.emitter if telemetry is not None else None
    )
    policy = TablePolicy.from_seed()
    state = control or _control_state(settings)
    surfaces = WorkflowSurfaceProjection(state, synthetic=settings.synthetic)
    kill_switch = KillSwitch()
    dispatcher = _dispatcher(settings, log=events, clock=clock, telemetry=telemetry)
    commands = CommandService(
        control=state,
        surfaces=surfaces,
        log=events,
        approvals=approvals,
        policy=policy,
        guardrails=guardrails,
        clock=clock,
        latencies=latencies,
        synthetic=settings.synthetic,
        # The *write* side of §15, which this service did not have. `telemetry` reaches the app for
        # spans and the admin pages for reads; without this the gateway recorded a turn's latency in
        # its own ledger and told the telemetry path nothing, which is why `latency.turn.ms` had no
        # writer and the Feedback page had no denominator.
        emitter=telemetry.emitter if telemetry is not None else None,
        dispatch=dispatcher,
        kill_switch=kill_switch,
    )
    patient_verifier, admin_verifier = build_verifiers(settings, clock)

    patient_issuer: JwtIssuer | None = None
    admin_issuer: JwtIssuer | None = None
    if settings.is_demo:
        # The fixtures exist to be *verified* by the routes above: the same verifier that refuses a
        # tampered token is the one that accepts these. A fixture issuer that minted something the
        # verifier would not accept would test nothing.
        patient_issuer = JwtIssuer(
            settings.patient_secret,
            kind=SessionKind.patient,
            audience=settings.patient_audience,
            issuer=settings.jwt_issuer,
            ttl_seconds=settings.session_ttl_seconds,
            clock=clock,
        )
        admin_issuer = JwtIssuer(
            settings.admin_secret,
            kind=SessionKind.admin,
            audience=settings.admin_audience,
            issuer=settings.jwt_issuer,
            ttl_seconds=settings.session_ttl_seconds,
            clock=clock,
        )

    deps = GatewayDeps(
        kill_switch=kill_switch,
        settings=settings,
        clock=clock,
        control=state,
        log=events,
        approvals=approvals,
        policy=policy,
        guardrails=guardrails,
        commands=commands,
        latencies=latencies,
        patient_verifier=patient_verifier,
        admin_verifier=admin_verifier,
        patient_issuer=patient_issuer,
        admin_issuer=admin_issuer,
        telemetry=admin_reads if admin_reads is not None else build_reads(settings),
    )
    return _Wiring(
        deps=deps,
        control=state,
        patient_issuer=patient_issuer,
        admin_issuer=admin_issuer,
        coordination=(
            CoordinationService(
                control=state, commands=commands, coordinator=dispatcher, clock=clock
            )
            if dispatcher is not None
            else None
        ),
    )


def _dispatcher(
    settings: GatewaySettings,
    *,
    log: EventLog,
    clock: Clock,
    telemetry: Telemetry | None,
) -> CoordinatorDispatch | None:
    """The coordinator dispatcher, or `None` when this deployment has nothing to reach.

    `None` is a supported state and the unit suite's, exactly as it is for `build_reads`: an
    environment with no `A2A_BASE_URL` records commands and dispatches nothing, and the command's own
    receipt says so rather than the patient being told a coordinator is on it. Returning the object
    unconditionally and failing at call time would move the same fact to a place nobody reads.
    """
    if not (settings.a2a_base_url and settings.a2a_service_token):
        _log.info(
            "A2A_BASE_URL or A2A_SERVICE_TOKEN is unset, so no committed message will be dispatched "
            "to the coordinator. Commands are still recorded and answered."
        )
        return None
    if not settings.release_manifest_id:
        # A different kind of "cannot dispatch" from the one above: a coordinator *is* configured,
        # but every task must carry a §13 release pin and none can be named. Sending the task
        # anyway would mean inventing one, or sending an envelope the agent refuses.
        _log.warning(
            "a coordinator is configured but no release can be named: set RELEASE_MANIFEST_ID or "
            "build registry/releases/latest.json. No committed message will be dispatched — a task "
            "is pinned to a release (§13) or it is not sent."
        )
        return None
    return CoordinatorDispatch(
        base_url=settings.a2a_base_url,
        token=settings.a2a_service_token,
        release_manifest_id=settings.release_manifest_id,
        log=log,
        clock=clock,
        emitter=telemetry.emitter if telemetry is not None else None,
    )


def _install_demo_cors(app: FastAPI) -> None:
    """Cross-origin access for the development profile, and only there.

    `apps/web` is served from another origin in development (Vite on :5173, this service on :8080), so
    without this the browser client cannot read a single response. Credentials stay off: the session
    is a bearer token, never a cookie, so there is nothing for `allow_credentials` to protect and no
    reason to reflect an origin. `Last-Event-ID` is an allowed *request* header because that is how a
    resumed `EventSource` in a reconnecting client presents its cursor.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-request-id", "last-event-id"],
        expose_headers=["x-request-id"],
    )


# The span middleware itself lives in `referraleasy_telemetry.middleware`, not here: the seven
# agent services need the same four behaviours (extraction, ending on error, isolation, and
# recording the route but not the payload), and two copies of that would drift — which is the
# lesson the guardrail layers already taught this repository. `install_server_spans` is imported
# above and called from `create_app`.


def _install_request_ids(app: FastAPI) -> None:
    """Give every request an id before anything else runs.

    It is stored on `request.state` rather than returned, so the error handlers put the *same* id in
    the envelope that the response header carries. A trace id is correlation only: nothing in this
    service reads it as an identity, and `tests/gateway/test_sessions.py` pins that a request carrying
    one and no credential is still refused.
    """

    @app.middleware("http")
    async def _request_id(request: Any, call_next: Any) -> Any:
        supplied = request.headers.get("x-request-id")
        request.state.request_id = (
            supplied.strip() if supplied and supplied.strip() else uuid4().hex
        )
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response


def _install_demo_routes(app: FastAPI, wiring: _Wiring) -> None:
    """The fixture route that seeds a workflow. Registered only in the demo profile.

    A demo has to start somewhere, and §18 asks for the starting point to be labelled rather than
    implied: the response says `synthetic` and `fixture`, and the label is on `/healthz` too. It needs
    a coordinator session — seeding is not a patient action, and a patient who could create their own
    workflow could also choose its `patient_ref`.
    """
    deps = wiring.deps
    require_admin = admin_session_dependency(deps.admin_verifier)

    @app.post("/v1/demo/workflows", summary="Seed a workflow (demo profile only)", tags=["demo"])
    def seed_workflow(
        body: DemoWorkflowBody,
        session: Session = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        if session.patient_ref is not None:  # pragma: no cover - the admin dependency refuses first
            raise ContractViolationError("seeding is a coordinator action")
        row = wiring.control.create(
            body.patient_ref,
            ticket_ref=body.ticket_ref,
            state=body.state.value,
            payload=_demo_payload(body),
        )
        return {
            "workflow_id": str(row.workflow_id),
            "ticket_ref": row.ticket_ref,
            "patient_ref": row.patient_ref,
            "state": row.state,
            "state_version": row.state_version,
            "seeded_by": session.subject,
            "fixture": True,
            "synthetic": True,
            "offline_profile": OFFLINE_PROFILE_LABEL,
        }


def _demo_payload(body: DemoWorkflowBody) -> dict[str, Any]:
    from referraleasy_gateway.referral_sources import demo_sources

    sources = demo_sources(body.patient_ref)
    return {**({"source_data": sources} if sources else {}), **body.payload}


def _assert_profile_agrees(settings: GatewaySettings, control: SeedableControlState | None) -> None:
    """Refuse a control state that is not the one `GATEWAY_CONTROL_BACKEND` selected.

    This looks like a test convenience and is not one. An injected store that disagrees with the
    settings produces a *half*-applied profile: the workflows live in PostgreSQL while the approval
    ledger §9's guard consults is still a dictionary. The transport then looks correct — consent
    returns 200, the ledger holds a live binding — and the database refuses the transition with
    "no live approval", which reads as a product bug and is a wiring mistake. The failure is worth
    refusing at startup for the same reason the profile itself is: it is invisible from either half.
    """
    if control is None:
        return
    expected = _CONTROL_IMPLEMENTATIONS[settings.control_backend]
    if not isinstance(control, expected):
        raise ConfigurationError(
            f"{settings.control_backend} profile but the injected control state is "
            f"{type(control).__name__}; every port has to come from one profile, or the store and "
            "the approval ledger disagree about which one is running."
        )


def _approval_ledger(settings: GatewaySettings) -> ApprovalLedger:
    """Where consent is kept: `approval` in the postgres profile, a dictionary in memory.

    Named in the same file as the control state, and for the same reason — this is the only place a
    port is chosen, so "what is actually running?" is one file's worth of reading. It is also the
    same kind of choice and the same kind of refusal: §9's binding is only structural when a database
    holds it, because `approval_is_live` is what decides whether a workflow may enter
    `BOOKING_IN_FLIGHT`. A ledger under a `postgres` label that was really a dictionary would be that
    lie, so the two move together and neither falls back to the other.
    """
    if settings.control_backend == "postgres":
        return PostgresApprovalLedger(settings.database_url)
    return InMemoryApprovalLedger()


def _control_state(settings: GatewaySettings) -> SeedableControlState:
    """The store `GATEWAY_CONTROL_BACKEND` names, and the only place the two profiles are told apart.

    One branch, and no fallback between them: `memory` never opens a socket, and `postgres` never
    answers from a dictionary. A profile that could quietly become the other is the failure this
    function exists to make impossible, which is also why `GatewaySettings.__post_init__` validates
    the name before this is ever reached.

    The PostgreSQL branch then makes the profile *prove it can serve*, before any request arrives.
    Two things a name check cannot see produce the worst available failure — a process that starts,
    reports healthy and fails every request: a DSN pointing at a server that is not there, and a
    database whose migrations were never applied. Both are a refusal to start instead
    (`PostgresControlState.ping`).

    This takes the place of the `refuse_unimplemented_profiles` that stood here while the adapter was
    unwritten. It is the same mechanism for the same reason — a `postgres` label must never run on
    something that is not PostgreSQL — asked of the database rather than of a hard-coded list of
    profiles, and it keeps working for a profile nobody has written yet, because a new one either
    answers or it does not.
    """
    if settings.control_backend != "postgres":
        return InMemoryControlState()

    state = PostgresControlState(settings.database_url, worker_id=settings.resolved_worker_id)
    try:
        state.ping()
    except Exception as exc:
        raise ConfigurationError(
            f"GATEWAY_CONTROL_BACKEND=postgres cannot serve: {exc}. The gateway refuses to start "
            "rather than report healthy and fail every request (§7)."
        ) from exc
    return state
