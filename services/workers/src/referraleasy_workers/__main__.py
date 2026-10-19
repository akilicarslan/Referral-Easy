"""`python -m referraleasy_workers <command>` — the two loops a supervisor would run (§7).

The entry point is deliberately thin. Everything it can do is a call to a class that is tested
without it, and it exists so that a deployment has *something* to run rather than so that behaviour
lives here:

* ``outbox`` — drain the notification queue until it is empty or the batch ceiling is reached, then
  sleep and drain again. One pass per transaction: the enqueue happened inside a booking's
  transaction, and the delivery happening outside it is what keeps a bounced message from rolling a
  booking back (invariant #11).
* ``reconcile <workflow-id> <idempotency-key>`` — resolve an unknown booking outcome under the
  original key, once, printing the conclusion. It is a *one-shot* on purpose: the workflow's own
  lease decides who may act, and a standalone loop would be a second owner for the same rows.

Nothing here makes a model call, and nothing here reads a credential from anywhere but the
environment. The commands exit non-zero on a configuration problem, because a worker that starts
without a database URL and then idles is worse than one that fails loudly.
"""

from __future__ import annotations

import asyncio
import atexit
import sys
from collections.abc import Sequence

from referraleasy_agent_runtime import ConfigurationError
from referraleasy_telemetry import Telemetry, TelemetryEmitter, configure_telemetry
from referraleasy_workers.booking import HttpBookingApi
from referraleasy_workers.db import connection
from referraleasy_workers.hitl import HumanQueue, queued_handoffs
from referraleasy_workers.outbox import HttpNotificationSender, OutboxWorker
from referraleasy_workers.reconcile import BookingReconciler
from referraleasy_workers.settings import WorkerSettings

_USAGE = """usage: python -m referraleasy_workers <command> [args]

commands:
  outbox                 drain the notification outbox until it is empty (repeats with --watch)
  reconcile <key>        resolve an unknown booking outcome under the idempotency key given
  handoff list           the operator queue: live tickets, oldest first
  handoff resolve <id>   close a ticket and return its workflow to an allow-listed state
                         --operator <id> --generation <n> --return-state <STATE>
                         [--resolution <text>] [--note <text>]

environment:
  DATABASE_URL                 the control-state database (required)
  BOOKING_API_URL              booking API base URL (required for `reconcile`)
  NOTIFICATION_URL             notification API base URL (required for `outbox`)
  WORKER_ID                    defaults to <hostname>:<pid>
"""


async def _outbox_once(settings: WorkerSettings) -> int:
    sender = HttpNotificationSender(
        settings.require_notification_url(), token=settings.notification_token, caps=settings.caps
    )
    try:
        with connection(settings) as conn:
            worker = OutboxWorker(
                conn, worker_id=settings.worker_id, sender=sender, settings=settings
            )
            report = await worker.run_once()
    finally:
        await sender.aclose()
    print(
        f"outbox: batches={report.report.batches} claimed={report.report.claimed} "
        f"delivered={report.report.delivered} retried={report.report.retried} "
        f"dead={report.report.dead} lost={report.report.lost} "
        f"retry_after_honoured={report.retry_after_honoured}"
    )
    for note in report.notes:
        print(f"  {note}")
    return 0


async def _reconcile_once(settings: WorkerSettings, key: str) -> int:
    """Resolve one key. No database connection: a reconciliation is a lookup and a conclusion.

    Recording that conclusion onto a workflow is the *pipeline's* job, under the workflow's lease —
    which is why this command prints its answer instead of writing one.
    """
    caps = settings.caps
    api = HttpBookingApi(
        settings.require_booking_api_url(), token=settings.booking_api_token, caps=caps
    )
    try:
        reconciler = BookingReconciler(
            api=api, caps=caps, lookup_budget=settings.reconcile_lookup_budget
        )
        outcome = await reconciler.reconcile(key=key)
    finally:
        await api.aclose()
    print(f"{outcome.status.value}: {outcome.detail}")
    print(f"  key: {outcome.key} (never changed: {outcome.key_never_changed})")
    print(f"  lookups: {outcome.attempts_summary()}")
    print(f"  next: {outcome.action.value}")
    return 0 if outcome.is_resolved else 1


def _handoff_list(settings: WorkerSettings) -> int:
    """The operator queue, as an operator sees it.

    The first caller of `HumanQueue`'s operator half. `claim`, `release`, `resolve`, `inspect` and
    the annotation drafts all existed with nothing in production reaching them — §11's operator side
    was written and never invoked, which is why §15's two HITL measurements had no production path
    either (the emission sits inside `resolve`).

    Read-only and needs no lease: reading the queue is not a claim on anything.
    """
    with connection(settings) as conn:
        records = queued_handoffs(conn)
    if not records:
        print("handoff: the queue is empty")
        return 0
    print(f"handoff: {len(records)} live ticket(s)")
    for record in records:
        held = record.claimed_by or "-"
        print(
            f"  {record.ticket_ref}  {record.id}  state={record.state}  "
            f"reason={record.reason}  generation={record.claim_generation}  claimed_by={held}"
        )
    return 0


def _handoff_resolve(settings: WorkerSettings, rest: Sequence[str]) -> int:
    """Close one ticket. The path §15's hitl_wait and hitl_resolve are emitted from.

    Extraction of `--flag value` pairs rather than argparse, matching this module's existing style:
    the entry point is deliberately thin, and a parser here would be a second place the arguments are
    described.
    """
    if not rest:
        print("handoff resolve needs a ticket id", file=sys.stderr)
        return 2
    handoff_id, options = rest[0], list(rest[1:])
    values: dict[str, str] = {}
    index = 0
    while index < len(options):
        if not options[index].startswith("--") or index + 1 >= len(options):
            print(f"handoff resolve: cannot read {options[index]!r}", file=sys.stderr)
            return 2
        values[options[index][2:].replace("-", "_")] = options[index + 1]
        index += 2

    missing = [name for name in ("operator", "return_state") if name not in values]
    if missing:
        print(
            "handoff resolve needs "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
            + ". There is no default operator (it is an attribution) and no default return state "
            "(the allow-list is checked, and guessing would pick one).",
            file=sys.stderr,
        )
        return 2

    with connection(settings) as conn:
        queue = HumanQueue(conn, telemetry=_telemetry_emitter())
        outcome = queue.resolve(
            handoff_id,
            operator_id=values["operator"],
            expected_generation=(int(values["generation"]) if "generation" in values else None),
            resolution=values.get("resolution", "resolved_by_operator"),
            return_state=values["return_state"],
            note=values.get("note"),
        )
    print(
        f"handoff: {outcome.handoff_id} closed, workflow {outcome.workflow_id} -> {outcome.return_state}"
    )
    print(f"  booking consent granted: {outcome.booking_consent_granted} (always false)")
    print(f"  final approval still required: {outcome.final_approval_still_required}")
    return 0


#: The bundle `configure_telemetry` returned, so `main` can shut it down without a second call —
#: a second `configure_telemetry` would install a second provider and export every event twice.
_emitter_bundle: list[Telemetry] = []


def _shutdown_telemetry() -> None:
    """Flush and close, if anything was configured. Safe to call when nothing was."""
    if _emitter_bundle:
        _emitter_bundle[0].shutdown()


def _telemetry_emitter() -> TelemetryEmitter:
    """§15's emitter, or `None` when no collector is configured.

    `configure_telemetry` is inert without `OTEL_EXPORTER_OTLP_ENDPOINT` — real providers, no
    exporter, no connection attempts — so this is safe to call unconditionally, and the returned
    bundle's emitter is what the queue measures through.
    """
    bundle = configure_telemetry("workers", version=service_version())
    if not _emitter_bundle:
        _emitter_bundle.append(bundle)
    return bundle.emitter


def service_version() -> str:
    """The package version, for the `service.version` resource attribute. Absent is not fatal."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("referraleasy-workers")
    except PackageNotFoundError:  # pragma: no cover - only when run from a source tree
        return "0.0.0"


async def _run(argv: Sequence[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if argv else 2

    settings = WorkerSettings.from_env()
    command, rest = argv[0], list(argv[1:])
    if command == "outbox":
        if "--watch" in rest:
            while True:
                await _outbox_once(settings)
                await asyncio.sleep(settings.poll_seconds)
        return await _outbox_once(settings)
    if command == "handoff":
        if not rest or rest[0] == "list":
            return _handoff_list(settings)
        if rest[0] == "resolve":
            return _handoff_resolve(settings, rest[1:])
        print(f"unknown handoff subcommand {rest[0]!r}", file=sys.stderr)
        return 2
    if command == "reconcile":
        if not rest:
            print("reconcile needs an idempotency key", file=sys.stderr)
            return 2
        return await _reconcile_once(settings, rest[0])
    print(f"unknown command {command!r}\n\n{_USAGE}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the exit code rather than calling `sys.exit` — a caller may be a test."""
    # Registered here rather than wrapped in a `finally`, for the same reason the gateway does it:
    # a flush that ran before the loop finished would lose the events a shutdown is worth having.
    atexit.register(_shutdown_telemetry)
    try:
        return asyncio.run(_run(sys.argv[1:] if argv is None else list(argv)))
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover - exercised by the process, not the suite
    raise SystemExit(main())
