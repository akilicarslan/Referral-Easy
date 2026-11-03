"""§14's offline replay: drive every scenario through the real stack and write run records.

`make eval` judges *observations*; this produces them. Each scenario in the dataset is driven once,
end to end, through the same in-process stack the autonomous E2E journeys use — the gateway (memory
control store), the seven agent services on the offline model, and an isolated mock hospital API on a
temporary SQLite database. The test-only inputs are the patient's own commands and the fault the
scenario names; no specialist result, transition or booking is supplied by this script.

What is captured comes from three places, and nothing else:

* the gateway's **admin view** of the workflow (final state, payload, bookings, transitions);
* a **recording coordinator port** wrapped around the real `CoordinatorDispatch`, which sees every
  delegation the gateway asked for and every result the agents returned;
* the **clock**, around the whole journey.

`referraleasy_eval.capture` turns one scenario observation into one record per case. A record is
never written for a scenario that did not run: a journey that raises is reported as a capture error
and its cases stay unmeasured, which the report counts separately from a low score.

    uv run python scripts/eval_replay.py --out captures/replay.json
    make eval RUNS=captures/replay.json

The replay never writes to a live hospital: the mock API is a fresh process on a throwaway database,
and the run is stamped `synthetic`.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from referraleasy_agent_runtime import AgentRole, AgentSettings, BudgetCaps
from referraleasy_agents.service import HttpDelegation, build_app
from referraleasy_agents.tools.client import DirectoryApi
from referraleasy_agents.tools.toolbox import ToolSet
from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_eval.capture import (
    DEFAULT_VARIANT,
    DelegationObservation,
    JourneyObservation,
    ToolObservation,
    records_for,
    variant_of,
)
from referraleasy_eval.dataset import load_dataset
from referraleasy_eval.runs import records_as_payload
from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.dispatch import CoordinatorDispatch
from referraleasy_gateway.guardrails import (
    DecisionModelGuardrail,
    GuardrailPipeline,
    LayerVerdict,
    LocalClassifierGuardrail,
    RegexGuardrail,
)
from referraleasy_gateway.policy import TablePolicy
from referraleasy_gateway.referral_sources import demo_sources
from referraleasy_gateway.timing import SystemClock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from e2e import isolated_mock_api  # type: ignore[attr-defined]  # noqa: E402

SERVICE_TOKEN = "replay-service-token"
PATIENT_SECRET = "replay-patient-secret-0123456789abcdef"
ADMIN_SECRET = "replay-admin-secret-0123456789abcdef"
WAIT_SECONDS = 20.0
DEFAULT_DATASET = ROOT / "evaluations/datasets/offline-v1.json"
DEFAULT_MANIFEST = ROOT / "registry/releases/latest.json"


# ---------------------------------------------------------------------------
# The recording coordinator port
# ---------------------------------------------------------------------------


class RecordingDispatch(CoordinatorDispatch):
    """The real dispatcher, plus a record of every call. It never alters a call or a result."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[DelegationObservation] = []
        self._calls_lock = threading.Lock()

    def execute(self, **kwargs: Any) -> Mapping[str, Any]:
        started = time.monotonic()
        try:
            result = super().execute(**kwargs)
        except Exception as error:
            self._add(kwargs, None, f"{type(error).__name__}: {error}", started)
            raise
        self._add(kwargs, dict(result), None, started)
        return result

    def _add(
        self,
        kwargs: Mapping[str, Any],
        result: dict[str, Any] | None,
        error: str | None,
        started: float,
    ) -> None:
        with self._calls_lock:
            self.calls.append(
                DelegationObservation(
                    workflow_state=str(kwargs.get("workflow_state", "")),
                    inputs=json.loads(json.dumps(kwargs.get("specialist_inputs", {}), default=str)),
                    result=json.loads(json.dumps(result, default=str)) if result else None,
                    error=error,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )

    def take(self) -> list[DelegationObservation]:
        with self._calls_lock:
            calls, self.calls = self.calls, []
        return calls


class ToolRecorder:
    """Records every governed tool call the in-process agents make, at `ToolSet.invoke`.

    Installed for the length of one journey and removed after it. It wraps the one path to a tool
    handler (`ToolSet.invoke` is documented as such), so the names and arguments are the agents' own
    rather than inferred from the delegation that caused them.
    """

    def __init__(self) -> None:
        self.calls: list[ToolObservation] = []
        self._lock = threading.Lock()

    @contextmanager
    def installed(self) -> Iterator[ToolRecorder]:
        original = ToolSet.invoke
        recorder = self

        async def invoke(tools: ToolSet, name: str, /, **arguments: Any) -> dict[str, Any]:
            outcome = "ok"
            try:
                return await original(tools, name, **arguments)
            except Exception as error:
                outcome = type(error).__name__
                raise
            finally:
                with recorder._lock:
                    recorder.calls.append(
                        ToolObservation(
                            role=str(getattr(tools.box.role, "value", tools.box.role)),
                            name=name,
                            arguments=json.loads(json.dumps(arguments, default=str)),
                            outcome=outcome,
                        )
                    )

        ToolSet.invoke = invoke  # type: ignore[method-assign]
        try:
            yield self
        finally:
            ToolSet.invoke = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# The in-process stack
# ---------------------------------------------------------------------------


class _SourcePolicy:
    """The seed policy table, read without a database (as the E2E journeys do)."""

    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        TablePolicy.from_seed().assert_allowed(f"{role}:{tool}", workflow_state)


def _guardrails() -> GuardrailPipeline:
    """The regex layer for real; the two model layers answer `allow`.

    The replay measures coordination, not screening: §9's layers are measured on their own corpus
    (ADR-0038), and a live decision model here would make a replay cost money and vary by run.
    """

    def allow(layer: GuardrailLayer) -> Callable[..., LayerVerdict]:
        def screen(*_: Any) -> LayerVerdict:
            return LayerVerdict(
                layer=layer,
                decision=GuardrailDecision.allow,
                category=GuardrailCategory.benign,
                reason="Replay: model layers are measured separately (ADR-0038).",
            )

        return screen

    return GuardrailPipeline(
        layers=(
            RegexGuardrail(max_chars=2000),
            DecisionModelGuardrail("replay", screen=allow(GuardrailLayer.decision_model)),
            LocalClassifierGuardrail("replay", screen=allow(GuardrailLayer.local_classifier)),
        ),
        clock=SystemClock(),
    )


@dataclass
class Stack:
    gateway: Any
    hospital: httpx.Client
    recorder: RecordingDispatch
    admin_headers: dict[str, str]
    release_manifest_id: str

    def patient_headers(self, patient_ref: str) -> dict[str, str]:
        session = self.gateway.post(
            "/v1/auth/patient-session", json={"subject": patient_ref, "patient_ref": patient_ref}
        ).json()
        return {"Authorization": f"Bearer {session['access_token']}"}


@contextmanager
def replay_stack(mock_api_url: str, release_manifest_id: str) -> Iterator[Stack]:
    """Gateway + seven agents + the given mock API, all in this process except the mock API."""
    apps: dict[str, Any] = {}

    async def router(scope: Any, receive: Any, send: Any) -> None:
        host = dict(scope["headers"])[b"host"].decode().split(":")[0]
        await apps[host](scope, receive, send)

    peer_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=router))
    delegation = HttpDelegation(
        base_url="http://{role}", token=SERVICE_TOKEN, caps=BudgetCaps(), client=peer_client
    )
    for role in AgentRole:
        env = {
            "APP_ENV": "test",
            "MODEL_BACKEND": "offline",
            "AGENT_ROLE": role.value,
            "AGENT_MODEL_PROVIDER": "openrouter",
            "AGENT_MODEL_ID": "offline/replay",
            "OPENROUTER_API_KEY": "offline-unused",
            "A2A_SERVICE_TOKEN": SERVICE_TOKEN,
        }
        apps[role.value] = build_app(
            AgentSettings.from_env(env),
            env=env,
            policy=_SourcePolicy(),
            delegation=delegation if role is AgentRole.CARE_COORDINATOR else None,
            api=DirectoryApi(mock_api_url, token=SERVICE_TOKEN, caps=BudgetCaps()),
        )

    with ExitStack() as stack:
        hospital = stack.enter_context(httpx.Client(base_url=mock_api_url, timeout=5))
        coordinator_client = stack.enter_context(TestClient(apps[AgentRole.CARE_COORDINATOR.value]))
        recorders: list[RecordingDispatch] = []

        def dispatcher(_settings: Any, **kwargs: Any) -> RecordingDispatch:
            recorders.append(
                RecordingDispatch(
                    base_url="http://testserver",
                    token=SERVICE_TOKEN,
                    release_manifest_id=release_manifest_id,
                    log=kwargs["log"],
                    clock=kwargs["clock"],
                    client=coordinator_client,
                )
            )
            return recorders[-1]

        settings = GatewaySettings.from_env(
            {
                "APP_ENV": "test",
                "JWT_PATIENT_SECRET": PATIENT_SECRET,
                "JWT_ADMIN_SECRET": ADMIN_SECRET,
                "GATEWAY_STREAM_HOLD_MS": "0",
            }
        )
        gateway = stack.enter_context(
            TestClient(
                create_app(settings=settings, guardrails=_guardrails(), dispatcher=dispatcher)
            )
        )
        admin = gateway.post("/v1/auth/admin-session", json={"subject": "eval-replay"}).json()
        yield Stack(
            gateway=gateway,
            hospital=hospital,
            recorder=recorders[0],
            admin_headers={"Authorization": f"Bearer {admin['access_token']}"},
            release_manifest_id=release_manifest_id,
        )


# ---------------------------------------------------------------------------
# The scripted patient
# ---------------------------------------------------------------------------

#: States where the workflow waits for the patient or has stopped. A journey only acts on these.
SETTLED = frozenset(
    {
        "AWAITING_HOSPITAL_CHOICE",
        "AWAITING_DOCTOR_CHOICE",
        "AWAITING_SLOT_CHOICE",
        "AWAITING_FINAL_APPROVAL",
        # Settled only while it shows the patient a card: the documents request (scenario 4).
        "READINESS_CHECK",
        "BOOKED",
        "BOOKING_UNVERIFIED",
        "HUMAN_HANDOFF",
        "FAILED",
        "CANCELLED",
        "ABANDONED",
    }
)
_SELECTION_KEY = {
    "AWAITING_HOSPITAL_CHOICE": "hospital_id",
    "AWAITING_DOCTOR_CHOICE": "provider_id",
    "AWAITING_SLOT_CHOICE": "slot_id",
}


class JourneyError(RuntimeError):
    """The journey could not be driven. Its cases stay unmeasured; nothing is scored from it."""


@dataclass
class Journey:
    """One patient's conversation, driven only through the patient and admin HTTP surfaces."""

    stack: Stack
    patient_ref: str
    workflow_id: str = ""
    steps: list[str] | None = None
    cards: list[dict[str, Any]] = dataclass_field(default_factory=list)

    @property
    def path(self) -> str:
        return f"/v1/workflows/{self.workflow_id}"

    def start(self, sources: Mapping[str, Any], text: str) -> dict[str, Any]:
        self.steps = []
        self._patient = self.stack.patient_headers(self.patient_ref)
        seeded = self.stack.gateway.post(
            "/v1/demo/workflows",
            headers=self.stack.admin_headers,
            json={
                "patient_ref": self.patient_ref,
                "ticket_ref": f"REPLAY-{uuid4()}",
                "payload": {"source_data": dict(sources)},
            },
        )
        if seeded.status_code != 200:
            raise JourneyError(f"seeding the workflow failed: {seeded.status_code} {seeded.text}")
        self.workflow_id = seeded.json()["workflow_id"]
        message = self.stack.gateway.post(
            f"{self.path}/messages",
            headers=self._patient,
            json={"command_id": str(uuid4()), "workflow_state_version": 1, "text": text},
        )
        if message.status_code != 202:
            raise JourneyError(f"the opening message was refused: {message.text}")
        self.steps.append(f"message: {text}")
        return self.settle()

    def snapshot(self) -> dict[str, Any]:
        response = self.stack.gateway.get(self.path, headers=self._patient)
        if response.status_code != 200:
            raise JourneyError(f"snapshot failed: {response.status_code} {response.text}")
        return dict(response.json())

    def settle(self, *, leave: str | None = None, answered: str | None = None) -> dict[str, Any]:
        """Wait until the workflow waits for the patient or has stopped, with its card current.

        `leave` is the state just answered; a new question in that same state (another card, such
        as the coverage review after a doctor choice) also counts, which `answered` makes visible.
        """
        deadline = time.monotonic() + WAIT_SECONDS
        snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if (
                snapshot["state"] in SETTLED
                and snapshot["components"]
                and (
                    snapshot["state"] != leave
                    or (
                        answered is not None
                        and snapshot["components"][0]["instance_id"] != answered
                        and snapshot["components"][0]["action"]
                    )
                )
                and (snapshot["state"] != "READINESS_CHECK" or snapshot["components"][0]["action"])
                and all(
                    c["workflow_state_version"] == snapshot["state_version"]
                    for c in snapshot["components"]
                )
            ):
                return snapshot
            time.sleep(0.05)
        raise JourneyError(
            f"the workflow did not settle within {WAIT_SECONDS:.0f}s; last state "
            f"{snapshot.get('state')!r}"
        )

    def act(self, snapshot: Mapping[str, Any], value: Mapping[str, Any]) -> None:
        card = snapshot["components"][0]
        if not card.get("action"):
            raise JourneyError(f"{snapshot['state']} offers the patient no action")
        response = self.stack.gateway.post(
            f"{self.path}/actions",
            headers=self._patient,
            json={
                "command_id": str(uuid4()),
                "workflow_state_version": snapshot["state_version"],
                "instance_id": card["instance_id"],
                "action": card["action"],
                "value": dict(value),
            },
        )
        if response.status_code != 200:
            raise JourneyError(f"{card['action']} was refused: {response.text}")
        assert self.steps is not None
        self.steps.append(f"{snapshot['state']}: {card['action']} {dict(value)}")
        self.cards.append(
            {"state": snapshot["state"], "component": card["component"], "props": card["props"]}
        )

    def choose(self, snapshot: Mapping[str, Any], prefer: str | None = None) -> dict[str, Any]:
        """Pick an option (the one whose id ends with `prefer`, else the first) and settle."""
        state = snapshot["state"]
        options = snapshot["components"][0]["props"].get("options") or []
        if not options:
            raise JourneyError(f"{state} offers no option to choose")
        chosen = next((o for o in options if prefer and str(o["id"]).endswith(prefer)), options[0])
        self.act(
            snapshot,
            {"hospital_id": None, "provider_id": None, _SELECTION_KEY[state]: chosen["id"]},
        )
        return self.settle(leave=state, answered=snapshot["components"][0]["instance_id"])

    def attach(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Attach every document the card offered — the patient's own, as listed by the server."""
        documents = snapshot["components"][0]["props"].get("documents") or []
        if not documents:
            raise JourneyError("the documents card offered nothing to attach")
        self.act(
            snapshot,
            {"values": {}, "document_ids": [str(d["document_id"]) for d in documents]},
        )
        return self.settle(leave=None)

    def confirm(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Approve, consent, or refresh — whatever the current card's single action is."""
        state = snapshot["state"]
        self.act(snapshot, {"consented": True})
        return self.settle(leave=state if state != "HUMAN_HANDOFF" else None)

    def admin_view(self) -> dict[str, Any]:
        response = self.stack.gateway.get(
            f"/v1/admin/workflows/{self.workflow_id}", headers=self.stack.admin_headers
        )
        if response.status_code != 200:
            raise JourneyError(f"admin view failed: {response.status_code} {response.text}")
        return dict(response.json())


# ---------------------------------------------------------------------------
# Scenario scripts: what the patient does, and which fault the scenario injects
# ---------------------------------------------------------------------------

_PATIENT = "b0000000-0000-4000-8000-000000000{tail}"
_TERMINAL = frozenset({"BOOKED", "HUMAN_HANDOFF", "FAILED", "CANCELLED", "ABANDONED"})


@dataclass(frozen=True)
class Script:
    """One scenario run. The patient is the scenario's own fixture patient (`fixtures/scenarios.json`).

    `arm` names a mock-API fault to arm *before* the patient acts in a given state — the same points
    the autonomous E2E journeys use. `at_unverified` is what the patient does when a booking's outcome
    is unknown: `stop` observes the unknown outcome itself, `refresh` lifts the fault and asks again.
    """

    patient_tail: str
    prefer: Mapping[str, str] = dataclass_field(default_factory=dict)
    arm: Mapping[str, str] = dataclass_field(default_factory=dict)
    at_unverified: str = "stop"
    repeat: bool = False
    message: str = "Please arrange the appointment my referral is for."


SCRIPTS: Mapping[tuple[str, str], Script] = {
    ("standard_booking", DEFAULT_VARIANT): Script(
        "001", prefer={"AWAITING_HOSPITAL_CHOICE": "001"}
    ),
    ("multi_constraint_search", DEFAULT_VARIANT): Script("003"),
    # Prairie Ridge: within the hard 20-mile radius, and its network status for plan 004 is not
    # confirmed. (Coverage 002, the conflict at Lakeview, lies outside that radius.)
    ("network_conflict", DEFAULT_VARIANT): Script(
        "005", prefer={"AWAITING_HOSPITAL_CHOICE": "003", "AWAITING_DOCTOR_CHOICE": "002"}
    ),
    # Des Plaines Valley needs a referral letter and a prior report; the patient holds both.
    ("missing_document", DEFAULT_VARIANT): Script(
        "002", prefer={"AWAITING_HOSPITAL_CHOICE": "004"}
    ),
    ("timeout_after_commit", DEFAULT_VARIANT): Script(
        "007", arm={"AWAITING_FINAL_APPROVAL": "timeout_after_commit"}
    ),
    ("timeout_after_commit", "before_commit"): Script(
        "007", arm={"AWAITING_FINAL_APPROVAL": "timeout_before_commit"}, at_unverified="refresh"
    ),
    ("three_transient_errors", DEFAULT_VARIANT): Script(
        "004", arm={"AWAITING_DOCTOR_CHOICE": "three_transient_errors"}
    ),
    ("no_specialist_found", DEFAULT_VARIANT): Script("006"),
    ("version_regression", DEFAULT_VARIANT): Script(
        "001", prefer={"AWAITING_HOSPITAL_CHOICE": "001"}, repeat=True
    ),
}


def _drive(stack: Stack, script: Script) -> tuple[Journey, dict[str, Any]]:
    patient = _PATIENT.format(tail=script.patient_tail)
    journey = Journey(stack, patient)
    sources = demo_sources(patient)
    if not sources:
        raise JourneyError(f"no fixture sources for patient {patient}")
    snapshot = journey.start(sources, script.message)
    for _ in range(12):
        state = snapshot["state"]
        if state in script.arm:
            stack.hospital.post(
                "/_faults/arm", json={"scenario_id": script.arm[state]}
            ).raise_for_status()
        if state in _TERMINAL:
            if state == "HUMAN_HANDOFF" and snapshot["components"][0]["props"].get(
                "consent_required"
            ):
                # The patient reviews and approves the handoff summary, as a patient would.
                snapshot = journey.confirm(snapshot)
            return journey, snapshot
        if state == "BOOKING_UNVERIFIED":
            if script.at_unverified == "stop":
                return journey, snapshot
            stack.hospital.post("/_faults/arm", json={"mode": "off"}).raise_for_status()
            snapshot = journey.confirm(snapshot)
            continue
        if state == "READINESS_CHECK":
            version = snapshot["state_version"]
            snapshot = journey.attach(snapshot)
            if snapshot["state"] == "READINESS_CHECK" and snapshot["state_version"] == version:
                snapshot = journey.settle(leave="READINESS_CHECK")
            continue
        if state in _SELECTION_KEY:
            snapshot = journey.choose(snapshot, script.prefer.get(state))
        else:
            snapshot = journey.confirm(snapshot)
    raise JourneyError("the journey did not finish within twelve patient actions")


def _observe(
    mock_api_url: str, release_id: str, scenario_id: str, variant: str, script: Script
) -> JourneyObservation:
    tools = ToolRecorder()
    with tools.installed(), replay_stack(mock_api_url, release_id) as stack:
        stack.hospital.post("/_faults/arm", json={"mode": "off"}).raise_for_status()
        stack.hospital.post("/_faults/reset", json={}).raise_for_status()
        started = time.monotonic()
        journey = Journey(stack, "")
        try:
            journey, snapshot = _drive(stack, script)
        except Exception as error:
            return JourneyObservation(
                scenario_id=scenario_id,
                variant=variant,
                workflow_id=journey.workflow_id,
                steps=tuple(journey.steps or ()),
                error=f"{type(error).__name__}: {error}",
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        view = journey.admin_view()
        return JourneyObservation(
            scenario_id=scenario_id,
            variant=variant,
            workflow_id=journey.workflow_id,
            final_state=str(snapshot["state"]),
            payload=view["workflow"]["payload"],
            bookings=tuple(view.get("bookings") or ()),
            delegations=tuple(stack.recorder.take()),
            tool_calls=tuple(tools.calls),
            latency_ms=latency_ms,
            # The standard journey's agents run their fixed workflow path, which makes no model call
            # (`referraleasy_agents.workflow`); the offline profile's canned model costs nothing.
            model_calls=0,
            cost_usd=0.0,
            steps=tuple(journey.steps or ()),
            cards=tuple(journey.cards),
        )


def observe(release_id: str, scenario_id: str, variant: str) -> JourneyObservation:
    """Run one scenario variant on a fresh mock API, so no run sees another's bookings."""
    script = SCRIPTS.get((scenario_id, variant))
    if script is None:
        return JourneyObservation(
            scenario_id=scenario_id,
            variant=variant,
            error=f"no replay script for {scenario_id}/{variant}",
        )
    runs = []
    for _ in range(2 if script.repeat else 1):
        with (
            tempfile.TemporaryDirectory(prefix="referraleasy-replay-") as directory,
            isolated_mock_api(Path(directory)) as url,
        ):
            runs.append(_observe(url, release_id, scenario_id, variant, script))
    if len(runs) == 2 and runs[0].error is None:
        return replace(runs[0], repeat=runs[1])
    return runs[0]


def _summary(observation: JourneyObservation) -> dict[str, Any]:
    return {
        "scenario_id": observation.scenario_id,
        "variant": observation.variant,
        "workflow_id": observation.workflow_id,
        "final_state": observation.final_state,
        "latency_ms": observation.latency_ms,
        "delegations": len(observation.delegations),
        "tool_calls": [
            f"{call.role}:{call.name}:{call.outcome}" for call in observation.tool_calls
        ],
        "steps": list(observation.steps),
        "coordination_error": (observation.payload.get("coordination_error") or {}).get("detail"),
        "error": observation.error,
        "repeat_final_state": observation.repeat.final_state if observation.repeat else None,
    }


def _release_manifest_id(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(
            f"no release manifest at {path}; build one first (docs/runbooks/evaluation.md §1)"
        )
    return str(json.loads(path.read_text())["release_manifest_id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, default=ROOT / "captures/replay.json")
    parser.add_argument("--scenario", action="append", help="replay only these scenario ids")
    args = parser.parse_args()
    release_id = _release_manifest_id(args.release_manifest)
    dataset = load_dataset(args.dataset)
    wanted = sorted({(case.scenario_id, variant_of(case)) for case in dataset.cases})
    if args.scenario:
        wanted = [key for key in wanted if key[0] in set(args.scenario)]
    observations: dict[tuple[str, str], JourneyObservation] = {}
    for scenario_id, variant in wanted:
        observation = observe(release_id, scenario_id, variant)
        observations[(scenario_id, variant)] = observation
        print(
            f"{scenario_id}/{variant}: "
            + (f"ERROR {observation.error}" if observation.error else observation.final_state),
            file=sys.stderr,
            flush=True,
        )
    records = records_for(dataset.cases, observations)
    payload = {
        **records_as_payload(records),
        "replay": {
            "release_manifest_id": release_id,
            "dataset_version": dataset.version,
            "synthetic": True,
            "journeys": [_summary(observation) for observation in observations.values()],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    failed = [key for key, observation in observations.items() if observation.error]
    print(
        f"wrote {len(records)} record(s) for {len(dataset.cases)} case(s) to {args.out}; "
        f"{len(failed)} journey(s) could not be driven",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
