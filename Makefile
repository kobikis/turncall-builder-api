.PHONY: help docker-up docker-up-local docker-migrate migrate docker-down docker-down-local docker-logs seed-guest test

# TurnCall's postgres container — the builder reuses it (see docker-compose.yml).
TURNCALL_PG ?= localstack-postgres-1

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

docker-migrate: ## Create the builder database in TurnCall's postgres (idempotent)
	@docker exec $(TURNCALL_PG) createdb -U turncall builder 2>/dev/null \
		&& echo "created database 'builder'" \
		|| echo "database 'builder' already exists"

migrate: ## Apply builder schema migrations (alembic upgrade head, in the api image)
	docker compose run --rm --build api alembic upgrade head

TURNCALL_CONTAINER ?= localstack-turncall-1

turncall-migrate: ## Apply TurnCall's own DB migrations (runs alembic inside its container)
	@docker exec -w /app $(TURNCALL_CONTAINER) sh -c \
		"sed 's|@localhost:5432|@postgres:5432|' alembic.ini > /tmp/a.ini && alembic -c /tmp/a.ini upgrade head" \
		&& echo "TurnCall migrations applied" \
		|| echo "could not migrate — is TurnCall up? (in turncall/: make docker-up)"

turncall-setup: docker-migrate turncall-migrate ## After a TurnCall reset: migrate TurnCall, create builder db, mint a fresh key
	./scripts/provision-turncall.sh

docker-up: docker-migrate ## Create the db, build + start the builder API, apply migrations
	docker compose up -d --build
	$(MAKE) migrate

docker-up-local: ## docker-up against TurnCall's local-storage stack (turncall-local-*)
	$(MAKE) docker-up TURNCALL_PG=turncall-local-postgres-1 TURNCALL_CONTAINER=turncall-local-turncall-1

docker-down: ## Stop the builder API
	docker compose down

docker-down-local: docker-down ## Stop the builder API (alias of docker-down; symmetry with docker-up-local)

docker-logs: ## Tail the builder API logs
	docker compose logs -f api

seed-guest: ## Seed a dev guest login (guest/guest); needs the stack up. Refuses in production
	docker compose run --rm --no-deps -v "$(CURDIR)/scripts:/app/scripts" api python -m scripts.seed_guest

test: ## Run unit tests (in the api image — no host Python needed)
	docker compose run --rm --build \
		-v "$(CURDIR)/app:/app/app" -v "$(CURDIR)/tests:/app/tests" \
		api python -m pytest tests/ -q
