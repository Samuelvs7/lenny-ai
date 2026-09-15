# The Lenny Growth Assistant — common tasks.
#
#   make up        start the full Docker stack
#   make ingest    load the knowledge base (required before first use)
#   make test      run the backend test suite
#   make dev-api   run the API locally without Docker

.PHONY: help up down logs ingest ingest-quick ingest-all stats test test-db \
        lint dev-api dev-web build-web health clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Docker ----------------------------------------------------------------

up:  ## Start the full stack (postgres, ollama, api, web)
	docker compose up --build -d
	@echo "Web  http://localhost:8080"
	@echo "API  http://localhost:8000/docs"
	@echo "Next: make ingest-quick"

down:  ## Stop the stack (volumes are preserved)
	docker compose down

logs:  ## Follow API logs
	docker compose logs -f api

ingest-quick:  ## Ingest 8 episodes (~7 min) — enough to demo
	docker compose run --rm ingest --episodes 8

ingest:  ## Ingest the configured default (30 episodes, ~25 min)
	docker compose run --rm ingest

ingest-all:  ## Ingest the full 269-episode archive (hours)
	docker compose run --rm ingest --all

stats:  ## Show knowledge-base statistics
	docker compose run --rm ingest --stats

health:  ## Deep health check
	@curl -s http://localhost:8000/health/deep | python -m json.tool

# --- Local development (no Docker) -----------------------------------------

dev-api:  ## Run the API locally with reload
	cd backend && uvicorn app.main:app --reload --port 8000

dev-web:  ## Run the frontend dev server
	cd frontend && npm run dev

build-web:  ## Type-check and build the frontend
	cd frontend && npm run typecheck && npm run build

# --- Quality ---------------------------------------------------------------

test:  ## Run tests that need no database
	cd backend && pytest

test-db:  ## Run the full suite including database-backed tests
	cd backend && TEST_DATABASE_URL=postgresql+asyncpg://lenny:lenny@localhost:5432/lenny_test pytest

lint:  ## Lint the backend
	cd backend && ruff check app tests

clean:  ## Remove caches and build output
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf backend/.pytest_cache backend/.ruff_cache frontend/dist
