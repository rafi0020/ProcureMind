.PHONY: dev-up dev-down check test test-live test-ocr test-ocr-live lint eval-extraction eval-matching eval-build eval-agent eval-attacks
dev-up:    ## database, pooler and redis for local development and tests
	docker compose up -d --build db pgbouncer redis
dev-down:
	docker compose down
lint:
	cd backend && uv run ruff format --check -q . && uv run ruff check .
test:      ## everything except tests that call a real model
	cd backend && RUN_LIVE=0 uv run pytest -q
test-live: ## tests that call a real model through the gateway in ../.env.local
	cd backend && uv run pytest -q -m live
check: lint test
test-ocr:  ## OCR tests, in the test image (it has tesseract)
	docker compose --profile test run --rm --build tests
test-ocr-live:  ## OCR + real model, in the test image; reads NINEROUTER_API_KEY from ../.env.local without printing it
	@set -a && . ../.env.local && set +a && docker compose --profile test run --rm --build -e NINEROUTER_API_KEY \
		-e NINEROUTER_BASE_URL=http://host.docker.internal:20128/v1 tests python -m pytest -q -m "live and ocr"

# Evaluation, in the test image (it has Tesseract). Results are written to backend/eval/results.
# MODEL is a route on the gateway named in ../.env.local (NINEROUTER_*), or set EVAL_BASE_URL / EVAL_API_KEY.
EVAL_RUN = @set -a && . ../.env.local && set +a && docker compose --profile test run --rm --build --user "$$(id -u):$$(id -g)" \
	-e NINEROUTER_API_KEY -e NINEROUTER_BASE_URL=http://host.docker.internal:20128/v1
DEV_DB = -e DATABASE_URL=postgresql+psycopg://procuremind_app:$${PROCUREMIND_APP_PASSWORD:-procuremind_app}@pgbouncer:6432/procuremind \
	-e DATABASE_URL_OWNER=postgresql+psycopg://procuremind_owner:$${PROCUREMIND_OWNER_PASSWORD:-procuremind_owner}@db:5432/procuremind
eval-extraction:  ## field accuracy on held-out documents; MODEL=... adds the model to the rules
	$(EVAL_RUN) tests python -m eval.run_extraction $(if $(MODEL),--model $(MODEL) --limit 12,)
eval-matching:    ## match statuses and findings against the answer key (rebuilds the demo in the TEST database)
	$(EVAL_RUN) tests python -m eval.run_matching --scale 0.1
eval-build:       ## the agent's evaluation cases, in their own organisation in the development database
	$(EVAL_RUN) $(DEV_DB) tests python -m eval.build_questions
eval-agent:       ## SPLIT=dev|test MODEL=...
	$(EVAL_RUN) $(DEV_DB) tests python -m eval.run_eval --split $(SPLIT) --model $(MODEL)
eval-attacks:     ## MODEL=...  (ONLY=case,case re-runs those cases into the existing results file)
	$(EVAL_RUN) $(DEV_DB) tests python -m eval.attacks --model $(MODEL) $(if $(ONLY),--only $(ONLY),)
