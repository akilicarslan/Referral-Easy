"""`make demo` reports what happened and never more than that (§18).

The orchestrator's whole value is in what it refuses to say. It used to be a Phase 0 stub whose one
real property was that it would not print a URL it had not started; now that it runs a real
lifecycle, the same property has to survive the rewrite, and it has three ways to break:

* a step fails and the run continues anyway, so a later step's success line covers for it;
* a step is skipped by a flag and is reported as if it ran — the exact failure `--skip-index` warns
  about, where retrieval answers with no evidence and no error;
* the entry-point list stops being the probe's answer and becomes a table of addresses this file
  hopes are right.

Each of those is a test below. The lifecycle itself — fourteen containers converging, fourteen
migrations applying — is not testable without docker, and is what the CI job's `demo` step and a
real `make demo` are for.
"""

from __future__ import annotations

from typing import Any

import pytest
import scripts_fixtures as fx

demo = fx.demo_module()
health = fx.health_module()


@pytest.fixture
def output(capsys: pytest.CaptureFixture[str]) -> Any:
    """Return whatever `main` printed, however it ended."""

    def read() -> str:
        return capsys.readouterr().out

    return read


#: The one step `demo` shells out to without `_run_step`. It is named here so a stub exists for it
#: too — see the note in `_stub_steps`.
INDEX_TEMPLATES = demo.INDEX_TEMPLATES_SCRIPT.name


def _stub_steps(monkeypatch: pytest.MonkeyPatch, *, failing: str | None = None) -> list[str]:
    """Replace the subprocess steps, recording the order they ran in.

    `failing` names the script whose step returns 1 — the only way to reach a later step's success
    line is for this to be ignored, which is what the first test asserts cannot happen.

    **Every subprocess step has to be listed here, and the list is not the same as `_run_step`'s.**
    The index-template step is its own function — it resolves its own URL and passes its own
    environment — so patching `_run_step` alone leaves it real, and a real one curls OpenSearch. That
    is how these tests failed once already: with the stack down the step returned non-zero, the run
    stopped at step 3, and three tests below reported the stop as if it were the failure they were
    asserting. A unit test that reaches the network is measuring the machine, not the code.
    """
    ran: list[str] = []

    def run_step(script: str, *args: str) -> int:
        ran.append(script)
        return 1 if script == failing else 0

    def apply_templates(health_module: Any, env: dict[str, str]) -> int:
        ran.append(INDEX_TEMPLATES)
        return 1 if failing == INDEX_TEMPLATES else 0

    monkeypatch.setattr(demo, "_run_step", run_step)
    monkeypatch.setattr(demo, "_apply_index_templates", apply_templates)
    monkeypatch.setattr(demo, "_compose_up", lambda profiles: (ran.append("compose"), 0)[1])
    # The real health module, with only its probe replaced: faking the module would let this test
    # pass while `demo` called a function the real one does not have.
    monkeypatch.setattr(demo, "_load", lambda name: health)
    return ran


def _probe_answering(
    monkeypatch: pytest.MonkeyPatch, *, down: frozenset[str] = frozenset()
) -> None:
    """Make every probe answer, except the services named in `down`."""

    def probe(entry: Any, env: dict[str, str]) -> tuple[str, str]:
        if entry.service in down:
            return health.DOWN, "refused by the test"
        return health.OK, "answered by the test"

    monkeypatch.setattr(health, "_probe", probe)


def test_a_failed_step_stops_the_run_and_no_later_step_reports_success(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    """The load-bearing property: the success line is unreachable behind a failure.

    `migrate` is the step chosen to fail because everything after it reads through the schema, so a
    run that continued would be reporting on a database that does not exist.
    """
    ran = _stub_steps(monkeypatch, failing="migrate.py")

    code = demo.main(["--no-up"])

    assert code == demo.EXIT_STEP_FAILED, "a failed step did not fail the run"
    printed = output()
    assert "Done." not in printed, "the success line was printed behind a failed step"
    assert "seed.py" not in ran, "the run continued into a step after the failure"
    assert "migrate.py" in ran


def test_validate_env_stops_the_run_before_anything_is_started(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    """Step one is a gate, not a prelude: nothing is brought up for an environment that cannot run."""
    ran = _stub_steps(monkeypatch, failing="validate_env.py")

    code = demo.main([])

    assert code == demo.EXIT_ENV_INVALID
    assert "compose" not in ran, "the stack was started despite an invalid environment"
    assert "Done." not in output()


def test_a_skipped_step_is_reported_as_skipped_not_as_done(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    """`--skip-index` leaves retrieval answering with no evidence, and the report has to say so.

    An empty index answers every query with zero hits and no error — which reads as "no matching
    directory entries" rather than "the indexer never ran". A demo that called that step done would
    be reporting the wrong one of those.
    """
    ran = _stub_steps(monkeypatch)
    _probe_answering(monkeypatch)

    code = demo.main(["--no-up", "--skip-index"])

    assert code == demo.EXIT_OK
    printed = output()
    assert "index_directory.py" not in ran, "the skipped step ran anyway"
    assert "skipped (--skip-index)" in printed
    # And it does not claim the index exists: the script is named nowhere except the skip line.
    assert "index_directory.py" not in printed


def test_the_entry_points_list_is_the_probe_s_answer_and_not_an_address_table(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    """A URL is printed only for a service that answered, and a failure is a failure.

    The gateway is made to fail while the others answer: the run must stop, and the gateway must not
    appear in a list of addresses — printing it would be the stub's original sin, a URL offered for
    something that is not running.
    """
    _stub_steps(monkeypatch)
    _probe_answering(monkeypatch, down=frozenset({"gateway"}))

    code = demo.main(["--no-up"])

    assert code == demo.EXIT_STEP_FAILED, "a service that did not answer did not fail the run"
    printed = output()
    # The exit code alone does not say *which* step stopped the run, and this test only means
    # something if the health step is the one that did. Without this line it passed while an
    # unstubbed index-template step was stopping the run four steps earlier.
    assert "health" in printed, "the run never reached the health step, so this proves nothing"
    assert "entry points" not in printed, "the URL list was printed for a failed probe"
    assert "Done." not in printed


def test_every_answering_service_appears_with_the_url_the_probe_used(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    """The happy path, asserted for the same reason: a list that is always incomplete is no better."""
    _stub_steps(monkeypatch)
    _probe_answering(monkeypatch)

    code = demo.main(["--no-up"])

    assert code == demo.EXIT_OK
    printed = output()
    assert "Done." in printed
    assert "entry points" in printed

    env = health._read_dotenv()
    for entry in health.PROBES:
        if entry.profile != "core":
            continue
        url = health.probe_url(entry, env)
        if url is None:
            # `postgres` is probed through the driver and has no addressable URL, which is why the
            # list is derived from `probe_url` rather than from a table of every service.
            continue
        assert url in printed, f"{entry.service} answered and {url} was not listed"


def test_postgres_is_probed_but_has_no_url_to_print() -> None:
    """The one service whose probe is not a URL, asserted so the skip above is not a silent gap."""
    env = health._read_dotenv()
    postgres = next(entry for entry in health.PROBES if entry.service == "postgres")
    assert health.probe_url(postgres, env) is None
