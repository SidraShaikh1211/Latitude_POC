PY := .venv/bin/python
PIP := .venv/bin/pip
UVICORN := .venv/bin/uvicorn
PYTEST := .venv/bin/pytest

.PHONY: help install seed run ui ui-install ui-build mcp test eval clean docker-build docker-run deploy

# GCP deploy config — override on the command line, e.g.
#   make deploy SQL_INSTANCE=my-proj:us-central1:latitude-poc-db
GCP_PROJECT  ?= $(shell gcloud config get-value project 2>/dev/null)
GCP_REGION   ?= us-central1
SQL_INSTANCE ?=

help:
	@echo "Targets:"
	@echo "  install        Install Python dependencies into .venv"
	@echo "  seed           Seed Smith case (build Bundle, ingest, write to DB)"
	@echo "  run            Start FastAPI on :8000"
	@echo "  ui-install     npm install in frontend-react/ (one-time)"
	@echo "  ui             Start the React UI (Vite dev server) on :5173"
	@echo "  ui-build       Production build of the React UI"
	@echo "  mcp            Start MCP server (stdio mode)"
	@echo "  test           Run unit + integration tests"
	@echo "  eval           Run full eval pyramid (L0 + L1 + L2 + L3)"
	@echo "  clean          Remove caches and the local DB"
	@echo "  docker-build   Build the production image locally"
	@echo "  docker-run     Run the production image locally on :8080"
	@echo "  deploy         Cloud Build + deploy to Cloud Run (needs SQL_INSTANCE)"

install:
	$(PIP) install -r requirements.txt

seed:
	$(PY) -m scripts.seed_smith_case

run:
	# --reload-dir app: only restart on backend code edits. Skill prompts,
	# policies, the React app, and the SQLite DB don't trigger reloads, so
	# in-flight pipelines aren't killed by an unrelated file save.
	$(UVICORN) app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000

ui-install:
	cd frontend-react && npm install

ui:
	cd frontend-react && npm run dev

ui-build:
	cd frontend-react && npm run build

mcp:
	$(PY) -m app.mcp_server.server

test:
	$(PYTEST) tests/ -v

eval:
	$(PY) -m scripts.run_evals

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -f data/app.db data/app.db-journal data/app.db-wal data/app.db-shm

docker-build:
	docker build -t latitude-poc:local .

docker-run: docker-build
	docker run --rm -p 8080:8080 \
	  -e ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" \
	  -e DATABASE_URL="sqlite+aiosqlite:////tmp/app.db" \
	  latitude-poc:local

deploy:
	@if [ -z "$(SQL_INSTANCE)" ]; then \
	  echo "ERROR: SQL_INSTANCE is required, e.g. SQL_INSTANCE=$(GCP_PROJECT):$(GCP_REGION):latitude-poc-db"; \
	  exit 1; \
	fi
	gcloud builds submit \
	  --config=cloudbuild.yaml \
	  --substitutions=_REGION=$(GCP_REGION),_SQL_INSTANCE=$(SQL_INSTANCE) \
	  --project=$(GCP_PROJECT)
