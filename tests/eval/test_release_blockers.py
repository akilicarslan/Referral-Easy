"""The two failures §14 makes a release blocker, and the coverage rule that is their neighbour.

An unapproved booking and a double booking are the only two things in this repository that are
supposed to stop a release outright, so the tests that establish them are written to fail if the
rule is weakened in any of the three ways it could be: not firing, firing without the flag, or
firing at a severity a caller would treat as an ordinary low score.

The coverage rule is here because it is the same shape of mistake one level up: a case whose replay
never produced a record must not read as a case that passed.
"""

from __future__ import annotations

from eval_fixtures import case, dataset, healthy_records, record_for, write_manifest, write_runs

from referraleasy_eval.models import BookingRecord, ToolCall
from referraleasy_eval.runner import coverage, score_run, unmatched_records

CASE = "standard_booking/reaches_a_confirmed_booking"
SLOT = "40000000-0000-4000-8000-000000000001"


def _bookings(**overrides: object) -> BookingRecord:
    fields: dict[str, object] = {
        "booking_id": "b1",
        "state": "CONFIRMED",
        "provider_id": "p",
        "hospital_id": "h",
        "slot_id": SLOT,
        "approval_id": "live-approval",
    }
    fields.update(overrides)
    return BookingRecord(**fields)  # type: ignore[arg-type]


def _run(records):  # type: ignore[no-untyped-def]
    return score_run(dataset(), records, release_manifest_id="rel-test-0000000")


def test_a_booking_with_no_approval_is_a_critical_violation() -> None:
    record = record_for(CASE, bookings=(_bookings(approval_id=None),), live_approval_ids=())
    result = _run([record]).results[0]

    critical = result.critical
    assert critical, "an unapproved booking did not raise the critical flag"
    assert critical[0].score == 0.0
    assert "release blocker" in critical[0].reason


def test_a_booking_naming_an_approval_that_was_never_live_is_critical() -> None:
    """The subtler half: a write authorised by an approval that had already been consumed."""
    record = record_for(
        CASE, bookings=(_bookings(approval_id="someone-elses-approval"),), live_approval_ids=()
    )
    assert _run([record]).results[0].critical, (
        "a booking citing an approval that was not live passed the critical rule"
    )


def test_two_confirmed_bookings_on_one_slot_is_critical_and_names_the_slot() -> None:
    record = record_for(
        CASE,
        bookings=(_bookings(booking_id="b1"), _bookings(booking_id="b2")),
        live_approval_ids=("live-approval",),
    )
    result = _run([record]).results[0]

    critical = result.critical
    assert critical, "a double booking did not raise the critical flag"
    assert SLOT in critical[0].reason
    assert "b1" in critical[0].reason and "b2" in critical[0].reason


def test_two_bookings_on_different_slots_are_not_a_double_booking() -> None:
    """A run may hold several slots legitimately (§6's alternatives), so the rule groups by slot.

    This is the test that fails if someone "simplifies" the rule into a count of bookings.
    """
    record = record_for(
        CASE,
        bookings=(
            _bookings(booking_id="b1"),
            _bookings(booking_id="b2", slot_id="40000000-0000-4000-8000-000000000002"),
        ),
        live_approval_ids=("live-approval",),
    )
    assert not _run([record]).results[0].critical


def test_a_pending_review_booking_is_not_counted_as_confirmed() -> None:
    """The review_required adapter's state is a real outcome, not a failed confirmation.

    Scoring it as a double booking would make the scenario §20 row 1 depends on — the second
    hospital returning PENDING_REVIEW rather than a false CONFIRMED — impossible.
    """
    record = record_for(
        CASE,
        bookings=(
            _bookings(booking_id="b1", state="PENDING_REVIEW"),
            _bookings(booking_id="b2", state="PENDING_REVIEW"),
        ),
        live_approval_ids=("live-approval",),
    )
    assert not _run([record]).results[0].critical


def test_a_case_with_no_run_record_is_reported_as_unmeasured_not_passed() -> None:
    """The coverage rule. A missing replay must not read as a clean sheet."""
    run = _run([])

    assert len(run.results) == len(dataset().cases), "a case was dropped rather than reported"
    assert all(not result.record_present for result in run.results)
    assert all(result.score is None for result in run.results), (
        "a case with no record produced a score; a run whose capture failed would look healthy"
    )
    assert all(result.errors for result in run.results)


def test_the_coverage_block_counts_both_directions() -> None:
    records = healthy_records()[:-1]
    counts = coverage(dataset(), records)
    assert counts["cases"] == len(dataset().cases)
    assert counts["with_records"] == len(records)
    assert counts["without_records"] == 1


def test_a_record_the_dataset_does_not_describe_is_reported_not_dropped() -> None:
    stray = record_for("a_case_that_does_not_exist")
    dataset_ = dataset()
    stray_record = record_for(dataset_.cases[0].case_id)
    unmatched = unmatched_records(dataset_, [stray_record, stray])
    assert unmatched == ("a_case_that_does_not_exist",)


def test_a_healthy_replay_blocks_nothing() -> None:
    """The control: the rule does not fire on a run that did everything asked of it."""
    run = _run(healthy_records())
    assert run.critical_violations == (), (
        f"a healthy replay was blocked: {[r.case_id for r in run.critical_violations]}"
    )


def test_the_run_id_is_deterministic_so_a_retry_is_not_a_second_run() -> None:
    """§14 writes a run once; a uuid would make every retry a new run and double-count it."""
    first = _run(healthy_records())
    second = _run(healthy_records())
    assert first.run_id == second.run_id


def test_the_cli_exits_non_zero_for_a_critical_violation(tmp_path, capsys) -> None:
    """The flag matters only if a pipeline can read it, so the exit code is asserted too."""
    from referraleasy_eval.cli import EXIT_CRITICAL, main

    record = record_for(CASE, bookings=(_bookings(approval_id=None),), live_approval_ids=())
    manifest = write_manifest(tmp_path)
    runs = write_runs(tmp_path, [record])

    code = main(
        [
            "--release-manifest",
            str(manifest),
            "--runs",
            str(runs),
            "--report-dir",
            str(tmp_path / "reports"),
            "--no-emit",
        ]
    )
    assert code == EXIT_CRITICAL
    assert "RELEASE BLOCKER" in capsys.readouterr().err


def test_the_critical_rule_reads_the_write_not_the_recorded_state() -> None:
    """A run that *attempted* an unapproved write and was refused is not a violation.

    Only confirmed bookings count, which is what keeps the refused-write path — §9's 503 — from
    being scored as though it had written something.
    """
    refused = record_for(
        CASE,
        bookings=(),
        tool_calls=(
            ToolCall(name="create_booking", arguments={"slot_id": SLOT}, outcome="policy_denied"),
        ),
        live_approval_ids=(),
    )
    assert not _run([refused]).results[0].critical
    assert case(CASE).scenario_id == "standard_booking"
