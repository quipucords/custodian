#!/usr/bin/env bash
# Deploy Cloud Custodian policies to Lambda across all AWS regions.
#
# Renders policy.yml.j2 via render-policy.py, then deploys to all regions
# in parallel (up to MAX_PARALLEL at a time). Per-region logs go to deploy-logs/.
#
# Usage:
#   ./deploy.sh            — live mode: resources WILL be terminated/deleted
#   ./deploy.sh --dryrun   — observation mode: Lambdas run but take no action

set -euo pipefail

# ── Parse arguments ──────────────────────────────────────────────────────────────
DRYRUN=""
for arg in "$@"; do
    case $arg in
        --dryrun) DRYRUN=true ;;
        --live)   DRYRUN=false ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 --dryrun | --live"
            exit 1
            ;;
    esac
done

if [ -z "$DRYRUN" ]; then
    echo "Error: you must specify a mode."
    echo ""
    echo "  ./deploy.sh --dryrun   observation only — no resources will be modified"
    echo "  ./deploy.sh --live     destructive — resources WILL be terminated/deleted"
    echo ""
    exit 1
fi

# ── Mode-specific labels ─────────────────────────────────────────────────────────
if [ "$DRYRUN" = true ]; then
    OUTPUT_PREFIX="dryrun-output"
    MODE_LABEL="Dry-Run — observation only, no resources will be modified"
    RENDER_FLAG="--dryrun"
else
    OUTPUT_PREFIX="output"
    MODE_LABEL="Live — resources matching policies WILL be terminated or deleted"
    RENDER_FLAG=""
fi

# ── Configuration ────────────────────────────────────────────────────────────────
SSM_BUCKET_PARAM="/custodian/output-bucket-name"
PRIMARY_REGION="us-east-2"
MAX_PARALLEL=8
LOG_DIR="deploy-logs"

echo "=================================================="
echo " Cloud Custodian — Deploy"
echo " Mode: ${MODE_LABEL}"
echo "=================================================="
echo ""

# ── Live deployment requires explicit confirmation ───────────────────────────────
if [ "$DRYRUN" = false ]; then
    echo " WARNING: Live policies terminate EC2 instances and delete EBS volumes,"
    echo " AMIs, snapshots, security groups, key pairs, EIPs, ENIs, NAT gateways,"
    echo " and CloudWatch log groups. Ensure you have reviewed dry-run output first."
    echo ""
    read -rp " Type 'yes' to confirm live deployment: " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo " Deployment cancelled."
        exit 0
    fi
    echo ""
fi

# ── Resolve bucket name and account ID ──────────────────────────────────────────
echo ">>> Resolving output bucket from SSM..."
BUCKET=$(aws ssm get-parameter \
    --name "$SSM_BUCKET_PARAM" \
    --region "$PRIMARY_REGION" \
    --query 'Parameter.Value' \
    --output text)
echo "    Bucket: s3://${BUCKET}"
echo ""

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# ── Render the policy template ───────────────────────────────────────────────────
echo ">>> Rendering policy.yml.j2..."
RENDERED_POLICY=$(mktemp /tmp/custodian-policy-XXXXXX)
LOCKDIR=$(mktemp -d)
trap 'rm -f "$RENDERED_POLICY"; rm -rf "$LOCKDIR"' EXIT

uv run python3 render-policy.py $RENDER_FLAG --account-id "$ACCOUNT_ID" -o "$RENDERED_POLICY"
echo "    OK. (${RENDERED_POLICY})"
echo ""

# ── Parallel deployment ──────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

REGIONS=$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text | tr '\t' '\n' | sort)
TOTAL=$(echo "$REGIONS" | wc -l | tr -d ' ')

echo ">>> Deploying to ${TOTAL} regions (up to ${MAX_PARALLEL} in parallel)..."
echo "    S3 output : s3://${BUCKET}/${OUTPUT_PREFIX}/"
echo "    Logs      : ${LOG_DIR}/"
echo ""

for region in $REGIONS; do
    while [ "$(ls "$LOCKDIR" | wc -l | tr -d ' ')" -ge "$MAX_PARALLEL" ]; do
        sleep 2
    done

    touch "${LOCKDIR}/${region}"

    (
        custodian run \
            -r "$region" \
            --output-dir "s3://${BUCKET}/${OUTPUT_PREFIX}/${region}" \
            "$RENDERED_POLICY" \
            > "${LOG_DIR}/${region}.log" 2>&1
        status=$?
        rm -f "${LOCKDIR}/${region}"
        if [ $status -eq 0 ]; then
            echo "    ✓ ${region}"
        else
            echo "    ✗ ${region} FAILED — see ${LOG_DIR}/${region}.log"
        fi
    ) &
done

wait

echo ""
echo "=================================================="
echo " Deployment complete."
echo "=================================================="
echo ""

# ── Mode-specific next steps ─────────────────────────────────────────────────────
if [ "$DRYRUN" = true ]; then
    echo " Lambdas are deployed in observation mode and will run on schedule."
    echo " They write matched resources to S3 but take no action."
    echo ""
    echo " Force an immediate run in a specific region:"
    echo "   uv run invoke-now.py us-east-1"
    echo ""
    echo " Review findings:"
    echo "   uv run s3-summary.py"
    echo "   uv run s3-summary.py --region us-east-1"
    echo "   uv run s3-summary.py --policy ec2"
    echo ""
    echo " When satisfied, go live:"
    echo "   ./deploy.sh --live"
    echo ""
    echo " After going live, remove dry-run Lambdas:"
    echo "   ./cleanup-dryrun.sh"
else
    echo " Live policies are active. Resources matching cleanup criteria will be"
    echo " terminated or deleted on their configured schedules."
    echo ""
    echo " Review what was acted on:"
    echo "   uv run s3-summary.py --prefix output"
    echo "   uv run s3-summary.py --prefix output --region us-east-1"
fi
echo ""
