"""Distance comes from coordinates, and from nothing else.

An embedding score is a similarity in a learned vector space; it is not a number of kilometres and
cannot be turned into one. So the assertions here pin two things: the arithmetic is the
great-circle one to within a known tolerance, and a chunk without a coordinate reports *no*
distance rather than a big one — "unknown" and "far" are different facts.
"""

from __future__ import annotations

import pytest

from referraleasy_rag.degradation import LadderState
from referraleasy_rag.geo import (
    EARTH_RADIUS_KM,
    Located,
    coordinate_from_pair,
    distance_km,
    haversine_km,
    is_valid_coordinate,
    sort_by_distance,
)
from referraleasy_rag.retriever import RetrievalResult, RetrievedChunk

#: Real coordinates, so the expected distances can be checked against a known figure.
CHICAGO = (41.8781, -87.6298)
NAPERVILLE = (41.7508, -88.1535)
NEW_YORK = (40.7128, -74.0060)


def test_the_same_point_is_zero_kilometres_away() -> None:
    assert haversine_km(CHICAGO, CHICAGO) == pytest.approx(0.0, abs=1e-9)


def test_a_known_distance_is_reproduced() -> None:
    """Chicago to New York is about 1144 km. A wrong formula lands nowhere near it."""
    assert haversine_km(CHICAGO, NEW_YORK) == pytest.approx(1144.0, rel=0.01)


def test_distance_is_symmetric() -> None:
    assert haversine_km(CHICAGO, NAPERVILLE) == pytest.approx(haversine_km(NAPERVILLE, CHICAGO))


def test_a_suburban_distance_is_plausible() -> None:
    """Two cities in the same metro area are tens of kilometres apart, not hundreds."""
    metres_apart = haversine_km(CHICAGO, NAPERVILLE)
    assert 20.0 < metres_apart < 80.0


def test_an_absent_coordinate_yields_no_distance() -> None:
    assert distance_km(None, CHICAGO) is None
    assert distance_km(CHICAGO, None) is None
    assert distance_km(None, None) is None


def test_an_out_of_range_coordinate_yields_no_distance_rather_than_a_wrong_one() -> None:
    assert distance_km(CHICAGO, (91.0, 0.0)) is None
    assert distance_km(CHICAGO, (41.0, 200.0)) is None
    with pytest.raises(ValueError, match="out of range"):
        haversine_km(CHICAGO, (91.0, 0.0))


def test_coordinate_validation_rejects_an_impossible_latitude_or_longitude() -> None:
    assert not is_valid_coordinate(100.0, 41.8781)
    assert not is_valid_coordinate(41.8781, 200.0)
    assert is_valid_coordinate(41.8781, -87.6298)


def test_a_swapped_pair_is_only_caught_when_it_leaves_the_valid_range() -> None:
    """Range checking bounds the damage; it does not detect every swap, and pretending it does
    would be the more dangerous claim. Tokyo (35.6762 N, 139.6503 E) is caught; Chicago is not.
    """
    assert not is_valid_coordinate(139.6503, 35.6762)
    assert coordinate_from_pair([139.6503, 35.6762]) is None
    assert is_valid_coordinate(35.6762, 139.6503)
    # Chicago swapped stays inside both ranges, so it cannot be detected by range alone.
    assert is_valid_coordinate(-87.6298, 41.8781)


def test_coordinate_from_pair_keeps_a_usable_pair() -> None:
    assert coordinate_from_pair([41.8781, -87.6298]) == CHICAGO
    assert coordinate_from_pair([1.0]) is None
    assert coordinate_from_pair([]) is None
    assert coordinate_from_pair(None) is None


def test_the_earth_radius_is_the_standard_one() -> None:
    assert pytest.approx(6371.0088) == EARTH_RADIUS_KM


def test_sorting_puts_the_nearer_first_and_the_unknown_last() -> None:
    ordered = sort_by_distance(
        [
            _located("nyc", NEW_YORK),
            _located("nowhere", None),
            _located("naperville", NAPERVILLE),
            _located("chicago", CHICAGO),
        ],
        CHICAGO,
    )
    assert [value for value, _ in ordered] == ["chicago", "naperville", "nyc", "nowhere"]
    assert ordered[-1][1] is None


def test_sorting_is_stable_for_items_without_coordinates() -> None:
    first = sort_by_distance([_located("a", None), _located("b", None)], CHICAGO)
    second = sort_by_distance([_located("a", None), _located("b", None)], CHICAGO)
    assert [value for value, _ in first] == [value for value, _ in second] == ["a", "b"]


def test_a_result_is_reordered_by_distance_not_by_score() -> None:
    """The two orderings must disagree here, or the test proves nothing."""
    result = RetrievalResult(
        query="cardiologist",
        results=(
            _chunk("far-high-score", rank=1, score=9.9, coordinate=NEW_YORK),
            _chunk("near-low-score", rank=2, score=0.1, coordinate=NAPERVILLE),
        ),
        ladder=LadderState(),
    )
    ordered = result.sorted_by_distance(CHICAGO)
    assert [chunk.chunk_id for chunk in ordered] == ["near-low-score", "far-high-score"]
    near, far = ordered[0].distance_km, ordered[1].distance_km
    assert near is not None and far is not None
    assert near < far
    assert [chunk.rank for chunk in ordered] == [1, 2], "ranks are recomputed, not left stale"


def test_a_result_without_coordinates_reports_no_distance() -> None:
    result = RetrievalResult(
        query="cardiologist",
        results=(_chunk("no-coordinates", rank=1, score=1.0, coordinate=None),),
        ladder=LadderState(),
    )
    assert result.results[0].distance_km is None
    assert result.sorted_by_distance(CHICAGO)[0].distance_km is None


def test_a_distance_never_carries_an_embedding_score() -> None:
    """The score field survives the reorder untouched; the distance field is a pure function."""
    result = RetrievalResult(
        query="cardiologist",
        results=(_chunk("a", rank=1, score=0.87, coordinate=CHICAGO),),
        ladder=LadderState(),
    )
    reordered = result.sorted_by_distance(NAPERVILLE)
    assert reordered[0].score == 0.87
    assert reordered[0].distance_km == pytest.approx(distance_km(NAPERVILLE, CHICAGO))


def _located(name: str, coordinate: tuple[float, float] | None) -> Located[str]:
    return Located(value=name, coordinate=coordinate)


def _chunk(
    chunk_id: str,
    *,
    rank: int,
    score: float,
    coordinate: tuple[float, float] | None,
) -> RetrievedChunk:
    metadata: dict[str, object] = {}
    if coordinate is not None:
        metadata["geo_lat"] = coordinate[0]
        metadata["geo_lon"] = coordinate[1]
    return RetrievedChunk(
        chunk_id=chunk_id,
        entity_id=chunk_id,
        entity_type="provider",
        source_id=f"provider_directory:{chunk_id}",
        knowledge_version="kv-test",
        text=f"{chunk_id} is a cardiologist.",
        score=score,
        rank=rank,
        metadata=metadata,
    )
