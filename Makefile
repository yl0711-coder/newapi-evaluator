.PHONY: up dev down restart logs ps test prod-up prod-down prod-logs prod-ps

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

prod-up:
	docker compose -f compose.prod.yml pull
	docker compose -f compose.prod.yml up -d

prod-down:
	docker compose -f compose.prod.yml down

prod-logs:
	docker compose -f compose.prod.yml logs -f --tail=200 workbench

prod-ps:
	docker compose -f compose.prod.yml ps
