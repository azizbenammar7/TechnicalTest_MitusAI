.PHONY: v2-demo v2-test v2-v1-compat-setup v2-v1-compat-readiness v2-v1-compat-smoke v2-demo-v1-compat p1-build p1-up p1-down p1-logs p2-db-up p2-db-down p2-db-migrate p2-test

# P2 cloud-adapter local testing. The URLs/keys below are development-only and
# match compose.p2.yaml; the Azurite key is Microsoft's public well-known
# emulator key (not a secret). Override P2_DATABASE_URL for another instance.
P2_PYTHON ?= .venv-test/bin/python
P2_DATABASE_URL ?= postgresql+psycopg://footballai:devonly_local_p2@localhost:55432/footballai_p2
P2_BLOB_CONNECTION_STRING ?= DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;

v2-demo:
	./v2/dev/run_demo.sh

v2-test:
	./v2/dev/run_tests.sh

v2-v1-compat-setup:
	./v2/dev/setup_v1_compat.sh

v2-v1-compat-readiness:
	PYTHONPATH=v2/src $${FOOTBALLAI_V2_PYTHON:-.venv-test/bin/python} -m footballai_v2.cli.v1_compat_runtime check

v2-v1-compat-smoke:
	./v2/dev/run_v1_compat_smoke.sh

v2-demo-v1-compat:
	./v2/dev/run_v1_compat_demo.sh

p1-build:
	docker compose build frontend api worker

p1-up:
	FOOTBALLAI_CODE_REVISION=$$(git rev-parse HEAD) FOOTBALLAI_CODE_DIRTY=$$(test -z "$$(git status --porcelain --untracked-files=no)" && echo 0 || echo 1) docker compose up --detach

p1-down:
	docker compose down

p1-logs:
	docker compose logs --follow frontend api worker

p2-db-up:
	docker compose -f compose.p2.yaml up --detach

p2-db-down:
	docker compose -f compose.p2.yaml down --volumes

p2-db-migrate:
	cd v2 && FOOTBALLAI_DATABASE_URL="$(P2_DATABASE_URL)" PYTHONPATH=src ../$(P2_PYTHON) -m alembic upgrade head

p2-test: p2-db-migrate
	FOOTBALLAI_TEST_DATABASE_URL="$(P2_DATABASE_URL)" \
	FOOTBALLAI_TEST_BLOB_CONNECTION_STRING="$(P2_BLOB_CONNECTION_STRING)" \
	PYTHONPATH=v2/src $(P2_PYTHON) -m pytest v2/tests -q -ra
