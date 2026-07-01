PYTHON ?= python3
VENV ?= .venv
ACTIVATE = . $(VENV)/bin/activate

.PHONY: venv install install-dev up down migrate lint test api worker

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(ACTIVATE) && python -m pip install --upgrade pip && python -m pip install -e .

install-dev: venv
	$(ACTIVATE) && python -m pip install --upgrade pip && python -m pip install -e '.[dev]'

up:
	docker compose -f infra/docker/docker-compose.dev.yml up -d

down:
	docker compose -f infra/docker/docker-compose.dev.yml down

migrate:
	$(ACTIVATE) && alembic upgrade head

lint:
	$(ACTIVATE) && ruff check apps packages tests

test:
	$(ACTIVATE) && pytest -q

api:
	$(ACTIVATE) && ragos-api

worker:
	$(ACTIVATE) && ragos-workflow-worker
