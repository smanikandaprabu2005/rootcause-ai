.PHONY: up down logs test scenarios clear

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=50 gateway orders inventory payments notifications

test:
	python -m pytest tests -q

scenarios:
	python faults/inject.py list

clear:
	python faults/inject.py clear
