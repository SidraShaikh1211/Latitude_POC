PY := .venv/bin/python
PIP := .venv/bin/pip
UVICORN := .venv/bin/uvicorn
STREAMLIT := .venv/bin/streamlit
PYTEST := .venv/bin/pytest

.PHONY: help install seed run ui mcp test eval clean

help:
	@echo "Targets:"
	@echo "  install   Install dependencies into .venv"
	@echo "  seed      Seed Smith case (build Bundle, ingest, write to DB)"
	@echo "  run       Start FastAPI on :8000"
	@echo "  ui        Start Streamlit on :8501"
	@echo "  mcp       Start MCP server (stdio mode)"
	@echo "  test      Run unit + integration tests"
	@echo "  eval      Run full eval pyramid (L0 + L1 + L2 + L3)"
	@echo "  clean     Remove caches and the local DB"

install:
	$(PIP) install -r requirements.txt

seed:
	$(PY) -m scripts.seed_smith_case

run:
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000

ui:
	$(STREAMLIT) run frontend/app.py --server.port 8501

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
