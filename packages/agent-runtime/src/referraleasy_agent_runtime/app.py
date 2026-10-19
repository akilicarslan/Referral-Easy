"""One image, seven services: ``AGENT_ROLE`` decides which one this process is.

The seven agent services are meant to be thin, so the runtime owns the HTTP
surface: authentication at the transport, a turn budget per request, result
shaping, and a health endpoint. A service supplies *only* a handler — what to do
with a task — and gets everything else for free:

    # services/agents/provider_matching/__main__.py
    from referraleasy_agent_runtime import main

    raise SystemExit(main(handler_factory=build_handler))

``main`` reads ``AGENT_ROLE``, resolves the model, the caps and the service token,
and either serves or — with ``--check`` — prints the release-manifest entry it
would serve with. ``--check`` imports no model SDK and opens no socket, so it is
safe to run as a container preflight.

Failure is not an HTTP code here. A turn that ends in ``outcome_unknown`` is a 200
with a protocol state of ``failed`` *and* the application outcome beside it: A2A
carries outcomes in task states, and a caller that read only the HTTP status would
learn nothing.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from typing import Any, Final, Protocol

from fastapi import FastAPI

from referraleasy_agent_runtime.auth import (
    ServicePrincipal,
    drop_untrusted_identity,
    service_auth_dependency,
)
from referraleasy_agent_runtime.budget import TurnBudget
from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.results import envelope, outcome_class, project_failure
from referraleasy_agent_runtime.settings import AgentSettings, resolve_port
from referraleasy_agent_runtime.suspend import Suspension
from referraleasy_contracts import AgentTaskRequest, AgentTaskResult
from referraleasy_telemetry import (
    LatencyKind,
    Telemetry,
    TelemetryEmitter,
    configure_telemetry,
    install_server_spans,
)

__all__ = [
    "HEALTH_PATH",
    "MANIFEST_PATH",
    "TASKS_PATH",
    "AgentTaskRequest",
    "TaskHandler",
    "TurnReport",
    "asgi_app",
    "create_app",
    "main",
    "preflight",
]

HEALTH_PATH: Final = "/healthz"
MANIFEST_PATH: Final = "/manifest"
TASKS_PATH: Final = "/tasks"


# `AgentTaskRequest` moved to `referraleasy_contracts.a2a` when the gateway became a second caller —
# a wire contract with a caller and a callee belongs in the contracts, not in the module that happens
# to serve it. Re-exported here because this is still the import site a role handler and the tests
# reach for, and moving the definition should not also be an import migration.


@dataclass(frozen=True, slots=True)
class TurnReport:
    """What a handler returns: the result, the effective skills, and its trace notes.

    The effective skill list travels with the result so the response and the trace
    record what the turn actually loaded (see `skills.SkillSelection.trace_entry`).
    """

    result: AgentTaskResult
    skills: tuple[str, ...] = ()
    trace: Mapping[str, Any] = field(default_factory=dict)
    suspension: Suspension | None = None
    output: Mapping[str, Any] | None = None


class TaskHandler(Protocol):
    """The seam a thin agent service implements. Everything else is the runtime's."""

    async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
        """Run one task within ``budget``. Must not mint its own time or quota."""
        ...


def create_app(
    settings: AgentSettings,
    *,
    handler: TaskHandler | None = None,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    """Build the ASGI app for ``settings.role``.

    ``handler`` is required to serve tasks. Without one the app still starts,
    reports its health and its manifest, and refuses tasks with a typed
    ``terminal_error`` — a container that is up but not wired is easier to spot
    than one that will not boot.

    ``telemetry`` is injected rather than configured here, for the same reason the
    gateway's is: this module builds the app, and a process-wide tracer provider is
    a process concern. ``main`` is what calls ``configure_telemetry``. The server
    span it installs is how an A2A call arrives already inside the caller's trace —
    §15's whole reason for propagating rather than correlating.
    """
    verifier = settings.verifier()
    require_principal = service_auth_dependency(verifier)

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # One SERVER span per request, from the shared implementation rather than a copy: an inbound A2A
    # call carries `traceparent` in its metadata, and extraction is what puts this agent's span
    # *inside* the coordinator's trace rather than beside it. Without it every agent would look like
    # the root of its own trace.
    install_server_spans(app, telemetry)

    @app.get(HEALTH_PATH)
    async def healthz() -> dict[str, str]:
        """Liveness. Unauthenticated on purpose: a probe has no token."""
        return {"status": "ok", "role": settings.role.value, "service": settings.service_name}

    @app.get(MANIFEST_PATH)
    async def manifest(
        principal: ServicePrincipal = _principal_dependency(require_principal),  # noqa: B008
    ) -> dict[str, Any]:
        """The recordable release-manifest entry, guarded like every other route."""
        return {**settings.manifest_entry(), "caller": principal.as_trace_entry()}

    @app.post(TASKS_PATH)
    async def run_task(
        request: AgentTaskRequest,
        principal: ServicePrincipal = _principal_dependency(require_principal),  # noqa: B008
    ) -> dict[str, Any]:
        """Run one task under a fresh turn budget and return a shaped envelope."""
        return await _run_task(settings, handler, request, principal, telemetry)

    return app


def _principal_dependency(require_principal: Any) -> Any:
    """The `Depends(...)` marker for the auth dependency, built per app.

    This *is* a call in an argument default, which B008 flags as a rule — and the
    obvious "fix" is worse. Because this module uses ``from __future__ import
    annotations``, FastAPI resolves parameter annotations against *module* globals,
    so an ``Annotated[ServicePrincipal, Depends(x)]`` whose ``x`` is a closure local
    is not resolvable and silently degrades into a required query parameter (a 422,
    not a 401) — verified against FastAPI 0.141. The dependency must therefore travel
    as the default value, where FastAPI can see the real object. The guard is not
    left implicit: ``test_the_manifest_route_is_guarded_like_the_task_route`` fails
    if this is ever rewritten into the broken form.
    """
    from fastapi import Depends

    return Depends(require_principal)


async def _run_task(
    settings: AgentSettings,
    handler: TaskHandler | None,
    request: AgentTaskRequest,
    principal: ServicePrincipal,
    telemetry: Telemetry | None = None,
) -> dict[str, Any]:
    emitter = telemetry.emitter if telemetry is not None else None
    trace: dict[str, Any] = {
        "trace_id": request.trace_id,
        "role": settings.role.value,
        "caller": principal.as_trace_entry(),
        "workflow_state": request.workflow_state,
    }
    # Identity from a payload is a claim, not evidence: it is dropped, and the drop
    # is recorded so an inconsistency is visible rather than silently ignored.
    _, dropped = drop_untrusted_identity(request.payload)
    if dropped:
        trace["dropped_untrusted_identity"] = sorted(dropped)

    budget = TurnBudget(settings.caps, role=settings.role)
    if handler is None:
        return _response(
            trace,
            envelope(
                project_failure(
                    ConfigurationError(
                        f"{settings.service_name} has no task handler wired; refusing the task "
                        "rather than answering with a plausible-looking result."
                    )
                ),
                trace_id=request.trace_id,
            ),
            budget,
        )

    if emitter is not None:
        # §15's **logical** trigger, and the word is the whole specification: an invocation that
        # succeeded on its third attempt was triggered once. The discriminator is the request's
        # idempotency key, which is stable across a caller's retries of one task and distinct
        # between two delegations of the same role — so `derive_event_id` collapses the retries into
        # one id and `cardinality(event.id)` counts one trigger, without this layer having to know
        # which request is a retry. Emitted before the work, because a trigger recorded only on
        # success would under-count exactly the invocations that failed.
        emitter.agent_trigger(
            agent_role=settings.role.value,
            discriminator=request.idempotency_key,
        )

    with _invocation(emitter, settings) as info:
        try:
            declared = request.payload.get("task_envelope")
            if isinstance(declared, Mapping):
                budget = TurnBudget.inherited(settings.caps, declared, role=settings.role)
                budget.require_time()
            report = await handler(request, budget)
        except Exception as error:
            # A typed runtime failure brings its own projection; anything else is
            # terminal_error. Neither is ever advertised as retryable by accident.
            info["outcome"] = outcome_class(project_failure(error).outcome)
            return _response(
                trace, envelope(project_failure(error), trace_id=request.trace_id), budget
            )

        info["outcome"] = outcome_class(report.result.outcome)
        trace["skills"] = list(report.skills)
        trace.update(report.trace)
        if report.suspension is not None:
            trace["suspension"] = {
                "id": report.suspension.id,
                "reason": str(report.suspension.reason),
            }
        return _response(
            trace,
            envelope(
                report.result,
                trace_id=request.trace_id,
                skills=report.skills,
                extra={
                    **({"suspension_id": report.suspension.id} if report.suspension else {}),
                    **({"output": dict(report.output)} if report.output is not None else {}),
                },
            ),
            budget,
        )


@contextmanager
def _invocation(
    emitter: TelemetryEmitter | None, settings: AgentSettings
) -> Iterator[dict[str, Any]]:
    """§15's **invocation** — one agent's work on one task, attempts and tool calls included.

    `LatencyKind` is what says which of §15's three distributions this is, and an invocation is the
    middle one: an attempt is one try, a turn is the conversational unit the *gateway* measures, and
    this is everything between them. Recording all three under one kind would make the p95 that
    points at a slow dependency indistinguishable from the one that points at retries.

    `attempt_span` rather than a start/stop pair, because it emits from a `finally`: an invocation
    that raises still contributes its latency, which is §15's rule that timeouts are not quietly
    removed from the distribution. The yield is a dict the body fills in, so the outcome is known
    before the record is written on the way out.
    """
    if emitter is None:
        # The unit suite's app: no collector, no records, and no branch in the body either.
        yield {}
        return
    with emitter.attempt_span(
        agent_role=settings.role.value,
        number=1,
        latency_kind=LatencyKind.INVOCATION,
    ) as info:
        yield info


def _response(trace: Mapping[str, Any], body: dict[str, Any], budget: TurnBudget) -> dict[str, Any]:
    trace = {**trace, "budget": budget.snapshot().as_trace_entry()}
    return {"trace": dict(trace), "result": body}


def preflight(settings: AgentSettings, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What ``main --check`` prints: the resolved deployment, without side effects.

    ``env`` is threaded through so the port is read from the *same* environment the
    rest of the settings came from. Reading one value from ``os.environ`` while
    everything else comes from the caller's mapping makes a preflight that reports a
    container it would not actually start.
    """
    return {
        "service": settings.service_name,
        "role": settings.role.value,
        "app_env": settings.app_env,
        "manifest": settings.manifest_entry(),
        "port": resolve_port(env),
    }


def service_version() -> str:
    """The runtime's installed version, for §15's resource attributes.

    Read from the distribution rather than written here: a literal in this file is a number that goes
    stale silently, and a trace claiming a version the image is not makes the attribute worse than
    absent. `0.0.0` is the dataclass default and the honest answer for a tree that was never
    installed as a distribution.
    """
    try:
        return _distribution_version("referraleasy-agent-runtime")
    except PackageNotFoundError:  # pragma: no cover - the installed path is the one that runs
        return "0.0.0"


def main(
    argv: Sequence[str] | None = None,
    *,
    handler_factory: Any = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Serve the role named by ``AGENT_ROLE``, or report it with ``--check``.

    ``handler_factory`` is how a thin service injects its agent: a callable that
    receives the resolved settings and returns a :class:`TaskHandler`. Everything
    else — the role, the caps, the model, the token — is resolved here.
    """
    parser = argparse.ArgumentParser(prog="agent-runtime", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the resolved configuration and exit (no socket, no model SDK)",
    )
    parser.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="bind port")
    args = parser.parse_args(argv)

    resolved: Mapping[str, str] = env if env is not None else os.environ
    settings = AgentSettings.from_env(resolved)
    if args.check:
        preflight_report = json.dumps(preflight(settings, env=resolved), indent=2, sort_keys=True)
        print(preflight_report)
        return 0

    handler: TaskHandler | None = handler_factory(settings) if handler_factory is not None else None

    # §15's adoption seam, one process. The service name is the *role*, because the same image runs
    # as all seven and a trace that says "agent" would not tell an operator which one produced a
    # span — `service_name()` is already what `/healthz` and the manifest report.
    #
    # Inert when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset: real providers, no exporter, no attempts.
    telemetry = configure_telemetry(settings.service_name, version=service_version())
    atexit.register(telemetry.shutdown)

    app = create_app(settings, handler=handler, telemetry=telemetry)

    import uvicorn

    uvicorn.run(
        app,
        host=args.host or resolved.get("AGENT_HOST", "0.0.0.0"),
        port=args.port if args.port is not None else resolve_port(resolved),
        log_level=resolved.get("AGENT_LOG_LEVEL", "info"),
    )
    return 0


def asgi_app() -> FastAPI:
    """ASGI entry point: ``uvicorn referraleasy_agent_runtime.app:asgi_app --factory``.

    Kept handler-less on purpose: an image started without a wired handler still
    boots, reports its role, and refuses work with a typed terminal error, which is
    far easier to diagnose than a container that never becomes healthy.
    """
    return create_app(AgentSettings.from_env())
