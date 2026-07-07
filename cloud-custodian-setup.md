# Cloud Custodian Setup Guide
## Red Hat Discovery — AWS Account Automated Cleanup

---

## What Is Cloud Custodian?

[Cloud Custodian](https://cloudcustodian.io/) (c7n) is an open-source rules engine for managing public cloud resources. Users write policies in YAML that describe:

1. **What to look at** — a resource type (EC2 instance, EBS volume, AMI, etc.)
2. **What to match** — filters (age, tags, state, etc.)
3. **What to do** — actions (terminate, delete, stop, release, etc.)

For our use case, c7n runs as a set of **AWS Lambda functions** triggered by **EventBridge scheduled rules**. Each policy becomes its own Lambda function that runs on a configurable schedule, queries AWS for matching resources, and takes the configured action. No persistent server or CI runner is required — it is entirely serverless and AWS-native.

---

## Architecture

The following AWS resources are created by this setup:

| Resource | Name / Pattern | Purpose |
|---|---|---|
| IAM Role | `custodian-cleanup-role` | Lambda execution role with least-privilege permissions |
| IAM Inline Policy | `custodian-cleanup-permissions` | Grants the role only the permissions it needs |
| S3 Bucket | `redhat-discovery-custodian-{uuid}` | Stores structured output from every policy run |
| SSM Parameter | `/custodian/output-bucket-name` | Stores the S3 bucket name; the source of truth for scripts |
| Lambda Functions | `custodian-{policy-name}` per region | One function per policy per AWS region |
| EventBridge Rules | `custodian-{policy-name}` per region | Triggers each Lambda on its configured schedule |

`custodian run` creates and manages the Lambda functions and EventBridge rules automatically. You do not create them by hand.

The S3 bucket name uses a random UUID rather than the AWS account ID. This prevents the bucket name from leaking account identity through publicly routable DNS. The bucket name is stored in SSM Parameter Store and retrieved by scripts at runtime — you do not need to know or record it manually.

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

Twelve cleanup policies are configured in `policy.yml`. All run in every AWS region to catch accidental resource creation outside our primary region (us-east-2).

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
- **Stopped instances (2 days):** A stopped instance still incurs EBS volume charges. Two days is enough time to notice and tag it if it is intentional.
- **AMIs (90 days):** Generous to accommodate long-lived Jenkins pipelines. Any AMI actively referenced by infrastructure must be tagged `custodian:exempt = true`. Deregistering an AMI also deletes its backing EBS snapshots (`delete-snapshots: true`).
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

### 2. Python 3.8 or later

```bash
python3 --version
```

### 3. Cloud Custodian

Create a virtual environment and install c7n, pinned to the version tested with this configuration:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install c7n==0.9.51 jinja2
custodian version
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

At the end, the script prints your role ARN and the exact deploy command to use.

### Step 2 — Update policy files with your account ID

Both `policy.yml` and `policy-dryrun.yml` need your 12-digit AWS account ID substituted in the `role` variable. The setup script prints this, or run:

```bash
aws sts get-caller-identity --query Account --output text
```

Edit the top of each file:

```yaml
vars:
  role: &role arn:aws:iam::123456789012:role/custodian-cleanup-role
  #                        ^^^^^^^^^^^^^ replace this in both files
```

### Step 3 — Quick local sanity check (optional)

Before deploying any Lambda functions, you can run a one-shot local check directly from your terminal. This executes immediately using your local credentials, takes no action, and writes results to a local directory:

```bash
source .venv/bin/activate
custodian run --dryrun -r all --output-dir ./local-dryrun policy.yml
```

Inspect matches:

```bash
# Which policies found something?
find ./local-dryrun -name resources.json -not -empty

# Inspect a specific result
cat ./local-dryrun/us-east-2/terminate-stale-running-ec2/resources.json | python3 -m json.tool
```

This is a fast sanity check but does not represent scheduled Lambda behavior. For a realistic observation period before going live, use the dry-run Lambda deployment described in the next section.

---

## Dry-Run Observation Period (Recommended Before Going Live)

Before deploying policies with destructive actions, it is strongly recommended to first deploy a **dry-run version** that runs on the same schedule as the live policies but takes no action. This lets the team observe exactly which resources c7n would target over a realistic period before pulling the trigger.

### What the dry-run deployment does

`policy-dryrun.yml` contains the same filters as `policy.yml` but with all `actions` removed. Deployed to Lambda, these functions will:

- Run on their configured schedules (daily and weekly)
- Query AWS and apply all filters
- Write structured output to S3 showing which resources matched
- Take **no destructive action whatsoever**

The dry-run Lambda functions are named with a `-dryrun` suffix (e.g., `custodian-terminate-stale-running-ec2-dryrun`) and coexist safely with the eventual live deployment.

### Deploy the dry-run Lambdas

```bash
chmod +x deploy-dryrun.sh
source .venv/bin/activate
./deploy-dryrun.sh
```

The script resolves the bucket name from SSM, deploys all dry-run Lambda functions across all regions, and prints commands for reviewing findings and going live.

### How long to observe

- **Daily policies** will have output within 24 hours of deployment.
- **Weekly policies** (`delete-old-snapshots`, `deregister-old-amis`, `delete-unused-key-pairs`, `delete-stale-log-groups`) will have output within 7 days.

For a thorough review, wait at least **one full week** before going live so every policy has fired at least once.

### Reviewing dry-run findings

See [Understanding S3 Output](#understanding-s3-output) below for a full explanation of what is written and how to read it. The dry-run output lands under `dryrun-output/` in the S3 bucket:

```bash
BUCKET=$(aws ssm get-parameter --name /custodian/output-bucket-name \
    --region us-east-2 --query 'Parameter.Value' --output text)

# List all files written by dry-run runs (non-empty runs only)
aws s3 ls s3://${BUCKET}/dryrun-output/ --recursive | grep resources.json

# Inspect findings for a specific policy and region
aws s3 cp \
    "s3://${BUCKET}/dryrun-output/us-east-2/terminate-stale-running-ec2-dryrun/" \
    ./review/ --recursive
gunzip -c review/*/resources.json.gz | python3 -m json.tool

# Tabular summary across recent days
custodian report \
    --source s3://${BUCKET}/dryrun-output \
    --policy terminate-stale-running-ec2-dryrun \
    --region us-east-2 --days 7 --format table
```

If a policy folder does not appear in S3, that policy found no matching resources — which either means the account is already clean for that resource type, or the Lambda has not yet fired. Check CloudWatch to confirm it ran (see [CloudWatch Logs](#cloudwatch-logs) below).

### Going live

Once the team is satisfied that the findings are correct:

```bash
# 1. Deploy the live policies
BUCKET=$(aws ssm get-parameter --name /custodian/output-bucket-name \
    --region us-east-2 --query 'Parameter.Value' --output text)
custodian run -r all --output-dir s3://${BUCKET}/output policy.yml

# 2. Remove the dry-run Lambda functions (they are no longer needed)
python cloud-custodian/tools/ops/mugc.py -c policy-dryrun.yml -r all
```

---

## Understanding S3 Output

This section explains exactly what c7n writes to S3 after each Lambda execution, so you know where to look and what to expect.

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

The primary audit file. Contains the **full AWS API response object for every resource that matched all filters** — i.e., everything that was (or in dry-run mode, would have been) acted on.

For an EC2 instance, each entry is the complete `describe-instances` response for that instance. c7n also appends a `c7n:MatchedFilters` key showing which specific filters caused it to be selected:

```json
[
  {
    "InstanceId": "i-0abc1234def56789",
    "InstanceType": "t3.medium",
    "State": {"Code": 16, "Name": "running"},
    "LaunchTime": "2026-06-23T14:30:00+00:00",
    "Placement": {"AvailabilityZone": "us-east-2a"},
    "Tags": [
      {"Key": "Name", "Value": "brad-test-june23"}
    ],
    "c7n:MatchedFilters": ["State.Name", "instance-age"]
  }
]
```

The `c7n:MatchedFilters` field is particularly useful for understanding *why* a resource was selected — for example, whether it was age, missing tag, or both.

**Resources that do not match are never recorded.** If your account has 50 EC2 instances and 2 are old enough and untagged to be flagged, `resources.json` contains only those 2. The other 48 leave no trace in S3.

### `metadata.json`

Written alongside `resources.json`. Contains the policy definition as deployed, execution timing, API call counts, and resource match metrics:

```json
{
  "policy": { "...full policy definition..." },
  "version": "0.9.51",
  "execution": {
    "id": "a1b2c3d4-...",
    "start": 1751462400.0,
    "end_time": 1751462403.2,
    "duration": 3.2
  },
  "api-stats": {
    "ec2.DescribeInstances": 1
  },
  "metrics": {
    "ResourceCount": 2,
    "ResourceTime": 1.8
  }
}
```

Useful for confirming a policy ran successfully and checking how many resources matched (`ResourceCount`).

### `action-{name}.json`

Written only in the live deployment (`policy.yml`), not in dry-run mode. Contains the IDs and results for each resource that was actually acted on. For termination this is a list of instance IDs; for deletion it is volume or snapshot IDs, and so on. If an action produces no results (e.g., the action was skipped due to an error), this file is not written.

### When nothing matches — no S3 output

When a Lambda policy run finds zero matching resources, **nothing is written to S3** for that run. The Lambda still executes and logs to CloudWatch (see below), but there are no S3 files. A missing policy folder in S3 is therefore normal and expected for policies covering resource types that are currently clean.

---

## CloudWatch Logs

Each Lambda function writes a short execution log to CloudWatch Logs under the log group `/aws/lambda/custodian-{policy-name}` in its region.

**On a run that found matching resources:**
```
policy:terminate-stale-running-ec2 resource:ec2 region:us-east-2 count:2 time:1.83
```

**On a run that found nothing:**
```
policy:terminate-stale-running-ec2 resources:ec2 region:us-east-2 no resources matched
```

**On a live run after taking action:**
```
policy:terminate-stale-running-ec2 action:terminate resources:2 execution_time:0.54
```

### View logs for a specific policy

```bash
# Stream recent logs (follow mode)
aws logs tail \
    /aws/lambda/custodian-terminate-stale-running-ec2 \
    --region us-east-2 \
    --follow

# Search for a specific resource ID
aws logs filter-log-events \
    --log-group-name /aws/lambda/custodian-terminate-stale-running-ec2 \
    --region us-east-2 \
    --filter-pattern "i-0abc1234def56789"
```

---

## Viewing Live Output and Audit Trail

### Resolve the bucket name

All commands below require the bucket name. Resolve it once per session:

```bash
BUCKET=$(aws ssm get-parameter --name /custodian/output-bucket-name \
    --region us-east-2 --query 'Parameter.Value' --output text)
```

### Check whether a policy has run and found anything

```bash
# List all output files (non-empty runs only)
aws s3 ls s3://${BUCKET}/output/ --recursive | grep resources.json
```

### Inspect the latest findings for a policy

```bash
# Download and decompress
aws s3 cp \
    "s3://${BUCKET}/output/us-east-2/terminate-stale-running-ec2/" \
    ./logs/ --recursive
gunzip -c logs/*/resources.json.gz | python3 -m json.tool
```

### Generate a human-readable report

The `custodian report` command reads historical output from S3 and summarizes findings across multiple runs:

```bash
# Table of all matched EC2 instances in the last 7 days
custodian report \
    --source s3://${BUCKET}/output \
    --policy terminate-stale-running-ec2 \
    --region us-east-2 \
    --days 7 \
    --format table

# CSV export
custodian report \
    --source s3://${BUCKET}/output \
    --policy terminate-stale-running-ec2 \
    --region us-east-2 \
    --days 30 \
    --format csv > ec2-cleanup-report.csv
```

---

## Viewing the Current Configuration

### Source of truth: policy.yml

The `policy.yml` file in this directory is the authoritative definition of all active policies. It should be kept in version control. What is in this file is what c7n has deployed.

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

To change any policy (thresholds, schedules, filters), edit `policy.yml` and redeploy. c7n compares checksums and patches Lambda functions in-place — they are not deleted and recreated.

```bash
# 1. Edit policy.yml

# 2. Optional: quick local check of the new thresholds
custodian run --dryrun -r all --output-dir ./local-dryrun policy.yml

# 3. Deploy
BUCKET=$(aws ssm get-parameter --name /custodian/output-bucket-name \
    --region us-east-2 --query 'Parameter.Value' --output text)
custodian run -r all --output-dir s3://${BUCKET}/output policy.yml
```

Remember to apply the same change to `policy-dryrun.yml` if you want the dry-run configuration to stay in sync.

Any team member with AWS admin access and c7n installed can run these commands from their local machine.

### Adding a new region

No configuration change is needed. The next deploy with `-r all` automatically covers any newly available region.

### Removing a policy

Delete it from `policy.yml`, then remove the associated Lambda and EventBridge rule:

```bash
python cloud-custodian/tools/ops/mugc.py -c policy.yml -r all
```

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
- **Encryption in transit** — bucket policy denies all non-HTTPS (`aws:SecureTransport: false`) requests
- **Automatic expiration** — objects expire after 90 days to limit data retention
