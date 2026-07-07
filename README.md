# Cloud Custodian Setup Guide
## quipucords / Red Hat Discovery — AWS Account Automated Cleanup

---

## What Is Cloud Custodian?

[Cloud Custodian](https://cloudcustodian.io/) (c7n) is an open-source rules engine for managing public cloud resources. Users write policies in YAML that describe:

1. **What to look at** — a resource type (EC2 instance, EBS volume, AMI, etc.)
2. **What to match** — filters (age, tags, state, etc.)
3. **What to do** — actions (terminate, delete, stop, release, etc.)

For our use case, c7n runs as a set of **AWS Lambda functions** triggered by **EventBridge scheduled rules**. Each policy becomes its own Lambda function that runs on a configurable schedule, queries AWS for matching resources, and takes the configured action. No persistent server or CI runner is required — it is entirely serverless and AWS-native.

---

## Files in This Repository

| File | Purpose |
|---|---|
| `policy.yml.j2` | **Source of truth** — Jinja2 template for all 12 cleanup policies. Edit this file to change policy configuration. |
| `render-policy.py` | Renders `policy.yml.j2` into a deployable YAML file. Called automatically by `deploy.sh`; also useful standalone. |
| `setup.sh` | One-time AWS infrastructure setup (IAM role, S3 bucket, SSM parameter). Idempotent. |
| `deploy.sh` | Deploys Lambda functions to all AWS regions. Requires `--dryrun` or `--live` flag. |
| `invoke-now.py` | Manually triggers all custodian Lambda functions in a region immediately, without waiting for the schedule. |
| `s3-summary.py` | Reads S3 output from Lambda runs and prints a compact per-resource summary. |
| `prune-orphans.py` | Removes Lambda functions and EventBridge rules for policies deleted from `policy.yml.j2`. |
| `cleanup-dryrun.sh` | Removes dry-run Lambda functions and EventBridge rules after going live. |
| `teardown.sh` | Removes **all** resources created by `setup.sh` and `deploy.sh`. Use for test account cleanup. |
| `tests/` | pytest suite covering policy template rendering and s3-summary helpers. |
| `pyproject.toml` / `uv.lock` | Python project metadata and pinned dependency lockfile. |

---

## Architecture

The following AWS resources are created by `setup.sh` and `deploy.sh`:

| Resource | Name / Pattern | Purpose |
|---|---|---|
| IAM Role | `custodian-cleanup-role` | Lambda execution role with least-privilege permissions |
| IAM Inline Policy | `custodian-cleanup-permissions` | Grants the role only the permissions it needs |
| S3 Bucket | `redhat-discovery-custodian-{uuid}` | Stores structured output from every policy run |
| SSM Parameter | `/custodian/output-bucket-name` | Stores the S3 bucket name; the source of truth for all scripts |
| Lambda Functions | `custodian-{policy-name}[-dryrun]` per region | One function per policy per AWS region |
| EventBridge Rules | `custodian-{policy-name}[-dryrun]` per region | Triggers each Lambda on its configured schedule |

`deploy.sh` creates and manages the Lambda functions and EventBridge rules automatically. You do not create them by hand.

The S3 bucket name uses a random UUID rather than the AWS account ID to avoid leaking account identity through publicly routable DNS. The bucket name is stored in SSM Parameter Store and retrieved by scripts at runtime — you do not need to know or record it manually.

---

## Tag Convention

**This is the most important operational convention for the team to follow.**

There are three tiers of behavior, controlled by tags on each resource:

### Tier 1 — Full exemption: `custodian:exempt = true`

The resource is **never touched** — not stopped, not terminated, not deleted — regardless of its age or state. Apply this to anything that must persist indefinitely:

- Jenkins worker instances and their base AMIs
- Long-lived supporting services (HashiCorp Vault, Ansible Automation Platform, etc.)
- Any resource you explicitly intend to keep running

### Tier 2 — Stop only: `custodian:stop-only = true`

EC2 instances with this tag will be **stopped** if they have been running for 24 hours or more, but will **never be terminated** regardless of how long they remain stopped. Use this for "pet" instances you need to keep around but don't want running unattended over weekends or holidays. A stopped pet instance can be restarted manually at any time.

### Tier 3 — Normal cleanup (no tag)

The resource is subject to all standard cleanup policies based on its age and state.

### The rule is absolute

There are no region-based or role-based exceptions. Any resource that must survive must be tagged. This applies to all team members equally.

| Tag | Value | EC2 behavior | Other resources |
|---|---|---|---|
| `custodian:exempt` | `true` | Never stopped or terminated | Never deleted/released/deregistered |
| `custodian:stop-only` | `true` | Stopped after 24h running; never terminated | N/A (EC2 only) |
| *(no tag)* | — | Terminated per age thresholds | Deleted/released per age thresholds |

---

## Policy Summary

Twelve cleanup policies are defined in `policy.yml.j2` and run in every AWS region to catch accidental resource creation outside the primary region (us-east-2).

| Policy | Resource | Schedule | Trigger |
|---|---|---|---|
| `stop-long-running-pet-ec2` | Running EC2 (`stop-only` tagged) | Daily | Running ≥ 24 hours |
| `terminate-stale-running-ec2` | Running EC2 (untagged) | Daily | Running ≥ 7 days |
| `terminate-stale-stopped-ec2` | Stopped EC2 (untagged) | Daily | Stopped ≥ 2 days |
| `delete-unattached-volumes` | Unattached EBS volumes | Daily | Unattached ≥ 3 days |
| `delete-old-snapshots` | EBS snapshots | Weekly | Age ≥ 30 days |
| `deregister-old-amis` | AMIs (owned by us) | Weekly | Age ≥ 90 days |
| `release-unassociated-eips` | Elastic IPs | Daily | Unassociated (any age) |
| `delete-detached-enis` | Network interfaces | Daily | Detached ≥ 1 day |
| `delete-old-nat-gateways` | NAT gateways | Daily | Age ≥ 3 days |
| `delete-unused-security-groups` | Security groups | Daily | Unused (any age) |
| `delete-unused-key-pairs` | EC2 key pairs | Weekly | Not referenced by any instance or ASG |
| `delete-stale-log-groups` | CloudWatch log groups | Weekly | No writes in ≥ 30 days |

**Notes on specific policies:**

- **`stop-long-running-pet-ec2`** only matches instances tagged `custodian:stop-only = true`. The terminate policies explicitly exclude those instances; they cannot be terminated automatically under any circumstance.
- **Stopped instances (2 days):** A stopped instance still incurs EBS volume charges. Two days is enough time to notice and tag it if it was intentional.
- **AMIs (90 days):** Generous to accommodate long-lived Jenkins pipelines. Any AMI actively referenced by infrastructure must be tagged `custodian:exempt = true`. Deregistering an AMI also deletes its backing EBS snapshots.
- **Elastic IPs:** There is no creation-time field available for EIPs, so any unassociated EIP is immediately eligible. Tag any EIP you need to keep with `custodian:exempt = true`.
- **NAT gateways (~$32/month):** Short 3-day threshold because the cost is immediate. Any intentional NAT gateway must be tagged on creation.
- **Snapshot deletion** automatically skips snapshots that still back a registered AMI, so the snapshot and AMI policies are safe to run together.
- **Stale log groups:** Active custodian Lambda log groups receive writes on every scheduled run and will never exceed the 30-day threshold.

---

## Prerequisites

### 1. AWS CLI

Install and configure the AWS CLI with credentials that have full admin access:

```bash
aws configure
# or export credentials:
# export AWS_ACCESS_KEY_ID=...
# export AWS_SECRET_ACCESS_KEY=...
# export AWS_DEFAULT_REGION=us-east-2
```

Verify authentication:

```bash
aws sts get-caller-identity
```

### 2. Python 3.10.2 or later

```bash
python3 --version
```

### 3. uv

[uv](https://docs.astral.sh/uv/) manages the Python virtual environment and dependencies automatically. If not already installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`. No manual venv creation or `pip install` needed — `uv run` handles everything on first use:

```bash
uv run custodian version
```

---

## First-Time Setup

### Step 1 — Run the setup script

The setup script creates all required AWS infrastructure. It is **idempotent**: safe to run multiple times. On subsequent runs it skips creation of existing resources and re-applies all settings to ensure they match the expected state.

```bash
chmod +x setup.sh
./setup.sh
```

The script creates:
- IAM role `custodian-cleanup-role` with a trust policy allowing Lambda to assume it
- An inline IAM policy granting least-privilege permissions (see [Security](#security) below)
- S3 bucket with a random opaque name, created in `us-east-2`:
  - All public access blocked
  - AES-256 server-side encryption enabled
  - HTTPS-only access enforced (bucket policy denies HTTP)
  - 90-day object expiration (output logs auto-delete after 90 days)
- SSM Parameter `/custodian/output-bucket-name` storing the bucket name for future use

### Step 2 — Optional: local sanity check

Before deploying any Lambda functions, you can render the policy template and run a one-shot local check directly from your terminal. This executes immediately, takes no action, and writes results to a local directory:

```bash
# Render the template to a temporary file and run a local dryrun
uv run render-policy.py --dryrun | \
    uv run custodian run --dryrun -r us-east-2 --output-dir ./local-dryrun /dev/stdin
```

Inspect matches:

```bash
find ./local-dryrun -name resources.json -not -empty
```

This is a fast sanity check. For a realistic observation period using scheduled Lambda functions, proceed to the next section.

---

## Dry-Run Observation Period (Recommended Before Going Live)

Before deploying policies with destructive actions, it is strongly recommended to first deploy a **dry-run version** that runs on the same schedule as the live policies but takes no action. This lets the team observe exactly which resources c7n would target over a realistic period before pulling the trigger.

In dry-run mode, Lambda functions:
- Run on their configured schedules (daily and weekly)
- Query AWS and apply all filters
- Write structured output to S3 showing which resources matched
- Take **no destructive action whatsoever**

Dry-run Lambda functions use a `-dryrun` name suffix (e.g., `custodian-terminate-stale-running-ec2-dryrun`) and coexist safely with a later live deployment.

### Deploy dry-run Lambdas

```bash
chmod +x deploy.sh
./deploy.sh --dryrun
```

This renders the policy template, then deploys Lambda functions to all AWS regions in parallel (8 at a time). Expect it to take 10–15 minutes.

### Force an immediate run (don't wait for the schedule)

After deployment, trigger all Lambda functions in a region immediately rather than waiting for their scheduled time:

```bash
uv run invoke-now.py us-east-2
```

Allow 1–2 minutes for the functions to complete, then check for output.

### Review findings

`s3-summary.py` reads the S3 output from Lambda runs and prints a compact one-line-per-resource summary:

```bash
# All findings across all regions (latest run per policy+region)
uv run s3-summary.py

# Filter to a specific region
uv run s3-summary.py --region us-east-1

# Filter to a specific resource type
uv run s3-summary.py --policy ec2
uv run s3-summary.py --policy snapshot

# Show all historical runs, not just the latest
uv run s3-summary.py --all-runs
```

Each row shows: region, policy name, resource ID, and resource name.

### How long to observe

- **Daily policies** will have output within 24 hours.
- **Weekly policies** (`delete-old-snapshots`, `deregister-old-amis`, `delete-unused-key-pairs`, `delete-stale-log-groups`) will have output within 7 days.

For a thorough review, wait at least **one full week** before going live so every policy has fired at least once. Use `invoke-now.py` to trigger specific regions on demand if you don't want to wait for the schedule.

If a policy produces no S3 output for a given region, either nothing matched or the Lambda has not yet fired. Check CloudWatch logs to confirm execution (see [CloudWatch Logs](#cloudwatch-logs) below).

---

## Going Live

Once the team is satisfied with dry-run findings:

### Step 1 — Deploy live Lambda functions

```bash
./deploy.sh --live
```

This prompts for explicit confirmation, renders the live policy (with destructive actions enabled), and deploys Lambda functions across all regions. Resources matching cleanup criteria will be terminated or deleted on their configured schedules.

### Step 2 — Remove dry-run Lambda functions

The dry-run Lambdas are no longer needed once live policies are deployed:

```bash
chmod +x cleanup-dryrun.sh
./cleanup-dryrun.sh
```

This removes all `custodian-*-dryrun` Lambda functions and their EventBridge rules from every region. It does not touch live functions, the IAM role, the S3 bucket, or SSM. CloudWatch log groups for the removed dry-run Lambdas are left in place — the live `delete-stale-log-groups` policy will clean them up automatically after 30 days of inactivity.

### Step 3 — Review what was acted on

After the live Lambdas have run:

```bash
uv run s3-summary.py --prefix output
uv run s3-summary.py --prefix output --region us-east-2
```

---

## Understanding S3 Output

### S3 path structure

Output is organized by region, policy name, and UTC timestamp (to the hour):

```
s3://{bucket}/{prefix}/
  {region}/
    {policy-name}/
      {YYYY/MM/DD/HH}/
        resources.json.gz
        metadata.json
        action-{name}.json    ← live deployments only; absent in dry-run
```

Example after a daily run on July 2, 2026 at 15:00 UTC:

```
s3://redhat-discovery-custodian-{uuid}/output/
  us-east-2/
    terminate-stale-running-ec2/
      2026/07/02/15/
        resources.json.gz
        metadata.json
        action-terminate.json
  eu-west-1/
    terminate-stale-running-ec2/
      2026/07/02/15/
        resources.json.gz
        metadata.json
        action-terminate.json
```

Each region that contained matching resources produces its own set of files. Regions with no matches produce no S3 output for that run.

### `resources.json.gz`

The primary audit file. Contains the **full AWS API response object for every resource that matched all filters**. c7n also appends a `c7n:MatchedFilters` key showing which specific filters caused it to be selected:

```json
[
  {
    "InstanceId": "i-0abc1234def56789",
    "InstanceType": "t3.medium",
    "State": {"Code": 16, "Name": "running"},
    "LaunchTime": "2026-06-23T14:30:00+00:00",
    "Tags": [{"Key": "Name", "Value": "brad-test-june23"}],
    "c7n:MatchedFilters": ["State.Name", "instance-age"]
  }
]
```

**Resources that do not match are never recorded.** If your account has 50 EC2 instances and 2 are flagged, `resources.json` contains only those 2.

### `metadata.json`

Contains the policy definition, execution timing, API call counts, and resource match metrics. Useful for confirming a policy ran and checking `ResourceCount`.

### `action-{name}.json`

Written only in live mode. Contains the IDs of resources that were actually acted on. Absent in dry-run runs.

### When nothing matches — no S3 output

When a Lambda run finds zero matching resources, **nothing is written to S3** for that run. A missing policy folder is normal for clean regions.

---

## CloudWatch Logs

Each Lambda function writes a short execution log to `/aws/lambda/custodian-{policy-name}` in its region.

**Run that found matching resources:**
```
policy:terminate-stale-running-ec2 resource:ec2 region:us-east-2 count:2 time:1.83
```

**Run that found nothing:**
```
policy:terminate-stale-running-ec2 resources:ec2 region:us-east-2 no resources matched
```

**Live run after taking action:**
```
policy:terminate-stale-running-ec2 action:terminate resources:2 execution_time:0.54
```

```bash
# Stream recent logs for a policy
aws logs tail /aws/lambda/custodian-terminate-stale-running-ec2 \
    --region us-east-2 --follow

# Search for a specific resource ID
aws logs filter-log-events \
    --log-group-name /aws/lambda/custodian-terminate-stale-running-ec2 \
    --region us-east-2 \
    --filter-pattern "i-0abc1234def56789"
```

---

## Viewing the Current Configuration

### Source of truth: `policy.yml.j2`

`policy.yml.j2` is the authoritative definition of all cleanup policies. `deploy.sh` renders it at deploy time — no separate policy files need to be maintained.

To inspect what a deployment will use before running it:

```bash
# Preview the rendered live policy
uv run render-policy.py

# Preview the rendered dry-run policy
uv run render-policy.py --dryrun
```

### List deployed Lambda functions

```bash
# All custodian Lambda functions in a specific region
aws lambda list-functions \
    --region us-east-2 \
    --query 'Functions[?starts_with(FunctionName, `custodian-`)].{Name:FunctionName,Modified:LastModified}' \
    --output table

# All custodian Lambda functions across every region
for region in $(aws ec2 describe-regions --query 'Regions[].RegionName' --output text); do
    fns=$(aws lambda list-functions \
        --region "$region" \
        --query 'Functions[?starts_with(FunctionName, `custodian-`)].FunctionName' \
        --output text 2>/dev/null)
    if [ -n "$fns" ]; then
        echo "=== $region ==="
        echo "$fns" | tr '\t' '\n'
    fi
done
```

### List EventBridge rules

```bash
aws events list-rules \
    --name-prefix custodian- \
    --region us-east-2 \
    --query 'Rules[].{Name:Name,Schedule:ScheduleExpression,State:State}' \
    --output table
```

---

## Updating the Configuration

To change any policy (thresholds, schedules, filters, resource types):

1. Edit `policy.yml.j2`
2. Redeploy

```bash
# Optional: preview the rendered policy before deploying
uv run render-policy.py | head -60

# Redeploy dry-run to review the effect of changes
./deploy.sh --dryrun
uv run invoke-now.py us-east-2
uv run s3-summary.py

# When satisfied, deploy live
./deploy.sh --live
```

`deploy.sh` compares checksums and patches Lambda functions in-place — unchanged functions are skipped. Any team member with AWS admin access and the dependencies installed can run these commands.

### Adding a new region

No configuration change is needed. `deploy.sh` queries all available regions at runtime and deploys to each automatically.

### Removing a policy

Delete the policy block from `policy.yml.j2` and redeploy. The Lambda function and EventBridge rule for the removed policy are **not deleted by redeployment** — they remain in place and will continue to fire on schedule until explicitly removed.

`prune-orphans.py` automates this: it renders the current template, compares the expected function names against what is actually deployed across all regions, and removes the difference.

```bash
# Preview what would be removed
uv run prune-orphans.py --dry-run

# Remove orphaned functions and rules across all regions
uv run prune-orphans.py
```

---

## Teardown

To remove **all** Cloud Custodian resources from the AWS account (useful for test environments):

```bash
chmod +x teardown.sh
./teardown.sh
```

This permanently deletes:
- All Lambda functions prefixed `custodian-` (every region)
- All EventBridge rules prefixed `custodian-` (every region)
- All CloudWatch log groups prefixed `/aws/lambda/custodian-` (every region)
- The S3 output bucket and all its contents
- The IAM role `custodian-cleanup-role` and all its policies
- The SSM parameter `/custodian/output-bucket-name`

The script prompts for explicit confirmation and is safe to run more than once. Running `./setup.sh` afterwards starts fresh from scratch.

---

## Security

### IAM Role — Least-Privilege Permissions

The `custodian-cleanup-role` Lambda execution role is granted only the permissions required to run these specific policies:

| Permission Group | Actions Granted | Why |
|---|---|---|
| EC2 Read | `DescribeInstances`, `DescribeImages`, `DescribeVolumes`, `DescribeSnapshots`, `DescribeTags`, `DescribeSecurityGroups`, `DescribeNetworkInterfaces`, `DescribeKeyPairs`, `DescribeAddresses`, `DescribeNatGateways` | Required to query and filter all targeted resource types |
| EC2 Write | `StopInstances`, `TerminateInstances`, `DeregisterImage`, `DeleteSnapshot`, `DeleteVolume`, `DetachVolume`, `DeleteSecurityGroup`, `DeleteNetworkInterface`, `DeleteKeyPair`, `ReleaseAddress`, `DeleteNatGateway` | Required for cleanup actions only |
| AutoScaling Read | `DescribeLaunchConfigurations` | Required by the security group and key pair `unused` filters to check ASG references |
| Lambda Read | `ListFunctions` | Required by the security group `unused` filter to check Lambda VPC attachments |
| Cross-Service Read | `ecs:ListClusters`, `ecs:DescribeClusters`, `batch:DescribeComputeEnvironments`, `codebuild:ListProjects`, `codebuild:BatchGetProjects` | Required by the security group `unused` filter |
| CloudWatch Logs (write) | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` (scoped to `/aws/lambda/custodian-*`) | Lambda runtime logging |
| CloudWatch Logs (manage) | `DescribeLogGroups`, `DescribeLogStreams`, `DeleteLogGroup` | Required by the stale log group cleanup policy |
| CloudWatch Metrics | `PutMetricData` | c7n emits execution metrics per run |
| S3 Output | `PutObject`, `GetBucketLocation` (scoped to the output bucket only) | Writing audit output |

The role has no IAM permissions, no access to other AWS services beyond the above, and no ability to modify its own permissions.

### S3 Bucket Security

The output bucket enforces:
- **No public access** — all four S3 public access block settings are enabled
- **Opaque name** — uses a random UUID rather than the AWS account ID to avoid leaking account identity through bucket name DNS
- **Encryption at rest** — AES-256 (SSE-S3) with bucket key enabled
- **Encryption in transit** — bucket policy denies all non-HTTPS requests
- **Automatic expiration** — objects expire after 90 days to limit data retention
