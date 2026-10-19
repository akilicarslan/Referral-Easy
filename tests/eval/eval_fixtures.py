"""Helpers shared by the `tests/eval` suite.

Uniquely named, and that is not decoration: pytest puts every `conftest.py` it collects on
`sys.path` under the bare name `conftest`, so `from conftest import X` resolves to whichever
directory's conftest pytest imported *last*. `tests/test_test_layout.py` asserts no test module does
that, and the fix it prescribes is a uniquely named module beside the tests — the same way
`tests/gateway/gateway_fixtures.py` and `tests/scripts/scripts_fixtures.py` are named.

Three things live here, and the first is the one that matters:

* **A case is a real dataset case.** The suite scores against `load_dataset()`, not against a
  hand-built ``Case``, because a hand-built one would let the tests pass while the committed dataset
  failed to load — and the committed dataset is what `make eval` runs.
* **A record is a healthy run by default.** `record_for` starts from "everything the case asked for,
  done", so a test that breaks one thing is testing that one thing rather than fighting a dozen
  unrelated defaults.
* **A manifest is written, not stubbed.** The CLI reads the §13 manifest as JSON and refuses to
  invent an id, so the tests write a real file with a real id into `tmp_path`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from referraleasy_eval.dataset import Dataset, load_dataset
from referraleasy_eval.models import (
    BookingRecord,
    ConstraintOutcome,
    Escalation,
    RunRecord,
    ToolCall,
)
from referraleasy_eval.runs import records_as_payload

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A release id in the shape §13 defines, so the CLI's validation is exercised rather than bypassed.
RELEASE_ID = "rel-20261015-abc1234"

#: The slot the standard-booking fixtures actually name, so a record built by default is internally
#: consistent with the dataset's `selected_ids` expectations.
STANDARD_SLOT = "40000000-0000-4000-8000-000000000001"
STANDARD_HOSPITAL = "20000000-0000-4000-8000-000000000001"
STANDARD_PROVIDER = "30000000-0000-4000-8000-000000000001"
STANDARD_EVIDENCE = (
    "50000000-0000-4000-8000-000000000001",
    "50000000-0000-4000-8000-000000000002",
)


def dataset() -> Dataset:
    """The committed dataset. Not a fixture copy — see the module docstring."""
    return load_dataset()


def case(case_id: str) -> Any:
    """One committed case by id, failing loudly rather than returning None."""
    for entry in dataset().cases:
        if entry.case_id == case_id:
            return entry
    raise AssertionError(f"the committed dataset has no case {case_id!r}")


def write_manifest(tmp_path: Path, *, release_id: str = RELEASE_ID) -> Path:
    """A §13 manifest on disk, in the shape the CLI reads."""
    path = tmp_path / "latest.json"
    path.write_text(
        json.dumps(
            {
                "release_manifest_id": release_id,
                "release_version": "1.0.0",
                "source": {
                    "git_sha": "d0b1e2f3" * 5,
                    "image_digest": "sha256:" + "ab" * 32,
                    "lockfile_hash": "cd" * 32,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_runs(tmp_path: Path, records: Sequence[RunRecord]) -> Path:
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(records_as_payload(records)), encoding="utf-8")
    return path


def record_for(
    case_id: str,
    *,
    bookings: Sequence[BookingRecord] | None = None,
    live_approval_ids: Sequence[str] | None = None,
    **overrides: Any,
) -> RunRecord:
    """A run that satisfies the standard-booking case, then whatever the caller changes.

    The defaults are deliberately *generous*: an outcome, a selection, evidence, a live approval and
    a confirmed booking bound to it. A test that starts from a good run and breaks one field is
    testing that field; a test that starts from an empty record is testing the empty record.
    """
    approval = "11111111-2222-4333-8444-555555555555"
    default_bookings: tuple[BookingRecord, ...] = (
        BookingRecord(
            booking_id="bbbbbbbb-0000-4000-8000-000000000001",
            state="CONFIRMED",
            provider_id=STANDARD_PROVIDER,
            hospital_id=STANDARD_HOSPITAL,
            slot_id=STANDARD_SLOT,
            approval_id=approval,
        ),
    )
    fields: dict[str, Any] = {
        "case_id": case_id,
        "workflow_id": "wf-test",
        "outcome": "succeeded",
        "final_state": "BOOKED",
        "selected_provider_id": STANDARD_PROVIDER,
        "selected_hospital_id": STANDARD_HOSPITAL,
        "selected_slot_id": STANDARD_SLOT,
        "bookings": default_bookings,
        "tool_calls": (
            ToolCall(name="create_booking", arguments={"slot_id": STANDARD_SLOT}, outcome="ok"),
        ),
        "delegated_roles": ("coverage_network", "provider_matching", "scheduling"),
        "evidence_ids": STANDARD_EVIDENCE,
        "constraints": (),
        "escalation": None,
        "required_document_kinds": ("referral_letter",),
        "attached_document_kinds": ("referral_letter",),
        "live_approval_ids": (approval,),
        "booking_attempts": 1,
        "latency_ms": 1200,
        "model_calls": 6,
        "tool_calls_made": 9,
        "cost_usd": 0.0004,
        "synthetic": True,
    }
    fields.update(overrides)
    # Popped unconditionally: leaving them in `fields` while also passing them as keywords is a
    # duplicate argument, and only on the path where a caller *did* override them — which is every
    # test that matters and none of the ones that do not.
    default_bookings_value = tuple(fields.pop("bookings"))
    default_approvals_value = tuple(fields.pop("live_approval_ids"))
    return RunRecord(
        bookings=tuple(bookings) if bookings is not None else default_bookings_value,
        live_approval_ids=(
            tuple(live_approval_ids) if live_approval_ids is not None else default_approvals_value
        ),
        **fields,
    )


def healthy_records() -> tuple[RunRecord, ...]:
    """One record per committed case, each satisfying the expectations it can.

    Built from the *dataset* rather than hand-listed, so adding a case does not silently leave the
    suite's happy path covering 43 of 44 and reporting a failure the maintainer did not cause.
    """
    out: list[RunRecord] = []
    for entry in dataset().cases:
        out.append(_healthy_for(entry))
    return tuple(out)


def _healthy_for(entry: Any) -> RunRecord:
    """The record that satisfies one case, driven by that case's own expectation.

    This is the only place in the suite that reads an expectation to *build* rather than to check,
    and it exists so the happy path is derived rather than copied: a case whose expectation moves
    takes this with it, and a case whose expectation cannot be satisfied by any record would show up
    as a failure here rather than as a suspiciously low score in the report.
    """
    expected_documents = entry.expected("required_document_kinds", ("referral_letter",))
    selected = entry.expected("selected_ids", {})
    escalates = entry.expected("escalates")
    escalation = None
    if escalates:
        escalation = Escalation(
            handoff_id="f1000000-0000-4000-8000-000000000001",
            reason_code=str(entry.expected("escalation_reason_code") or "no_match"),
        )
    wanted_outcome = entry.expected("outcome")
    constraints = tuple(
        ConstraintOutcome(code=str(code), kind="hard", satisfied=True)
        for code in entry.expected("constraint_codes", ())
    )
    return record_for(
        entry.case_id,
        outcome=str(wanted_outcome) if wanted_outcome else "succeeded",
        final_state=str(entry.expected("recovered_state") or "BOOKED"),
        selected_hospital_id=selected.get("hospital_id", STANDARD_HOSPITAL),
        selected_provider_id=selected.get("provider_id", STANDARD_PROVIDER),
        selected_slot_id=selected.get("slot_id", STANDARD_SLOT),
        evidence_ids=tuple(entry.expected("required_evidence_ids", STANDARD_EVIDENCE)),
        constraints=constraints,
        escalation=escalation,
        required_document_kinds=tuple(expected_documents),
        attached_document_kinds=tuple(expected_documents),
    )


def record_payload(case_id: str, **overrides: Any) -> Mapping[str, Any]:
    """A record as raw JSON, for the `runs.py` boundary tests."""
    payload = dict(records_as_payload([record_for(case_id)])["records"][0])
    payload.update(overrides)
    return payload
