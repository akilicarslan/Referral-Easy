"""Shared constants, path setup and the demo driver for the gateway tests.

This is a plain module with a unique name, not `conftest.py`, for the reason `skills_fixtures.py`
documents at length: pytest puts every `conftest.py` on `sys.path` under the bare name `conftest`, so
with six of them in this repository `from conftest import X` resolves against whichever was imported
last. Keep importing from here.

The `sys.path` block is the fallback rule for this workspace, not a preference: `services/gateway` is
a uv workspace member, so normally it is importable as an editable install. This suite also runs from
a checkout where the shared virtualenv has not been synced (the environment is shared with other
agents, and `uv sync` is the one command nobody may run here), so the three source roots this service
needs are added explicitly. `tests/skills/skills_fixtures.py` does the same thing for the same reason.

**Every test states its own environment.** `SPEC_ENV` is a mapping the tests pass to
`GatewaySettings.from_env`, so nothing reads the developer's real `.env` and a test cannot pass
because of a secret that happens to be exported in the shell.

**Two profiles, one suite.** `CONTROL_BACKENDS` decides which control-state profiles the suite is
parameterised over, and it is computed from `TEST_DATABASE_URL` at import time rather than gated by a
`skip` inside a fixture. That distinction matters:

* With no `TEST_DATABASE_URL` the `postgres` parameter is **never collected**, so the default run
  needs no database at all and reports no skips — a suite that skipped its own central claim would
  look green and mean nothing.
* With `TEST_DATABASE_URL` set the parameter is collected and carries the `integration` marker, so
  CI's dedicated job (`pytest -m integration`, which fails if anything skips) runs it, and the
  default `-m 'not spike and not e2e'` run picks it up locally as well.

The reset between tests follows `tests/domain/conftest.py` exactly — the same table list, the same
step around the immutability guards — because there is one database and two suites, and a second
convention for emptying it would be a second definition of "empty".
"""

from __future__ import annotations

import os
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]

for _relative in (
    "services/gateway/src",
    "packages/contracts/python/src",
    "packages/domain/src",
):
    _path = REPO_ROOT / _relative
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pytest  # noqa: E402  (path set up above)
from fastapi.testclient import TestClient  # noqa: E402

from referraleasy_contracts.generated.common import (  # noqa: E402
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
    WorkflowState,
)
from referraleasy_domain.approval import canonical_booking_payload_hash  # noqa: E402
from referraleasy_gateway.config import GatewaySettings  # noqa: E402
from referraleasy_gateway.guardrails import (  # noqa: E402
    DecisionModelGuardrail,
    GuardrailPipeline,
    LayerVerdict,
    LocalClassifierGuardrail,
    RegexGuardrail,
    ScreeningRequest,
)
from referraleasy_gateway.timing import Clock  # noqa: E402

__all__ = [
    "BACKEND_PARAMS",
    "BOOKING_PAYLOAD",
    "DEMO_WORKFLOW_BODY",
    "GUARDED_TABLES",
    "MEMORY_ONLY_MARKER",
    "PATIENT_REF",
    "RESET_TABLES",
    "SPEC_ENV",
    "TEST_DATABASE_URL",
    "TEST_WORKER_ID",
    "Demo",
    "allow_all_pipeline",
    "canonical_booking_payload_hash",
    "demo_settings",
    "hospital_ref",
    "profile_settings",
    "recording_layer",
    "require_migrated_database",
    "reset_database",
]

PATIENT_REF = "patient-1"
COORDINATOR_REF = "coordinator-1"

#: Where the PostgreSQL profile's server is. Unset means the suite runs the memory profile only.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

#: The profiles the suite is parameterised over. See the module docstring for why the unavailable
#: one is absent rather than skipped.
BACKEND_PARAMS = (
    [
        pytest.param("memory", id="memory"),
        # The marker is what puts this parameter in CI's database job and keeps the memory parameter
        # out of it: the same assertion, run twice, once where it is cheap and once where it counts.
        pytest.param("postgres", id="postgres", marks=pytest.mark.integration),
    ]
    if TEST_DATABASE_URL
    else [pytest.param("memory", id="memory")]
)

#: Reset between postgres tests. Copied verbatim from `tests/domain/conftest.py`: `schema_migrations`
#: is the ledger rather than test state, and the vocabulary and policy-seed tables are part of the
#: schema — a test that deleted them would be testing a different database than the one that ships.
RESET_TABLES = (
    "audit",
    "release_event",
    "release_registry",
    "handoff",
    "booking_request",
    "approval",
    "command_dedup",
    "outbox",
    "task",
    "session",
    "workflow",
    "evidence",
)

#: Tables whose purpose is to refuse deletion, so emptying them is a deliberate act.
GUARDED_TABLES = ("audit", "evidence", "release_registry", "release_event")

#: The marker that says a test can only pass in the memory profile. Defined here rather than in
#: `conftest.py` because a test module may not import from a `conftest` (see that file), and the guard
#: in `test_postgres_control_state.py` has to read the same marker the deselector does.
MEMORY_ONLY_MARKER = "memory_only"

#: The identity the postgres profile writes into `workflow.owner_worker_id`. A constant rather than
#: `GatewaySettings.resolved_worker_id`, because two tests in one process must not be able to look
#: like two workers: each test's rows are truncated before it starts, and a worker id that moved
#: between tests would hide a claim that leaked.
TEST_WORKER_ID = "gateway-test-worker"

#: §7's three vocabulary tables, whose contents the gateway reads and does not own. Named here so a
#: test can say "the database answered this, out of its own table" without spelling the table out.
VOCABULARY_TABLES = ("workflow_state", "workflow_transition", "policy_seed")

#: Fixture secrets. Long enough to be a real key, and *not* the `.env.example` placeholder — the
#: gateway refuses a `CHANGEME` secret outside the demo profile, and a test that used one would be
#: exercising a configuration the service refuses to start with.
SPEC_ENV: dict[str, str] = {
    "APP_ENV": "demo",
    "JWT_PATIENT_SECRET": "fixture-patient-secret-0123456789abcdef",
    "JWT_ADMIN_SECRET": "fixture-admin-secret-0123456789abcdef",
    "JWT_TTL_SECONDS": "3600",
    # Zero hold: the stream route drains the log and closes, so a test can read a stream with a plain
    # request instead of racing a generator. The hold itself is tested where it belongs —
    # `test_stream.py` sets it explicitly.
    "GATEWAY_STREAM_HOLD_MS": "0",
}


def demo_settings(**overrides: str) -> GatewaySettings:
    """The demo profile with fixture secrets, plus any overrides the test names.

    Reads *environment variable* names, because that is what it is: the env contract of §19 with a
    test's fixture secrets. A test that wants to move one field of the profile it was handed —
    which is what a test parameterised over the control-state backend has to do — wants
    `profile_settings` instead, or it will silently rebuild the profile as the memory one.
    """
    return GatewaySettings.from_env({**SPEC_ENV, **overrides})


def profile_settings(settings: GatewaySettings, **fields: Any) -> GatewaySettings:
    """`settings` with named fields replaced, keeping the profile it was built for.

    The distinction from `demo_settings` is the whole of why this exists. `demo_settings(...)` builds
    the profile from scratch and defaults `GATEWAY_CONTROL_BACKEND` to `memory`; a test that reached
    for it while the `control` fixture was the PostgreSQL store would hand `create_app` a memory
    profile and a postgres store. `create_app` refuses that now, but the reason it *can* be written is
    that "the profile" and "the settings" were two things. Here they are one.
    """
    from dataclasses import replace

    return replace(settings, **fields)


def require_migrated_database(url: str) -> None:
    """Fail, with the fix, when the database at `url` has no schema.

    Deliberately **not** a skip. `TEST_DATABASE_URL` being set is the suite's statement that a
    migrated database is there, and CI's database job turns any skip into a failed build for exactly
    that reason: a database-backed test that did not run must not be able to look like one that
    passed. A missing schema is therefore an error naming the two commands that produce one.

    The check is the same question `PostgresControlState.ping` asks at startup — is
    `workflow_transition` there — because the migration that creates it (003) is the first one this
    profile needs and the one that populates it (014) is the last.
    """
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.workflow_transition') IS NOT NULL")
        row = cur.fetchone()
    if row is None or not row[0]:
        raise RuntimeError(
            f"TEST_DATABASE_URL is set ({url}) but the database has no schema: "
            "`workflow_transition` does not exist, so migrations 003 and 014 have not run.\n"
            "    uv run --no-sync python scripts/migrate.py --database-url "
            f"'{url}'\n"
            "or unset TEST_DATABASE_URL to run the memory profile only."
        )


def reset_database(url: str) -> None:
    """Empty every data table, stepping explicitly around the immutability guards.

    The convention is `tests/domain/conftest.py`'s, lines and all, because there is one database
    shared by both suites and a second way of emptying it would be a second definition of empty —
    which is how a suite starts passing because of what the previous one left behind.

    Its own connection: the app under test opens and closes one per operation, so nothing is shared
    to hand over.
    """
    import psycopg

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for table in GUARDED_TABLES:
                cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            cur.execute("TRUNCATE " + ", ".join(RESET_TABLES) + " RESTART IDENTITY CASCADE")
            for table in GUARDED_TABLES:
                cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        conn.commit()


def allow_screen(layer: GuardrailLayer):
    """A stand-in for a configured model layer: it answers `allow` and says so.

    Used only where the *booking* path needs a complete screening. It is a test double and the tests
    that use it say so — the shipped profile has nothing behind layers 2 and 3, which is why
    `test_guardrails.py::test_booking_is_refused_while_the_model_layers_are_unconfigured` exists
    beside them.
    """

    def _screen(request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        return LayerVerdict(
            layer=layer,
            decision=GuardrailDecision.allow,
            category=GuardrailCategory.benign,
            reason=f"test double for {layer.value} (received {masked_text!r} in {request.workflow_state})",
        )

    return _screen


def block_screen(layer: GuardrailLayer, *, category: GuardrailCategory, reason: str):
    """A stand-in layer that refuses. Used to prove a later layer's verdict is the one that travels."""

    def _screen(request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        return LayerVerdict(
            layer=layer, decision=GuardrailDecision.block, category=category, reason=reason
        )

    return _screen


def allow_all_pipeline(clock: Clock) -> GuardrailPipeline:
    """All three layers configured, with the two model layers as doubles.

    This is what a *complete* screening looks like, and it is the only configuration in which §9
    permits a booking write. It is deliberately not what `create_app` builds.
    """
    return GuardrailPipeline(
        layers=(
            RegexGuardrail(max_chars=2000),
            DecisionModelGuardrail(
                "openrouter:test-double", screen=allow_screen(GuardrailLayer.decision_model)
            ),
            LocalClassifierGuardrail(
                "local:test-double", screen=allow_screen(GuardrailLayer.local_classifier)
            ),
        ),
        clock=clock,
    )


def recording_layer(
    layer: GuardrailLayer, decision: GuardrailDecision, category: GuardrailCategory, log: list[str]
):
    """A layer that records that it ran. Ordering claims need a call log, not a comment."""

    def _screen(request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        log.append(layer.value)
        return LayerVerdict(layer=layer, decision=decision, category=category, reason="recorded")

    return _screen


#: The payload the patient reviews and consents to. Small and explicit: every key here travels into
#: `workflow.payload[booking_payload]` and then into the consent binding's canonical hash.
BOOKING_PAYLOAD: dict[str, Any] = {
    "hospital_id": "hospital-1",
    "doctor_id": "doctor-1",
    "slot_id": "slot-1",
    "starts_at": "2026-11-02T10:00:00+00:00",
}

DEMO_WORKFLOW_BODY: dict[str, Any] = {"patient_ref": PATIENT_REF, "ticket_ref": "T-DEMO-1"}


def hospital_ref(*, for_request: str | None = None) -> str:
    """A reference the hospital's scheduler would have returned with a confirmation.

    Unique in both forms, because 008's `booking_request_external_ref_key` is unique across the whole
    table: two bookings in one database cannot share a reference any more than two appointments can be
    the same appointment, so a hard-coded default would pass once and collide on the next booking. The
    prefix is not a real system's and is not meant to look like one — the *fixture* is issuing it,
    standing in for the party that is the only one able to.

    `for_request` makes the reference a function of the request instead of a fresh draw, and that is
    what lets a test retry one command: §7's ledger compares request hashes, so a scheduler that
    invented a new reference on every attempt would turn the client's retry into "this command_id was
    reused with a different body" (`RE005`). A real one answers the same request with the same
    reference — it processed it once — and this is the closest a fixture can come to that.
    """
    if for_request is None:
        return f"F-DEMO-{uuid4().hex[:8]}"
    return f"F-DEMO-{sha256(for_request.encode()).hexdigest()[:8]}"


class Demo:
    """One demo session driven over HTTP, so a test body is the sequence and not the plumbing.

    It deliberately holds no opinion about what should happen: every method returns the response and
    the assertions live in the test. A driver that asserted would be a second place for the contract
    to live, and the first place a failure would be reported from.
    """

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.admin_token = self._mint("/v1/auth/admin-session", {"subject": COORDINATOR_REF})
        self.patient_token = self._mint(
            "/v1/auth/patient-session", {"subject": PATIENT_REF, "patient_ref": PATIENT_REF}
        )

    # -- sessions -------------------------------------------------------------------------------

    def _mint(self, path: str, body: dict[str, Any]) -> str:
        response = self.client.post(path, json=body)
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])

    @property
    def admin(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}

    @property
    def patient(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.patient_token}"}

    # -- setup ----------------------------------------------------------------------------------

    def seed(self, **body: Any) -> str:
        """Create a workflow and return its id.

        **The ticket ref is fresh per call**, and that is a fixture correction rather than a
        preference: `workflow` carries `workflow_active_ticket_key`, a partial unique index over live
        rows, so one ticket can have one workflow. A driver that reused a fixed ref could only ever
        seed one workflow per database — which is invisible against a dictionary, and is what the
        postgres profile surfaced as `duplicate key value violates unique constraint`. A test that
        needs two workflows in one test is not testing something exotic; §7's "one ticket, one
        workflow" is a rule, not a limitation to work around, and a fixture that cannot express the
        second one would have hidden that the rule exists.
        """
        response = self.client.post(
            "/v1/demo/workflows",
            json={**DEMO_WORKFLOW_BODY, "ticket_ref": f"T-DEMO-{uuid4().hex[:8]}", **body},
            headers=self.admin,
        )
        assert response.status_code == 200, response.text
        return str(response.json()["workflow_id"])

    def move(
        self,
        workflow_id: str,
        *,
        version: int,
        to_state: WorkflowState,
        props: dict[str, Any] | None = None,
        command_id: str | None = None,
        complete: bool = True,
        partial_text: str = "",
    ):
        """The coordinator's transition. The response is returned, never asserted on."""
        body: dict[str, Any] = {
            "command_id": command_id or f"transition-{uuid4().hex[:8]}",
            "expected_state_version": version,
            "to_state": to_state.value,
            "complete": complete,
        }
        if props is not None:
            body["props"] = props
        if partial_text:
            body["partial_text"] = partial_text
        return self.client.post(
            f"/v1/workflows/{workflow_id}/transitions", json=body, headers=self.admin
        )

    def action(
        self,
        workflow_id: str,
        *,
        instance: dict[str, Any],
        version: int,
        value: dict[str, Any] | None,
        command_id: str | None = None,
        action: str | None = None,
    ):
        """Submit a card action. `instance` is a component from a snapshot or a transition receipt.

        The ids come from the server, which is the point of the route: a test that invented them would
        be testing a client that the gateway refuses.
        """
        body = {
            "command_id": command_id or str(uuid4()),
            "instance_id": instance["instance_id"],
            "action": action if action is not None else instance["action"],
            "workflow_state_version": version,
            "value": value,
        }
        return self.client.post(
            f"/v1/workflows/{workflow_id}/actions", json=body, headers=self.patient
        )

    def message(self, workflow_id: str, *, text: str, version: int, command_id: str | None = None):
        return self.client.post(
            f"/v1/workflows/{workflow_id}/messages",
            json={
                "command_id": command_id or f"message-{uuid4().hex[:8]}",
                "workflow_state_version": version,
                "text": text,
            },
            headers=self.patient,
        )

    def snapshot(self, workflow_id: str):
        return self.client.get(f"/v1/workflows/{workflow_id}", headers=self.patient)

    def help_card(self, workflow_id: str):
        return self.client.get(f"/v1/workflows/{workflow_id}/help", headers=self.patient)

    def events(self, workflow_id: str, *, last_event_id: str | None = None, token: bool = False):
        """Read the stream over one request. Requires `GATEWAY_STREAM_HOLD_MS=0` (the default here)."""
        params = {"last_event_id": last_event_id} if last_event_id else None
        if token:
            params = {**(params or {}), "token": self.patient_token}
            return self.client.get(f"/v1/workflows/{workflow_id}/events", params=params)
        return self.client.get(
            f"/v1/workflows/{workflow_id}/events", params=params, headers=self.patient
        )

    def admin_view(self, workflow_id: str):
        return self.client.get(f"/v1/admin/workflows/{workflow_id}", headers=self.admin)

    def metrics(self):
        return self.client.get("/v1/admin/metrics", headers=self.admin)

    # -- the path the booking tests need ---------------------------------------------------------

    def review_card(self, workflow_id: str) -> dict[str, Any]:
        """Take a fresh workflow to `AWAITING_FINAL_APPROVAL` and return its `BookingReview` card."""
        version = 1
        for target, props in (
            (WorkflowState.PROVIDER_MATCHING, None),
            (WorkflowState.AWAITING_HOSPITAL_CHOICE, {"options": [{"id": "hospital-1"}]}),
            (WorkflowState.AWAITING_DOCTOR_CHOICE, {"options": [{"id": "doctor-1"}]}),
            (WorkflowState.AWAITING_SLOT_CHOICE, {"options": [{"id": "slot-1"}]}),
            (WorkflowState.AWAITING_FINAL_APPROVAL, {"booking_payload": BOOKING_PAYLOAD}),
        ):
            response = self.move(workflow_id, version=version, to_state=target, props=props)
            assert response.status_code == 200, response.text
            version += 1
        card = response.json()["components"][0]
        assert card["component"] == "BookingReview"
        return dict(card)

    def consent(self, workflow_id: str, card: dict[str, Any], *, version: int):
        """Submit the consent the review card offers. Returns the response."""
        return self.action(workflow_id, instance=card, version=version, value={"approved": True})

    def in_booking_flight(self, workflow_id: str) -> str:
        """Consent, then move to `BOOKING_IN_FLIGHT`. Returns the approval id."""
        card = self.review_card(workflow_id)
        granted = self.consent(workflow_id, card, version=6)
        assert granted.status_code == 200, granted.text
        approval_id = str(granted.json()["approval_id"])
        moved = self.move(workflow_id, version=6, to_state=WorkflowState.BOOKING_IN_FLIGHT)
        assert moved.status_code == 200, moved.text
        return approval_id

    def booking(
        self,
        workflow_id: str,
        *,
        approval_id: str,
        version: int,
        payload: dict[str, Any] | None = None,
        screening_text: str = "Please book the appointment the patient approved.",
        outcome: str | None = "confirmed",
        external_ref: str | None = None,
        command_id: str | None = None,
    ):
        """The booking write, as the coordinator's surface sends it.

        `external_ref` defaults to a fresh `hospital_ref()` when the outcome is a confirmation, and to
        nothing otherwise. That default is the fixture playing the scheduler: a confirmed booking
        reaches `BOOKED` only with the hospital's own reference beside it (008's
        `booking_request_confirmed_has_ref`, and the port's matching refusal), and the reference is
        the one fact in the request that only the other side of the wire can supply. A test that wants
        the refusal sends `external_ref=""` — a confirmation with no reference is exactly the request
        that must not be recorded.

        `outcome=None` leaves the field out of the body entirely, which is the other half of the
        write: the route then derives the outcome from the reference (`commands._outcome_for`), so a
        test can exercise both derived branches — with a `hospital_ref()`, and with neither field
        saying anything about the hospital.
        """
        command_id = command_id or f"booking-{uuid4().hex[:8]}"
        if external_ref is None and outcome == "confirmed":
            external_ref = hospital_ref(for_request=f"{workflow_id}:{command_id}")
        body: dict[str, Any] = {
            "command_id": command_id,
            "approval_id": approval_id,
            "expected_state_version": version,
            "booking_payload": payload if payload is not None else BOOKING_PAYLOAD,
            "screening_text": screening_text,
            "external_ref": external_ref,
        }
        if outcome is not None:
            body["outcome"] = outcome
        return self.client.post(
            f"/v1/workflows/{workflow_id}/bookings", json=body, headers=self.admin
        )

    def bookings(self, workflow_id: str) -> list[dict[str, Any]]:
        """Every booking row for a workflow, read through the admin view (there is no public list)."""
        response = self.admin_view(workflow_id)
        assert response.status_code == 200, response.text
        return list(response.json()["bookings"])
