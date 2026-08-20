.PHONY: help install dev-install lint test run playground deploy deploy-trigger destroy setup smoke clean

PY := .venv/Scripts/python.exe
ifeq ($(OS),)
PY := .venv/bin/python
endif

help:
	@echo "fluffy-memory - ARMCL Fleet"
	@echo ""
	@echo "  make install      Install runtime dependencies into .venv"
	@echo "  make dev-install  Install with dev + local-fallback extras"
	@echo "  make lint         Ruff check and format"
	@echo "  make test         Run the unit + eval test suite"
	@echo "  make run          Run the fleet locally against the reference workload"
	@echo "  make playground   Launch the ADK web UI"
	@echo "  make smoke        Phase 0: deploy a hello-world agent, verify, tear down"
	@echo "  make deploy       Deploy the fleet to Agent Runtime"
	@echo "  make deploy-trigger  Deploy Pub/Sub bridge to the fleet"
	@echo "  make destroy      Delete the deployed Agent Runtime instance"
	@echo "  make clean        Remove caches and local state"

install:
	uv venv --python 3.12
	uv pip install -e .

dev-install:
	uv venv --python 3.12
	uv pip install -e ".[dev,local]"

lint:
	$(PY) -m ruff check --fix app deploy tests main.py
	$(PY) -m ruff format app deploy tests main.py

test:
	$(PY) -m pytest -q

run:
	$(PY) -m app.local_run

playground:
	$(PY) -m google.adk.cli web app

smoke:
	$(PY) deploy/smoke_test.py

deploy:
	$(PY) deploy/deploy.py

deploy-trigger:
	$(PY) deploy/deploy_trigger.py

destroy:
	$(PY) deploy/destroy.py

clean:
	-rm -rf .pytest_cache .ruff_cache chroma_data
	-find . -type d -name __pycache__ -exec rm -rf {} +
