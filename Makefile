.PHONY: install lock sync test lint format type tox clean

install:
	uv sync

lock:
	uv lock

sync:
	uv sync --all-extras

test:
	uv run tox -e py313

lint:
	uv run tox -e lint

format:
	uv run tox -e format

type:
	uv run tox -e type

tox:
	uv run tox

clean:
	-rm -rf .venv dist .ruff_cache .pytest_cache .mypy_cache .tox
