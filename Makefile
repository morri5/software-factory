.PHONY: lint test check-names watch run report langfuse-up langfuse-down

# Settings live in .env (see .env.example); every target sees them.
-include .env
export

PY_SOURCES := src tests

lint:
	uv run ruff check $(PY_SOURCES)
	uv run ruff format --check $(PY_SOURCES)
	uv run mypy src tests

test:
	uv run pytest

check-names:
	./scripts/check_names.sh

# Both need the session API of ../sandboxed-agents running (`make api` there).
watch:
	uv run factory watch

run:
	@test -n "$(ISSUE)" || { echo "usage: make run ISSUE=<number>"; exit 2; }
	uv run factory run --issue $(ISSUE)

report:
	uv run factory report

# Langfuse (compose.yaml): UI on http://localhost:3100, login admin@factory.local.
langfuse-up:
	docker compose up -d --wait

langfuse-down:
	docker compose down
