"""Matching, merging and run-planning tests."""

from __future__ import annotations

import pytest

from app.domain.matching import (
    GeoPoint,
    MergeCandidate,
    PickupPointInfo,
    Stop,
    cost_per_unit_cents,
    estimate_run_cost_cents,
    haversine_m,
    plan_merge,
    plan_runs,
    rank_pickup_points,
    route_length_m,
    two_opt,
)

# Orlando-ish coordinates; ~1km apart in latitude per 0.009 degrees.
DEPOT = GeoPoint(28.5383, -81.3792)
NEAR = GeoPoint(28.5473, -81.3792)     # ~1.0 km north of depot
FAR = GeoPoint(28.6283, -81.3792)      # ~10 km north of depot


class TestGeometry:
    def test_haversine_matches_known_distance(self):
        # 0.009 degrees of latitude is almost exactly 1 km.
        assert haversine_m(DEPOT, NEAR) == pytest.approx(1000, rel=0.02)

    def test_distance_is_symmetric_and_zero_at_identity(self):
        assert haversine_m(DEPOT, FAR) == pytest.approx(haversine_m(FAR, DEPOT))
        assert haversine_m(DEPOT, DEPOT) == pytest.approx(0.0, abs=1e-6)

    def test_rejects_impossible_coordinates(self):
        with pytest.raises(ValueError):
            GeoPoint(lat=91, lon=0)
        with pytest.raises(ValueError):
            GeoPoint(lat=0, lon=181)


class TestPickupRanking:
    def build(self, **overrides):
        base = dict(
            pickup_point_id="p", location=NEAR, capacity_units=100,
            committed_units=0, is_active=True, has_live_campaign=False,
        )
        base.update(overrides)
        return PickupPointInfo(**base)

    def test_ranks_nearest_first(self):
        points = [
            self.build(pickup_point_id="far", location=GeoPoint(28.5563, -81.3792)),
            self.build(pickup_point_id="near", location=NEAR),
        ]
        ranked = rank_pickup_points(DEPOT, points, max_radius_m=5000)
        assert [r.pickup_point_id for r in ranked] == ["near", "far"]

    def test_prefers_a_stop_that_already_has_a_campaign(self):
        # The consolidation bonus is what makes this group buying rather than a
        # proximity search: a slightly further stop that already exists avoids
        # opening a second one.
        points = [
            self.build(pickup_point_id="empty", location=NEAR),  # 1000m
            self.build(
                pickup_point_id="busy",
                location=GeoPoint(28.5495, -81.3792),           # ~1250m
                has_live_campaign=True,
            ),
        ]
        ranked = rank_pickup_points(
            DEPOT, points, max_radius_m=5000, consolidation_bonus_m=400
        )
        assert ranked[0].pickup_point_id == "busy"

        # ...but the bonus is bounded. A genuinely distant stop still loses.
        ranked_small_bonus = rank_pickup_points(
            DEPOT, points, max_radius_m=5000, consolidation_bonus_m=50
        )
        assert ranked_small_bonus[0].pickup_point_id == "empty"

    def test_excludes_inactive_full_and_distant_points(self):
        points = [
            self.build(pickup_point_id="inactive", is_active=False),
            self.build(pickup_point_id="full", capacity_units=5, committed_units=5),
            self.build(pickup_point_id="distant", location=FAR),
            self.build(pickup_point_id="good"),
        ]
        ranked = rank_pickup_points(
            DEPOT, points, max_radius_m=2000, required_units=1
        )
        assert [r.pickup_point_id for r in ranked] == ["good"]

    def test_respects_required_units(self):
        points = [self.build(pickup_point_id="tight", capacity_units=3)]
        assert rank_pickup_points(DEPOT, points, required_units=2)
        assert not rank_pickup_points(DEPOT, points, required_units=4)


class TestMerge:
    def test_absorbs_the_densest_neighbour_first(self):
        plan = plan_merge(
            anchor_campaign_id="a",
            anchor_location=DEPOT,
            anchor_units=14,
            threshold_units=24,
            candidates=[
                MergeCandidate("b", "pb", NEAR, committed_units=4),
                MergeCandidate("c", "pc", NEAR, committed_units=11),
            ],
            min_units_per_stop=3,
        )
        # 'c' alone closes the gap, so 'b' is never added -- an extra stop that
        # buys nothing is pure cost.
        assert [c.campaign_id for c in plan.absorbed] == ["c"]
        assert plan.combined_units == 25
        assert plan.clears_threshold
        assert ("b", "threshold already cleared") in plan.rejected

    def test_rejects_candidates_too_thin_to_pay_for_their_stop(self):
        # This is the discipline that keeps merging from destroying margin:
        # 2 units cannot earn back a whole van stop, even though adding them
        # would technically clear the threshold.
        plan = plan_merge(
            anchor_campaign_id="a",
            anchor_location=DEPOT,
            anchor_units=22,
            threshold_units=24,
            candidates=[MergeCandidate("b", "pb", NEAR, committed_units=2)],
            min_units_per_stop=8,
        )
        assert plan.absorbed == ()
        assert not plan.clears_threshold
        assert "below the 8 needed" in plan.rejected[0][1]

    def test_rejects_candidates_outside_the_merge_radius(self):
        plan = plan_merge(
            anchor_campaign_id="a",
            anchor_location=DEPOT,
            anchor_units=10,
            threshold_units=24,
            candidates=[MergeCandidate("b", "pb", FAR, committed_units=20)],
            merge_radius_m=3000,
            min_units_per_stop=1,
        )
        assert plan.absorbed == ()
        assert "outside merge radius" in plan.rejected[0][1]

    def test_is_worthwhile_only_when_it_pays_for_the_added_stops(self):
        good = plan_merge(
            anchor_campaign_id="a", anchor_location=DEPOT, anchor_units=14,
            threshold_units=24,
            candidates=[MergeCandidate("b", "pb", NEAR, committed_units=12)],
            min_units_per_stop=5, stop_cost_cents=1200,
            contribution_per_unit_cents=250,   # 12 x 250 = 3000 > 1200
        )
        assert good.is_worthwhile

        marginal = plan_merge(
            anchor_campaign_id="a", anchor_location=DEPOT, anchor_units=14,
            threshold_units=24,
            candidates=[MergeCandidate("b", "pb", NEAR, committed_units=12)],
            min_units_per_stop=5, stop_cost_cents=5000,
            contribution_per_unit_cents=250,   # 3000 < 5000
        )
        assert marginal.clears_threshold
        assert not marginal.is_worthwhile

    def test_caps_the_number_of_added_stops(self):
        candidates = [
            MergeCandidate(f"c{i}", f"p{i}", NEAR, committed_units=3)
            for i in range(6)
        ]
        plan = plan_merge(
            anchor_campaign_id="a", anchor_location=DEPOT, anchor_units=1,
            threshold_units=100, candidates=candidates,
            min_units_per_stop=1, max_added_stops=2,
        )
        assert plan.added_stops == 2
        assert not plan.clears_threshold

    def test_never_absorbs_itself(self):
        plan = plan_merge(
            anchor_campaign_id="a", anchor_location=DEPOT, anchor_units=10,
            threshold_units=24,
            candidates=[MergeCandidate("a", "pa", DEPOT, committed_units=10)],
            min_units_per_stop=1,
        )
        assert plan.absorbed == ()


class TestRunPlanning:
    def stops(self, spec):
        return [
            Stop(f"s{i}", GeoPoint(28.5383 + 0.005 * i, -81.3792), units)
            for i, units in enumerate(spec)
        ]

    def test_every_stop_is_assigned_exactly_once(self):
        stops = self.stops([10, 20, 30, 40, 50])
        runs = plan_runs(DEPOT, stops, vehicle_capacity_units=100)
        assigned = [s.pickup_point_id for run in runs for s in run.stops]
        assert sorted(assigned) == sorted(s.pickup_point_id for s in stops)
        assert len(assigned) == len(set(assigned))

    def test_never_exceeds_vehicle_capacity(self):
        stops = self.stops([40, 40, 40, 40, 40])
        runs = plan_runs(DEPOT, stops, vehicle_capacity_units=100)
        assert all(run.total_units <= 100 for run in runs)
        assert len(runs) == 3

    def test_respects_max_stops_per_run(self):
        stops = self.stops([5] * 10)
        runs = plan_runs(DEPOT, stops, vehicle_capacity_units=1000, max_stops_per_run=3)
        assert all(run.stop_count <= 3 for run in runs)

    def test_oversized_stop_gets_its_own_run_rather_than_being_dropped(self):
        stops = self.stops([500, 10])
        runs = plan_runs(DEPOT, stops, vehicle_capacity_units=100)
        assigned = {s.pickup_point_id for run in runs for s in run.stops}
        assert assigned == {"s0", "s1"}
        oversized = [r for r in runs if r.total_units == 500]
        assert len(oversized) == 1 and oversized[0].stop_count == 1

    def test_units_per_stop_is_reported(self):
        stops = self.stops([30, 30])
        run = plan_runs(DEPOT, stops, vehicle_capacity_units=100)[0]
        assert run.units_per_stop == 30.0

    def test_empty_input_produces_no_runs(self):
        assert plan_runs(DEPOT, [], vehicle_capacity_units=100) == []

    def test_rejects_invalid_configuration(self):
        with pytest.raises(ValueError):
            plan_runs(DEPOT, [], vehicle_capacity_units=0)
        with pytest.raises(ValueError):
            Stop("s", NEAR, units=0)

    def test_two_opt_never_makes_a_route_longer(self):
        # Deliberately crossed ordering.
        scrambled = [
            Stop("a", GeoPoint(28.54, -81.38), 5),
            Stop("b", GeoPoint(28.58, -81.38), 5),
            Stop("c", GeoPoint(28.55, -81.38), 5),
            Stop("d", GeoPoint(28.57, -81.38), 5),
        ]
        before = route_length_m(DEPOT, scrambled)
        after = route_length_m(DEPOT, two_opt(DEPOT, scrambled))
        assert after <= before

    def test_route_length_returns_to_the_depot(self):
        single = [Stop("a", NEAR, 5)]
        assert route_length_m(DEPOT, single) == pytest.approx(
            2 * haversine_m(DEPOT, NEAR)
        )
        assert route_length_m(DEPOT, []) == 0.0


class TestRunCosting:
    def test_cost_per_unit_falls_as_density_rises(self):
        sparse = plan_runs(
            DEPOT,
            [Stop(f"s{i}", GeoPoint(28.54 + 0.005 * i, -81.38), 4) for i in range(5)],
            vehicle_capacity_units=1000,
        )[0]
        dense = plan_runs(
            DEPOT,
            [Stop(f"s{i}", GeoPoint(28.54 + 0.005 * i, -81.38), 40) for i in range(5)],
            vehicle_capacity_units=1000,
        )[0]

        params = dict(base_cents=3000, per_km_cents=120, per_stop_cents=1200)
        sparse_cpu = cost_per_unit_cents(sparse, estimate_run_cost_cents(sparse, **params))
        dense_cpu = cost_per_unit_cents(dense, estimate_run_cost_cents(dense, **params))

        # Same stops, same route, ten times the units: cost per unit collapses.
        # This is the entire thesis of the business in one assertion.
        assert dense_cpu < sparse_cpu / 5

    def test_empty_run_has_infinite_cost_per_unit(self):
        from app.domain.matching import PlannedRun

        assert cost_per_unit_cents(PlannedRun(), 1000) == float("inf")
