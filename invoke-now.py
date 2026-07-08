#!/usr/bin/env python3
"""
Manually trigger all custodian Lambda functions in a region.

Useful for getting immediate S3 output without waiting for the EventBridge
schedule. Functions are invoked asynchronously so they all run in parallel.

By default the custodian- prefix matches BOTH live and dry-run functions.
Use --live-only or --dryrun-only to restrict which set is triggered.

Usage:
    uv run invoke-now.py us-east-1
    uv run invoke-now.py us-east-1 --dry-run        # list without invoking
    uv run invoke-now.py us-east-1 --live-only      # skip -dryrun functions
    uv run invoke-now.py us-east-1 --dryrun-only    # only -dryrun functions
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError


def main():
    ap = argparse.ArgumentParser(description="Trigger custodian Lambdas immediately.")
    ap.add_argument("region", help="AWS region (e.g. us-east-1)")
    ap.add_argument(
        "--dry-run", action="store_true", help="List matching functions without invoking them"
    )
    ap.add_argument(
        "--prefix", default="custodian-", help="Lambda function name prefix (default: custodian-)"
    )
    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--live-only",
        action="store_true",
        help="Skip dry-run functions (those ending in -dryrun)",
    )
    mode_group.add_argument(
        "--dryrun-only",
        action="store_true",
        help="Only trigger dry-run functions (those ending in -dryrun)",
    )
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)

    # Collect all matching Lambda functions
    functions = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            name = fn["FunctionName"]
            if not name.startswith(args.prefix):
                continue
            if args.live_only and name.endswith("-dryrun"):
                continue
            if args.dryrun_only and not name.endswith("-dryrun"):
                continue
            functions.append(name)
    functions.sort()

    if not functions:
        print(f"No Lambda functions starting with '{args.prefix}' found in {args.region}.")
        print("Have you run './deploy.sh --dryrun' yet?")
        sys.exit(1)

    verb = "Would trigger" if args.dry_run else "Triggering"
    print(f"{verb} {len(functions)} Lambda functions in {args.region}:\n")

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

    if not args.dry_run:
        print(f"\n{triggered} function(s) triggered" + (f", {failed} failed." if failed else "."))
        if triggered:
            print("Allow 1-2 minutes for runs to complete, then check results:")
            print(f"  uv run s3-summary.py --region {args.region}")
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
