.DEFAULT_GOAL := help
.PHONY: help secrets up up-live live-check down reset logs test test-sandbox test-unit eval eval-check eval-live claims lint psql

ROLES := $(shell sed -n 's/^  \(u_[a-z_]*\):.*/\1/p' data/users.yaml)
SECRET_KEYS := POSTGRES_PASSWORD APP_WRITER_PASSWORD GOLD_READER_PASSWORD SESSION_SECRET \
	$(foreach role,$(ROLES),PGPASS_$(shell echo $(role) | tr a-z A-Z))

help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

secrets: ## Mint any missing password in var/secrets.env
	@mkdir -p var && touch var/secrets.env var/no-gcloud-credentials.json && chmod 600 var/secrets.env
	@for key in $(SECRET_KEYS); do \
		grep -q "^$$key=" var/secrets.env || echo "$$key=$$(openssl rand -hex 16)" >> var/secrets.env; \
	done

up: secrets ## Build and start everything, then wait until it is healthy
	docker compose --profile sandbox-image build sandbox
	docker compose up -d --build --wait
	@echo "Open http://$$(docker compose port app 8000 | sed s/127.0.0.1/localhost/)"

# Live mode reads LLM_BACKEND and its settings from .env or the shell, and mounts the gcloud credentials for vertex.
# gcloud's credentials file, which Claude on Vertex and the Gemini checker sign in with. Without one the live overlay
# mounts an empty stand-in, which the Anthropic API never reads.
ADC := $(HOME)/.config/gcloud/application_default_credentials.json
export GCLOUD_ADC ?= $(if $(wildcard $(ADC)),$(ADC),$(CURDIR)/var/no-gcloud-credentials.json)

up-live: secrets ## Start everything as up does, with live mode's model settings layered on
	docker compose --profile sandbox-image build sandbox
	docker compose -f compose.yaml -f compose.live.yaml up -d --build --wait
	@echo "Open http://$$(docker compose port app 8000 | sed s/127.0.0.1/localhost/)"

live-check: ## Ask each live model for one token, with the settings in .env or the shell, and say which answered
	uv run $(if $(wildcard .env),--env-file .env) python tools/live_check.py

down: ## Stop the stack, keeping the database volume
	docker compose down

reset: ## Stop the stack and delete the database volume
	docker compose down -v --remove-orphans

logs: ## Follow service logs
	docker compose logs -f --tail=200

test: secrets ## Run the suite in the test container, which has no Docker: run make test-sandbox too
	docker compose --profile test run --rm --build test

# The test container has no Docker CLI or socket, so the container limit tests only run here.
# REQUIRE_DOCKER=1 makes them fail, not skip, when Docker or the image is missing.
test-sandbox: ## Build the sandbox image and run tests/sandbox on the host, where Docker is
	docker compose --profile sandbox-image build sandbox
	REQUIRE_DOCKER=1 uv run pytest tests/sandbox

test-unit: ## Run the tests that need no database, on the host
	uv run pytest -m "not integration"

# The eval runs in the test container, the only place with the embedding models, on the internal network.
# /src is mounted read-only there, so eval mounts evals/ writable for the report.
COMMIT_ENV = -e GIT_COMMIT="$$(git rev-parse HEAD 2>/dev/null)$$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
EVAL_RUN = docker compose --profile test run --rm --build $(COMMIT_ENV)
# A live eval reads LLM_BACKEND and its settings from .env or the shell, as up-live does, and the live overlay lets
# the test container reach the models. Every run calls them, so it costs money, and CI never runs it.
EVAL_LIVE = docker compose -f compose.yaml -f compose.live.yaml --profile test run --rm --build $(COMMIT_ENV)
RUNS ?= 3

eval: secrets ## Run every eval case as its user and write evals/report.json (needs make up)
	$(EVAL_RUN) -v "$(CURDIR)/evals:/src/evals" test python -m evals.run --write

eval-check: secrets ## Run the evals fresh and compare with the committed evals/report.json
	$(EVAL_RUN) test python -m evals.run --check

eval-live: secrets ## Score live mode RUNS times (default 3) into the live section of evals/report.json (needs make up)
	$(EVAL_LIVE) -v "$(CURDIR)/evals:/src/evals" test python -m evals.run --live --write --runs $(RUNS)

claims: ## Check that the numbers in README.md and docs/ match evals/report.json
	uv run python tools/recount.py --check

lint: ## Ruff and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

psql: ## Open psql in the database as postgres
	docker compose exec db psql -U postgres -d claims

.PHONY: demos
demos: ## Record the README's demo clips and dashboard still into docs/demo (needs make up, and CLIPS=why records one)
	uv run python tools/record_demos.py $(CLIPS)
