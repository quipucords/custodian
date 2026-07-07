#!/usr/bin/env bash
# Remove all dry-run Cloud Custodian Lambda functions and their EventBridge
# rules from every AWS region. Safe to run after going live with deploy.sh --live.
#
# Only removes functions and rules whose names end with "-dryrun".
# Does not touch live functions, the IAM role, the S3 bucket, or SSM.
#
# Usage:
#   ./cleanup-dryrun.sh

set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "=================================================="
echo " Cloud Custodian — Remove Dry-Run Lambdas"
echo "=================================================="
echo " AWS Account ID : $ACCOUNT_ID"
echo ""
echo " Scanning all regions for custodian-*-dryrun functions and rules..."
echo " (This runs sequentially across all regions — expect 10–15 minutes.)"
echo ""

REGIONS=$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text | tr '\t' '\n' | sort)
FOUND_SOMETHING=false

for region in $REGIONS; do

    # shellcheck disable=SC2016  # backticks are JMESPath syntax, not shell substitution
    fns=$(aws lambda list-functions \
        --region "$region" \
        --query 'Functions[?starts_with(FunctionName, `custodian-`)].FunctionName' \
        --output text 2>/dev/null |
        tr '\t' '\n' |
        grep -- '-dryrun$' || true)

    rules=$(aws events list-rules \
        --name-prefix "custodian-" \
        --region "$region" \
        --query 'Rules[].Name' \
        --output text 2>/dev/null |
        tr '\t' '\n' |
        grep -- '-dryrun$' || true)

    if [ -z "$fns" ] && [ -z "$rules" ]; then
        continue
    fi

    FOUND_SOMETHING=true
    echo "    === $region ==="

    # EventBridge rules: remove targets first, then delete rule
    for rule in $rules; do
        target_ids=$(aws events list-targets-by-rule \
            --rule "$rule" \
            --region "$region" \
            --query 'Targets[].Id' \
            --output text 2>/dev/null || true)
        if [ -n "$target_ids" ]; then
            # shellcheck disable=SC2086
            aws events remove-targets \
                --rule "$rule" \
                --region "$region" \
                --ids $target_ids \
                >/dev/null 2>&1 || true
        fi
        aws events delete-rule \
            --name "$rule" \
            --region "$region" \
            2>/dev/null || true
        echo "        Deleted EventBridge rule:  $rule"
    done

    # Lambda functions
    for fn in $fns; do
        aws lambda delete-function \
            --function-name "$fn" \
            --region "$region" \
            2>/dev/null || true
        echo "        Deleted Lambda function:   $fn"
    done

done

if [ "$FOUND_SOMETHING" = false ]; then
    echo "    Nothing found — already removed or never deployed."
fi

echo ""
echo "=================================================="
echo " Done."
echo "=================================================="
echo ""
echo " Note: CloudWatch log groups for dry-run Lambdas are left in place."
echo " They will be cleaned up automatically by the live delete-stale-log-groups"
echo " policy once they have had no activity for 30 days."
echo ""
