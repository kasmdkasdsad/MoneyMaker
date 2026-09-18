"""Matching: buyers -> pickup points -> merged campaigns -> delivery runs.

Three distinct matching problems, all of which exist to push down the same
number: **cost per unit delivered**.

1. :func:`rank_pickup_points` -- which stop should this buyer collect from?
   Every buyer we can pull onto an existing stop is a free unit of density.

2. :func:`plan_merge` -- an under-subscribed campaign can be rescued by
   absorbing demand from a nearby stop for the same item. The discipline here is
   that a merge must *earn* its extra stop: pulling in 2 units to clear a
   threshold, at the cost of a whole extra van stop, destroys the margin it was
   supposed to save.

3. :func:`plan_runs` -- pack stops into vans and sequence them.

Solver honesty: :func:`plan_runs` is a capacity-constrained nearest-neighbour
construction with 2-opt improvement. That is not optimal, and it is not meant to
be. At launch scale (one metro, tens of stops per run) it lands within a few
percent of optimal in microseconds and has no external dependency. The seam is
clean, so Phase 2 can swap in an OR-Tools VRP solver or hand sequencing to the
carrier's own API without touching anything upstream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90 <= self.lat <= 90:
            raise ValueError(f"latitude out of range: {self.lat}")
        if not -180 <= self.lon <= 180:
            raise ValueError(f"longitude out of range: {self.lon}")


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in metres.

    Straight-line distance, not driving distance. For ranking pickup points
    within a couple of kilometres the difference is immaterial; for billing a
    carrier it is not, so run costs are always reconciled against the carrier's
    actual invoice (``delivery_runs.total_cost_cents``) rather than this.
    """
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = phi2 - phi1
    dlambda = math.radians(b.lon - a.lon)
    h = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


# ---------------------------------------------------------------------------
# 1. Buyer -> pickup point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PickupPointInfo:
    pickup_point_id: str
    location: GeoPoint
    capacity_units: int
    committed_units: int = 0
    is_active: bool = True
    # Existing demand already routed here. A stop that is already happening is
    # strictly cheaper to add a buyer to than a stop that is not.
    has_live_campaign: bool = False

    @property
    def remaining_capacity(self) -> int:
        return max(0, self.capacity_units - self.committed_units)


@dataclass(frozen=True)
class RankedPickupPoint:
    pickup_point_id: str
    distance_m: float
    remaining_capacity: int
    has_live_campaign: bool
    score: float  # lower is better


def rank_pickup_points(
    buyer: GeoPoint,
    points: Iterable[PickupPointInfo],
    *,
    max_radius_m: float = 2_000.0,
    required_units: int = 1,
    consolidation_bonus_m: float = 400.0,
    limit: int | None = None,
) -> list[RankedPickupPoint]:
    """Rank collection points for a buyer, nearest-and-densest first.

    ``consolidation_bonus_m`` is the thumb on the scale that makes this a
    group buying algorithm rather than a proximity search: a stop that already
    has a live campaign is scored as if it were this many metres closer. We are
    deliberately willing to ask a buyer to walk a little further in order to
    avoid opening a second stop, because the second stop costs us a fixed
    per-stop fee that dwarfs the inconvenience.
    """
    if required_units <= 0:
        raise ValueError("required_units must be positive")

    ranked: list[RankedPickupPoint] = []
    for point in points:
        if not point.is_active:
            continue
        if point.remaining_capacity < required_units:
            continue
        distance = haversine_m(buyer, point.location)
        if distance > max_radius_m:
            continue
        score = distance - (consolidation_bonus_m if point.has_live_campaign else 0.0)
        ranked.append(
            RankedPickupPoint(
                pickup_point_id=point.pickup_point_id,
                distance_m=distance,
                remaining_capacity=point.remaining_capacity,
                has_live_campaign=point.has_live_campaign,
                score=score,
            )
        )

    ranked.sort(key=lambda r: (r.score, r.pickup_point_id))
    return ranked[:limit] if limit else ranked


# ---------------------------------------------------------------------------
# 2. Campaign merging
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeCandidate:
    """A sibling campaign for the same variant at a different stop."""

    campaign_id: str
    pickup_point_id: str
    location: GeoPoint
    committed_units: int


@dataclass(frozen=True)
class MergePlan:
    anchor_campaign_id: str
    absorbed: tuple[MergeCandidate, ...]
    combined_units: int
    threshold_units: int
    clears_threshold: bool
    added_stops: int
    incremental_stop_cost_cents: int
    incremental_contribution_cents: int
    rejected: tuple[tuple[str, str], ...] = ()  # (campaign_id, reason)
    reason: str = ""

    @property
    def is_worthwhile(self) -> bool:
        """A merge is only worth doing if it both saves the campaign and pays
        for the stops it adds."""
        return (
            self.clears_threshold
            and self.incremental_contribution_cents
            >= self.incremental_stop_cost_cents
        )


def plan_merge(
    *,
    anchor_campaign_id: str,
    anchor_location: GeoPoint,
    anchor_units: int,
    threshold_units: int,
    candidates: Sequence[MergeCandidate],
    merge_radius_m: float = 3_000.0,
    min_units_per_stop: int = 1,
    stop_cost_cents: int = 0,
    contribution_per_unit_cents: int = 0,
    max_added_stops: int = 3,
) -> MergePlan:
    """Rescue a short campaign by absorbing nearby demand for the same item.

    Greedy by units descending: the densest neighbouring stop is the cheapest
    threshold progress available, so it goes in first. We stop as soon as the
    combined total clears the threshold -- every stop beyond that point is pure
    added cost.

    ``min_units_per_stop`` is the gate that keeps this honest. It comes from
    :func:`app.domain.pricing.min_units_per_stop` and represents the volume
    needed to earn back one marginal van stop. A candidate below it is rejected
    even though it would help clear the threshold, because it would turn a
    failed campaign into a losing one.
    """
    absorbed: list[MergeCandidate] = []
    rejected: list[tuple[str, str]] = []
    combined = anchor_units

    viable: list[MergeCandidate] = []
    for candidate in candidates:
        if candidate.campaign_id == anchor_campaign_id:
            continue
        distance = haversine_m(anchor_location, candidate.location)
        if distance > merge_radius_m:
            rejected.append(
                (candidate.campaign_id, f"{distance:.0f}m away, outside merge radius")
            )
            continue
        if candidate.committed_units < min_units_per_stop:
            rejected.append(
                (
                    candidate.campaign_id,
                    f"only {candidate.committed_units} units, below the "
                    f"{min_units_per_stop} needed to pay for an extra stop",
                )
            )
            continue
        viable.append(candidate)

    # Densest first: fewest added stops for the most threshold progress.
    viable.sort(key=lambda c: (-c.committed_units, c.campaign_id))

    for candidate in viable:
        if combined >= threshold_units:
            rejected.append((candidate.campaign_id, "threshold already cleared"))
            continue
        if len(absorbed) >= max_added_stops:
            rejected.append((candidate.campaign_id, "max added stops reached"))
            continue
        absorbed.append(candidate)
        combined += candidate.committed_units

    added_stops = len(absorbed)
    absorbed_units = sum(c.committed_units for c in absorbed)
    clears = combined >= threshold_units

    return MergePlan(
        anchor_campaign_id=anchor_campaign_id,
        absorbed=tuple(absorbed),
        combined_units=combined,
        threshold_units=threshold_units,
        clears_threshold=clears,
        added_stops=added_stops,
        incremental_stop_cost_cents=added_stops * stop_cost_cents,
        incremental_contribution_cents=absorbed_units * contribution_per_unit_cents,
        rejected=tuple(rejected),
        reason=(
            f"absorbed {added_stops} stop(s) for {absorbed_units} units -> "
            f"{combined}/{threshold_units}"
            if absorbed
            else f"no viable merge candidates; stuck at {combined}/{threshold_units}"
        ),
    )


# ---------------------------------------------------------------------------
# 3. Stops -> delivery runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stop:
    pickup_point_id: str
    location: GeoPoint
    units: int

    def __post_init__(self) -> None:
        if self.units <= 0:
            raise ValueError("a stop with no units should not be scheduled")


@dataclass
class PlannedRun:
    stops: list[Stop] = field(default_factory=list)
    distance_m: float = 0.0

    @property
    def total_units(self) -> int:
        return sum(s.units for s in self.stops)

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def units_per_stop(self) -> float:
        """The headline operational metric. Everything in the go-to-market plan
        is, ultimately, an attempt to raise this number."""
        return self.total_units / self.stop_count if self.stops else 0.0


def route_length_m(depot: GeoPoint, stops: Sequence[Stop]) -> float:
    """Total distance of depot -> stops in order -> depot."""
    if not stops:
        return 0.0
    total = haversine_m(depot, stops[0].location)
    for a, b in zip(stops, stops[1:]):
        total += haversine_m(a.location, b.location)
    return total + haversine_m(stops[-1].location, depot)


def two_opt(depot: GeoPoint, stops: list[Stop], *, max_passes: int = 20) -> list[Stop]:
    """Improve a route by repeatedly reversing segments that cross.

    Classic 2-opt. Converges in a handful of passes for the stop counts we care
    about, and removes the obvious zig-zags that nearest-neighbour leaves behind.
    """
    if len(stops) < 4:
        return stops

    best = list(stops)
    best_length = route_length_m(depot, best)

    for _ in range(max_passes):
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 2, len(best)):
                candidate = best[:i + 1] + best[i + 1:j + 1][::-1] + best[j + 1:]
                length = route_length_m(depot, candidate)
                if length < best_length - 1e-9:
                    best, best_length = candidate, length
                    improved = True
        if not improved:
            break
    return best


def plan_runs(
    depot: GeoPoint,
    stops: Sequence[Stop],
    *,
    vehicle_capacity_units: int,
    max_stops_per_run: int = 25,
) -> list[PlannedRun]:
    """Pack stops into vans and sequence each van's route.

    Construction is nearest-neighbour under a capacity and stop-count ceiling;
    each resulting route is then improved with 2-opt. Stops are consumed
    densest-first so the highest-value stops are guaranteed a slot on a van
    rather than being stranded on a marginal final run.

    A single stop whose volume exceeds one vehicle is placed on a dedicated run
    rather than being dropped -- the ops team splits it manually, and the
    returned run makes that visible instead of silently losing the demand.
    """
    if vehicle_capacity_units <= 0:
        raise ValueError("vehicle_capacity_units must be positive")
    if max_stops_per_run <= 0:
        raise ValueError("max_stops_per_run must be positive")

    remaining = sorted(stops, key=lambda s: (-s.units, s.pickup_point_id))
    runs: list[PlannedRun] = []

    while remaining:
        seed = remaining.pop(0)

        if seed.units > vehicle_capacity_units:
            run = PlannedRun(stops=[seed])
            run.distance_m = route_length_m(depot, run.stops)
            runs.append(run)
            continue

        route = [seed]
        load = seed.units
        cursor = seed.location

        while remaining and len(route) < max_stops_per_run:
            feasible = [
                (idx, s)
                for idx, s in enumerate(remaining)
                if load + s.units <= vehicle_capacity_units
            ]
            if not feasible:
                break
            idx, nearest = min(
                feasible,
                key=lambda pair: (
                    haversine_m(cursor, pair[1].location),
                    pair[1].pickup_point_id,
                ),
            )
            route.append(nearest)
            load += nearest.units
            cursor = nearest.location
            remaining.pop(idx)

        ordered = two_opt(depot, route)
        runs.append(
            PlannedRun(stops=ordered, distance_m=route_length_m(depot, ordered))
        )

    return runs


def estimate_run_cost_cents(
    run: PlannedRun,
    *,
    base_cents: int = 0,
    per_km_cents: int = 0,
    per_stop_cents: int = 0,
) -> int:
    """Model a run's cost. Reconciled against the carrier invoice after the fact
    so the neighbourhood cost parameters -- and therefore every threshold
    derived from them -- stay tethered to reality."""
    km = run.distance_m / 1000.0
    return (
        base_cents
        + int(round(km * per_km_cents))
        + run.stop_count * per_stop_cents
    )


def cost_per_unit_cents(run: PlannedRun, run_cost_cents: int) -> float:
    """The number the whole business is judged on."""
    return run_cost_cents / run.total_units if run.total_units else float("inf")


__all__ = [
    "EARTH_RADIUS_M",
    "GeoPoint",
    "MergeCandidate",
    "MergePlan",
    "PickupPointInfo",
    "PlannedRun",
    "RankedPickupPoint",
    "Stop",
    "cost_per_unit_cents",
    "estimate_run_cost_cents",
    "haversine_m",
    "plan_merge",
    "plan_runs",
    "rank_pickup_points",
    "route_length_m",
    "two_opt",
]
