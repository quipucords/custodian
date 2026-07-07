#!/usr/bin/env python3
"""
Manually trigger all custodian Lambda functions in a region.

Useful for getting immediate S3 output without waiting for the EventBridge
schedule. Functions are invoked asynchronously so they all run in parallel.

Usage:
    source .venv/bin/activate
    python3 invoke-now.py us-east-1
    python3 invoke-now.py us-east-1 --dry-run    # just lists, does not invoke
"""

import argparse
import json
import sys

import boto3


def main():
    ap = argparse.ArgumentParser(description="Trigger custodian Lambdas immediately.")
    ap.add_argument("region", help="AWS region (e.g. us-east-1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="List matching functions without invoking them")
    ap.add_argument("--prefix", default="custodian-",
                    help="Lambda function name prefix (default: custodian-)")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)

    # Collect all matching Lambda functions
    functions = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            if fn["FunctionName"].startswith(args.prefix):
                functions.append(fn["FunctionName"])
    functions.sort()

    if not functions:
        print(f"No Lambda functions starting with '{args.prefix}' found in {args.region}.")
        print("Have you run deploy-dryrun.sh yet?")
        sys.exit(1)

    verb = "Would trigger" if args.dry_run else "Triggering"
    print(f"{verb} {len(functions)} Lambda functions in {args.region}:\n")

    for fn in functions:
        if args.dry_run:
            print(f"  {fn}")
        else:
            lam.invoke(
                FunctionName=fn,
                InvocationType="Event",   # async: fire-and-forget, all run in parallel
                Payload=json.dumps({}).encode(),
            )
            print(f"  ▶ {fn}")

    if not args.dry_run:
        print(f"\nAll {len(functions)} functions triggered.")
        print("Allow 1-2 minutes for runs to complete, then check results:")
        print(f"  python3 s3-summary.py --region {args.region}")


if __name__ == "__main__":
    main()
