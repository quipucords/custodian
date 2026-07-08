#!/usr/bin/env bash
# Cloud Custodian - One-time AWS infrastructure setup
# Idempotent: safe to run multiple times. Creates on first run; updates on subsequent runs.
#
# Prerequisites:
#   - AWS CLI configured with credentials that have full admin access
#   - Run: aws configure (or export AWS_PROFILE / AWS_ACCESS_KEY_ID / etc.)
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh

set -euo pipefail

for arg in "$@"; do
    case $arg in
        --help)
            echo "Usage: $0"
            echo ""
            echo "One-time AWS infrastructure setup for Cloud Custodian."
            echo "Idempotent — safe to run multiple times."
            echo ""
            echo "Creates (or verifies) the IAM role, S3 output bucket, and"
            echo "SSM parameter needed before running ./deploy.sh."
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0"
            exit 1
            ;;
    esac
done

# ── Configuration ───────────────────────────────────────────────────────────────
ROLE_NAME="custodian-cleanup-role"
PRIMARY_REGION="us-east-2"
S3_EXPIRATION_DAYS=90
SSM_BUCKET_PARAM="/custodian/output-bucket-name"

if [ "$PRIMARY_REGION" = "us-east-1" ]; then
    echo "ERROR: PRIMARY_REGION is set to us-east-1." >&2
    echo "       S3 create-bucket rejects LocationConstraint for us-east-1." >&2
    echo "       Change PRIMARY_REGION to another region or remove the" >&2
    echo "       LocationConstraint from this script if us-east-1 is required." >&2
    exit 1
fi

# Derive account ID from current credentials (fails loudly if not authenticated)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Bucket name: opaque random identifier stored in SSM Parameter Store.
# Using an account-ID-based name would leak the account ID to anyone who
# discovers the bucket name (DNS probing, leaked logs, etc.).
# SSM is the source of truth; the same bucket is used on every run.
#
# Capture stdout+stderr together so we can distinguish ParameterNotFound
# (expected on first run) from real failures (bad credentials, network, etc.).
if SSM_RESULT=$(aws ssm get-parameter \
    --name "$SSM_BUCKET_PARAM" \
    --region "$PRIMARY_REGION" \
    --query 'Parameter.Value' \
    --output text 2>&1); then
    BUCKET_NAME="$SSM_RESULT"
elif echo "$SSM_RESULT" | grep -q 'ParameterNotFound'; then
    UUID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    BUCKET_NAME="redhat-discovery-custodian-${UUID}"
    aws ssm put-parameter \
        --name "$SSM_BUCKET_PARAM" \
        --region "$PRIMARY_REGION" \
        --value "$BUCKET_NAME" \
        --type "String" \
        --description "Cloud Custodian output S3 bucket name — do not change"
else
    echo "ERROR: Could not read SSM parameter ${SSM_BUCKET_PARAM}: ${SSM_RESULT}" >&2
    exit 1
fi

echo "=================================================="
echo " Cloud Custodian Setup"
echo "=================================================="
echo " AWS Account ID : $ACCOUNT_ID"
echo " IAM Role       : $ROLE_NAME"
echo " S3 Bucket      : $BUCKET_NAME"
echo " Primary Region : $PRIMARY_REGION"
echo "=================================================="
echo ""

# ── IAM Role ────────────────────────────────────────────────────────────────────
echo ">>> IAM Role: $ROLE_NAME"

if ! aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --description "Execution role for Cloud Custodian Lambda cleanup functions" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }' \
        --output text --query 'Role.RoleName' >/dev/null
    echo "    Created."
else
    echo "    Already exists, skipping creation."
fi

# ── IAM Inline Policy ───────────────────────────────────────────────────────────
# put-role-policy is always idempotent: creates or replaces.
echo ">>> IAM Policy: custodian-cleanup-permissions (attached to $ROLE_NAME)"

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "custodian-cleanup-permissions" \
    --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
            {
                \"Sid\": \"EC2Read\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"ec2:DescribeInstances\",
                    \"ec2:DescribeImages\",
                    \"ec2:DescribeVolumes\",
                    \"ec2:DescribeSnapshots\",
                    \"ec2:DescribeTags\",
                    \"ec2:DescribeSecurityGroups\",
                    \"ec2:DescribeNetworkInterfaces\",
                    \"ec2:DescribeKeyPairs\",
                    \"ec2:DescribeAddresses\",
                    \"ec2:DescribeNatGateways\",
                    \"ec2:DescribeLaunchTemplates\",
                    \"ec2:DescribeLaunchTemplateVersions\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"EC2Write\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"ec2:StopInstances\",
                    \"ec2:TerminateInstances\",
                    \"ec2:DeregisterImage\",
                    \"ec2:DeleteSnapshot\",
                    \"ec2:DeleteVolume\",
                    \"ec2:DetachVolume\",
                    \"ec2:DeleteSecurityGroup\",
                    \"ec2:DeleteNetworkInterface\",
                    \"ec2:DeleteKeyPair\",
                    \"ec2:ReleaseAddress\",
                    \"ec2:DeleteNatGateway\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"AutoScalingRead\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"autoscaling:DescribeAutoScalingGroups\",
                    \"autoscaling:DescribeLaunchConfigurations\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"LambdaRead\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"lambda:ListFunctions\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"CrossServiceRead\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"ecs:ListClusters\",
                    \"ecs:DescribeClusters\",
                    \"batch:DescribeComputeEnvironments\",
                    \"codebuild:ListProjects\",
                    \"codebuild:BatchGetProjects\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"CloudWatchLogsWrite\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"logs:CreateLogGroup\",
                    \"logs:CreateLogStream\",
                    \"logs:PutLogEvents\"
                ],
                \"Resource\": \"arn:aws:logs:*:${ACCOUNT_ID}:log-group:/aws/lambda/custodian-*:*\"
            },
            {
                \"Sid\": \"LogGroupManagement\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"logs:DescribeLogGroups\",
                    \"logs:DescribeLogStreams\",
                    \"logs:DeleteLogGroup\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"CloudWatchMetrics\",
                \"Effect\": \"Allow\",
                \"Action\": [\"cloudwatch:PutMetricData\"],
                \"Resource\": \"*\"
            },
            {
                \"Sid\": \"S3Output\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"s3:PutObject\",
                    \"s3:GetBucketLocation\"
                ],
                \"Resource\": [
                    \"arn:aws:s3:::${BUCKET_NAME}\",
                    \"arn:aws:s3:::${BUCKET_NAME}/*\"
                ]
            }
        ]
    }"
echo "    Applied."

# ── S3 Bucket ───────────────────────────────────────────────────────────────────
echo ">>> S3 Bucket: $BUCKET_NAME"

if ! aws s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
    aws s3api create-bucket \
        --bucket "$BUCKET_NAME" \
        --region "$PRIMARY_REGION" \
        --create-bucket-configuration LocationConstraint="$PRIMARY_REGION" \
        --output text --query 'Location' >/dev/null
    echo "    Created."
else
    echo "    Already exists, skipping creation."
fi

# All remaining S3 API calls are idempotent (they overwrite/set the current state).

echo "    Blocking all public access..."
aws s3api put-public-access-block \
    --bucket "$BUCKET_NAME" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

echo "    Enabling AES-256 server-side encryption..."
aws s3api put-bucket-encryption \
    --bucket "$BUCKET_NAME" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

echo "    Enforcing HTTPS-only access..."
aws s3api put-bucket-policy \
    --bucket "$BUCKET_NAME" \
    --policy "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Sid\": \"DenyHTTP\",
            \"Effect\": \"Deny\",
            \"Principal\": \"*\",
            \"Action\": \"s3:*\",
            \"Resource\": [
                \"arn:aws:s3:::${BUCKET_NAME}\",
                \"arn:aws:s3:::${BUCKET_NAME}/*\"
            ],
            \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
        }]
    }"

echo "    Setting ${S3_EXPIRATION_DAYS}-day object expiration..."
aws s3api put-bucket-lifecycle-configuration \
    --bucket "$BUCKET_NAME" \
    --lifecycle-configuration "{
        \"Rules\": [{
            \"ID\": \"expire-custodian-output\",
            \"Status\": \"Enabled\",
            \"Filter\": {\"Prefix\": \"\"},
            \"Expiration\": {\"Days\": ${S3_EXPIRATION_DAYS}}
        }]
    }"

# ── Summary ─────────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Setup complete."
echo "=================================================="
echo ""
echo " Next steps:"
echo ""
echo " 1. Install uv if not already installed:"
echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
echo "    (Dependencies in pyproject.toml are installed automatically on first use.)"
echo ""
echo " 2. Deploy in dry-run mode to observe what would be cleaned up:"
echo "    ./deploy.sh --dry-run"
echo ""
echo " 3. Trigger an immediate run in your primary region and review findings:"
echo "    uv run invoke-now.py --region us-east-2"
echo "    uv run s3-summary.py"
echo ""
echo " 4. When satisfied with findings, deploy live:"
echo "    ./deploy.sh --live"
echo ""
