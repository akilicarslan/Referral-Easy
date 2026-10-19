# ruff: noqa: RUF001, RUF002, RUF003  # Turkish quoted from §9, and the Turkish patterns it matches
"""Layer 1 — regex, and deliberately nothing cleverer (§9).

§9's first layer is *"Unicode normalize, uzunluk limiti, kelime sınırları, açık yasak kalıplar"*:
normalise, cap the length, match explicit patterns on word boundaries. It is the cheapest layer and
the only one that is always available, and it carries two distinctions §9 spells out because they are
easy to get backwards:

* **Profanity inside a genuine request is not an attack.** *"Küfür içeren gerçek randevu talebi ile
  saldırı ayrılır"*. A patient who swears about a lost appointment is a patient, and the layer looks
  for a request signal (a date, a ZIP, a member id, a request verb) before it reads the register.
  Only abuse with nothing to answer is refused.
* **A short reply is valid depending on state.** *"'evet', tarih, ZIP code gibi kısa yanıtlar
  state'e göre geçerlidir"*. ``"evet"`` answers a question; with no question open it is not suspicious
  — it is merely unaddressed, which is a ``clarify`` (ask), not a refusal. A length heuristic that
  refused short messages would break the product's most common turn, so there is no length heuristic
  here beyond the upper cap.

The pattern tables are transcribed from the gateway's implementation on purpose. That module is the
runtime until the wiring lands, this one is the package's own, and
``tests/guardrails/test_layer1_parity.py`` fails if the two diverge on any corpus case — so the
duplication is guarded rather than merely intended. When the gateway's copy is deleted, that test is
deleted with it.

Read this layer as a *filter*, not as the defence. §9: *"Input screening prompt injection için tek
savunma değildir"* — the tool allowlist, minimum context, treating sources as data and server-side
policy are what carry prompt injection, and a regex list is not one of them.
"""

from __future__ import annotations

import re
from typing import Final

# First-party: the contract enums. Referenced by the seam, so both sides name the same members.
from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_guardrails.emergency import APPROVED_EMERGENCY_RESPONSE, matches_emergency
from referraleasy_guardrails.types import LayerVerdict, ScreeningRequest, normalise

__all__ = ["METHOD", "RegexGuardrail"]

#: The method id reported with every verdict this layer produces.
METHOD: Final = "regex-v1"

#: Prompt-injection phrasing. Word boundaries, because a substring match would refuse a patient
#: mentioning their own instructions for a visit ("talimatları" in a sentence about paperwork).
_INJECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(ignore|disregard|forget)\b[^.]{0,40}\b(previous|prior|above|earlier)\b[^.]{0,40}"
        r"\b(instruction|prompt|rule)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(system prompt|developer mode|jailbreak|grandma|DAN mode)\b", re.IGNORECASE),
    re.compile(
        r"\b(reveal|print|show|repeat|dump)\b[^.]{0,30}\b(your|the)\b[^.]{0,30}"
        r"\b(system prompt|instructions|rules|api[_ ]key|token)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(önceki|yukarıdaki|evvelki)\b[^.]{0,40}\b(talimat|kural|yönerge)\w*\b[^.]{0,30}"
        r"\b(yoksay|unut|görmezden gel)\w*",
        re.IGNORECASE,
    ),
    re.compile(
        # A mention of the system message is not an attack; asking for it to be *printed* is. The
        # verb is required here because "sistem mesajı gelmedi, bilgilendirir misiniz?" ("the system
        # message did not arrive, can you tell me?") is an ordinary patient sentence that a bare
        # mention would refuse — the same false-positive shape the English patterns avoid by needing
        # an extraction verb or an "ignore"/"disregard" context.
        r"\b(sistem mesaj\w*|sistem talimat\w*)\b[^.]{0,40}"
        r"\b(yazdır|göster|oku|paylaş|ver|açıkla)\w*",
        re.IGNORECASE,
    ),
    # "geliştirici modu" is the Turkish twin of the English bare "developer mode" in the pattern
    # above, and for the same reason: no patient sentence about an appointment contains it, so the
    # mention itself is the signal rather than a verb that would have to follow it.
    re.compile(r"\bgeliştirici mod\w*", re.IGNORECASE),
)

#: Asking the platform to hand over other people's data.
_EXFILTRATION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(list|show|give|send|export)\b[^.]{0,30}\b(all|other|every)\b[^.]{0,30}"
        r"\b(patient|user|record|referral)s?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(tüm|bütün|diğer)\b[^.]{0,30}\b(hasta|kullanıcı|kayıt)\w*\b[^.]{0,30}"
        r"\b(listele|göster|ver|paylaş)\w*",
        re.IGNORECASE,
    ),
)

#: Mild profanity only, and only in English and Turkish. The list is short on purpose: a long list is
#: a false-positive generator, and layer 1's job is the explicit case, not the judgement call.
_PROFANITY: Final[re.Pattern[str]] = re.compile(
    r"\b(f+u+c+k+\w*|s+h+i+t+\w*|bastard|asshole|siktir\w*|orospu\w*|salak|aptal)\b", re.IGNORECASE
)

#: Evidence that a message is a genuine request rather than only an insult: a date, a ZIP, a member
#: id, or a verb that asks for something. This is what separates §9's two cases.
_REQUEST_SIGNALS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?\b"),  # a date
    re.compile(r"\b\d{6,}\b"),  # a member id
    re.compile(r"\b\d{5}\b"),  # a ZIP
    re.compile(
        r"\b(appointment|randevu|doctor|doktor|hospital|hastane|slot|book|randevu al)\w*",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(can you|could you|please|i want|i need|isterim|istiyorum|lütfen)\b", re.IGNORECASE
    ),
)

#: Short replies. Anchored, because the claim is about the *whole* message being an answer — a long
#: sentence that happens to start with "ok" is not a yes/no reply, and letting it through here would
#: be the length heuristic §9 warns about wearing a different hat. A single trailing ``.`` or ``!`` is
#: tolerated because "evet." is the same answer as "evet", and a patient typing a full stop must not
#: turn a valid reply into a message nothing recognises.
_SHORT_REPLIES: Final[re.Pattern[str]] = re.compile(
    r"^(?:evet|hayır|hayir|tamam|olur|peki|yes|no|yep|nope|ok|okay|sure|1|2|3|4|5|\d{1,2}[./-]\d{1,2}"
    r"([./-]\d{2,4})?|\d{5})[.!]?$",
    re.IGNORECASE,
)

#: Categories that close the input path rather than asking a question.
_BLOCKING: Final[frozenset[GuardrailCategory]] = frozenset(
    {
        GuardrailCategory.prompt_injection,
        GuardrailCategory.pii_leak_attempt,
        GuardrailCategory.abuse,
        GuardrailCategory.oversized_input,
    }
)


class RegexGuardrail:
    """§9 layer 1: patterns, normalisation, a length cap, and the two state-aware exceptions."""

    name: GuardrailLayer = GuardrailLayer.regex

    def __init__(self, *, max_chars: int) -> None:
        self._max_chars = max_chars

    @property
    def max_chars(self) -> int:
        return self._max_chars

    def is_configured(self) -> bool:
        """Always. No model, no artifact, no credential — which is why this layer never closes."""
        return True

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        """The patterns, in the order §9 implies: safety first, then attacks, then register, then form.

        ``masked_text`` is ignored: this layer reads ``request.text``. Masking exists so that a
        provider does not receive the patient's identifiers, and layer 1 is the layer that decides
        *whether* the message may be sent at all, so it is the one layer that must see what was
        actually said. Both spellings are accepted because the orchestrator hands over the masked
        text positionally, as §9's seam does.
        """
        collapsed = normalise(request.text)

        if not collapsed:
            return self._verdict(GuardrailCategory.malformed_input, "the message is empty")
        if len(collapsed) > self._max_chars:
            return self._verdict(
                GuardrailCategory.oversized_input,
                f"the message is {len(collapsed)} characters, over the {self._max_chars} limit",
            )

        emergency = matches_emergency(collapsed)
        if emergency is not None:
            return LayerVerdict(
                layer=self.name,
                decision=GuardrailDecision.block,
                category=GuardrailCategory.emergency,
                reason=(
                    f"emergency phrasing {emergency!r} matched; §9 routes this to the approved fixed "
                    "safety response instead of blocking it as off-topic"
                ),
                response=APPROVED_EMERGENCY_RESPONSE,
                method=METHOD,
                decisive=True,
            )

        for pattern in _INJECTION_PATTERNS:
            if pattern.search(collapsed):
                return self._verdict(
                    GuardrailCategory.prompt_injection,
                    "the message tries to change the system's instructions (§9 layer 1)",
                )
        for pattern in _EXFILTRATION_PATTERNS:
            if pattern.search(collapsed):
                return self._verdict(
                    GuardrailCategory.pii_leak_attempt,
                    "the message asks for other patients' data (§9 layer 1)",
                )

        if _PROFANITY.search(collapsed):
            genuine = any(pattern.search(collapsed) for pattern in _REQUEST_SIGNALS)
            if not genuine:
                return self._verdict(
                    GuardrailCategory.abuse,
                    "the message is only abuse; no request is present to answer",
                )
            return self._verdict(
                GuardrailCategory.benign,
                "profanity inside a genuine request is not an input attack (§9 layer 1)",
            )

        if _SHORT_REPLIES.match(collapsed):
            if request.short_reply_expected:
                return LayerVerdict(
                    layer=self.name,
                    decision=GuardrailDecision.allow,
                    category=GuardrailCategory.benign,
                    reason=f"a short reply is valid while {request.workflow_state} has a question open",
                    method=METHOD,
                    # Decisive by construction, and only because the pattern is *anchored*: the whole
                    # message is one of a fixed set of answer strings. A model call on "evet" adds
                    # cost and cannot add information, which is the one skip the conditional profile
                    # makes (§9). The profanity branch above is deliberately *not* decisive — the
                    # same message can carry an injection, and the corpus contains that shape.
                    decisive=True,
                )
            return LayerVerdict(
                layer=self.name,
                decision=GuardrailDecision.clarify,
                category=GuardrailCategory.off_topic,
                reason=(
                    f"{collapsed!r} is a valid answer to a question, but {request.workflow_state} "
                    "has none open; asking is cheaper than guessing (§9 layer 1)"
                ),
                method=METHOD,
            )

        return self._verdict(GuardrailCategory.benign, "no layer-1 pattern matched")

    def _verdict(self, category: GuardrailCategory, reason: str) -> LayerVerdict:
        if category in _BLOCKING:
            decision = GuardrailDecision.block
        elif category is GuardrailCategory.malformed_input:
            # An empty message is not an attack and not an answer: there is something to ask for.
            decision = GuardrailDecision.clarify
        else:
            decision = GuardrailDecision.allow
        return LayerVerdict(
            layer=self.name,
            decision=decision,
            category=category,
            reason=reason,
            method=METHOD,
            # Every category except `benign` is decided by the pattern itself. `benign` is not: a
            # message that merely failed to match anything is exactly the one the model-backed layers
            # are for, and the profanity-inside-a-genuine-request case is benign *and* worth a second
            # look, which is why both of those go all the way down the pipeline.
            decisive=category is not GuardrailCategory.benign,
        )
