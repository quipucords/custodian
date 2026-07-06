#!/usr/bin/env bash
# Deploy Cloud Custodian in dry-run mode.
#
# Deploys Lambda functions that run on the same schedule as the real policies
# but take NO destructive action. Each run writes resources.json to S3 showing
# exactly which resources would have been terminated, deleted, or stopped.
#
# Prerequisites:
#   - setup.sh must have been run first (creates the IAM role and S3 bucket)
#   - c7n installed: pip install c7n==0.9.51
#   - policy-dryrun.yml updated with your account ID (same as policy.yml)
#
# Workflow:
#   1. Run this script to deploy dry-run Lambdas.
#   2. Wait for at least one full scheduling cycle (1-2 days to catch daily policies,
#      up to 7 days to catch weekly ones).
#   3. Review findings (see commands printed at the end of this script).
#   4. When satisfied, deploy the live policies and remove the dry-run Lambdas
#      (commands also printed at the end).

set -euo pipefail

SSM_BUCKET_PARAM="/custodian/output-bucket-name"

echo "=================================================="
echo " Cloud Custodian — Dry-Run Deployment"
echo "=================================================="
echo ""

echo ">>> Resolving output bucket from SSM..."
BUCKET=$(aws ssm get-parameter \
    --name "$SSM_BUCKET_PARAM" \
    --query 'Parameter.Value' \
    --output text)
echo "    Bucket: s3://${BUCKET}"
echo ""

echo ">>> Deploying dry-run Lambda functions (all regions)..."
custodian run \
    -r all \
    --output-dir "s3://${BUCKET}/dryrun-output" \
    policy-dryrun.yml

echo ""
echo "=================================================="
echo " Dry-run deployment complete."
echo "=================================================="
echo ""
echo " Lambda functions are now deployed and will run on their configured"
echo " schedules. They write matched resources to S3 but take no action."
echo ""
echo " Output location:"
echo "   s3://${BUCKET}/dryrun-output/"
echo ""
echo " ── Reviewing findings ──────────────────────────────────────────"
echo ""
echo " List all output files written so far:"
echo "   aws s3 ls s3://${BUCKET}/dryrun-output/ --recursive"
echo ""
echo " Download and inspect matched resources for a specific policy and region:"
echo "   aws s3 cp s3://${BUCKET}/dryrun-output/terminate-stale-running-ec2-dryrun/us-east-2/ \\"
echo "       ./dryrun-local/ --recursive"
echo "   gunzip -c dryrun-local/*/resources.json.gz | python3 -m json.tool"
echo ""
echo " Tabular summary of findings for the past 7 days (example):"
echo "   custodian report \\"
echo "       --source s3://${BUCKET}/dryrun-output \\"
echo "       --policy terminate-stale-running-ec2-dryrun \\"
echo "       --region us-east-2 --days 7 --format table"
echo ""
echo " ── Going live ──────────────────────────────────────────────────"
echo ""
echo " When the team is satisfied with the findings, deploy the live policies:"
echo "   custodian run -r all --output-dir s3://${BUCKET}/output policy.yml"
echo ""
echo " Then remove the dry-run Lambda functions (they are no longer needed):"
echo "   python cloud-custodian/tools/ops/mugc.py -c policy-dryrun.yml -r all"
echo ""
