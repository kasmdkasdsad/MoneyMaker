.PHONY: help db-up db-down db-reset test test-integration api lint

DB_URL ?= postgresql+psycopg://moneymaker:moneymaker@localhost:5432/moneymaker

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

db-up:  ## Start PostgreSQL (applies schema.sql + seed.sql on first boot)
	docker compose up -d db

db-down:  ## Stop PostgreSQL
	docker compose down

db-reset:  ## Drop the volume and rebuild the database from scratch
	docker compose down -v && docker compose up -d db

test:  ## Unit tests only (no database required)
	cd backend && python -m pytest tests -q

test-integration:  ## Full suite including PostgreSQL integration tests
	cd backend && MM_TEST_DATABASE_URL=$(DB_URL) python -m pytest tests -q

api:  ## Run the API locally with reload
	cd backend && MM_DATABASE_URL=$(DB_URL) uvicorn app.main:app --reload

reconcile:  ## Run the operational reconciliation queries
	psql $(subst postgresql+psycopg,postgresql,$(DB_URL)) -f db/reconcile.sql
