# ruff: noqa: RUF002  # §16 is quoted in Turkish, verbatim from the spec
"""§16's alerting: rules, dedup/cooldown, and a mock email outbox that sends nothing.

    Örnek kurallar: 5 dakikada en az 20 invocation varken terminal failure >%20; üç pencerede p95
    attempt >2.5 saniye; circuit breaker açık; yeterli örnekle kalite düşüşü. Bunlar başlangıç
    ayarlarıdır. Scheduler OpenSearch'ü sorgular; dedup/cooldown ile alert event ve mock email outbox
    üretir. Resolved olayı da görünür. Bildirim hatası yeni sonsuz mail döngüsü açmaz.

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
