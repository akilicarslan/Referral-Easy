# ruff: noqa: RUF001, RUF003  # Intentional Unicode patterns and examples.
"""Emergency handling: the one input class that must never be answered with a block (§9).

§9's sentence is short and unusually absolute: *"Emergency language is not silently blocked as
off-topic; it is routed to an approved fixed safety response"* — an emergency phrase is not silently
blocked as
off-topic, it is routed to an approved fixed safety response. Of every failure this system can
produce, silently refusing someone describing a heart attack is the worst, so it is a rule with its
own module rather than one branch among several in layer 1.

Three properties this module is built to make true, each of which a test asserts:

1. **A match is not a refusal.** The verdict is ``block`` because the workflow must not proceed on
   the message, but it carries :data:`APPROVED_EMERGENCY_RESPONSE` — the patient is told what to do
   instead (call the local emergency number), not told that their message was rejected.
2. **It is answered before the layers that could be closed.** The emergency check runs inside layer 1
   and is ordered before every other pattern, so the approved text goes out even when layers 2 and 3
   have nothing behind them and every other input would be refused with §9's closed-dependency rule.
3. **The text says what a demo can honestly say.** It names no service this project cannot call, it
   makes no clinical judgement, and it does not claim a human is standing by when none is. The handoff
   queue is real, so the text points at it as a follow-up rather than as an emergency service.

The pattern list is deliberately short and phrase-based, the same list the gateway ships: a semantic
*might* recognise a paraphrase that this does not, which is why the classifier rung exists, but a
regex that fires on the word "pain" would route a great many ordinary referral messages to a
safety response. The list is widened by measurement, not by imagination.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["APPROVED_EMERGENCY_RESPONSE", "EMERGENCY_PATTERNS", "matches_emergency"]

#: §9's approved fixed safety response. Fixed means fixed: it is not generated, not varied per
#: patient and not rewritten by a model, so it cannot acquire a claim nobody approved.
APPROVED_EMERGENCY_RESPONSE: Final = (
    "If this is a medical emergency, call your local emergency number now (911 in the US, 112 in "
    "the EU and Türkiye). This demo cannot contact emergency services and cannot give medical "
    "advice. A human coordinator can follow up on your referral once you are safe."
)

#: Emergency phrasing, English and Turkish, matched on word boundaries. Turkish agglutinates, so the
#: stems are matched with a trailing ``\w*`` where the inflected forms are common ("nefes alamıyorum"
#: is covered by the literal, "kanama"/"kanamam" by the stem).
EMERGENCY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(chest pain|can'?t breathe|cannot breathe|severe bleeding|unconscious|"
        r"suicid\w*|overdos\w*|heart attack|stroke)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(göğüs ağrısı|nefes alamıyorum|kanama|bayıldım|intihar|kalp krizi|inme)\b",
        re.IGNORECASE,
    ),
)


def matches_emergency(text: str) -> str | None:
    """The emergency phrase found in ``text``, or None.

    Returns the matched text rather than a bool so that the verdict's reason can quote what matched.
    An operator reviewing a routed emergency needs to see *why* it was routed, and "the pattern list
    matched" is not that.
    """
    for pattern in EMERGENCY_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return None
