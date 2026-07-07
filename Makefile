SHELL_SCRIPTS = setup.sh deploy.sh cleanup-dryrun.sh teardown.sh

.PHONY: check lint lint-python lint-shell lint-yaml test fmt

check: lint test

lint: lint-python lint-shell lint-yaml

lint-python:
	uv run ruff check .
	uv run ruff format --check .

lint-shell:
	@command -v shellcheck >/dev/null 2>&1 || { echo "shellcheck not found — install: brew install shellcheck"; exit 1; }
	@command -v shfmt >/dev/null 2>&1 || { echo "shfmt not found — install: brew install shfmt"; exit 1; }
	shellcheck --severity=warning $(SHELL_SCRIPTS)
	shfmt -d -i 4 -ci $(SHELL_SCRIPTS)

lint-yaml:
	uv run render-policy.py | uv run yamllint -c .yamllint.yml -
	uv run render-policy.py --dryrun | uv run yamllint -c .yamllint.yml -
	uv run yamllint -c .yamllint.yml .github/workflows/

test:
	uv run pytest tests/ -v

fmt:
	uv run ruff format .
	uv run ruff check --fix .
	shfmt -w -i 4 -ci $(SHELL_SCRIPTS)
