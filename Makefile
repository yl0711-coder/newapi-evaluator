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
	docker compose run --rm workbench sh -ec 'test_artifact_root=$$(mktemp -d /tmp/eval-tests.XXXXXX); export EVAL_TEST_ARTIFACT_ROOT="$$test_artifact_root" PYTHONDONTWRITEBYTECODE=1; exec python scripts/test_all.py --output "$$test_artifact_root/evidence"'

prod-up:
	docker compose -f compose.prod.yml pull
	docker compose -f compose.prod.yml up -d

prod-down:
	docker compose -f compose.prod.yml down

prod-logs:
	docker compose -f compose.prod.yml logs -f --tail=200 workbench

prod-ps:
	docker compose -f compose.prod.yml ps
