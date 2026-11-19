.PHONY: lint test check-names watch run

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

# Both need the session API of ../sandboxed-agents-lab running (`make api` there).
watch:
	uv run factory watch

run:
	@test -n "$(ISSUE)" || { echo "usage: make run ISSUE=<number>"; exit 2; }
	uv run factory run --issue $(ISSUE)
