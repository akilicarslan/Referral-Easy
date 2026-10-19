"""`make seed` is in the demo flow, so it is run more than once — and must leave one state, not two.

Marked `integration`, because "leaves the same state" is a claim about a database and can only be
answered by looking at one. Each test gets a database of its own from the `scratch` fixture, migrated
but otherwise untouched, and the mock hospital API is the fixture-serving stub from
`scripts_fixtures.py`.

**Rows are counted, not assumed.** `table_row_counts` counts every base table in the public schema
rather than a list of tables that "should" be unchanged: a list is where the one table that *did*
change would be left out, and the whole point of `make seed` being safe to re-run is that nothing is
exempt from it.

This is also the end-to-end reason the script is a verifier rather than a loader. A script that wrote
rows would need a story about what a second run does to them; this one cannot write, and the count
comparison is what turns that from an assertion in a docstring into evidence.
"""

from __future__ import annotations

import json
import urllib.request

import psycopg
import pytest
import scripts_fixtures as fx

pytestmark = pytest.mark.integration

seed = fx.seed_module()


def test_two_runs_leave_the_same_state_as_one(scratch: str) -> None:
    """The property the demo flow depends on.

    Both halves are asserted on the same run pair: the second run must exit 0 (it did not fail), and
    the row counts must be identical after it. A script that "passed" the second time by deciding it
    had already run would show up here as a state change, and a script that duplicated its rows would
    show up here as a count change.
    """
    before = fx.table_row_counts(scratch)

    with fx.fake_mock_api() as api_url:
        first = fx.run_seed(scratch, api_url)
        assert first.returncode == seed.EXIT_OK, fx.describe(first)
        after_first = fx.table_row_counts(scratch)

        second = fx.run_seed(scratch, api_url)
        assert second.returncode == seed.EXIT_OK, fx.describe(second)
        after_second = fx.table_row_counts(scratch)

    assert after_first.counts, "no tables were counted, so the comparison below is vacuous"
    assert after_first == before, (
        "the first run changed the database:\n"
        f"  before: {before.describe()}\n  after:  {after_first.describe()}"
    )
    assert after_second == after_first, (
        "the second run left a different state from the first:\n"
        f"  after one:   {after_first.describe()}\n  after two:   {after_second.describe()}"
    )


def test_the_second_run_reports_exactly_what_the_first_did(scratch: str) -> None:
    """Identical output, so a run that quietly stopped verifying one dataset would be visible.

    A count comparison alone would not catch that: a check that silently stopped being performed
    changes no rows. Comparing the report makes "the second run verified the same things" the
    assertion rather than "the second run did not break anything".
    """
    with fx.fake_mock_api() as api_url:
        first = fx.run_seed(scratch, api_url)
        second = fx.run_seed(scratch, api_url)

    assert first.returncode == second.returncode == seed.EXIT_OK, fx.describe(first)
    assert first.stdout == second.stdout, fx.describe(second)


def test_the_run_reports_a_count_for_every_fixture_file_it_compared(scratch: str) -> None:
    """ "n/n" for every dataset, with `n` read from the fixture file here rather than from the run.

    The expectation is recomputed in the test from the same file the script reads, so the assertion
    is "the run compared the corpus" rather than "the run printed the numbers it printed".
    """
    with fx.fake_mock_api() as api_url:
        result = fx.run_seed(scratch, api_url)

    assert result.returncode == seed.EXIT_OK, fx.describe(result)
    for field, fixture in fx.SEED_REPORT_FIELDS:
        expected = len(fx.fixture_records(fixture))
        assert expected > 0, f"{fixture} is empty, so a comparison against it would pass vacuously"
        assert f"{field}={expected}/{expected}" in result.stdout, fx.describe(result)


def test_the_ids_the_api_serves_are_the_fixture_ids(scratch: str) -> None:
    """Every fixture record the run claims to have found is queryable afterwards.

    Asserted directly rather than read out of the run's own summary: the run's summary says whether
    the ids matched, and this asks the API the same question so that a check which reported success
    without making the requests would fail here.

    The API in this test is the stub, so what it establishes is that the fixture corpus is complete
    and self-consistent — every record the script counts has a retrievable id. That the *running*
    service answers the same way is what the live run in this change's evidence shows, and what
    `services/mock-api/test/fixtures-integration.test.ts` owns.
    """
    with fx.fake_mock_api() as api_url:
        for route in fx.ROUTES:
            fixture = fx.fixture_records(route.fixture)
            expected = {record[route.id_key] for record in fixture}
            assert expected, f"{route.fixture} has no {route.id_key}"

            served: set[str] = set()
            groups = (
                [None]
                if route.group_by is None
                else sorted({record[route.group_by] for record in fixture})
            )
            for group in groups:
                url = api_url + route.endpoint
                if group is not None:
                    url += f"?{route.group_by}={group}"
                with urllib.request.urlopen(url, timeout=10) as response:
                    payload = json.loads(response.read())
                served.update(record[route.id_key] for record in payload[route.envelope])

            assert served == expected, (
                f"{route.endpoint}: served {len(served)} id(s), the fixture has {len(expected)}"
            )


def test_a_corpus_that_is_not_loaded_is_reported_by_the_cli(scratch: str) -> None:
    """With nothing answering on `MOCK_API_URL`, the run must fail.

    Not "skip the API checks and verify the database": the corpus is loaded by the service at
    startup, so a service that is not running is a corpus that is not loaded, and a green run there
    would be the lie this script replaced.
    """
    result = fx.run_seed(scratch, "http://127.0.0.1:1")

    assert result.returncode == seed.EXIT_NOT_SEEDED, fx.describe(result)
    assert "mock API reachable" in result.stderr, fx.describe(result)
    assert "make up" in result.stderr, fx.describe(result)


def test_the_run_reports_no_connection_left_open(scratch: str) -> None:
    """The script holds one database connection and must close it.

    A verifier in the demo flow runs beside a migrate and a healthcheck; leaving a connection behind
    is a slow leak that only shows up in the middle of a demo.
    """
    with fx.fake_mock_api() as api_url:
        result = fx.run_seed(scratch, api_url)
    assert result.returncode == seed.EXIT_OK, fx.describe(result)

    with psycopg.connect(scratch, autocommit=True) as connection:
        idle = connection.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()[0]
    assert idle == 0, f"{idle} connection(s) still open after the run"
