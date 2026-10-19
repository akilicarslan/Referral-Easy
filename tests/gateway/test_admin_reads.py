"""The admin read API: §12's nine pages, and the two ways reporting them could lie (ADR-0039).

Two properties carry this file, and every test below is one of them.

**No source is not zero.** A page whose cluster is missing, or whose one query failed, must be
distinguishable from a page that looked and found nothing. §12 forbids a fabricated chart
(*"fabricated static success charts are not used"*) and §14 has the same rule for scores, so the
failure
mode is not "the page breaks" — it is "the page draws a flat line at zero and nobody can tell". The
tests below assert the *shape*: no measurement keys at all on the no-source answer, so a chart cannot
find a number to plot even by accident.

**A window is refused, never defaulted.** An unknown `range`, a `custom` with one bound, a named range
carrying bounds it would ignore, and an unknown timezone are all 422s naming the problem. Each of them
has a plausible wrong answer — silently widen to "day", silently drop the bounds — and each wrong
answer produces a chart that is correct about a window nobody asked for.

The window arithmetic itself is the library's and is covered by `tests/telemetry/test_query.py`; what
is asserted here is that this layer passes it through and reports back what was *resolved*.
"""

from __future__ import annotations

from collections.abc import Iterator

import admin_reads_fixtures as admin_fx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import Demo, demo_settings, profile_settings

from referraleasy_gateway.admin import DEFAULT_TIMEZONE, RANGES, Window
from referraleasy_gateway.api import NO_TELEMETRY_SOURCE
from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.control import SeedableControlState
from referraleasy_gateway.timing import ManualClock

#: The four pages this phase wired. Parametrised so a fifth route cannot be added with its own idea
#: of what "no source" looks like.
PAGES = ("overview", "guardrails", "feedback", "evaluations")


@pytest.fixture
def reads_app(
    settings: GatewaySettings,
    clock: ManualClock,
    control: SeedableControlState,
    request: pytest.FixtureRequest,
) -> FastAPI:
    """An app whose admin reads are the fake, so no OpenSearch is involved.

    Indirect through `request.param` so a test can hand in a *failing* query without a second
    fixture; the default is a healthy one.
    """
    query = getattr(request, "param", None) or admin_fx.FakeQuery()
    return create_app(
        settings=settings,
        clock=clock,
        control=control,
        admin_reads=admin_fx.reads_for(query),
    )


@pytest.fixture
def reads_client(reads_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(reads_app) as test_client:
        yield test_client


def _admin(client: TestClient) -> dict[str, str]:
    return Demo(client).admin


# ---------------------------------------------------------------------------
# 1. No source is not zero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES)
def test_without_a_cluster_every_page_reports_no_source_and_no_numbers(client, page: str) -> None:
    """`client` is the shipped profile, which has no `OPENSEARCH_URL` — so this is the real state.

    The assertion on the key set is the load-bearing one. Checking only `available is False` would
    pass for an answer that also carried `{"count": 0, "latency": {"p95": 0}}`, and a chart rendering
    that is the fabricated zero this whole shape exists to prevent.
    """
    response = client.get(f"/v1/admin/{page}", headers=_admin(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert body["reason"] == NO_TELEMETRY_SOURCE
    assert set(body) == {"available", "reason"}, "a no-source answer carried a measurement"


@pytest.mark.parametrize("reads_app", [admin_fx.exploded()], indirect=True)
@pytest.mark.parametrize("page", PAGES)
def test_a_failing_query_degrades_one_panel_rather_than_the_page(
    reads_client: TestClient, page: str
) -> None:
    """A cluster that refuses one aggregation must not become a 500 for the whole dashboard.

    A different state from the test above — there *is* a cluster and it answered, with a refusal —
    and it has to answer the same way. The reason strings are asserted to differ, because an
    operator reading the page has to be able to tell "nobody configured this" from "this is broken".
    """
    response = reads_client.get(f"/v1/admin/{page}", headers=_admin(reads_client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert "connection refused" in body["reason"]
    assert NO_TELEMETRY_SOURCE not in body["reason"]
    assert set(body) == {"available", "reason"}, "a failed read carried a measurement"


def test_a_source_that_answered_says_so_and_carries_its_samples() -> None:
    """The other half: when the cluster does answer, the page must not report "no source".

    Without this the shape would be satisfiable by answering `available: False` always — a green
    test suite over a dashboard that never draws.
    """
    settings = demo_settings(OPENSEARCH_URL="http://opensearch:9200")
    query = admin_fx.FakeQuery()
    app = create_app(settings=settings, admin_reads=admin_fx.reads_for(query))

    with TestClient(app) as client:
        body = client.get("/v1/admin/overview", headers=_admin(client)).json()

    assert body["available"] is True
    # §12: a sample count on every chart. Asserted on every measurement in the payload rather than
    # on one, because the failure this catches is a *new* aggregation added without its sample.
    # The count is spelled out so adding one to the payload makes this line fail rather than pass
    # with the new measurement unexamined.
    measurements = [
        body["events"],  # 1
        body["agent_triggers"],  # 2
        body["series"]["sample"],  # 3
        *body["outcomes"].values(),  # 4, 5
        *(entry["sample"] for entry in body["latency"].values()),  # 6, 7, 8
        *(entry["sample"] for entry in body["api_failures"]),  # 9
    ]
    assert len(measurements) == 9
    assert len(body["api_failures"]) == 1
    assert len(body["outcomes"]) == 2
    assert len(body["latency"]) == 3
    for sample in measurements:
        assert set(sample) == {"count", "documents", "duplicated", "truncated", "other"}, sample
        assert isinstance(sample["count"], int)
    # Then the values themselves, on one measurement, so the loop above cannot be satisfied by a
    # payload of shaped-but-empty samples.
    assert body["events"]["count"] == 2
    assert body["events"]["documents"] == 3, "the dedup signal was dropped on the way out"
    assert body["events"]["duplicated"] == 1
    assert body["series"]["points"][0]["sample"]["count"] == 2
    # And the three latencies are three distributions, never averaged into one number (§9).
    assert set(body["latency"]) == set(("attempt", "invocation", "turn"))
    assert body["latency"]["turn"]["p95"] == 411.25


# ---------------------------------------------------------------------------
# 2. A window is refused, never defaulted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query_string", "expected"),
    [
        ("?range=week", "range='week' is not one of day, month, custom"),
        ("?range=custom", "range=custom needs both from and to"),
        ("?range=custom&from=2026-10-01T00:00:00Z", "range=custom needs both from and to"),
        ("?range=day&from=2026-10-01T00:00:00Z", "range='day' was given with from/to"),
        ("?range=custom&from=nonsense&to=2026-10-02T00:00:00Z", "from='nonsense' is not"),
        ("?timezone=Mars/Olympus", "timezone='Mars/Olympus' is not usable"),
    ],
)
def test_a_window_that_cannot_be_meant_is_refused(client, query_string: str, expected: str) -> None:
    """Each of these has a plausible wrong answer, and the test names which one it refuses.

    The last case is the one a 500 would hide: the zone is validated by the telemetry library, so
    without the conversion in `admin_query` a typo in a dashboard URL would surface as an
    unhandled exception rather than a 422 naming the zone.
    """
    response = client.get(f"/v1/admin/overview{query_string}", headers=_admin(client))

    assert response.status_code == 422, response.text
    assert expected in response.json()["error"]["message"]


def test_a_custom_window_is_resolved_in_the_zone_that_was_asked_for(client, reads_client) -> None:
    """The payload reports the *resolved* window, in the zone it resolved in.

    Two zones, one wall-clock pair: if the report echoed the request instead of the resolution, both
    answers would be identical and neither would say which instant the chart covers.
    """
    chicago = reads_client.get(
        "/v1/admin/overview?range=custom&from=2026-10-01T00:00&to=2026-10-02T00:00",
        headers=_admin(reads_client),
    ).json()["window"]
    istanbul = reads_client.get(
        "/v1/admin/overview?range=custom&from=2026-10-01T00:00&to=2026-10-02T00:00"
        "&timezone=Europe/Istanbul",
        headers=_admin(reads_client),
    ).json()["window"]

    assert chicago["timezone"] == "America/Chicago"
    assert istanbul["timezone"] == "Europe/Istanbul"
    assert chicago["start"] != istanbul["start"], "the zone did not reach the window"
    # The same wall clock, two offsets: this is what makes "day" mean different instants in the two
    # zones, and it is the reason §12 lists the timezone as a filter rather than a display option.
    assert chicago["start"].endswith("-05:00"), chicago["start"]
    assert istanbul["start"].endswith("+03:00"), istanbul["start"]
    assert chicago["start"].startswith("2026-10-01T00:00")
    assert istanbul["start"].startswith("2026-10-01T00:00")


def test_a_named_range_is_cut_in_the_default_zone(client, reads_client) -> None:
    """`range=day` is midnight-to-midnight local, and the payload says which local."""
    window = reads_client.get("/v1/admin/overview", headers=_admin(reads_client)).json()["window"]

    assert window["range"] == "day"
    assert window["timezone"] == DEFAULT_TIMEZONE
    assert window["interval"] == "hour"
    assert window["start"].endswith("-05:00") or window["start"].endswith("-06:00"), (
        "a Chicago day is five or six hours behind UTC, never zero"
    )


def test_the_gateway_default_zone_is_the_library_default() -> None:
    """The one constant this module copies, and the reason it can be copied safely.

    `TimeRange` spells the zone as a default argument rather than a constant, and `Window` needs a
    value before a `TimeRange` exists. This asserts the two agree, so the copy cannot drift into a
    gateway that reports one zone and windows by another.
    """
    assert Window().timezone == DEFAULT_TIMEZONE
    assert Window().resolve().timezone == DEFAULT_TIMEZONE


# ---------------------------------------------------------------------------
# 3. Authentication and filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES)
def test_a_patient_session_cannot_read_an_admin_page(client, page: str) -> None:
    """§9: the session kinds are separate, on the new routes as on the old ones.

    401 and not 403 **in this profile**, and the reason is worth stating because the opposite is
    tempting: the patient and admin tokens are signed with different fixture secrets, so the admin
    verifier cannot verify a patient token at all and never reaches the audience claim. The 403 case
    needs the secrets to match — `test_a_token_for_the_other_audience_is_refused_when_the_secrets_match`
    in `test_sessions.py` sets that up, and the test below repeats it for this route family.
    """
    demo = Demo(client)
    response = client.get(f"/v1/admin/{page}", headers=demo.patient)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("page", PAGES)
def test_the_audience_claim_is_what_separates_the_pages(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState, page: str
) -> None:
    """With one secret behind both surfaces, the `aud` claim is the only thing standing.

    That is the configuration where "the routes are admin-scoped" is actually tested: a token that
    verifies cryptographically is still refused at the wrong surface, and the refusal is 403 because
    re-authenticating as the same principal would not help.
    """
    shared = profile_settings(settings, admin_secret=demo_settings().patient_secret)
    app = create_app(
        settings=shared,
        clock=clock,
        control=control,
        admin_reads=admin_fx.reads_for(admin_fx.FakeQuery()),
    )
    with TestClient(app) as client:
        demo = Demo(client)
        refused = client.get(f"/v1/admin/{page}", headers=demo.patient)
        allowed = client.get(f"/v1/admin/{page}", headers=demo.admin)

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "audience_mismatch"
    # And the refusal is scoped, not blanket: the admin token still works on the same route.
    assert allowed.status_code == 200
    assert allowed.json()["available"] is True


@pytest.mark.parametrize("page", PAGES)
def test_an_admin_page_without_a_session_is_a_401_not_a_422(client, page: str) -> None:
    """The dependency's annotation has to be resolvable, or FastAPI turns it into a query parameter.

    `api.py` documents this trap at length: with `from __future__ import annotations`, an
    `Annotated[..., Depends(...)]` built from a closure silently degrades into a *required query
    parameter*, which answers 422 for a missing session where 401 belongs. The `AdminQuery`
    dependency added alongside the routes is a module-level function, so this asserts the two
    coexist rather than one shadowing the other.
    """
    response = client.get(f"/v1/admin/{page}")

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthenticated"


def test_the_filter_set_reaches_the_query() -> None:
    """§12's dimensions are carried through, and an absent one does not filter.

    `synthetic` is asserted on the three states rather than one, because a filter that read "absent"
    as a value would hide every record of the other label without saying it had — §15 keeps
    "unlabelled" out of the vocabulary precisely so that cannot happen quietly.
    """
    query = admin_fx.FakeQuery()
    app = create_app(settings=demo_settings(), admin_reads=admin_fx.reads_for(query))
    with TestClient(app) as client:
        client.get(
            "/v1/admin/overview?agents=scheduling&agents=triage&releases=2026.10.15&synthetic=live_demo",
            headers=_admin(client),
        )
        filtered = query.calls[0][2].clauses()
        query.calls.clear()
        client.get("/v1/admin/overview", headers=_admin(client))
        unfiltered = query.calls[0][2].clauses()

    assert filtered == [
        {"terms": {"log.attributes.agent@role.keyword": ["scheduling", "triage"]}},
        {"terms": {"log.attributes.agent@release.keyword": ["2026.10.15"]}},
        {"term": {"log.attributes.synthetic.keyword": "live_demo"}},
    ], (
        "The clause list is asserted whole rather than by substring: these field names are the "
        "encoding `conventions` chose for a dotted attribute, and a filter that silently targeted "
        "the wrong field would match nothing and read as 'no such agent'."
    )
    assert unfiltered == [], "an absent filter became a clause"


def test_the_feedback_denominator_is_turns_and_the_payload_says_so() -> None:
    """§12's participation rate needs a denominator, and a chart cannot check a number it cannot see.

    The turn count is the population a vote could have come from (§15 measures every turn), and it
    is reported beside the summary so the page can show "2 of 8 turns" rather than a bare percentage.
    """
    query = admin_fx.FakeQuery()
    app = create_app(settings=demo_settings(), admin_reads=admin_fx.reads_for(query))
    with TestClient(app) as client:
        body = client.get("/v1/admin/feedback", headers=_admin(client)).json()

    assert body["available"] is True
    assert body["denominator"]["label"] == "turns"
    assert "latency.turn.ms" in body["denominator"]["metric"]
    # The fake's `event_count` returns 2, and that is what the summary was given — the route did not
    # invent a denominator of its own.
    assert query.feedback_total == 2
    assert body["summary"]["total_responses"] == 2
    assert body["summary"]["raw_count"] == 2
    assert body["summary"]["like_rate"] == 0.5


def test_every_page_that_has_a_window_reports_the_ranges_it_accepts(client) -> None:
    """`RANGES` is what the routes validate against, so a page cannot document a wider set.

    Asserted through the refusal message rather than by importing the tuple, because the message is
    what a caller reads.
    """
    message = client.get("/v1/admin/overview?range=quarter", headers=_admin(client)).json()[
        "error"
    ]["message"]
    assert all(name in message for name in RANGES)


def test_every_page_answers_live_against_the_fake_transport() -> None:
    """A new aggregation the fake does not implement must fail here, not silently become "no source".

    This is the trap `_guarded` creates: it catches `Exception` so one panel cannot take a page down,
    which means a `TelemetryReads` that starts calling a method the fake lacks raises
    `AttributeError`, gets swallowed, and reports as "no source" — a green suite over a page that
    never ran. So the assertion is `available is True` on every page: a fake that has fallen behind
    the reader cannot satisfy it.
    """
    query = admin_fx.FakeQuery()
    app = create_app(settings=demo_settings(), admin_reads=admin_fx.reads_for(query))

    with TestClient(app) as client:
        answers = {
            page: client.get(f"/v1/admin/{page}", headers=_admin(client)).json() for page in PAGES
        }

    assert all(body["available"] is True for body in answers.values()), answers
    # And the fake was actually exercised, rather than every page answering from a cache.
    assert len(query.calls) >= len(PAGES)


# ---------------------------------------------------------------------------
# 4. Fault Scenarios: the one read that is not telemetry
# ---------------------------------------------------------------------------


def test_the_scenario_page_answers_without_a_telemetry_cluster(client) -> None:
    """`client` is the shipped profile, which has no `OPENSEARCH_URL`.

    Every other admin page reports "no source" there, and this one must not: the scenarios are a
    committed fixture, so gating them on a cluster that has nothing to do with them would be the
    wrong absence — and would hide eight scenarios from exactly the profile most likely to be reading
    a README.
    """
    response = client.get("/v1/admin/scenarios", headers=_admin(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert len(body["scenarios"]) == 8
    assert NO_TELEMETRY_SOURCE not in response.text


def test_the_scenarios_are_the_ones_the_evaluation_validates_against(client) -> None:
    """One manifest, two readers — asserted through both public surfaces.

    `load_dataset()` refuses a case whose `scenario_id` is not in the manifest, so a successful load
    proves the dataset's scenarios are all in the file this route serves. The equality below is the
    other direction: no scenario is served that the evaluation cannot describe, which would be a
    page offering something to run that nothing measures.
    """
    from referraleasy_eval.dataset import DEFAULT_DATASET, load_dataset

    dataset = load_dataset(DEFAULT_DATASET)
    evaluated = {case.scenario_id for case in dataset.cases}
    served = {
        entry["scenario_id"]
        for entry in client.get("/v1/admin/scenarios", headers=_admin(client)).json()["scenarios"]
    }

    assert served == evaluated


def test_every_scenario_carries_what_the_page_renders(client) -> None:
    """The page reads four fields off each entry, so all four have to be there — a missing one would
    render as an empty section rather than as a failure."""
    scenarios = client.get("/v1/admin/scenarios", headers=_admin(client)).json()["scenarios"]

    for entry in scenarios:
        assert entry["title"], entry["scenario_id"]
        assert entry["summary"], entry["scenario_id"]
        assert isinstance(entry["fault_scenarios"], list), entry["scenario_id"]
        assert entry["assertions"], entry["scenario_id"]
        assert isinstance(entry["expected_result_kinds"], list), entry["scenario_id"]


def test_a_missing_manifest_is_no_source_and_not_an_empty_list(tmp_path) -> None:
    """ "no scenarios" and "no file" are different problems and only one is about the scenarios."""
    from referraleasy_gateway.admin import load_scenarios

    absent = load_scenarios(tmp_path / "nope.json")
    assert absent["available"] is False
    assert "nope.json" in absent["reason"]

    hollow = tmp_path / "hollow.json"
    hollow.write_text('{"scenarios": []}', encoding="utf-8")
    assert load_scenarios(hollow)["available"] is False


def test_the_read_says_no_run_is_linked(client) -> None:
    """§12 asks for a trace and metric link per scenario. Saying so is the honest half of not having
    one — a blank column reads as a run that produced nothing."""
    body = client.get("/v1/admin/scenarios", headers=_admin(client)).json()

    assert body["runs"]["linked"] is False
    assert "7.4" in body["runs"]["reason"]


# ---------------------------------------------------------------------------
# 5. The operator queue (§11, §12)
# ---------------------------------------------------------------------------


def test_the_human_queue_says_which_profile_it_is_on(client, backend: str) -> None:
    """Two profiles, two answers, and the difference has to be visible.

    This suite is parameterised over the control-state backend, and the first version of this test
    asserted only the memory branch — so it *passed* in the memory profile and failed under
    `[postgres]`, where the route correctly answered `available: True` against a real handoff table.
    The test was wrong and the code was right, which is the useful direction for that to go.

    In `memory` there is no handoff table, so the route must not answer an empty list: "nobody is
    waiting" and "there is no queue to read" are different facts, and an operator must not have to
    guess which one they are looking at. In `postgres` the queue is real and may legitimately be
    empty — and then `available` is True, because the read happened.
    """
    response = client.get("/v1/admin/human-queue", headers=_admin(client))

    assert response.status_code == 200, response.text
    body = response.json()
    if backend == "memory":
        assert body["available"] is False
        assert "in-memory control profile" in body["reason"]
        assert "tickets" not in body, "a profile without a store answered with a queue"
    else:
        assert body["available"] is True
        # The database is emptied before each test, so the queue is empty *because it was read*.
        assert body["tickets"] == []
        assert body["counts"] == {"open": 0, "claimed": 0, "claimed_expired": 0}
        assert body["truncated"] is False


def test_the_human_queue_read_and_the_workers_cli_share_one_statement() -> None:
    """The property §11 needs: the dashboard and the operator see the same queue.

    Asserted structurally rather than by comparing results, because the two callers reach the same
    `HandoffStore.open_tickets` — a second SQL statement here would be a second answer, and the two
    would drift the first time someone added a `WHERE`.
    """
    import inspect

    from referraleasy_gateway import human_queue
    from referraleasy_workers import hitl

    assert "open_tickets" in inspect.getsource(human_queue)
    assert "open_tickets" in inspect.getsource(hitl.queued_handoffs)
    # And the statement itself lives in one place.
    from referraleasy_domain.handoff import HandoffStore

    assert "FROM handoff WHERE state IN" in inspect.getsource(HandoffStore.open_tickets)


def test_the_human_queue_route_takes_no_write(client) -> None:
    """§11's races are decided by the database's compare-and-set in the worker's path. A second
    arbiter on the dashboard is exactly what the queue's design does not need."""
    admin = _admin(client)

    for method in (client.post, client.put, client.patch, client.delete):
        response = method("/v1/admin/human-queue", headers=admin)
        assert response.status_code in (404, 405), (method.__name__, response.status_code)
