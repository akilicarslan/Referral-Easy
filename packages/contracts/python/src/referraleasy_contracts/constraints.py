"""Cross-field rules that JSON Schema expresses but the generated Pydantic models do not.

`datamodel-code-generator` does not translate `allOf` + `if`/`then` into Pydantic validators, so an
instance that the frozen schema **rejects** can still be constructed as a Pydantic model. That gap
is a real hazard: code that validates by constructing a model would accept an envelope the contract
forbids.

Two ways to close it: weaken the schema so the layers agree, or restate the rule here and assert in
`tests/contracts/test_schema_bindings.py` that both layers agree. The schema is the contract (§1.1),
so it wins — the rule is restated here instead.

Anything added here must have a matching `if`/`then` (or equivalent) in the schema, and a test
proving the two layers accept and reject the same instances.
"""

from __future__ import annotations

from referraleasy_contracts.generated.common import ResultOutcome
from referraleasy_contracts.generated.task import ResultEnvelope

#: Outcomes that describe a failure and therefore must carry an `AgentError`.
_ERROR_OUTCOMES = frozenset({ResultOutcome.retryable_error, ResultOutcome.terminal_error})

#: Outcomes that describe a successful call and therefore must NOT carry an error.
#: `no_match` belongs here: §8 is explicit that a valid empty result is not an error.
_NON_ERROR_OUTCOMES = frozenset(
    {ResultOutcome.completed, ResultOutcome.no_match, ResultOutcome.needs_input}
)


class ContractViolation(ValueError):
    """An instance the frozen schema rejects but Pydantic alone would accept."""


def validate_result_envelope(envelope: ResultEnvelope) -> None:
    """Enforce the outcome <-> error coupling declared in `task.json`'s `ResultEnvelope.allOf`.

    Raises :class:`ContractViolation` if the envelope is inconsistent.
    """
    if envelope.outcome in _ERROR_OUTCOMES and envelope.error is None:
        raise ContractViolation(
            f"outcome {envelope.outcome.value!r} requires an `error` object; "
            "use `completed` with an empty payload for a valid empty result."
        )
    if envelope.outcome in _NON_ERROR_OUTCOMES and envelope.error is not None:
        raise ContractViolation(
            f"outcome {envelope.outcome.value!r} must not carry an `error`; "
            f"got {envelope.error.__class__.__name__}."
        )
