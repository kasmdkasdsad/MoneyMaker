"""HTTP-level tests.

Only the endpoints that need no database are covered here. The DB-backed flows
are exercised against a real PostgreSQL instance (``docker compose up db``)
because the invariants that matter -- ``FOR UPDATE`` serialisation, partial
unique indexes, the oversell CHECK -- have no SQLite equivalent and testing them
against a substitute engine would prove nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class TestHealth:
    def test_healthz(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestThresholdPreview:
    def base(self, **overrides) -> dict:
        body = {
            "unit_price_cents": 1200,
            "unit_cost_cents": 700,
            "stop_cost_cents": 1200,
            "line_haul_cost_cents": 6000,
            "expected_units_per_run": 300,
            "commission_bps": 500,
            "case_pack_units": 6,
            "supplier_min_units": 0,
            "target_margin_cents": 0,
        }
        body.update(overrides)
        return body

    def test_returns_a_case_aligned_threshold_with_a_tier_ladder(self, client):
        response = client.post("/v1/ops/threshold-preview", json=self.base())
        assert response.status_code == 200
        body = response.json()

        assert body["viable"] is True
        assert body["min_units"] % 6 == 0
        assert body["contribution_per_unit_cents"] > 0
        assert body["suggested_tiers"]
        # The ladder must open at the threshold and never price upward.
        prices = [t["unit_price_cents"] for t in body["suggested_tiers"]]
        assert body["suggested_tiers"][0]["min_units"] == body["min_units"]
        assert prices == sorted(prices, reverse=True)

    def test_flags_an_item_that_cannot_be_made_to_work(self, client):
        response = client.post(
            "/v1/ops/threshold-preview",
            json=self.base(unit_price_cents=600, unit_cost_cents=700),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["viable"] is False
        assert body["min_units"] is None
        assert body["binding_constraint"] == "economics"
        assert body["suggested_tiers"] == []

    def test_explains_which_constraint_binds(self, client):
        response = client.post(
            "/v1/ops/threshold-preview",
            json=self.base(case_pack_units=1, supplier_min_units=500),
        )
        assert response.json()["binding_constraint"] == "supplier_moq"

    def test_rejects_malformed_input(self, client):
        response = client.post(
            "/v1/ops/threshold-preview", json=self.base(unit_price_cents=0)
        )
        assert response.status_code == 422


class TestAuthGuards:
    def test_join_requires_identity(self, client):
        response = client.post(
            "/v1/campaigns/00000000-0000-0000-0000-000000000001/join",
            json={"quantity": 1},
            headers={"Idempotency-Key": "k1"},
        )
        assert response.status_code == 401

    def test_join_requires_an_idempotency_key(self, client):
        # Mobile clients retry. Without a key, a retry becomes a second charge.
        response = client.post(
            "/v1/campaigns/00000000-0000-0000-0000-000000000001/join",
            json={"quantity": 1},
            headers={"X-User-Id": "00000000-0000-0000-0000-0000000000aa"},
        )
        assert response.status_code == 400
        assert "Idempotency-Key" in response.json()["detail"]

    def test_rejects_nonsense_quantity_before_touching_the_database(self, client):
        response = client.post(
            "/v1/campaigns/00000000-0000-0000-0000-000000000001/join",
            json={"quantity": 0},
            headers={
                "X-User-Id": "00000000-0000-0000-0000-0000000000aa",
                "Idempotency-Key": "k1",
            },
        )
        assert response.status_code == 422
