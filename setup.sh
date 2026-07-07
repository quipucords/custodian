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

# ── Configuration ───────────────────────────────────────────────────────────────
ROLE_NAME="custodian-cleanup-role"
PRIMARY_REGION="us-east-2"
LOG_RETENTION_DAYS=90
SSM_BUCKET_PARAM="/custodian/output-bucket-name"

# Derive account ID from current credentials (fails loudly if not authenticated)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Bucket name: opaque random identifier stored in SSM Parameter Store.
# Using an account-ID-based name would leak the account ID to anyone who
# discovers the bucket name (DNS probing, leaked logs, etc.).
# SSM is the source of truth; the same bucket is used on every run.
if aws ssm get-parameter --name "$SSM_BUCKET_PARAM" --region "$PRIMARY_REGION" &>/dev/null; then
    BUCKET_NAME=$(aws ssm get-parameter \
        --name "$SSM_BUCKET_PARAM" \
        --region "$PRIMARY_REGION" \
        --query 'Parameter.Value' \
        --output text)
else
    UUID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    BUCKET_NAME="redhat-discovery-custodian-${UUID}"
    aws ssm put-parameter \
        --name  "$SSM_BUCKET_PARAM" \
        --region "$PRIMARY_REGION" \
        --value "$BUCKET_NAME" \
        --type  "String" \
        --description "Cloud Custodian output S3 bucket name — do not change"
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
        --output text --query 'Role.RoleName' > /dev/null
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
                    \"ec2:DescribeNatGateways\"
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
        --output text --query 'Location' > /dev/null
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

echo "    Setting ${LOG_RETENTION_DAYS}-day object expiration..."
aws s3api put-bucket-lifecycle-configuration \
    --bucket "$BUCKET_NAME" \
    --lifecycle-configuration "{
        \"Rules\": [{
            \"ID\": \"expire-custodian-output\",
            \"Status\": \"Enabled\",
            \"Filter\": {\"Prefix\": \"\"},
            \"Expiration\": {\"Days\": ${LOG_RETENTION_DAYS}}
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
echo " 1. Update policy.yml — replace YOUR_ACCOUNT_ID at the top:"
echo "    role: arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo " 2. Install cloud-custodian if not already installed:"
echo "    python3 -m venv .venv && source .venv/bin/activate"
echo "    pip install c7n==0.9.51"
echo ""
echo " 3. Dry-run to verify which resources would be affected:"
echo "    custodian run --dryrun -r all --output-dir ./dryrun-output policy.yml"
echo "    Review ./dryrun-output/*/resources.json before proceeding."
echo ""
echo " 4. Deploy Lambda functions to all regions:"
echo "    BUCKET=\$(aws ssm get-parameter --name ${SSM_BUCKET_PARAM} --region ${PRIMARY_REGION} --query 'Parameter.Value' --output text)"
echo "    custodian run -r all --output-dir s3://\${BUCKET}/output policy.yml"
echo ""
