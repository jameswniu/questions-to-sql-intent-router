.DEFAULT_GOAL := help
.PHONY: help secrets up down reset logs test test-sandbox test-unit lint psql

ROLES := $(shell sed -n 's/^  \(u_[a-z_]*\):.*/\1/p' data/users.yaml)
SECRET_KEYS := POSTGRES_PASSWORD APP_WRITER_PASSWORD GOLD_READER_PASSWORD \
	$(foreach role,$(ROLES),PGPASS_$(shell echo $(role) | tr a-z A-Z))

help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

secrets: ## Mint any missing password in var/secrets.env
	@mkdir -p var && touch var/secrets.env && chmod 600 var/secrets.env
	@for key in $(SECRET_KEYS); do \
		grep -q "^$$key=" var/secrets.env || echo "$$key=$$(openssl rand -hex 16)" >> var/secrets.env; \
	done

up: secrets ## Build and start everything, then wait until it is healthy
	docker compose --profile sandbox-image build sandbox
	docker compose up -d --build --wait
	@echo "Open http://$$(docker compose port app 8000 | sed s/127.0.0.1/localhost/)"

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

lint: ## Ruff and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

psql: ## Open psql in the database as postgres
	docker compose exec db psql -U postgres -d claims
