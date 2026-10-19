"""Phase 1 exit criterion: a scripted, no-LLM client drives a booking end to end.

"→ A scripted (no-LLM) client can drive one booking end to end against the mock API, under every
fault type, without violating any invariant."

The client here is deliberately *dumb*: HTTP and assertions, no agent, no model, no reasoning. That
is the point — it proves the deterministic core holds before any LLM is involved, so when something
fails in a later phase there is a smaller space to search.

Faults are armed through `POST /_faults/arm` and **match on method + path + occurrence number**, so
the order of requests matters: each test discovers its fixtures *before* arming, and the autouse
fixture disarms and resets between tests. Where a scenario names a specific entity (the stale-slot
conflict names a slot), the test uses that entity.

The assertions are about invariants, never about a status code for its own sake — §2.5 is explicit
that HTTP 200 does not mean a booking was confirmed.

**Precondition: a mock API whose database has not been booked against yet.** The suite consumes
slots and there is no reset endpoint, so a second run against surviving state fails in `_discover`
with "no bookable ... slot" — which reads as a product bug and is not one. Run it as `make e2e`,
which restarts the mock API on a clean database first. The `e2e` marker is deselected from the
default `pytest` run for exactly this reason (see `addopts` in pyproject.toml); `pytest tests/e2e`
still works when you have arranged the state yourself.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

BASE = os.environ.get("MOCK_API_URL", "http://localhost:4010").rstrip("/")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

CONFIRMED = "CONFIRMED"
UNCONFIRMED = {"REQUESTED", "PENDING_REVIEW"}

#: The slot `stale_slot_409.json` names, with the stale version it claims was presented.
STALE_SLOT_ID = "40000000-0000-4000-8000-000000000013"
STALE_SLOT_PRESENTED_VERSION = 2

TRANSPORT_FAILURES = (500, 502, 503, 504)


# ── HTTP ────────────────────────────────────────────────────────────────────────────────────


class Response:
    def __init__(self, status: int, body: Any, headers: dict[str, str]) -> None:
        self.status = status
        self.body = body
        self.headers = headers

    @property
    def data(self) -> dict[str, Any]:
        assert isinstance(self.body, dict), f"expected an object body, got {self.body!r}"
        return self.body

    @property
    def code(self) -> str | None:
        err = self.data.get("error") if isinstance(self.body, dict) else None
        return err.get("code") if isinstance(err, dict) else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.status} {json.dumps(self.body)[:200]}>"


class MockApi:
    def __init__(self, base: str) -> None:
        self.base = base

    def _call(self, method: str, path: str, payload: Any = None) -> Response:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                return Response(resp.status, json.loads(raw) if raw else None, dict(resp.headers))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return Response(exc.code, json.loads(raw) if raw else None, dict(exc.headers))

    def get(self, path: str, **query: Any) -> Response:
        if query:
            qs = urllib.parse.urlencode({k: str(v) for k, v in query.items()})
            path = f"{path}?{qs}"
        return self._call("GET", path)

    def post(self, path: str, payload: Any = None) -> Response:
        return self._call("POST", path, payload)

    def arm(self, scenario_id: str) -> Response:
        return self.post("/_faults/arm", {"scenario_id": scenario_id})

    def disarm(self) -> Response:
        return self.post("/_faults/arm", {"mode": "off"})

    def reset_faults(self) -> Response:
        return self.post("/_faults/reset", {})


# ── fixtures ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def api() -> MockApi:
    client = MockApi(BASE)
    try:
        client.get("/healthz")
    except Exception as exc:
        pytest.skip(
            f"mock API not reachable at {BASE} ({type(exc).__name__}). Start it with "
            "`pnpm mock-api:dev` or bring up the compose stack. This module IS the Phase 1 exit "
            "criterion, so it is meant to be run, not skipped."
        )
    return client


@pytest.fixture(autouse=True)
def _clean_faults(api: MockApi):
    api.disarm()
    api.reset_faults()
    yield
    api.disarm()
    api.reset_faults()


def _patient_id() -> str:
    rows = json.loads((REPO_ROOT / "fixtures" / "patients" / "patient_details.json").read_text())
    rows = rows if isinstance(rows, list) else rows.get("patients", [])
    return rows[0]["patient_id"]


def _discover(api: MockApi, adapter_type: str, *, skip: set[str] | None = None) -> dict[str, str]:
    """Find a hospital of the requested adapter that currently has an open slot.

    Always call this BEFORE arming a fault: scenarios that target `GET /availability` count the
    requests made against that path, so discovery would consume the fault.
    """
    skip = skip or set()
    for hospital in api.get("/hospitals", adapter_type=adapter_type).data["hospitals"]:
        if hospital["hospital_id"] in skip:
            continue
        providers = api.get("/providers", hospital_id=hospital["hospital_id"]).data["providers"]
        for provider in providers:
            slots = (
                api.get(
                    "/availability",
                    hospital_id=hospital["hospital_id"],
                    provider_id=provider["provider_id"],
                    visit_type="first_visit",
                ).data.get("slots")
                or []
            )
            for slot in slots:
                if slot.get("status") == "available":
                    return {
                        "patient_id": _patient_id(),
                        "hospital_id": hospital["hospital_id"],
                        "provider_id": provider["provider_id"],
                        "slot_id": slot["slot_id"],
                        "slot_version": str(slot.get("version", 0)),
                        "adapter_type": adapter_type,
                        "timezone": hospital["timezone"],
                    }
    pytest.fail(f"no bookable {adapter_type} hospital/provider/slot in the fixtures")


def _book(api: MockApi, target: dict[str, str], key: str, **over: Any) -> Response:
    return api.post(
        "/appointments",
        {
            "patient_id": target["patient_id"],
            "hospital_id": target["hospital_id"],
            "provider_id": target["provider_id"],
            "slot_id": target["slot_id"],
            "visit_type": "first_visit",
            "idempotency_key": key,
            **over,
        },
    )


def _rows_for_slot(api: MockApi, slot_id: str) -> list[dict[str, Any]]:
    """`GET /appointments` returns `{bookings, count}`."""
    rows = api.get("/appointments").data["bookings"]
    return [r for r in rows if (r.get("payload") or {}).get("slot_id") == slot_id]


def _key(prefix: str) -> str:
    return f"e2e-{prefix}-{uuid.uuid4().hex[:8]}"


# ── the golden path ─────────────────────────────────────────────────────────────────────────


def test_a_scripted_client_drives_a_booking_end_to_end(api: MockApi) -> None:
    """Discover → book → receipt. The whole Phase 1 path with no model involved."""
    target = _discover(api, "instant_confirm")
    booked = _book(api, target, _key("golden"))

    assert booked.status == 200, f"booking failed: {booked!r}"
    assert booked.data["outcome"] == "created"
    assert booked.data["booking"]["state"] == CONFIRMED, (
        f"an instant_confirm hospital returns CONFIRMED, got {booked.data['booking']['state']!r}"
    )
    appointment = booked.data["appointment"]
    assert appointment["verified"] is True, "the receipt must be a verified read-back"
    assert appointment["verification_method"] in ("create_response", "lookup_request")
    assert len(_rows_for_slot(api, target["slot_id"])) == 1


def test_a_review_required_hospital_never_reports_confirmation(api: MockApi) -> None:
    """§2.5: `PENDING_REVIEW` is not `CONFIRMED`, and HTTP 200 asserts neither.

    This is the assertion that stops a patient being told an appointment is booked when the
    hospital has not yet decided.
    """
    target = _discover(api, "review_required")
    response = _book(api, target, _key("review"))

    assert response.status == 200
    assert response.data["booking"]["state"] == "PENDING_REVIEW", (
        "a review_required hospital produces PENDING_REVIEW, not a confirmation"
    )
    assert response.data["booking"]["state"] in UNCONFIRMED
    # No boolean shortcut exists that would let a caller misread it.
    assert '"confirmed":true' not in json.dumps(response.data).lower()
    assert not (response.data.get("appointment") or {}).get("verified"), (
        "an unconfirmed booking must not carry a verified appointment"
    )


# ── invariant: no double booking ────────────────────────────────────────────────────────────


def test_the_same_key_and_payload_replays_instead_of_rebooking(api: MockApi) -> None:
    target = _discover(api, "instant_confirm")
    key = _key("replay")

    first = _book(api, target, key)
    second = _book(api, target, key)

    assert (
        first.data["booking"]["booking_request_id"] == second.data["booking"]["booking_request_id"]
    )
    assert second.data["outcome"] == "duplicate_replayed"
    assert second.data["idempotent_replay"] is True
    assert len(_rows_for_slot(api, target["slot_id"])) == 1, "a replay created a second row"


def test_the_same_key_with_a_different_payload_is_refused(api: MockApi) -> None:
    """§7: same key + *different* payload must be rejected, not silently replayed.

    The two targets must genuinely differ — `_discover` is deterministic, so calling it twice for
    the same adapter returns the *same* slot, which would make this test pass for the wrong reason.
    """
    first = _discover(api, "instant_confirm")
    second = _discover(api, "instant_confirm", skip={first["hospital_id"]})
    assert first["slot_id"] != second["slot_id"], "the two targets must be different bookings"

    key = _key("reuse")
    _book(api, first, key)
    conflict = _book(api, second, key)

    assert conflict.status == 409, f"expected a conflict, got {conflict!r}"
    assert conflict.code == "idempotency_key_reuse"


def test_two_racing_bookings_for_one_slot_produce_exactly_one(api: MockApi) -> None:
    """Invariant #2. A read-then-write would let both through."""
    target = _discover(api, "instant_confirm")
    keys = [_key("race") for _ in range(2)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda k: _book(api, target, k), keys))

    accepted = [r for r in results if r.status == 200]
    refused = [r for r in results if r.status == 409]
    assert len(accepted) == 1, f"exactly one must win, got {[r.status for r in results]}"
    assert len(refused) == 1, "the loser must be refused, not silently accepted"
    assert len(_rows_for_slot(api, target["slot_id"])) == 1, "the slot holds two bookings"


# ── invariant: an unknown outcome is reconciled with the SAME key ───────────────────────────


def test_a_timeout_after_commit_is_resolved_by_reconciling_the_same_key(api: MockApi) -> None:
    """Scenario 5 — the hardest invariant in the project.

    The hospital committed the booking and the response was lost. The client cannot distinguish
    this from "nothing happened" except by looking the request up under **the same key**.
    """
    target = _discover(api, "instant_confirm")
    api.arm("timeout_after_commit")
    key = _key("tac")

    lost = _book(api, target, key)
    assert lost.status in TRANSPORT_FAILURES, f"expected the response to be lost, got {lost!r}"

    reconciled = api.get("/appointments/lookup_request", idempotency_key=key)
    assert reconciled.status == 200, f"reconciliation failed: {reconciled!r}"
    assert reconciled.data["booking"]["state"] == CONFIRMED
    assert reconciled.data["reconciliation"]["status"] == "resolved_booked"
    assert len(_rows_for_slot(api, target["slot_id"])) == 1, "reconciling created a second booking"


def test_a_timeout_before_commit_resolves_to_not_booked(api: MockApi) -> None:
    """Identical silence, opposite truth. Only reconciliation tells them apart."""
    target = _discover(api, "instant_confirm")
    api.arm("timeout_after_commit/timeout_before_commit")
    key = _key("tbc")

    lost = _book(api, target, key)
    assert lost.status in TRANSPORT_FAILURES

    reconciled = api.get("/appointments/lookup_request", idempotency_key=key)
    assert reconciled.status == 200
    assert reconciled.data["reconciliation"]["status"] == "resolved_not_booked"
    assert reconciled.data.get("appointment") is None, "no appointment may exist"

    # A REQUESTED booking row is expected — that is the *client's* record of having tried. What
    # must not exist is a confirmed one: "not committed" is a claim about the hospital, not about
    # whether we wrote down the attempt.
    committed = [r for r in _rows_for_slot(api, target["slot_id"]) if r.get("state") == CONFIRMED]
    assert committed == [], "a pre-commit timeout must not leave a confirmed booking"


# ── invariant: a notification failure never rolls back a booking ────────────────────────────


def test_a_notification_failure_leaves_the_booking_committed(api: MockApi) -> None:
    """Invariant #11: telemetry, e-mail and notification failures never roll back a booking.

    The scenario names the **sms** leg, and the booking's own post-commit delivery is what runs
    against it — so the fault is armed *before* the booking and is spent by that delivery pass. An
    earlier version of this test armed it after booking and then asserted a 502 from an explicit
    `POST /notifications`; that call is a *retry*, the occurrence was already spent, and it
    correctly succeeded. The failure had in fact already happened, on the booking's own delivery,
    where nothing was looking.
    """
    target = _discover(api, "instant_confirm")
    api.arm("standard_booking/notification_failure")

    key = _key("notify")
    booked = _book(api, target, key)
    assert booked.status == 200, "the booking itself must still succeed despite the fault"
    booking_id = booked.data["booking"]["booking_request_id"]

    # 1. The appointment stands. This is the invariant, and it is the only thing that matters.
    read_back = api.get(f"/appointments/{booking_id}")
    assert read_back.data["booking"]["state"] == CONFIRMED, "the booking must survive"

    # Scoped to THIS booking's key: the suite shares one database, and several tests discover the
    # same slot, so a bare count of the slot's rows measures the other tests too.
    mine = [r for r in _rows_for_slot(api, target["slot_id"]) if r["idempotency_key"] == key]
    assert len(mine) == 1, f"exactly one booking row for this key, got {mine!r}"
    assert mine[0]["state"] == CONFIRMED

    # 2. The failure is RECORDED on the outbox rather than swallowed. A notification that failed
    #    with no trace is indistinguishable from one that was never attempted.
    entries = api.get("/notifications/outbox", booking_request_id=booking_id).data["entries"]
    by_channel = {e["channel"]: e for e in entries}
    assert set(by_channel) == {"email", "sms"}, "both legs are planned for every booking"
    assert by_channel["sms"]["status"] == "failed", f"sms leg: {by_channel['sms']}"
    assert by_channel["sms"]["last_error"], "a recorded failure must carry its reason"

    # 3. The other leg is not collateral damage: the scenario names one channel, so it faults one.
    assert by_channel["email"]["status"] == "sent", "the email leg must still reach the patient"

    # 4. A retry is a new attempt at the provider, not a re-run of the scenario — and the booking
    #    is still there for it to deliver against.
    retry = api.post("/notifications", {"booking_request_id": booking_id, "channel": "sms"})
    assert retry.status == 200, f"the retry should deliver once the provider recovers: {retry!r}"
    assert retry.data["report"]["sent"], "the retry must actually send"
    after = api.get("/notifications/outbox", booking_request_id=booking_id).data
    assert after["by_status"].get("sent") == 2, "both legs delivered"


# ── invariant: a conflict is a conflict, a failure is not an empty answer ───────────────────


def _stale_slot_target() -> dict[str, str]:
    """The named slot's *real* owner, read from the fixtures.

    Booking it under some other hospital's provider is rejected earlier as `slot_mismatch`, which
    would make this test pass while proving nothing about the version conflict.
    """
    slots = json.loads((REPO_ROOT / "fixtures" / "directory" / "slots.json").read_text())
    slot = next((s for s in slots if s["slot_id"] == STALE_SLOT_ID), None)
    assert slot is not None, f"{STALE_SLOT_ID} is no longer in the slot fixtures"
    return {
        "patient_id": _patient_id(),
        "hospital_id": slot["hospital_id"],
        "provider_id": slot["provider_id"],
        "slot_id": STALE_SLOT_ID,
        "slot_version": str(slot.get("version", 0)),
    }


def test_a_stale_slot_conflict_is_refused_and_leaves_no_booking(api: MockApi) -> None:
    """The scenario names one slot; the client presents a stale version for it."""
    api.arm("standard_booking/stale_slot_409")
    target = _stale_slot_target()

    stale = _book(
        api,
        target,
        _key("stale"),
        expected_slot_version=STALE_SLOT_PRESENTED_VERSION,
    )

    assert stale.status == 409, f"expected a stale-slot conflict, got {stale!r}"
    assert stale.code != "slot_mismatch", (
        "the request was rejected for the wrong reason — the slot was booked under the wrong owner"
    )
    committed = [r for r in _rows_for_slot(api, STALE_SLOT_ID) if r.get("state") == CONFIRMED]
    assert committed == [], "a conflict must not produce a confirmed booking"


def test_an_upstream_failure_is_not_reported_as_no_match(api: MockApi) -> None:
    """§17: an API failure and a genuine "no specialist" are different results.

    Collapsing them would let a fault reach a patient as a real answer — "there is no appointment
    for you" — when in truth the system simply could not ask.
    """
    target = _discover(api, "instant_confirm")

    # A genuine empty result is a *successful* call whose answer is "nothing matched".
    genuine = api.get(
        "/availability",
        hospital_id=target["hospital_id"],
        provider_id=target["provider_id"],
        visit_type="first_visit",
    )
    assert genuine.status == 200
    assert genuine.data["result_kind"] == "slots_found"  # this target has a slot

    # A fault on the same path is not that.
    api.arm("three_transient_errors/service_unavailable_503")
    faulted = None
    for _ in range(4):  # the scenario's first two occurrences are latency and 429
        faulted = api.get(
            "/availability",
            hospital_id=target["hospital_id"],
            provider_id=target["provider_id"],
            visit_type="first_visit",
        )
        if faulted.status >= 400:
            break

    assert faulted is not None
    assert faulted.status >= 400 or faulted.data.get("result_kind") == "upstream_error", (
        f"an upstream failure must be distinguishable from an empty result: {faulted!r}"
    )


def test_an_empty_valid_result_is_a_success_not_an_error(api: MockApi) -> None:
    """§8: "no slots for these constraints" is a completed call, not a failure to retry."""
    target = _discover(api, "instant_confirm")
    api.arm("multi_constraint_search/empty_valid_result")

    result = api.get(
        "/availability",
        hospital_id=target["hospital_id"],
        provider_id=target["provider_id"],
        visit_type="first_visit",
    )

    assert result.status == 200, "a valid empty result is not an HTTP error"
    assert result.data["result_kind"] == "no_slots_for_constraints"
    assert result.data.get("slots") == []


# ── every remaining fault is reproducible at all ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scenario_id", "method", "path"),
    [
        ("three_transient_errors/latency", "GET", "/availability"),
        ("three_transient_errors/rate_limit_429", "GET", "/availability"),
        ("three_transient_errors/service_unavailable_503", "GET", "/availability"),
        ("three_transient_errors/malformed_schema", "GET", "/directory/search"),
        ("network_conflict/stale_coverage_evidence", "GET", "/coverage"),
    ],
)
def test_every_fault_type_is_reproducible(
    api: MockApi, scenario_id: str, method: str, path: str
) -> None:
    """§17: faults are reproducible from seed + scenario + request sequence.

    The assertion is that the fault is *observable* — a distinct status, a distinct `result_kind`,
    or an explicit contract violation. A fault that changes nothing is not a fault.
    """
    target = _discover(api, "instant_confirm")
    api.arm(scenario_id)
    assert api.get("/_faults/state").data["armed"]["scenario_id"] == scenario_id

    query: dict[str, Any] = {}
    if path == "/availability":
        query = {
            "hospital_id": target["hospital_id"],
            "provider_id": target["provider_id"],
            "visit_type": "first_visit",
        }
    elif path == "/coverage":
        plans = api.get("/plans").data.get("plans") or []
        query = {"provider_id": target["provider_id"]}
        if plans:
            query["plan_id"] = plans[0]["plan_id"]
    else:
        query = {"q": "cardiology"}

    observed = False
    response: Response | None = None
    for _ in range(4):  # scenarios differ in which occurrence carries the fault
        started = time.monotonic()
        response = api.get(path, **query)
        elapsed_ms = (time.monotonic() - started) * 1000
        body = response.body if isinstance(response.body, dict) else {}
        if response.status >= 400:
            observed = True
            break
        if body.get("contract_violation") or body.get("conflict"):
            observed = True
            break
        if body.get("result_kind") == "upstream_error":
            observed = True
            break
        # A latency fault is observable as *time*, not as an error. Without this the latency
        # rung of the ladder would read as "no fault fired" when it demonstrably did.
        if elapsed_ms > 2000:
            observed = True
            break

    assert observed, (
        f"{scenario_id!r} was armed but nothing observable happened on {method} {path}. Either the "
        f"fault is not reproducible from its seed, or the scenario no longer matches a live route. "
        f"Last response: {response!r}"
    )
