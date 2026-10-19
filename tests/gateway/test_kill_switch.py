"""§16's kill switch, on the one route it governs and on the ones it must not.

    The admin kill switch can stop booking writes; the read-only directory remains available.

The sentence has two halves and both are tested. The first is that a booking write is refused while
the switch is engaged. The second is the one that is easy to get wrong by being *too* thorough: the
read-only guidance keeps answering, and so does everything else an operator needs while an incident is
open — the coordinator's own transitions, the admin view, the metrics. A switch that stopped all of
that would be an outage wearing a kill switch's name, and it would strand the workflows an operator
is trying to look at.

These run against `configured_client`, where the guardrail layers are real, so the booking path can
actually reach the switch. In the shipped profile a booking is refused earlier, by §9, and a test
there could not tell the two refusals apart.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import Demo

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_gateway.api import NO_TELEMETRY_SOURCE
from referraleasy_gateway.killswitch import KillSwitch

ENGAGE = {"engage": True, "reason": "the booking path is writing duplicates", "by": "oncall"}
RELEASE = {"engage": False, "reason": "released after the fix was deployed", "by": "oncall"}


def _booking_ready(client: TestClient) -> tuple[Demo, str, str]:
    """A workflow taken to the point where a booking write would succeed."""
    demo = Demo(client)
    workflow_id = demo.seed()
    approval_id = demo.in_booking_flight(workflow_id)
    return demo, workflow_id, approval_id


# ---------------------------------------------------------------------------
# The first half: booking writes stop
# ---------------------------------------------------------------------------


def test_an_engaged_switch_refuses_the_booking_write(configured_client: TestClient) -> None:
    """503 and not 403: it is a condition of the service, not of the caller. The same operator who
    could book a minute ago cannot now, and a different permission would not help."""
    demo, workflow_id, approval_id = _booking_ready(configured_client)
    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)

    response = demo.booking(workflow_id, approval_id=approval_id, version=7)

    assert response.status_code == 503, response.text
    body = response.json()["error"]
    assert body["code"] == "booking_halted"
    # The operator's own reason travels with the refusal. A halt with no stated reason is
    # indistinguishable from a bug.
    assert body["detail"]["reason"] == "the booking path is writing duplicates"
    assert body["detail"]["by"] == "oncall"

    # And nothing was written. A 503 that had already recorded a booking would be the worst of both.
    assert demo.bookings(workflow_id) == []


def test_releasing_the_switch_lets_the_same_write_through(configured_client: TestClient) -> None:
    """The other half of the first sentence, and the reason the refusal is the switch rather than
    something that happened to be true at the time."""
    demo, workflow_id, approval_id = _booking_ready(configured_client)
    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)
    assert demo.booking(workflow_id, approval_id=approval_id, version=7).status_code == 503

    configured_client.post("/v1/admin/control/booking-writes", json=RELEASE, headers=demo.admin)
    response = demo.booking(workflow_id, approval_id=approval_id, version=7)

    assert response.status_code == 200, response.text
    assert len(demo.bookings(workflow_id)) == 1


def test_engaging_twice_keeps_the_first_reason(configured_client: TestClient) -> None:
    """An operator escalating a switch must not have the specific reason replaced by a vaguer one —
    the reason is what the next reader has to act on."""
    demo, _, _ = _booking_ready(configured_client)
    first = configured_client.post(
        "/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin
    ).json()["booking_writes"]
    second = configured_client.post(
        "/v1/admin/control/booking-writes",
        json={"engage": True, "reason": "everything is stopped", "by": "someone else"},
        headers=demo.admin,
    ).json()["booking_writes"]

    assert second["reason"] == first["reason"] == "the booking path is writing duplicates"
    assert second["since"] == first["since"]
    assert second["by"] == "oncall"


# ---------------------------------------------------------------------------
# The second half: read-only guidance keeps answering
# ---------------------------------------------------------------------------


def test_the_help_card_keeps_answering_while_the_switch_is_engaged(
    configured_client: TestClient,
) -> None:
    """§16's *"the read-only directory remains available"*, tested because the tempting implementation is to
    refuse the whole workflow while a switch is on."""
    demo, workflow_id, _ = _booking_ready(configured_client)
    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)

    response = configured_client.get(f"/v1/workflows/{workflow_id}/help", headers=demo.patient)

    assert response.status_code == 200, response.text


def test_the_admin_view_and_the_metrics_keep_answering(configured_client: TestClient) -> None:
    """An operator opens the dashboard *because* they engaged the switch. A switch that took the
    dashboard with it would leave them blind at the one moment they are looking."""
    demo, workflow_id, _ = _booking_ready(configured_client)
    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)

    assert (
        configured_client.get(f"/v1/admin/workflows/{workflow_id}", headers=demo.admin).status_code
        == 200
    )
    assert configured_client.get("/v1/admin/metrics", headers=demo.admin).status_code == 200


def test_the_coordinators_own_transitions_keep_working(configured_client: TestClient) -> None:
    """A workflow already in flight must not be frozen mid-way: the switch stops *booking*, and the
    coordinator still has to be able to move or unwind what is already running."""
    demo, workflow_id, _ = _booking_ready(configured_client)
    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)

    response = demo.move(workflow_id, version=7, to_state=WorkflowState.ABANDONED)

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The control surface itself
# ---------------------------------------------------------------------------


def test_the_switch_state_is_readable_and_says_what_is_still_available(
    configured_client: TestClient,
) -> None:
    demo, _, _ = _booking_ready(configured_client)

    before = configured_client.get("/v1/admin/control", headers=demo.admin).json()
    assert before["booking_writes"]["engaged"] is False
    assert before["booking_writes"]["since"] is None
    assert before["read_only_guidance"] == "available"

    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)
    after = configured_client.get("/v1/admin/control", headers=demo.admin).json()
    assert after["booking_writes"]["engaged"] is True
    assert after["booking_writes"]["reason"] == "the booking path is writing duplicates"


def test_a_released_switch_remembers_it_was_engaged(configured_client: TestClient) -> None:
    """ "Never engaged" and "engaged and released" are different facts about an incident, and a
    released switch that looks untouched erases the second one."""
    demo, _, _ = _booking_ready(configured_client)
    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)

    released = configured_client.post(
        "/v1/admin/control/booking-writes", json=RELEASE, headers=demo.admin
    ).json()["booking_writes"]

    assert released["engaged"] is False
    assert "was: the booking path is writing duplicates" in released["reason"]
    assert released["since"] is not None


@pytest.mark.parametrize(
    "body", [{"engage": True}, {"reason": "x"}, {"engage": True, "reason": ""}]
)
def test_an_incomplete_switch_change_is_refused(configured_client: TestClient, body) -> None:
    """A halt with no reason and no attribution is not a control, it is a mystery."""
    demo, _, _ = _booking_ready(configured_client)

    response = configured_client.post(
        "/v1/admin/control/booking-writes", json=body, headers=demo.admin
    )

    assert response.status_code == 422, response.text


def test_a_patient_session_cannot_touch_the_switch(configured_client: TestClient) -> None:
    """§9's separation, on the route that can stop the service."""
    demo, _, _ = _booking_ready(configured_client)

    assert (
        configured_client.post(
            "/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.patient
        ).status_code
        == 401
    )
    assert configured_client.get("/v1/admin/control", headers=demo.patient).status_code == 401


# ---------------------------------------------------------------------------
# The object, on its own
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_release_is_idempotent_and_does_not_invent_a_history(self) -> None:
        from datetime import UTC, datetime

        switch = KillSwitch()
        now = datetime(2026, 10, 15, tzinfo=UTC)

        released = switch.release(by="oncall", now=now)

        assert released.engaged is False
        assert released.reason == "", "a switch that was never engaged gained a reason"
        assert released.since is None

    def test_the_engaged_state_carries_who_and_when(self) -> None:
        from datetime import UTC, datetime

        switch = KillSwitch()
        now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

        state = switch.engage(reason="testing", by="operator-7", now=now)

        assert state.engaged is True
        assert state.since == now
        assert state.by == "operator-7"


def test_every_gateway_builds_a_released_switch(app: FastAPI, configured_app: FastAPI) -> None:
    """The default matters: a gateway that started with booking writes stopped would look like the
    switch working rather than like a shipped default nobody chose."""
    for candidate in (app, configured_app):
        with TestClient(candidate) as client:
            demo = Demo(client)
            body = client.get("/v1/admin/control", headers=demo.admin).json()
            assert body["booking_writes"]["engaged"] is False


# ---------------------------------------------------------------------------
# The Alerts page's read (§16, §12)
# ---------------------------------------------------------------------------


def test_the_alerts_read_serves_the_rules_and_their_thresholds(client) -> None:
    """Thresholds come from `referraleasy_alerts`, so a page and a rule cannot disagree about what
    fires. Asserted against the package rather than against a copy of the strings."""
    from referraleasy_alerts import RULES

    demo = Demo(client)
    body = client.get("/v1/admin/alerts", headers=demo.admin).json()

    assert body["available"] is True
    assert {row["rule"] for row in body["rules"]} == {str(rule().name) for rule in RULES}
    by_name = {str(rule().name): rule().threshold for rule in RULES}
    for row in body["rules"]:
        assert row["threshold"] == by_name[row["rule"]]


def test_the_alerts_read_answers_without_a_telemetry_cluster(client) -> None:
    """Like the scenarios page and unlike the telemetry pages: the rules are the package's, and a
    profile with no OpenSearch still has four rules to describe."""
    demo = Demo(client)
    response = client.get("/v1/admin/alerts", headers=demo.admin)

    assert response.status_code == 200, response.text
    assert NO_TELEMETRY_SOURCE not in response.text


def test_the_alerts_read_says_nothing_has_been_evaluated(client) -> None:
    """The one thing an alerting page must not do is let an empty list read as a healthy system."""
    demo = Demo(client)
    body = client.get("/v1/admin/alerts", headers=demo.admin).json()

    assert body["evaluation"]["ran"] is False
    assert "7.3" in body["evaluation"]["reason"]
    assert body["outbox"]["sends"] is False
    assert body["outbox"]["messages"] == []


def test_the_alerts_read_carries_the_kill_switch_state(configured_client) -> None:
    """The switch belongs on this page: it is how an operator responds to an alert."""
    demo = Demo(configured_client)
    before = configured_client.get("/v1/admin/alerts", headers=demo.admin).json()["kill_switch"]
    assert before["engaged"] is False

    configured_client.post("/v1/admin/control/booking-writes", json=ENGAGE, headers=demo.admin)
    after = configured_client.get("/v1/admin/alerts", headers=demo.admin).json()["kill_switch"]

    assert after["engaged"] is True
    assert after["reason"] == ENGAGE["reason"]


def test_a_patient_session_cannot_read_the_alerts_page(client) -> None:
    demo = Demo(client)

    assert client.get("/v1/admin/alerts", headers=demo.patient).status_code == 401
