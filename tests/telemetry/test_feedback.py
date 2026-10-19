"""Feedback: last-active-thumbs, and three numbers that are never merged (§15, §12).

    Feedback is counted using the latest active thumbs up/down per ``response_id``; raw counts,
    approval rate, and participation rate are separate.

The case §12's dashboard most needs to be able to show is the one a single "satisfaction" number
hides: **a 100% like rate from four responses out of nine hundred**. Several tests below are that
case, because it is the one where merging the numbers produces a *flattering* answer rather than a
merely imprecise one.
"""

from __future__ import annotations

import pytest

from referraleasy_telemetry.feedback import (
    DISLIKE,
    LIKE,
    FeedbackTotals,
    Vote,
    last_active,
    parse_votes,
    summarise_feedback,
)


class TestVoteValidation:
    def test_the_two_values_are_accepted(self) -> None:
        for value in (LIKE, DISLIKE):
            assert Vote(response_id="r", value=value).value == value

    @pytest.mark.parametrize("value", ["like", "UP", "", "1", "thumbsup"])
    def test_a_third_vote_value_is_refused(self, value: str) -> None:
        """A third value would have to be given a meaning in the like rate, and there is no third
        meaning to give it."""
        with pytest.raises(ValueError, match=r"up.*down"):
            Vote(response_id="r", value=value)

    def test_a_vote_without_a_response_is_refused(self) -> None:
        """It would be counted in the raw count and dropped from the like rate, so the numerator
        and the denominator would describe different populations."""
        with pytest.raises(ValueError, match="response_id"):
            Vote(response_id="", value=LIKE)


class TestLastActive:
    def test_a_flip_flop_is_one_opinion(self) -> None:
        """A patient who taps thumbs-down then thumbs-up has expressed one opinion, the second
        one. Counting rows would report two."""
        collapsed = last_active(
            [
                Vote("r1", DISLIKE, "2026-10-15T10:00:00Z"),
                Vote("r1", LIKE, "2026-10-15T10:00:05Z"),
            ]
        )
        assert len(collapsed) == 1
        assert collapsed[0].value == LIKE

    def test_a_later_downvote_wins_over_an_earlier_upvote(self) -> None:
        """The direction matters: taking the first vote instead of the last would make the number
        improve every time somebody changed their mind downwards."""
        collapsed = last_active(
            [
                Vote("r1", LIKE, "2026-10-15T10:00:00Z"),
                Vote("r1", DISLIKE, "2026-10-15T10:00:05Z"),
            ]
        )
        assert collapsed[0].value == DISLIKE

    def test_votes_are_collapsed_per_response_not_globally(self) -> None:
        collapsed = last_active(
            [
                Vote("r1", LIKE, "2026-10-15T10:00:00Z"),
                Vote("r2", DISLIKE, "2026-10-15T10:00:01Z"),
            ]
        )
        assert {vote.response_id: vote.value for vote in collapsed} == {"r1": LIKE, "r2": DISLIKE}

    def test_without_timestamps_the_caller_order_decides(self) -> None:
        """Deferring to the caller's ordering rather than refusing: for a query result that ordering
        is the ``sort`` that was asked of OpenSearch."""
        collapsed = last_active([Vote("r1", DISLIKE), Vote("r1", LIKE)])
        assert collapsed[0].value == LIKE

    def test_a_tie_resolves_to_the_last_in_iteration_order(self) -> None:
        """Duplicate delivery of the same vote is a real possibility (at-least-once), and raising in
        a read path because two rows share a timestamp would be worse than picking one."""
        collapsed = last_active(
            [
                Vote("r1", LIKE, "2026-10-15T10:00:00Z"),
                Vote("r1", LIKE, "2026-10-15T10:00:00Z"),
            ]
        )
        assert len(collapsed) == 1

    def test_no_votes_is_an_empty_result(self) -> None:
        assert last_active([]) == []


class TestThreeNumbersAreSeparate:
    def test_a_perfect_like_rate_from_a_tiny_sample_is_visible_as_such(self) -> None:
        """**The case the whole module exists for.** Four votes out of nine hundred responses is a
        100% like rate *and* a 0.4% participation rate, and a merged "satisfaction" figure would
        report it as a perfect service."""
        votes = [Vote(f"r{i}", LIKE) for i in range(4)]
        summary = summarise_feedback(votes, total_responses=900)

        assert summary.raw_count == 4
        assert summary.like_rate == 1.0
        assert summary.participation_rate == pytest.approx(4 / 900)
        # All three are present in the wire form, so no consumer has to derive one from another.
        assert set(summary.as_dict()) >= {"raw_count", "like_rate", "participation_rate"}

    def test_the_raw_count_is_responses_not_vote_rows(self) -> None:
        """A rate whose numerator and denominator disagree about what a unit is produces
        percentages above 100."""
        summary = summarise_feedback(
            [
                Vote("r1", DISLIKE, "2026-10-15T10:00:00Z"),
                Vote("r1", LIKE, "2026-10-15T10:00:05Z"),
                Vote("r2", LIKE, "2026-10-15T10:00:06Z"),
            ],
            total_responses=10,
        )
        assert summary.raw_count == 2, "three rows, two rated responses"
        assert summary.likes == 2, "r1's last vote was a like; the earlier dislike is superseded"
        assert summary.dislikes == 0
        assert summary.like_rate == 1.0

    def test_the_like_rate_denominator_is_rated_responses(self) -> None:
        summary = summarise_feedback(
            [Vote("r1", LIKE), Vote("r2", LIKE), Vote("r3", DISLIKE)], total_responses=10
        )
        assert summary.like_rate == pytest.approx(2 / 3)

    def test_the_participation_denominator_is_all_responses(self) -> None:
        summary = summarise_feedback([Vote("r1", LIKE)], total_responses=4)
        assert summary.participation_rate == 0.25

    def test_the_three_number_as_dict_form_is_complete(self) -> None:
        summary = summarise_feedback([Vote("r1", LIKE)], total_responses=2)
        assert summary.as_dict() == {
            "raw_count": 1,
            "like_rate": 1.0,
            "participation_rate": 0.5,
            "likes": 1,
            "dislikes": 0,
            "responses_with_feedback": 1,
            "total_responses": 2,
            "truncated": False,
            "other_responses": 0,
        }


class TestUndefinedIsNoneNotZero:
    """Returning 0.0 for an undefined rate charts as "everybody hated it" and as "nobody
    participated" — both claims the data does not support."""

    def test_no_votes_gives_no_like_rate(self) -> None:
        summary = summarise_feedback([], total_responses=100)
        assert summary.like_rate is None
        assert summary.raw_count == 0
        assert summary.participation_rate == 0.0, (
            "zero of a hundred is a real, defined number: nobody rated anything"
        )

    def test_no_responses_at_all_gives_no_participation_rate(self) -> None:
        """Zero responses is not "0% participation", it is an empty period."""
        summary = summarise_feedback([], total_responses=0)
        assert summary.participation_rate is None
        assert summary.like_rate is None

    def test_has_data_distinguishes_empty_from_rated(self) -> None:
        assert summarise_feedback([], total_responses=0).has_data is False
        assert summarise_feedback([], total_responses=5).has_data is True
        assert summarise_feedback([Vote("r", LIKE)], total_responses=5).has_data is True

    def test_a_perfect_dislike_rate_is_zero_and_not_none(self) -> None:
        """The mirror case, so the two are distinguishable: 0.0 is a measured zero."""
        summary = summarise_feedback([Vote("r", DISLIKE)], total_responses=1)
        assert summary.like_rate == 0.0
        assert summary.like_rate is not None


class TestImpossibleDenominatorsAreRefused:
    def test_more_rated_responses_than_responses_is_refused(self) -> None:
        """It means the denominator is from a different population than the votes — a different
        time range, most often — and an above-100% participation rate destroys trust in every other
        number on the dashboard."""
        with pytest.raises(ValueError, match="cannot exceed 100%"):
            summarise_feedback([Vote("r1", LIKE), Vote("r2", LIKE)], total_responses=1)

    def test_a_negative_total_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            summarise_feedback([], total_responses=-1)

    def test_exactly_all_rated_is_allowed(self) -> None:
        summary = summarise_feedback([Vote("r1", LIKE), Vote("r2", LIKE)], total_responses=2)
        assert summary.participation_rate == 1.0


class TestTruncationIsReported:
    def test_the_overflow_is_carried_through(self) -> None:
        """When the terms aggregation's ``size`` is smaller than the number of distinct responses,
        the like rate is computed over a subset. A chart with a caveat beats a chart that is quietly
        wrong."""
        summary = summarise_feedback(
            [Vote("r1", LIKE)], total_responses=500, truncated=True, other_responses=200
        )
        assert summary.truncated is True
        assert summary.other_responses == 200
        assert summary.as_dict()["truncated"] is True


class TestFeedbackTotals:
    def test_the_accumulator_summarises(self) -> None:
        totals = FeedbackTotals(total_responses=4)
        totals.add(Vote("r1", LIKE))
        totals.add(Vote("r2", DISLIKE))
        summary = totals.summary()
        assert summary.like_rate == 0.5
        assert summary.participation_rate == 0.5

    def test_it_starts_empty(self) -> None:
        assert FeedbackTotals().summary().raw_count == 0


class TestParseVotes:
    """Reading the *flattened* field names Data Prepper writes."""

    def test_the_flattened_names_are_read(self) -> None:
        hits = [
            {
                "_source": {
                    "log.attributes.feedback@response_id": "resp-1",
                    "log.attributes.feedback@value": "up",
                    "time": "2026-10-15T10:00:00Z",
                }
            }
        ]
        votes = parse_votes(
            hits,
            value_field="log.attributes.feedback@value",
            response_id_field="log.attributes.feedback@response_id",
            time_field="time",
        )
        assert len(votes) == 1
        assert votes[0].response_id == "resp-1"
        assert votes[0].value == "up"
        assert votes[0].occurred_at == "2026-10-15T10:00:00Z"

    def test_a_hit_missing_the_value_is_skipped(self) -> None:
        """A feedback event that carried no vote is not a vote. Skipping is right; defaulting it to
        a value would invent an opinion."""
        hits = [{"_source": {"log.attributes.feedback@response_id": "resp-1"}}]
        assert (
            parse_votes(
                hits,
                value_field="log.attributes.feedback@value",
                response_id_field="log.attributes.feedback@response_id",
                time_field="time",
            )
            == []
        )

    def test_a_hit_missing_the_response_id_is_skipped(self) -> None:
        hits = [{"_source": {"log.attributes.feedback@value": "up"}}]
        assert (
            parse_votes(
                hits,
                value_field="log.attributes.feedback@value",
                response_id_field="log.attributes.feedback@response_id",
                time_field="time",
            )
            == []
        )

    def test_a_bare_source_without_the_envelope_is_accepted(self) -> None:
        """A caller that already unwrapped the hits should not have to re-wrap them."""
        votes = parse_votes(
            [{"a.b": "up", "a.c": "r1"}],
            value_field="a.b",
            response_id_field="a.c",
            time_field="t",
        )
        assert votes[0].response_id == "r1"

    def test_a_missing_timestamp_is_none_and_not_an_error(self) -> None:
        votes = parse_votes(
            [{"a.b": "up", "a.c": "r1"}],
            value_field="a.b",
            response_id_field="a.c",
            time_field="time",
        )
        assert votes[0].occurred_at is None

    def test_non_string_values_are_coerced(self) -> None:
        """``response_id`` arrives as a string, but a numeric-looking id is worth not crashing on."""
        votes = parse_votes(
            [{"a.b": "up", "a.c": 12345}],
            value_field="a.b",
            response_id_field="a.c",
            time_field="t",
        )
        assert votes[0].response_id == "12345"

    def test_the_parsed_votes_feed_the_summary(self) -> None:
        """The two halves joined: hits in, three numbers out."""
        hits = [
            {
                "_source": {
                    "log.attributes.feedback@response_id": "resp-1",
                    "log.attributes.feedback@value": "down",
                    "time": "2026-10-15T10:00:00Z",
                }
            },
            {
                "_source": {
                    "log.attributes.feedback@response_id": "resp-1",
                    "log.attributes.feedback@value": "up",
                    "time": "2026-10-15T10:05:00Z",
                }
            },
        ]
        votes = parse_votes(
            hits,
            value_field="log.attributes.feedback@value",
            response_id_field="log.attributes.feedback@response_id",
            time_field="time",
        )
        summary = summarise_feedback(votes, total_responses=3)
        assert summary.raw_count == 1, "one response, one opinion"
        assert summary.like_rate == 1.0
        assert summary.participation_rate == pytest.approx(1 / 3)
