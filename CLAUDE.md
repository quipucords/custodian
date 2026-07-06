# AWS IT Public Cloud — Cloud Custodian Research

## Project Purpose

This project evaluates [cloud-custodian (c7n)](https://cloudcustodian.io/) for managing resources in our Red Hat AWS account. The goal is to determine whether it suits our needs before committing to adoption.

## Repository Layout

```
./cloud-custodian/        # Local clone of the upstream cloud-custodian repo
./cloud-custodian/docs/   # Upstream documentation source (Sphinx)
./cloud-custodian/c7n/    # Core Python package (policies, resources, actions, filters)
./cloud-custodian/tools/  # Supporting tools (c7n-mailer, c7n-org, etc.)
```

## Our Team and Use Case

**Team:** Red Hat Discovery engineering team. Discovery is a product that ships as container images customers install in their own networks.

**AWS account purpose:** Full-admin account for running integration and e2e tests. Workloads include:
- Disposable EC2 instances created/destroyed during CI runs (Discovery itself)
- Jenkins workers running on custom base AMIs
- Supporting services (HashiCorp Vault, Ansible Automation Platform, etc.)

**Problem to solve:** No tooling exists to automatically monitor and clean up stale/unclaimed AWS resources. Engineers currently terminate instances and delete volumes manually, which doesn't scale.

**Desired behavior (tag-driven cleanup):**
- Run on a schedule (cron-style, periodically)
- Target: EC2 instances, AMIs, EBS volumes, EBS snapshots
- If a resource has certain "keep" tags → do not touch it
- Otherwise → check resource age; terminate/delete after a configured threshold

**Contact:** Brad Smith, principal software engineer on the Discovery team.

## Research Focus Areas

When helping with this project, keep the following evaluation questions in mind:

1. **Fit** — Does c7n support the AWS resource types and operations we need?
2. **Policy model** — How are YAML policies structured? What filters and actions are available?
3. **Execution modes** — Pull (CLI), push (CloudTrail/Config events), periodic (Lambda/scheduled)?
4. **Deployment** — How do policies get deployed and run in production (Lambda, ECS, local)?
5. **Ops burden** — What does ongoing maintenance look like (upgrades, policy drift, alerting)?
6. **Alternatives** — How does c7n compare to AWS Config Rules, Service Control Policies, or custom Lambda?

## Working Conventions

- Primary source of truth for behavior is the source code under `./cloud-custodian/c7n/`.
- Docs under `./cloud-custodian/docs/` are Sphinx RST — read them directly when researching features.
- Do not modify files inside `./cloud-custodian/` unless explicitly asked; treat it as a read-only reference.
- Research findings and notes should be written to files in the project root (outside the clone).
