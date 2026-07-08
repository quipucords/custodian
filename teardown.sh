#!/usr/bin/env bash
# Cloud Custodian - Teardown script
#
# Permanently removes everything created by setup.sh and deploy.sh:
#   - All Lambda functions prefixed "custodian-"   (every AWS region)
#   - All EventBridge rules prefixed "custodian-"  (every AWS region)
#   - All CloudWatch log groups "/aws/lambda/custodian-*" (every AWS region)
#   - The S3 output bucket and ALL its contents
#   - The IAM role and all its attached/inline policies
#   - The SSM parameter /custodian/output-bucket-name
#
# Safe to run more than once — every step checks whether the resource exists
# before attempting deletion.
#
# Usage:
#   chmod +x teardown.sh
#   ./teardown.sh
#   ./teardown.sh --yes   skip confirmation prompt (for CI)

set -euo pipefail

YES=""
for arg in "$@"; do
    case $arg in
        --yes) YES=true ;;
        --help)
            echo "Usage: $0 [--yes]"
            echo ""
            echo "Permanently removes all Cloud Custodian resources from every AWS region:"
            echo "  Lambda functions, EventBridge rules, CloudWatch log groups,"
            echo "  the S3 output bucket, the IAM role, and the SSM parameter."
            echo ""
            echo "  --yes   Skip confirmation prompt (for CI)"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--yes]"
            exit 1
            ;;
    esac
done

ROLE_NAME="custodian-cleanup-role"
SSM_BUCKET_PARAM="/custodian/output-bucket-name"
PRIMARY_REGION="us-east-2"

# ── Confirm identity ─────────────────────────────────────────────────────────────
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Resolve bucket name before the confirmation banner so the user sees the real
# bucket name and we still have it after SSM is deleted later in the script.
if BUCKET=$(aws ssm get-parameter \
    --name "$SSM_BUCKET_PARAM" \
    --region "$PRIMARY_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null); then
    BUCKET_DISPLAY="s3://${BUCKET}"
else
    BUCKET_DISPLAY="(SSM parameter not found — bucket name unknown)"
    BUCKET=""
fi

echo "=================================================="
echo " Cloud Custodian Teardown"
echo "=================================================="
echo " AWS Account ID : $ACCOUNT_ID"
echo ""
echo " This will PERMANENTLY delete:"
echo "   - All Lambda functions matching 'custodian-*' in every region"
echo "   - All EventBridge rules matching 'custodian-*' in every region"
echo "   - All CloudWatch log groups '/aws/lambda/custodian-*' in every region"
echo "   - S3 bucket: ${BUCKET_DISPLAY}"
echo "   - The IAM role '$ROLE_NAME' and all its policies"
echo "   - The SSM parameter '$SSM_BUCKET_PARAM'"
echo ""
if [ "$YES" = true ]; then
    echo " Confirmation skipped via --yes."
else
    read -rp " Type 'yes' to confirm: " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo " Teardown cancelled."
        exit 0
    fi
fi
echo ""

# ── Lambda Functions, EventBridge Rules, CloudWatch Log Groups ──────────────────
echo ">>> Scanning all regions for custodian resources..."
echo "    (Checking every available region — this takes about a minute.)"
echo ""

REGIONS=$(aws ec2 describe-regions --region "$PRIMARY_REGION" --query 'Regions[].RegionName' --output text | tr '\t' '\n' | sort)
FOUND_SOMETHING=false
DELETE_FAILED=false

# Run an AWS delete command; treat "resource not found" as success (idempotent)
# but surface any other error and record the failure.
# Sets aws_delete_status="ok" | "noop" | "error" so callers can print "Deleted"
# only when the resource actually existed (avoids false messages on re-runs).
# Note: this function is duplicated verbatim in cleanup-dryrun.sh.
aws_delete_status=ok
aws_delete() {
    local err
    aws_delete_status=ok
    if ! err=$(aws "$@" 2>&1 >/dev/null); then
        if echo "$err" | grep -qiE \
            'ResourceNotFoundException|NoSuchEntity|does not exist|not found'; then
            aws_delete_status=noop
            return 0 # already gone — expected for idempotent teardown
        fi
        echo "        ERROR: aws $* → ${err}" >&2
        aws_delete_status=error
        DELETE_FAILED=true
    fi
}

for region in $REGIONS; do

    # shellcheck disable=SC2016  # backticks are JMESPath syntax, not shell substitution
    fns=$(aws lambda list-functions \
        --region "$region" \
        --query 'Functions[?starts_with(FunctionName, `custodian-`)].FunctionName' \
        --output text 2>/dev/null || true)

    rules=$(aws events list-rules \
        --name-prefix "custodian-" \
        --region "$region" \
        --query 'Rules[].Name' \
        --output text 2>/dev/null || true)

    log_groups=$(aws logs describe-log-groups \
        --log-group-name-prefix "/aws/lambda/custodian-" \
        --region "$region" \
        --query 'logGroups[].logGroupName' \
        --output text 2>/dev/null || true)

    # Skip regions that have nothing — avoid printing dozens of empty lines
    if [ -z "$fns" ] && [ -z "$rules" ] && [ -z "$log_groups" ]; then
        continue
    fi

    FOUND_SOMETHING=true
    echo "    === $region ==="

    # EventBridge rules: remove targets first (required), then delete rule
    for rule in $rules; do
        target_ids=$(aws events list-targets-by-rule \
            --rule "$rule" \
            --region "$region" \
            --query 'Targets[].Id' \
            --output text 2>/dev/null || true)

        if [ -n "$target_ids" ]; then
            # shellcheck disable=SC2086
            aws_delete events remove-targets \
                --rule "$rule" \
                --region "$region" \
                --ids $target_ids
        fi

        aws_delete events delete-rule \
            --name "$rule" \
            --region "$region"
        [ "$aws_delete_status" = ok ] && echo "        Deleted EventBridge rule:  $rule"
    done

    # Lambda functions
    for fn in $fns; do
        aws_delete lambda delete-function \
            --function-name "$fn" \
            --region "$region"
        [ "$aws_delete_status" = ok ] && echo "        Deleted Lambda function:   $fn"
    done

    # CloudWatch log groups
    for lg in $log_groups; do
        aws_delete logs delete-log-group \
            --log-group-name "$lg" \
            --region "$region"
        [ "$aws_delete_status" = ok ] && echo "        Deleted log group:         $lg"
    done

done

if [ "$FOUND_SOMETHING" = false ]; then
    echo "    Nothing found — already removed or never deployed."
fi

if [ "$DELETE_FAILED" = true ]; then
    echo "" >&2
    echo "ERROR: One or more deletions failed — see errors above." >&2
    echo "       Resources that failed to delete may still be active." >&2
    exit 1
fi
echo ""

# ── S3 Bucket ───────────────────────────────────────────────────────────────────
echo ">>> S3 Bucket"

if [ -z "$BUCKET" ]; then
    echo "    SSM parameter was not found; bucket name is unknown."
    echo "    If a bucket was created, locate and delete it manually."
elif aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "    Emptying and deleting: $BUCKET"
    if ! aws s3 rb "s3://${BUCKET}" --force; then
        echo "    WARNING: could not fully empty/delete bucket '$BUCKET'." >&2
        echo "             It may have versioned objects; delete manually." >&2
        echo "             Continuing with remaining teardown steps..." >&2
    else
        echo "    Done."
    fi
else
    echo "    Bucket '$BUCKET' not found (already deleted?)."
fi
echo ""

# ── IAM Role ─────────────────────────────────────────────────────────────────────
# A role cannot be deleted while policies are attached. We remove all inline
# policies and detach all managed policies before deleting the role itself.

echo ">>> IAM Role: $ROLE_NAME"

if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then

    # Delete all inline policies
    inline_policies=$(aws iam list-role-policies \
        --role-name "$ROLE_NAME" \
        --query 'PolicyNames[]' \
        --output text 2>/dev/null || true)

    for policy_name in $inline_policies; do
        aws iam delete-role-policy \
            --role-name "$ROLE_NAME" \
            --policy-name "$policy_name"
        echo "    Deleted inline policy: $policy_name"
    done

    # Detach any managed policies
    managed_policies=$(aws iam list-attached-role-policies \
        --role-name "$ROLE_NAME" \
        --query 'AttachedPolicies[].PolicyArn' \
        --output text 2>/dev/null || true)

    for policy_arn in $managed_policies; do
        aws iam detach-role-policy \
            --role-name "$ROLE_NAME" \
            --policy-arn "$policy_arn"
        echo "    Detached managed policy: $policy_arn"
    done

    aws iam delete-role --role-name "$ROLE_NAME"
    echo "    Deleted role."

else
    echo "    Role '$ROLE_NAME' not found (already deleted?)."
fi
echo ""

# ── SSM Parameter ────────────────────────────────────────────────────────────────
echo ">>> SSM Parameter: $SSM_BUCKET_PARAM"

if aws ssm get-parameter --name "$SSM_BUCKET_PARAM" --region "$PRIMARY_REGION" &>/dev/null; then
    aws ssm delete-parameter --name "$SSM_BUCKET_PARAM" --region "$PRIMARY_REGION"
    echo "    Deleted."
else
    echo "    Not found (already deleted?)."
fi
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────────
echo "=================================================="
echo " Teardown complete."
echo "=================================================="
echo ""
echo " All Cloud Custodian resources removed from account $ACCOUNT_ID."
echo " Running setup.sh again will start fresh from scratch."
echo ""
