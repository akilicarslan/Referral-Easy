"""§16's alerting: rules, dedup/cooldown, and a mock email outbox that sends nothing.

    Example rules: terminal failure >20% with at least 20 invocations in 5 minutes; p95 attempt
    latency >2.5 seconds across three windows; an open circuit breaker; quality degradation with
    enough samples. These are initial settings. The scheduler queries OpenSearch and uses
    deduplication/cooldown to produce alert events and mock email outbox entries. The resolved event
    is also visible. A notification failure does not start a new infinite email loop.

Four modules, in the order the sentence above reaches them:

* :mod:`~referraleasy_alerts.rules` — the four rules, as decisions about a measurement;
* :mod:`~referraleasy_alerts.dedup` — a transition is an event, a state is not, and a resolution is
  an event of its own;
* :mod:`~referraleasy_alerts.outbox` — what a notification would say, and what happened to it;
* :mod:`~referraleasy_alerts.models` — the three states a rule can be in, where the third is "not
  enough data" and is deliberately not a pass.

**What is not here.** The scheduler that queries OpenSearch and runs the rules on an interval, and
the durable tables the events and messages land in. The evaluation itself is pure — it takes
measurements and returns decisions — so the loop and the storage are the parts that are still to be
written, and they are named rather than implied.
"""

from referraleasy_alerts.dedup import (
    SEVERITIES,
    AlertState,
    Cooldown,
    Decision,
    Engine,
    InMemoryTransitions,
    TransitionStore,
)
from referraleasy_alerts.models import (
    AlertEvent,
    Evaluation,
    LatencyWindow,
    QualityWindow,
    RateWindow,
    RuleName,
    Severity,
    State,
)
from referraleasy_alerts.outbox import (
    ComposedEmail,
    DeliveryState,
    Message,
    Outbox,
    RecordingSender,
    Sender,
    compose,
)
from referraleasy_alerts.rules import (
    RULES,
    CircuitBreakerOpen,
    QualityDrop,
    Rule,
    SustainedLatency,
    TerminalFailureRate,
)

__all__ = [
    "RULES",
    "SEVERITIES",
    "AlertEvent",
    "AlertState",
    "CircuitBreakerOpen",
    "ComposedEmail",
    "Cooldown",
    "Decision",
    "DeliveryState",
    "Engine",
    "Evaluation",
    "InMemoryTransitions",
    "LatencyWindow",
    "Message",
    "Outbox",
    "QualityDrop",
    "QualityWindow",
    "RateWindow",
    "RecordingSender",
    "Rule",
    "RuleName",
    "Sender",
    "Severity",
    "State",
    "SustainedLatency",
    "TerminalFailureRate",
    "TransitionStore",
    "compose",
]
