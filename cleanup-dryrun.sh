#!/usr/bin/env bash
# Remove all dry-run Cloud Custodian Lambda functions and their EventBridge
# rules from every AWS region. Safe to run after going live with deploy.sh --live.
#
# Only removes functions and rules whose names end with "-dryrun".
# Does not touch live functions, the IAM role, the S3 bucket, or SSM.
#
# Usage:
#   ./cleanup-dryrun.sh
#   ./cleanup-dryrun.sh --yes   skip confirmation prompt (for CI)

set -euo pipefail

YES=""
for arg in "$@"; do
    case $arg in
        --yes) YES=true ;;
        --help)
            echo "Usage: $0 [--yes]"
            echo ""
            echo "Removes all dry-run Lambda functions and EventBridge rules"
            echo "('custodian-*-dryrun') from every AWS region. Run after going"
            echo "live with './deploy.sh --live'."
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

PRIMARY_REGION="us-east-2"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "=================================================="
echo " Cloud Custodian — Remove Dry-Run Lambdas"
echo "=================================================="
echo " AWS Account ID : $ACCOUNT_ID"
echo ""
echo " This will delete all Lambda functions and EventBridge rules"
echo " whose names end with '-dryrun' across every AWS region."
echo ""
if [ "$YES" = true ]; then
    echo " Confirmation skipped via --yes."
else
    read -rp " Type 'yes' to confirm: " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo " Cancelled."
        exit 0
    fi
fi
echo ""
echo " Scanning all regions for custodian-*-dryrun functions and rules..."
echo " (This runs sequentially across all regions — expect 10–15 minutes.)"
echo ""

REGIONS=$(aws ec2 describe-regions --region "$PRIMARY_REGION" --query 'Regions[].RegionName' --output text | tr '\t' '\n' | sort)
FOUND_SOMETHING=false
DELETE_FAILED=false

# Run an AWS delete command; treat "resource not found" as success (idempotent)
# but surface any other error and record the failure.
# Sets aws_delete_status="ok" | "noop" | "error" so callers can print "Deleted"
# only when the resource actually existed (avoids false messages on re-runs).
# Note: this function is duplicated verbatim in teardown.sh.
aws_delete_status=ok
aws_delete() {
    local err
    aws_delete_status=ok
    if ! err=$(aws "$@" 2>&1 >/dev/null); then
        if echo "$err" | grep -qiE \
            'ResourceNotFoundException|NoSuchEntity|does not exist|not found'; then
            aws_delete_status=noop
            return 0 # already gone — expected for idempotent cleanup
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

done

if [ "$FOUND_SOMETHING" = false ]; then
    echo "    Nothing found — already removed or never deployed."
fi

if [ "$DELETE_FAILED" = true ]; then
    echo "" >&2
    echo "ERROR: One or more deletions failed — see errors above." >&2
    exit 1
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
