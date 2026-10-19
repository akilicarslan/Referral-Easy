# ruff: noqa: RUF002  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Feedback: the last active thumbs per response, and the three numbers (§15, §12).

§15 spends a sentence on this and every clause of it is load-bearing:

    Feedback ``response_id`` başına son aktif thumbs up/down ile sayılır; ham sayı, beğeni oranı ve
    katılım oranı ayrıdır.

Three decisions are encoded here.

**"Last active" — not every vote.** A patient who taps thumbs-down and then changes it to
thumbs-up has expressed one opinion, the second one. Counting rows would report two, and — worse —
would let a response with a flip-flop count twice in one denominator. So votes are collapsed to the
latest per ``response_id`` *before* anything is counted. A response with no vote is absent from the
collapsed set, which is what makes the three numbers below distinguishable at all.

**Three separate numbers, never one.** They answer three different questions and can move in
opposite directions:

======================  ==========================  ============================================
raw count               how many responses were      a product decision: is anyone using it
                        rated at all
like rate               likes / (likes + dislikes)   of the people who answered, how many were
                                                     happy
participation rate      rated responses / all        is the signal trustworthy, or is it four
                        responses                    votes out of nine hundred
======================  ==========================  ============================================

A single "satisfaction" figure would hide the case §12's dashboard most needs to show: a 100% like
rate from four responses out of nine hundred is not a healthy service, and a merged number reports
it as perfect.

**An undefined rate is ``None``, not 0.0.** With no votes there is no like rate; with no responses
there is no participation rate. Returning ``0.0`` would chart as "everybody hated it" and as "nobody
participated", both of which are claims the data does not support. §14's "zero samples shown as
no data" is the same principle, and the admin layer renders ``None`` as no data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

#: The only two vote values. A third would have to be given a meaning in the like rate, and there
#: is no third meaning to give it.
LIKE: Final[str] = "up"
DISLIKE: Final[str] = "down"
VOTES: Final[tuple[str, str]] = (LIKE, DISLIKE)


@dataclass(frozen=True, slots=True)
class Vote:
    """One thumbs event.

    ``occurred_at`` is what makes "last" well-defined. It is a comparable value — an ISO-8601
    string or an epoch number — rather than a ``datetime`` so that the value can come straight out
    of an OpenSearch hit without a parse in between, and compared with the same ordering OpenSearch
    used to sort.
    """

    response_id: str
    value: str
    occurred_at: Any = None

    def __post_init__(self) -> None:
        if self.value not in VOTES:
            raise ValueError(f"vote value must be one of {VOTES}, got {self.value!r}")
        if not self.response_id:
            raise ValueError(
                "a vote without a response_id cannot be attributed to a response, so it would be "
                "counted in the raw count and dropped from the like rate"
            )


@dataclass(frozen=True, slots=True)
class FeedbackSummary:
    """The three numbers, plus enough context to know whether to trust them."""

    responses_with_feedback: int
    likes: int
    dislikes: int
    total_responses: int
    like_rate: float | None
    participation_rate: float | None
    truncated: bool = False
    other_responses: int = 0

    @property
    def raw_count(self) -> int:
        """The raw count: responses that ended up with a vote, after collapsing to the last one.

        Named for what the dashboard calls it. It is a count of *responses*, not of vote rows,
        because a rate whose numerator and denominator disagree about what a unit is produces
        percentages above 100.
        """
        return self.responses_with_feedback

    @property
    def has_data(self) -> bool:
        """False when there is nothing to show. The admin layer renders this as "no data"."""
        return self.responses_with_feedback > 0 or self.total_responses > 0

    def as_dict(self) -> dict[str, Any]:
        """Flat form for an API response or a chart tooltip, with all three numbers present."""
        return {
            "raw_count": self.raw_count,
            "like_rate": self.like_rate,
            "participation_rate": self.participation_rate,
            "likes": self.likes,
            "dislikes": self.dislikes,
            "responses_with_feedback": self.responses_with_feedback,
            "total_responses": self.total_responses,
            "truncated": self.truncated,
            "other_responses": self.other_responses,
        }


def last_active(votes: Iterable[Vote]) -> list[Vote]:
    """Collapse ``votes`` to the latest vote per ``response_id``.

    Ordering is by ``occurred_at`` when present, and otherwise by position in ``votes`` — the
    caller's order, which for a query result is the ``sort`` it asked OpenSearch for. A missing
    timestamp therefore does not silently make the *first* vote win; it defers to the ordering the
    caller already established.

    Ties — two votes on one response with the same timestamp — resolve to the later one in iteration
    order. The alternative, refusing the tie, would make a duplicate-delivery of the same vote the
    end of the chart. Both outcomes are wrong in some way; picking one and stating it is better than
    raising in a read path.
    """
    latest: dict[str, Vote] = {}
    for index, vote in enumerate(votes):
        current = latest.get(vote.response_id)
        if current is None or _later_than(vote, current, index):
            latest[vote.response_id] = vote
    return list(latest.values())


def _later_than(candidate: Vote, current: Vote, index: int) -> bool:
    if candidate.occurred_at is None or current.occurred_at is None:
        return True  # iteration order already encodes the sort
    return bool(candidate.occurred_at >= current.occurred_at)


def summarise_feedback(
    votes: Sequence[Vote],
    *,
    total_responses: int,
    truncated: bool = False,
    other_responses: int = 0,
) -> FeedbackSummary:
    """The three §15 numbers from raw votes.

    ``total_responses`` is the denominator of the participation rate and is **required**, with no
    default. It cannot be derived from the votes — the responses nobody rated are exactly the ones
    that are missing — so the caller has to say how many responses there were. A default would make
    "participation is 4/4" the answer for every query that forgot, which reads as perfect
    engagement.

    ``truncated`` and ``other_responses`` carry the OpenSearch terms-aggregation overflow: when the
    aggregation's ``size`` is smaller than the number of distinct responses, ``sum_other_doc_count``
    is non-zero and the like rate is then computed over a *subset*. Reporting that rather than
    hiding it is the difference between a chart with a caveat and a chart that is quietly wrong.
    """
    if total_responses < 0:
        raise ValueError("total_responses cannot be negative")
    collapsed = last_active(votes)
    likes = sum(1 for vote in collapsed if vote.value == LIKE)
    dislikes = sum(1 for vote in collapsed if vote.value == DISLIKE)
    rated = likes + dislikes

    like_rate = (likes / rated) if rated else None
    participation = (rated / total_responses) if total_responses else None

    if rated > total_responses:
        # Worth refusing rather than charting: it means the caller's denominator is from a
        # different population than the votes (a different time range, most often), and an
        # above-100% participation rate on a dashboard destroys trust in every other number on it.
        raise ValueError(
            f"{rated} responses have feedback but total_responses is {total_responses}. The "
            f"denominator must come from the same population and time range as the votes; "
            f"participation rate cannot exceed 100%."
        )

    return FeedbackSummary(
        responses_with_feedback=rated,
        likes=likes,
        dislikes=dislikes,
        total_responses=total_responses,
        like_rate=like_rate,
        participation_rate=participation,
        truncated=truncated,
        other_responses=other_responses,
    )


# ---------------------------------------------------------------------------
# Wire form
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FeedbackTotals:
    """Accumulator for building a :class:`FeedbackSummary` from a search response."""

    votes: list[Vote] = field(default_factory=list)
    total_responses: int = 0
    truncated: bool = False
    other_responses: int = 0

    def add(self, vote: Vote) -> None:
        self.votes.append(vote)

    def summary(self) -> FeedbackSummary:
        return summarise_feedback(
            self.votes,
            total_responses=self.total_responses,
            truncated=self.truncated,
            other_responses=self.other_responses,
        )


def parse_votes(
    hits: Sequence[Any], *, value_field: str, response_id_field: str, time_field: str
) -> list[Vote]:
    """Build :class:`Vote` objects from OpenSearch hits.

    The field names are parameters because they are the *flattened* names Data Prepper writes
    (``log.attributes.feedback@response_id``), and only ``conventions.field_name`` knows how to
    build those — this function takes the finished names so the flattening lives in one place.

    **The lookup tries the whole key before it tries to walk a path.** Data Prepper writes the
    flattened name as a single ``_source`` key that *contains* dots —
    ``"log.attributes.feedback@response_id": "resp-1"`` — even though OpenSearch interprets those
    dots as nesting in the *mapping*. Reading ``_source`` by splitting on ``.`` therefore walked a
    structure that is not there and returned nothing, so this function silently produced an empty
    vote list for every real document: no error, just a dashboard showing zero feedback. The
    path-walking form is kept as a fallback for a caller that hands over a genuinely nested
    mapping, which the ``_source`` of a differently configured pipeline could be.
    """

    def _get(source: Any, path: str) -> Any:
        if not isinstance(source, dict):
            return None
        if path in source:
            return source[path]
        node: Any = source
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
            if node is None:
                return None
        return node

    votes: list[Vote] = []
    for hit in hits:
        source = hit.get("_source", hit) if isinstance(hit, dict) else hit
        value = _get(source, value_field)
        response_id = _get(source, response_id_field)
        if value is None or response_id is None:
            continue
        votes.append(
            Vote(
                response_id=str(response_id),
                value=str(value),
                occurred_at=_get(source, time_field),
            )
        )
    return votes
