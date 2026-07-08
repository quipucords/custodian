#!/usr/bin/env python3
"""
Remove orphaned custodian Lambda functions and EventBridge rules.

Renders policy.yml.j2 in both live and dry-run modes to determine the full
expected set of Lambda function names, then removes any deployed custodian-*
functions across all regions that are not in that set.

Run this after removing a policy from policy.yml.j2 and redeploying.

Usage:
    uv run prune-orphans.py              # scan all regions and remove orphans
    uv run prune-orphans.py --dry-run    # list orphans without removing them
    uv run prune-orphans.py --yes        # skip confirmation prompt (for CI)
    uv run prune-orphans.py --region us-east-2
"""

import argparse
import subprocess
import sys

import boto3
import yaml
from botocore.exceptions import ClientError


def get_expected_names(account_id):
    """Return the set of Lambda function names expected to exist."""
    expected = set()
    for extra in ([], ["--dry-run"]):
        try:
            result = subprocess.run(
                ["uv", "run", "render-policy.py", "--account-id", account_id] + extra,
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError:
            sys.exit("uv not found — is it installed and on PATH?")
        except subprocess.CalledProcessError as e:
            sys.exit(f"render-policy.py failed:\n{e.stderr}")
        try:
            data = yaml.safe_load(result.stdout)
        except yaml.YAMLError as e:
            sys.exit(f"render-policy.py produced invalid YAML:\n{e}")
        for policy in data.get("policies", []):
            expected.add(f"custodian-{policy['name']}")
    return expected


def list_deployed(region):
    """Return sorted list of custodian-* Lambda function names in a region."""
    lam = boto3.client("lambda", region_name=region)
    paginator = lam.get_paginator("list_functions")
    try:
        return sorted(
            fn["FunctionName"]
            for page in paginator.paginate()
            for fn in page["Functions"]
            if fn["FunctionName"].startswith("custodian-")
        )
    except ClientError as e:
        print(f"  WARNING: could not list functions in {region}: {e}", file=sys.stderr)
        return []


def remove_function(region, fn_name):
    """Remove a Lambda function and its EventBridge rule and targets."""
    lam = boto3.client("lambda", region_name=region)
    ev = boto3.client("events", region_name=region)
    try:
        paginator = ev.get_paginator("list_targets_by_rule")
        target_ids = [t["Id"] for page in paginator.paginate(Rule=fn_name) for t in page["Targets"]]
        if target_ids:
            ev.remove_targets(Rule=fn_name, Ids=target_ids)
        ev.delete_rule(Name=fn_name)
    except ev.exceptions.ResourceNotFoundException:
        pass  # rule already gone — proceed to Lambda deletion
    try:
        lam.delete_function(FunctionName=fn_name)
    except ClientError as e:
        raise RuntimeError(f"Failed to delete Lambda {fn_name}: {e}") from e


def main():
    ap = argparse.ArgumentParser(
        description="Remove orphaned custodian Lambda functions and EventBridge rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List orphans without removing them",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (for CI/automation)",
    )
    ap.add_argument("--region", help="Limit scan to a single AWS region")
    ap.add_argument(
        "--account-id",
        metavar="ID",
        help="AWS account ID (default: fetched from STS)",
    )
    args = ap.parse_args()

    if args.account_id:
        account_id = args.account_id
    else:
        try:
            account_id = boto3.client("sts").get_caller_identity()["Account"]
        except Exception as e:
            sys.exit(
                f"Could not fetch AWS account ID from STS: {e}\n"
                "Pass --account-id explicitly to skip this call."
            )
    expected = get_expected_names(account_id)

    if args.region:
        regions = [args.region]
    else:
        ec2 = boto3.client("ec2", region_name="us-east-2")
        try:
            regions = sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])
        except ClientError as e:
            sys.exit(f"Could not list AWS regions: {e}")

    print(f"Scanning {len(regions)} region(s) for orphaned custodian functions...")
    orphans = []
    for region in regions:
        for fn in list_deployed(region):
            if fn not in expected:
                orphans.append((region, fn))

    if not orphans:
        print("No orphaned functions found.")
        return

    print(f"\n{len(orphans)} orphaned function(s) found:\n")
    for region, fn in orphans:
        print(f"  {region}  {fn}")

    if args.dry_run:
        print(f"\nRun without --dry-run to remove these {len(orphans)} function(s).")
        return

    if not args.yes:
        print()
        answer = input(f"Type 'yes' to remove these {len(orphans)} function(s): ")
        if answer != "yes":
            print("Aborted.")
            sys.exit(0)

    print()
    removed = 0
    failed = 0
    for region, fn in orphans:
        try:
            remove_function(region, fn)
            print(f"  ✓ removed  {region}  {fn}")
            removed += 1
        except (ClientError, RuntimeError) as e:
            print(f"  ✗ FAILED   {region}  {fn}: {e}", file=sys.stderr)
            failed += 1

    print(f"\nDone. {removed} removed, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
