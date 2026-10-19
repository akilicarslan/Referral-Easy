"""The two §5 → §15 outcome mappings agree, on every value they both claim to answer for.

There are two of them and there is a reason: the gateway's version answers for a value the agent
runtime cannot see — a *refused* turn never produces a `ResultOutcome` at all, because §9's guardrail
raises out of the command — while the agent runtime's answers for an inbound task, which is never
refused that way. Neither table can be derived from the other, and the packages they would share
(`referraleasy_telemetry` does not depend on `referraleasy_contracts` and should not start) do not
exist.

So the duplication is declared, and this file is what pays for it. The precedent is
`referraleasy_gateway.guardrails`, which used to carry its own layer 1 and masker and kept a parity
test against `packages/guardrails` until the copies were deleted. A mapping that drifts produces the
worst kind of wrong chart: one that is correct in one service and incorrect in another, with nothing
to indicate which is which.
"""

from __future__ import annotations

import pytest

from referraleasy_agent_runtime.results import outcome_class
from referraleasy_contracts.generated.common import ReasonCode, ResultOutcome
from referraleasy_gateway.commands import turn_outcome


@pytest.mark.parametrize("outcome", list(ResultOutcome))
def test_both_mappings_agree_on_every_result(outcome: ResultOutcome) -> None:
    """`turn_outcome(ok)` is the gateway's table with no refusal reason, so it must match the
    runtime's — the gateway adds rows for refusals, it does not reinterpret results."""
    assert turn_outcome(outcome, ReasonCode.ok) == outcome_class(outcome)


def test_the_gateway_table_is_total_over_the_result_vocabulary() -> None:
    """A new `ResultOutcome` must not fall through the gateway's mapping.

    The runtime's `outcome_class` is total by construction (a dict lookup that raises `KeyError`), so
    a value added to §5's vocabulary fails there loudly. The gateway's has a `None` branch — "the turn
    never reached an outcome" — which would swallow a *missing row* as if it were a missing outcome.
    This is the check that tells the two apart.
    """
    for outcome in ResultOutcome:
        assert turn_outcome(outcome, ReasonCode.ok) is not None, (
            f"{outcome} has no §15 class in the gateway's mapping. It is not an unclassified turn — "
            f"the value exists in §5's vocabulary and the table simply does not answer for it."
        )


def test_neither_mapping_invents_a_class_for_a_result_that_has_none() -> None:
    """`None` is reachable in both services and means "there is no outcome", not "unmapped value".

    The runtime reaches it through `project_failure`, which produces a result with no §5 outcome for
    a failure it cannot name; the gateway reaches it through a turn that never emitted a `terminal`.
    An earlier version of `outcome_class` indexed the table directly and answered a `KeyError` — the
    same shape as a mapping gap, which is how a missing row would have looked. Both services are
    asserted here so that one of them cannot start inventing a class while the other does not.
    """
    assert outcome_class(None) is None
    assert turn_outcome(None, ReasonCode.ok) is None


@pytest.mark.parametrize(
    "reason", [ReasonCode.guardrail_blocked, ReasonCode.policy_blocked], ids=lambda r: r.value
)
def test_a_refusal_outranks_the_result(reason: ReasonCode) -> None:
    """The gateway-only rows, and the reason they are gateway-only.

    A refusal is not a result: §9's guardrail and §7's policy raise rather than returning, so the
    class cannot come from a `ResultOutcome` and has to come from the reason code. §16's alert rules
    are written on `blocked`, which is why it must not be flattened into `error`.
    """
    from referraleasy_telemetry import OutcomeClass

    assert turn_outcome(ResultOutcome.terminal_error, reason) is OutcomeClass.BLOCKED
    assert turn_outcome(None, reason) is OutcomeClass.BLOCKED
