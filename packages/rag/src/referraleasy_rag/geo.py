"""Distance, computed from coordinates and from nothing else.

An embedding score is a similarity in a learned vector space. It is not a distance in metres
and it cannot be turned into one: two providers 400 m apart on the same street are not
necessarily near each other in embedding space, and two providers 900 km apart can be very
close in it, because both are "a cardiologist in a city". Convenience of implementation is not
a reason to confuse the two, so this module is the only place distance is produced, and it
takes latitude/longitude as its input.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

#: Mean Earth radius (IUGG), kilometres.
EARTH_RADIUS_KM = 6371.0088

Coordinate = tuple[float, float]  # (latitude, longitude) in degrees, WGS-84


class HasCoordinate(Protocol):
    """Anything that can carry a coordinate; ``None`` means "no location on record"."""

    @property
    def coordinate(self) -> Coordinate | None: ...


def is_valid_coordinate(latitude: float, longitude: float) -> bool:
    """Range check, so a swapped lat/lon pair is rejected instead of silently plausible."""
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def haversine_km(origin: Coordinate, destination: Coordinate) -> float:
    """Great-circle distance between two coordinates, in kilometres."""
    lat1, lon1 = origin
    lat2, lon2 = destination
    if not (is_valid_coordinate(lat1, lon1) and is_valid_coordinate(lat2, lon2)):
        raise ValueError(f"coordinate out of range: {(origin, destination)!r}")
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def distance_km(origin: Coordinate | None, target: Coordinate | None) -> float | None:
    """Distance in kilometres, or ``None`` when either side has no coordinate on record.

    ``None`` is returned rather than a large sentinel: "unknown distance" and "far away" are
    different facts and must not be sorted as if they were the same one.
    """
    if origin is None or target is None:
        return None
    if not (is_valid_coordinate(*origin) and is_valid_coordinate(*target)):
        return None
    return haversine_km(origin, target)


@dataclass(frozen=True, slots=True)
class Located[T]:
    """A value with an optional coordinate; used to sort results by real distance."""

    value: T
    coordinate: Coordinate | None

    def distance_from(self, origin: Coordinate | None) -> float | None:
        return distance_km(origin, self.coordinate)


def sort_by_distance[T](
    items: Iterable[Located[T]], origin: Coordinate | None
) -> list[tuple[T, float | None]]:
    """Sort by coordinate distance.

    Items without a coordinate keep their original relative order and sort last — an unknown
    distance is never presented as a near one. The sort is stable and total, so the output is
    deterministic for a deterministic input.
    """
    known: list[tuple[float, int, T]] = []
    unknown: list[T] = []
    for position, item in enumerate(items):
        distance = item.distance_from(origin)
        if distance is None:
            unknown.append(item.value)
        else:
            known.append((distance, position, item.value))
    known.sort(key=lambda entry: (entry[0], entry[1]))
    ordered: list[tuple[T, float | None]] = [(value, distance) for distance, _, value in known]
    ordered.extend((value, None) for value in unknown)
    return ordered


def coordinate_from_pair(values: Sequence[float] | None) -> Coordinate | None:
    """Extract a coordinate from a `(latitude, longitude)` pair, or ``None`` when absent."""
    if not values or len(values) != 2:
        return None
    latitude, longitude = float(values[0]), float(values[1])
    if not is_valid_coordinate(latitude, longitude):
        return None
    return (latitude, longitude)
