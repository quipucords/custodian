# AWS IT Public Cloud — Automated Resource Cleanup (c7n)

## Project Purpose

This project deploys [cloud-custodian (c7n)](https://cloudcustodian.io/) to automatically clean up stale and unclaimed AWS resources in the team's AWS account. Policies run as Lambda functions on a schedule and enforce tag-driven retention rules across all AWS regions.

## Repository Layout

| File | Purpose |
|---|---|
| `policy.yml.j2` | Jinja2 template defining all 12 cleanup policies — edit this to change policy behavior |
| `render-policy.py` | Renders `policy.yml.j2` into a deployable YAML file |
| `setup.sh` | One-time AWS infra setup: IAM role, S3 bucket, SSM parameter |
| `deploy.sh` | Deploys Lambda functions to all regions (`--dryrun` or `--live`) |
| `invoke-now.py` | Manually triggers all custodian Lambdas in a region immediately |
| `s3-summary.py` | Reads Lambda run output from S3 and prints a compact resource summary |
| `prune-orphans.py` | Removes Lambda functions and EventBridge rules for policies deleted from `policy.yml.j2` |
| `cleanup-dryrun.sh` | Removes dry-run Lambda functions and EventBridge rules after going live |
| `teardown.sh` | Removes all resources created by `setup.sh` and `deploy.sh` |
| `tests/` | pytest suite for policy template rendering and s3-summary helpers |
| `cloud-custodian/` | Read-only upstream clone — reference source only, do not modify |

## Our Team and Use Case

**Team:** Red Hat engineers who maintain both [quipucords](https://github.com/quipucords/quipucords) (the open-source upstream) and Red Hat Discovery (the downstream product). Discovery is Red Hat's rebrand and rebuild of quipucords, shipped as container images that customers install in their own networks.

**AWS account purpose:** Full-admin account for running integration and e2e tests. Workloads include:
- Disposable EC2 instances created/destroyed during CI runs (quipucords and Discovery)
- Jenkins workers running on custom base AMIs
- Supporting services (HashiCorp Vault, Ansible Automation Platform, etc.)

**Problem solved:** Automated, tag-driven cleanup runs on a schedule (Lambda + EventBridge) targeting EC2 instances, AMIs, EBS volumes/snapshots, EIPs, ENIs, NAT gateways, security groups, key pairs, and CloudWatch log groups. Resources tagged `custodian:exempt = true` are never touched; resources tagged `custodian:stop-only = true` are stopped but never terminated.

**Contact:** Brad Smith, principal software engineer on the quipucords/Discovery team.

## Working Conventions

- Edit `policy.yml.j2` to change policy behavior; never edit a rendered policy file directly.
- Run `./deploy.sh --dryrun` and review `s3-summary.py` output before going live with `./deploy.sh --live`.
- The `cloud-custodian/` clone is a read-only reference — do not modify files inside it.
- Tests live in `tests/`; run with `uv run pytest tests/ -v`.
- All changes must arrive via pull request; CI runs ruff, shellcheck/shfmt, yamllint, and pytest.
