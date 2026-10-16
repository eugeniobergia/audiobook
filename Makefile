.PHONY: format lint typecheck test check

# Rewrites files: applies safe lint fixes (e.g. import order), then formats.
format:
	uv run ruff check --fix --exit-zero
	uv run ruff format

lint:
	uv run ruff check
	uv run ruff format --check

typecheck:
	uv run mypy

test:
	uv run pytest

# Everything CI runs, in order of speed.
check: lint typecheck test
