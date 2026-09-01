#!/usr/bin/env python3
"""
Manually trigger all custodian Lambda functions in a region.

Useful for getting immediate S3 output without waiting for the EventBridge
schedule. Functions are invoked asynchronously so they all run in parallel.

By default the custodian- prefix matches BOTH live and dry-run functions.
Use --live-only or --dry-run-only to restrict which set is triggered.

Usage:
    uv run invoke-now.py                                    # all regions
    uv run invoke-now.py --region us-east-1                 # only one region
    uv run invoke-now.py --region us-east-1 --dry-run       # list without invoking
    uv run invoke-now.py --region us-east-1 --live-only     # skip -dryrun functions
    uv run invoke-now.py --region us-east-1 --dry-run-only  # only -dryrun functions
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError


def main():
    ap = argparse.ArgumentParser(description="Trigger custodian Lambdas immediately.")
    ap.add_argument("--region", required=False, help="AWS region (e.g. us-east-1)")
    ap.add_argument(
        "--dry-run", action="store_true", help="List matching functions without invoking them"
    )
    ap.add_argument(
        "--name-prefix",
        default="custodian-",
        help="Lambda function name prefix (default: custodian-)",
    )
    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--live-only",
        action="store_true",
        help="Skip dry-run functions (those ending in -dryrun)",
    )
    mode_group.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Only trigger dry-run functions (those ending in -dryrun)",
    )
    args = ap.parse_args()

    triggered = 0
    failed = 0
    missing = []
    regions = [args.region] if args.region else get_regions()
    for region in regions:
        _triggered, _failed = invoke(region, args)
        triggered += _triggered
        failed += _failed
        if _triggered == 0 and _failed == 0:
            missing.append(region)

    if not args.dry_run:
        print(f"\n{triggered} function(s) triggered" + (f", {failed} failed." if failed else "."))
        if triggered:
            print("Allow 1-2 minutes for runs to complete, then check results:")
            if args.region:
                print(f"  uv run s3-summary.py --region {args.region}")
            else:
                print("  uv run s3-summary.py")
        if failed:
            print(f"{failed} errors were encountered.")
        if missing:
            print(f"{len(missing)} regions had no triggers.")
            for region in missing:
                print(f"  ? {region}")

    if failed or (triggered == 0 and not args.dry_run):
        sys.exit(1)


def get_regions() -> list[str]:
    """List available EC2 regions (no opt-in required).

    Returns:
        List of region names (e.g. ["us-east-1", "us-west-2", ...]).
    """
    ec2 = boto3.client("ec2", region_name="us-east-2")  # known reliable starting region
    regions = [
        r["RegionName"]
        for r in ec2.describe_regions().get("Regions", [])
        if r["OptInStatus"] == "opt-in-not-required"
    ]
    return regions


def invoke(region: str, args: argparse.Namespace) -> tuple[int, int]:
    """List and invoke custodian Lambda functions in a region.

    Args:
        region: AWS region name (e.g. us-east-1).
        args: Parsed command-line arguments with name_prefix, dry_run, live_only, dry_run_only.

    Returns:
        Tuple of (triggered_count, failed_count).
    """
    lam = boto3.client("lambda", region_name=region)

    # Collect all matching Lambda functions
    functions = []
    paginator = lam.get_paginator("list_functions")
    try:
        for page in paginator.paginate():
            for fn in page["Functions"]:
                name = fn["FunctionName"]
                if not name.startswith(args.name_prefix):
                    continue
                if args.live_only and name.endswith("-dryrun"):
                    continue
                if args.dry_run_only and not name.endswith("-dryrun"):
                    continue
                functions.append(name)
    except ClientError as e:
        print(f"Could not list Lambda functions in {region}: {e}", file=sys.stderr)
        return 0, 1
    functions.sort()

    if not functions:
        print(f"No Lambda functions starting with '{args.name_prefix}' found in {region}.")
        print("Have you run './deploy.sh --dry-run' yet?")
        return 0, 0

    verb = "Would trigger" if args.dry_run else "Triggering"
    print(f"\n{verb} {len(functions)} Lambda functions in {region}:\n")

    triggered = 0
    failed = 0
    for fn in functions:
        if args.dry_run:
            print(f"  {fn}")
        else:
            try:
                lam.invoke(
                    FunctionName=fn,
                    InvocationType="Event",  # async: fire-and-forget, all run in parallel
                    Payload=json.dumps({}).encode(),
                )
                print(f"  ▶ {fn}")
                triggered += 1
            except ClientError as e:
                print(f"  ✗ {fn}: {e}", file=sys.stderr)
                failed += 1
    return triggered, failed


if __name__ == "__main__":
    main()
