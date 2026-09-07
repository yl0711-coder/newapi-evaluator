.PHONY: up dev down restart logs ps test

up:
	docker compose up -d --build

dev:
	docker compose -f compose.yml -f compose.dev.yml up -d --build

down:
	docker compose down

restart:
	docker compose restart workbench

logs:
	docker compose logs -f --tail=200 workbench

ps:
	docker compose ps

test:
	docker compose run --rm workbench python scripts/test_all.py
