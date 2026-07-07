SHELL_SCRIPTS = setup.sh deploy.sh cleanup-dryrun.sh teardown.sh

# Poison AWS credentials for every recipe so no target can accidentally reach
# real AWS infrastructure. Any boto3 or aws-cli call will fail fast rather
# than silently hitting the real account. /dev/null blocks file-based auth.
export AWS_ACCESS_KEY_ID     := AKIAIOSFODNN7EXAMPLE
export AWS_SECRET_ACCESS_KEY := wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
export AWS_SESSION_TOKEN     :=
export AWS_SHARED_CREDENTIALS_FILE := /dev/null
export AWS_CONFIG_FILE       := /dev/null

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
	uv run render-policy.py --account-id 123456789012 | uv run yamllint -c .yamllint.yml -
	uv run render-policy.py --account-id 123456789012 --dryrun | uv run yamllint -c .yamllint.yml -
	uv run yamllint -c .yamllint.yml .github/workflows/

test:
	uv run pytest tests/ -v

fmt:
	uv run ruff format .
	uv run ruff check --fix .
	shfmt -w -i 4 -ci $(SHELL_SCRIPTS)
