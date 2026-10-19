"""The two tables the gateway transcribes, checked against the migration that owns them (§4, §9).

`workflow_transition` and `policy_rule` are PostgreSQL tables. The memory profile has no database, so
`transitions.LEGAL_EDGES` and `policy_seed.SEED_ROWS` are transcriptions of them — and a transcription
is a second copy of a rule, which is exactly the thing that goes stale without anyone noticing.

So both are re-parsed here, out of `migrations/014_align_domain_vocabulary.sql`, and compared field by
field. The comparison is equality in **both** directions: a row the migration has and the seed does
not is a permission the memory profile silently lacks, and a row the seed has and the migration does
not is one the memory profile silently invents. Neither direction is the safe one.

**The parser is the load-bearing part of this module, so it is not a regex.** The migration separates
its rows with `;` *inside a quoted note* (`'§10 hybrid RAG; distance is computed in code…'`) and inside
a comment (`-- §2.5: a reschedule goes back through slot choice; the old booking is not`). A parse that
split on `;` would stop at that comment and quietly report 56 of the migration's 74 edges — which is
precisely how `LEGAL_EDGES` came to be missing §2.5's and §11's edges while looking verified. Hence:
statements are read with quotes and comments tracked, and every test asserts the parse found a
plausible number of rows before it asserts anything about them. A parser that silently matched nothing
must fail, not pass.
"""

from __future__ import annotations

import pytest

# `gateway_fixtures` sets the import path for the three source roots; the constants below are what
# this module needs from it.
from gateway_fixtures import REPO_ROOT

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_domain import MalformedActionRefError, PolicyDeniedError
from referraleasy_gateway.policy import BOOKING_WRITE_ACTION, WRITE_TOOLS, TablePolicy
from referraleasy_gateway.policy_seed import POLICY_SEED_SOURCE, SEED_ROWS
from referraleasy_gateway.transitions import LEGAL_EDGES, TRANSITION_SOURCE

POLICY_INSERT = "INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls, note) VALUES"
TRANSITION_INSERT = "INSERT INTO workflow_transition (from_state, to_state) VALUES"
STATE_INSERT = "INSERT INTO workflow_state (code, label, terminal, sort_order) VALUES"

#: Fewer than this and the parse is broken rather than the file being small. The tables are §4's, and
#: §4 has more states than this between them.
MIN_PLAUSIBLE_ROWS = 40


def _statement(migration: str, header: str) -> str:
    """The statement that starts at `header`, up to its terminating `;`.

    Comments are dropped and quoted strings are tracked, because the migration's own headings and
    prose contain both `--` and `;` — the two characters a naive split would be made of. The tail of
    an `ON CONFLICT` clause is dropped too: it is not part of the row list, and its `(code)` would
    otherwise be read as a row of one field.
    """
    after = migration.split(header, 1)[1]
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(after):
        char = after[index]
        following = after[index + 1] if index + 1 < len(after) else ""
        if not in_string and char == "-" and following == "-":
            newline = after.find("\n", index)
            if newline == -1:
                break
            index = newline
            continue
        if not in_string and after.startswith("ON CONFLICT", index):
            break
        if in_string and char == "'" and following == "'":  # SQL's escaped quote
            out.append("''")
            index += 2
            continue
        if char == "'":
            in_string = not in_string
        if char == ";" and not in_string:
            break
        out.append(char)
        index += 1
    return "".join(out)


def _rows(statement: str, *, width: int, minimum: int = MIN_PLAUSIBLE_ROWS) -> list[list[str]]:
    """Split a `VALUES` statement into its tuples, one list of fields per row.

    `width` and `minimum` are asserted rather than assumed: a tuple the parser split wrongly, or a
    parse that found none of them, would otherwise be compared against nothing and pass.
    """
    rows: list[list[str]] = []
    field: list[str] = []
    in_string = False
    depth = 0
    index = 0
    while index < len(statement):
        char = statement[index]
        following = statement[index + 1] if index + 1 < len(statement) else ""
        if in_string and char == "'" and following == "'":
            field.append("'")
            index += 2
            continue
        if char == "'":
            in_string = not in_string
            index += 1
            continue
        if in_string:
            field.append(char)
            index += 1
            continue
        if char == "(":
            depth += 1
            if depth == 1:
                rows.append([])
                field = []
            index += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                rows[-1].append("".join(field).strip())
                field = []
            index += 1
            continue
        if char == "," and depth == 1:
            rows[-1].append("".join(field).strip())
            field = []
            index += 1
            continue
        field.append(char)
        index += 1

    assert len(rows) >= minimum, (
        f"the parse found {len(rows)} rows, which is too few to be this table: the statement or the "
        "parser is wrong, and every assertion below it would be vacuous"
    )
    assert {len(row) for row in rows} == {width}, (
        f"expected rows of {width} fields, got widths {sorted({len(row) for row in rows})}"
    )
    return rows


def _migration() -> str:
    assert POLICY_SEED_SOURCE == TRANSITION_SOURCE, (
        "the two transcriptions name different sources, so one of them is being checked against the "
        "wrong migration"
    )
    return (REPO_ROOT / POLICY_SEED_SOURCE).read_text()


def _migration_policy_rows() -> set[tuple[str, str, str, bool, str | None]]:
    """`(role, tool, state, allowed, note)` — the row, minus the column the gateway does not carry."""
    rows = _rows(_statement(_migration(), POLICY_INSERT), width=6)
    additions = (REPO_ROOT / "migrations/015_autonomous_coordination.sql").read_text()
    rows += _rows(_statement(additions, POLICY_INSERT), width=6, minimum=6)
    consent = (REPO_ROOT / "migrations/016_handoff_consent.sql").read_text()
    rows += _rows(_statement(consent, POLICY_INSERT), width=6, minimum=1)
    return {
        (role, tool, state, allowed == "true", None if note == "NULL" else note)
        for role, tool, state, allowed, _max_calls, note in rows
    }


def _migration_edges() -> set[tuple[str, str]]:
    rows = _rows(_statement(_migration(), TRANSITION_INSERT), width=2)
    return {(from_state, to_state) for from_state, to_state in rows}


def _migration_terminals() -> set[str]:
    rows = _rows(_statement(_migration(), STATE_INSERT), width=4, minimum=16)
    return {code for code, _label, terminal, _order in rows if terminal == "true"}


def _seed_rows() -> set[tuple[str, str, str, bool, str | None]]:
    return {(role, tool, state, allowed, note) for role, tool, state, allowed, note in SEED_ROWS}


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def test_the_state_machine_is_the_migration_s_edge_set() -> None:
    """Every edge, in both directions. `LEGAL_EDGES` is the memory profile's `workflow_transition`."""
    migration = _migration_edges()
    assert migration == set(LEGAL_EDGES), (
        "the memory profile's edges and the migration's differ.\n"
        f"  only in the migration (the memory profile illegally refuses them): "
        f"{sorted(migration - set(LEGAL_EDGES))}\n"
        f"  only in {TRANSITION_SOURCE} (the memory profile invents them): "
        f"{sorted(set(LEGAL_EDGES) - migration)}\n"
        "Re-copy the whole INSERT block, not the first section of it."
    )


def test_the_edges_that_leave_a_finished_state_are_present() -> None:
    """Named one by one, because a truncated transcription is exactly how these went missing.

    The three sections after `BOOKING_UNVERIFIED` are §2.5's reschedule path, RECOVERY's exits and
    §11's operator return — the part of the migration that lies beyond the comment containing a `;`.
    A parse that stops there produces a set that looks complete and is 18 edges short, so this test
    asserts each of those edges by name and would fail on its own even if the comparison above were
    somehow weakened.
    """
    for edge in (
        ("BOOKED", "AWAITING_SLOT_CHOICE"),
        ("BOOKED", "CANCELLED"),
        ("BOOKED", "FAILED"),
        ("RECOVERY", "PROVIDER_MATCHING"),
        ("RECOVERY", "AWAITING_SLOT_CHOICE"),
        ("RECOVERY", "READINESS_CHECK"),
        ("HUMAN_HANDOFF", "INTAKE"),
        ("HUMAN_HANDOFF", "AWAITING_SLOT_CHOICE"),
        ("HUMAN_HANDOFF", "CANCELLED"),
    ):
        assert edge in LEGAL_EDGES, f"{edge} is in {TRANSITION_SOURCE} and not in LEGAL_EDGES"
        assert edge in _migration_edges()


def test_only_a_non_terminal_state_has_an_exit() -> None:
    """`workflow_state.terminal` and `workflow_transition` have to agree with each other.

    A state the migration calls terminal and then lets you leave is a contradiction in the data, and
    the direction that matters here is the one `BOOKED` sits on: it is `terminal = false`, which is
    why §2.5 can reschedule out of it. Read from the same file as the edges, so the two cannot be
    checked against different copies.
    """
    terminals = _migration_terminals()
    assert terminals == {"ABANDONED", "CANCELLED", "FAILED"}, (
        "the terminal states are ABANDONED, CANCELLED and FAILED per the migration; a change here "
        "means the edge set and the state table need re-reading together"
    )
    for from_state, to_state in sorted(_migration_edges()):
        assert from_state not in terminals, (
            f"{from_state} is marked terminal and has an edge to {to_state}"
        )


# ---------------------------------------------------------------------------
# The policy table
# ---------------------------------------------------------------------------


def test_the_policy_table_is_the_migration_s_rows() -> None:
    """Row for row, note included — the note is the text a refusal shows, so it is part of the rule."""
    migration = _migration_policy_rows()
    seed = _seed_rows()
    assert seed == migration, (
        "the gateway's policy rows and the migration's differ.\n"
        f"  only in the migration (refused by the memory profile without a row): "
        f"{sorted(migration - seed)}\n"
        f"  only in {POLICY_SEED_SOURCE} (allowed by the memory profile without a rule): "
        f"{sorted(seed - migration)}\n"
        "Re-copy the INSERT block into `policy_seed.SEED_ROWS`."
    )
    assert len(seed) == len(SEED_ROWS), "the seed repeats a (role, tool, state) key"


def test_only_scheduling_may_write_a_booking_and_only_while_it_is_in_flight() -> None:
    """§9's sentence, stated as a query over both tables rather than as a promise about one queue.

    Read from the migration *and* asked of the seeded table, because the two can only disagree if the
    comparison above is wrong — and if it ever is, the answer a client gets is the one that matters.
    """
    assert BOOKING_WRITE_ACTION == "scheduling:create_booking"
    permitted = {
        (role, state)
        for role, tool, state, allowed, _note in _migration_policy_rows()
        if tool == "create_booking" and allowed
    }
    assert permitted == {("scheduling", "BOOKING_IN_FLIGHT")}, (
        "exactly one role may write a booking and only in one state; the migration says "
        + repr(permitted)
    )

    table = TablePolicy.from_seed()
    for state in WorkflowState:
        expected = state is WorkflowState.BOOKING_IN_FLIGHT
        assert table.is_allowed(BOOKING_WRITE_ACTION, state.value) is expected, (
            f"the seeded table answers {not expected} for {BOOKING_WRITE_ACTION} in {state.value}"
        )


def test_every_write_tool_is_gated_by_a_row_that_says_no_somewhere() -> None:
    """A write tool with no denying row is a write nothing can refuse.

    The gate this phase ships (a booking refused while the guardrail dependency is off) is built on
    `WRITE_TOOLS`, so each of them has to be a table-governed action in at least one state.
    """
    rows = _migration_policy_rows()
    for tool in WRITE_TOOLS:
        assert any(row[1] == tool for row in rows), f"{tool} is a write tool with no policy row"


def test_a_denial_carries_the_row_s_own_sentence() -> None:
    """The refusal quotes the migration's note, not a paraphrase written at the route.

    This is the test that fails if someone deletes the note from the seed while leaving the refusal:
    the message would still be a refusal, and it would no longer be the table's reason.
    """
    row = next(
        row
        for row in _migration_policy_rows()
        if row[:3] == ("scheduling", "create_booking", "AWAITING_FINAL_APPROVAL")
    )
    assert row[3] is False and row[4] is not None

    with pytest.raises(PolicyDeniedError) as denial:
        TablePolicy.from_seed().assert_allowed(BOOKING_WRITE_ACTION, "AWAITING_FINAL_APPROVAL")
    message = str(denial.value)
    assert row[4] in message, "the table's own note is not in the refusal"
    assert "scheduling may not call create_booking in state AWAITING_FINAL_APPROVAL" in message
    assert "RE006" in message


def test_an_absent_row_and_a_denied_row_are_different_refusals() -> None:
    """Default deny is not the same sentence as a denial, and a client can act on the difference.

    "There is no rule for this" is a deployment or a typo; "the rule says no" is policy working. A
    single message for both would hide the first kind behind the second, so the two paths are entered
    through the two rows that actually produce them: a tool the table never mentions, and a row that
    says no with a reason attached.
    """
    table = TablePolicy.from_seed()
    with pytest.raises(PolicyDeniedError) as absent:
        table.assert_allowed("scheduling:invented_tool", "BOOKING_IN_FLIGHT")
    assert "no policy_rule row" in str(absent.value)

    with pytest.raises(PolicyDeniedError) as denied:
        table.assert_allowed("referral_intake:create_booking", "INTAKE")
    assert "policy_rule denies it" in str(denied.value)
    assert "§3: no booking exists before the flow reaches it." in str(denied.value)

    assert str(absent.value) != str(denied.value)


def test_a_typo_or_a_malformed_reference_never_evaluates_to_allow() -> None:
    """Two ways to get an action wrong, and neither is a permission.

    A well-formed reference to something the table does not have is default deny. A reference that is
    not an `owner:action` at all is refused as malformed — a `ValueError` the error mapping turns
    into a `422`, because a client that sent `create_booking` with no owner has a bug on its side and
    should not be told that policy denied it.
    """
    table = TablePolicy.from_seed()
    for action, state in (
        ("scheduling:create_booking", "BOOKINGINFLIGHT"),
        ("schedulng:create_booking", "BOOKING_IN_FLIGHT"),
    ):
        assert table.is_allowed(action, state) is False
        with pytest.raises(PolicyDeniedError):
            table.assert_allowed(action, state)

    for malformed in ("", "create_booking", "scheduling:", "a:b:c"):
        with pytest.raises(MalformedActionRefError):
            table.is_allowed(malformed, "BOOKING_IN_FLIGHT")


def test_both_transcriptions_cite_the_file_this_module_parsed() -> None:
    """A drift test that reads a different migration than the code cites is a test of nothing.

    The citation is the only pointer a reader has, so it is checked rather than trusted: the two
    modules must name the same file, that file must exist, and it must be the one holding the two
    statements parsed above.
    """
    assert POLICY_SEED_SOURCE == TRANSITION_SOURCE
    source = (REPO_ROOT / TRANSITION_SOURCE).read_text()
    for header in (POLICY_INSERT, TRANSITION_INSERT, STATE_INSERT):
        assert header in source, f"{TRANSITION_SOURCE} has no `{header}`"
