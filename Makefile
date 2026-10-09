.PHONY: up down logs test eval-synthetic eval-live dashboard serve scenarios clear

up:            ## start the demo shop, telemetry pipeline and RootCause AI (http://localhost:8000)
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=50 gateway orders inventory payments notifications rootcause

test:
	python -m pytest tests -q

eval-synthetic:  ## offline regression benchmark on simulated telemetry
	python -m rootcause.evaluate --mode synthetic --runs 5

eval-live:       ## headline benchmark against the running Docker stack (~1.5 h)
	python -m rootcause.evaluate --mode live --runs 5

dashboard:       ## build the React dashboard
	cd dashboard && npm ci && npm run build

serve:           ## run API + dashboard on the host (needed for sandboxed remediation)
	python -m rootcause serve

scenarios:
	python faults/inject.py list

clear:
	python faults/inject.py clear
