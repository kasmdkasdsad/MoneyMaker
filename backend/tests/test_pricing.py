"""Pricing and threshold-derivation tests."""

from __future__ import annotations

import pytest

from app.domain.pricing import (
    CostModel,
    PriceTier,
    TierConfigurationError,
    campaign_contribution_cents,
    ceil_div,
    min_units_per_stop,
    min_viable_units,
    next_tier,
    resolve_unit_price_cents,
    round_up_to_multiple,
    suggest_tier_ladder,
    units_to_next_tier,
    validate_tiers,
)

LADDER = [
    PriceTier(min_units=10, unit_price_cents=1200),
    PriceTier(min_units=25, unit_price_cents=1100),
    PriceTier(min_units=50, unit_price_cents=1000),
]


def cost(**overrides) -> CostModel:
    base = dict(
        unit_cost_cents=700,
        stop_cost_cents=1200,
        line_haul_cost_cents=6000,
        expected_units_per_run=300,
        commission_bps=500,
        psp_fee_bps=290,
        psp_fixed_cents=30,
        avg_units_per_order=2,
    )
    base.update(overrides)
    return CostModel(**base)


class TestRounding:
    def test_ceil_div_rounds_up(self):
        assert ceil_div(10, 3) == 4
        assert ceil_div(9, 3) == 3
        assert ceil_div(0, 3) == 0

    def test_ceil_div_rejects_zero_denominator(self):
        with pytest.raises(ValueError):
            ceil_div(1, 0)

    def test_round_up_to_multiple(self):
        assert round_up_to_multiple(13, 6) == 18
        assert round_up_to_multiple(12, 6) == 12
        assert round_up_to_multiple(1, 6) == 6


class TestTiers:
    def test_resolves_to_best_applicable_tier(self):
        assert resolve_unit_price_cents(LADDER, 10) == 1200
        assert resolve_unit_price_cents(LADDER, 24) == 1200
        assert resolve_unit_price_cents(LADDER, 25) == 1100
        assert resolve_unit_price_cents(LADDER, 999) == 1000

    def test_below_first_tier_quotes_the_worst_price(self):
        # Early joiners are quoted the most expensive tier. They can only ever
        # be charged less than this, never more.
        assert resolve_unit_price_cents(LADDER, 0) == 1200
        assert resolve_unit_price_cents(LADDER, 3) == 1200

    def test_rejects_price_increasing_with_quantity(self):
        # The invariant the entire referral loop depends on.
        with pytest.raises(TierConfigurationError, match="must not increase"):
            validate_tiers(
                [
                    PriceTier(min_units=10, unit_price_cents=1000),
                    PriceTier(min_units=20, unit_price_cents=1100),
                ]
            )

    def test_rejects_duplicate_and_empty_ladders(self):
        with pytest.raises(TierConfigurationError, match="duplicate"):
            validate_tiers(
                [
                    PriceTier(min_units=10, unit_price_cents=1000),
                    PriceTier(min_units=10, unit_price_cents=900),
                ]
            )
        with pytest.raises(TierConfigurationError, match="at least one"):
            validate_tiers([])

    def test_next_tier_drives_the_share_prompt(self):
        assert units_to_next_tier(LADDER, 22) == 3
        assert next_tier(LADDER, 22).unit_price_cents == 1100
        assert units_to_next_tier(LADDER, 50) is None


class TestMinViableUnits:
    def test_threshold_covers_the_stop_cost(self):
        c = cost(line_haul_cost_cents=0, psp_fixed_cents=0, psp_fee_bps=0,
                 commission_bps=0, unit_cost_cents=700, stop_cost_cents=1200)
        # contribution = 1200 - 700 = 500/unit; 1200c stop cost => 3 units (2.4 -> 3)
        result = min_viable_units(unit_price_cents=1200, cost=c)
        assert result.viable
        assert result.contribution_per_unit_cents == 500
        assert result.raw_break_even_units == 3
        assert result.min_units == 3

    def test_reports_unviable_item_instead_of_inventing_a_threshold(self):
        # Selling below cost: no quantity can rescue this.
        result = min_viable_units(unit_price_cents=600, cost=cost(unit_cost_cents=700))
        assert not result.viable
        assert result.min_units is None
        assert result.binding_constraint == "economics"
        assert "cannot cover" in result.reason

    def test_case_pack_can_be_the_binding_constraint(self):
        c = cost(line_haul_cost_cents=0, psp_fixed_cents=0, psp_fee_bps=0,
                 commission_bps=0, stop_cost_cents=1200, unit_cost_cents=700)
        result = min_viable_units(unit_price_cents=1200, cost=c, case_pack_units=12)
        assert result.raw_break_even_units == 3
        assert result.min_units == 12
        assert result.binding_constraint == "case_pack"

    def test_supplier_moq_can_be_the_binding_constraint(self):
        c = cost(line_haul_cost_cents=0, psp_fixed_cents=0, psp_fee_bps=0,
                 commission_bps=0, stop_cost_cents=1200, unit_cost_cents=700)
        result = min_viable_units(
            unit_price_cents=1200, cost=c, case_pack_units=6, supplier_min_units=40
        )
        assert result.min_units == 42  # 40 lifted to the next whole case
        assert result.binding_constraint == "supplier_moq"
        assert result.min_units % 6 == 0

    def test_target_margin_raises_the_threshold(self):
        c = cost(line_haul_cost_cents=0, psp_fixed_cents=0, psp_fee_bps=0,
                 commission_bps=0, stop_cost_cents=1200, unit_cost_cents=700)
        lean = min_viable_units(unit_price_cents=1200, cost=c)
        profitable = min_viable_units(
            unit_price_cents=1200, cost=c, target_margin_cents=5000
        )
        assert profitable.min_units > lean.min_units

    def test_line_haul_is_amortised_not_charged_whole(self):
        # A 6000c line haul spread over 300 units is 20c/unit, not 6000c.
        c = cost(line_haul_cost_cents=6000, expected_units_per_run=300)
        assert c.line_haul_per_unit_cents == 20


class TestContribution:
    def test_case_overage_is_charged_against_the_campaign(self):
        c = cost()
        sold_exactly = campaign_contribution_cents(
            units=24, unit_price_cents=1200, cost=c, purchased_units=24
        )
        with_overage = campaign_contribution_cents(
            units=24, unit_price_cents=1200, cost=c, purchased_units=30
        )
        # Six unsold units cost us six units of supplier cost.
        assert sold_exactly - with_overage == 6 * c.unit_cost_cents

    def test_rejects_purchasing_less_than_sold(self):
        with pytest.raises(ValueError):
            campaign_contribution_cents(
                units=24, unit_price_cents=1200, cost=cost(), purchased_units=12
            )

    def test_threshold_quantity_actually_breaks_even(self):
        # The derived threshold must hold up against the independent
        # contribution calculation -- this is the property that matters.
        c = cost()
        analysis = min_viable_units(unit_price_cents=1200, cost=c, case_pack_units=1)
        assert analysis.viable and analysis.min_units is not None
        at_threshold = campaign_contribution_cents(
            units=analysis.min_units, unit_price_cents=1200, cost=c
        )
        below = campaign_contribution_cents(
            units=analysis.min_units - 1, unit_price_cents=1200, cost=c
        )
        assert at_threshold >= 0
        assert below < 0


class TestMergeGate:
    def test_marginal_stop_threshold_is_lower_than_standalone(self):
        # Line haul is already paid by the run, so an extra stop only has to
        # earn back the stop cost -- which is why merging is worth doing at all.
        c = cost()
        standalone = min_viable_units(unit_price_cents=1200, cost=c).min_units
        marginal = min_units_per_stop(unit_price_cents=1200, cost=c)
        assert marginal is not None
        assert marginal <= standalone


class TestTierLadderGeneration:
    def test_generated_ladder_is_valid_and_never_loss_making(self):
        c = cost()
        ladder = suggest_tier_ladder(
            cost=c, anchor_price_cents=1200, threshold_units=24
        )
        validate_tiers(ladder)  # must not raise
        for tier in ladder:
            assert c.contribution_per_unit_cents(tier.unit_price_cents) > 0

    def test_stops_generating_tiers_that_would_lose_money(self):
        thin = cost(unit_cost_cents=1150)
        ladder = suggest_tier_ladder(
            cost=thin,
            anchor_price_cents=1200,
            threshold_units=24,
            steps=((2, 2000), (4, 4000)),  # 20% and 40% off would go underwater
        )
        assert len(ladder) == 1
