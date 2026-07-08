#!/usr/bin/env python3
"""
Summarize c7n findings stored in S3 after Lambda dry-run (or live) runs.

For each policy+region combination, finds the most recent run and prints
one line per matched resource: region, policy, resource ID, name.

Usage:
    source .venv/bin/activate

    # Dry-run findings (default)
    python3 s3-summary.py

    # Live-run findings
    python3 s3-summary.py --prefix output

    # Filter to one region
    python3 s3-summary.py --region us-east-1

    # Filter to policies whose name contains a string
    python3 s3-summary.py --policy ec2

    # Show every run this week, not just the latest
    python3 s3-summary.py --all-runs

    # Specify bucket explicitly (skips SSM lookup)
    python3 s3-summary.py --bucket my-bucket-name
"""

import argparse
import gzip
import json
import sys
import zlib
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

SSM_PARAM = "/custodian/output-bucket-name"
SSM_REGION = "us-east-2"  # setup.sh always stores the parameter here


# ── Field extraction ─────────────────────────────────────────────────────────────


def tag(r, k="Name"):
    return next((t["Value"] for t in r.get("Tags", []) if t["Key"] == k), "")


def extract(policy, r):
    if "ec2" in policy:
        return r.get("InstanceId", "?"), tag(r)
    elif "volume" in policy:
        return r.get("VolumeId", "?"), tag(r)
    elif "snapshot" in policy:
        return r.get("SnapshotId", "?"), r.get("Description", "")[:60]
    elif "ami" in policy:
        return r.get("ImageId", "?"), r.get("Name", "")
    elif "eip" in policy:
        return r.get("AllocationId", "?"), r.get("PublicIp", "")
    elif "eni" in policy:
        return r.get("NetworkInterfaceId", "?"), r.get("Description", "")
    elif "nat" in policy:
        return r.get("NatGatewayId", "?"), tag(r)
    elif "security" in policy:
        return r.get("GroupId", "?"), r.get("GroupName", "")
    elif "key" in policy:
        return r.get("KeyPairId", "?"), r.get("KeyName", "")
    elif "log" in policy:
        return r.get("logGroupName", "?"), ""
    else:
        return "?", ""


# ── S3 helpers ───────────────────────────────────────────────────────────────────


def resolve_bucket(s3_arg):
    if s3_arg:
        return s3_arg
    ssm = boto3.client("ssm", region_name=SSM_REGION)
    try:
        return ssm.get_parameter(Name=SSM_PARAM)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            sys.exit(
                f"SSM parameter '{SSM_PARAM}' not found.\n"
                "Run setup.sh first, or pass --bucket explicitly."
            )
        raise


def read_gz(s3_client, bucket, key):
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    try:
        return json.loads(gzip.decompress(data))
    except (gzip.BadGzipFile, zlib.error, EOFError):
        return json.loads(data)  # uncompressed fallback for older c7n output


def read_log(s3_client, bucket, key):
    """Read a custodian-run.log(.gz) text file from S3. Returns content or None if absent."""
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        if key.endswith(".gz"):
            data = gzip.decompress(data)
        return data.decode("utf-8", errors="replace")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def extract_errors(log_content):
    """Scan a custodian-run.log and return (message, exception) tuples for ERROR/CRITICAL lines."""
    errors = []
    lines = log_content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if " - ERROR - " in line or " - CRITICAL - " in line:
            # Collect traceback lines until the next dated log entry
            j = i + 1
            tb_lines = []
            while j < len(lines):
                nxt = lines[j]
                if nxt and nxt[0].isdigit() and " - " in nxt:
                    break
                tb_lines.append(nxt)
                j += 1
            exc = next((ln.strip() for ln in reversed(tb_lines) if ln.strip()), None)
            for sep in (" - ERROR - ", " - CRITICAL - "):
                if sep in line:
                    msg = line.split(sep, 1)[1]
                    break
            else:
                msg = line
            errors.append((msg, exc))
            i = j
        else:
            i += 1
    return errors


# ── Main ─────────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="Summarize c7n S3 findings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--bucket", help="S3 bucket name (default: read from SSM)")
    ap.add_argument(
        "--prefix",
        default="dryrun-output",
        help="S3 key prefix — use 'output' for live runs (default: dryrun-output)",
    )
    ap.add_argument("--region", help="Show only this AWS region")
    ap.add_argument("--policy", help="Show only policies whose name contains this string")
    ap.add_argument(
        "--all-runs",
        action="store_true",
        help="Show every historical run, not just the latest per policy+region",
    )
    args = ap.parse_args()

    bucket = resolve_bucket(args.bucket)
    s3 = boto3.client("s3")
    prefix = args.prefix.rstrip("/") + "/"

    # ── Collect all resources.json.gz keys, grouped by (region, policy) ──────────
    # Key layout after stripping prefix:
    #   {region}/{policy}/{YYYY}/{MM}/{DD}/{HH}/resources.json.gz   (7 parts)

    runs = defaultdict(list)  # (region, policy) -> [(timestamp, resources_key), ...]
    log_runs = defaultdict(list)  # (region, policy) -> [(timestamp, log_key), ...]

    paginator = s3.get_paginator("list_objects_v2")
    try:
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                is_resources = key.endswith("/resources.json.gz")
                is_log = key.endswith("/custodian-run.log") or key.endswith("/custodian-run.log.gz")
                if not is_resources and not is_log:
                    continue
                rel = key[len(prefix) :]  # strip leading prefix/
                parts = rel.split("/")
                # Two supported layouts:
                #   7 parts: {region}/{policy}/{YYYY}/{MM}/{DD}/{HH}/<file>
                #            (deploy.sh with --output-dir .../{region})
                #   6 parts: {policy}/{YYYY}/{MM}/{DD}/{HH}/<file>
                #            (older deploy without explicit {region} in output path)
                if len(parts) == 7:
                    region, policy, year, month, day, hour, _ = parts
                elif len(parts) == 6:
                    region = "unknown-region"
                    policy, year, month, day, hour, _ = parts
                else:
                    continue  # unexpected structure, skip
                if args.region and region != args.region:
                    continue
                if args.policy and args.policy not in policy:
                    continue
                timestamp = f"{year}/{month}/{day}/{hour}"
                if is_resources:
                    runs[(region, policy)].append((timestamp, key))
                else:
                    log_runs[(region, policy)].append((timestamp, key))
    except ClientError as e:
        sys.exit(f"Could not list s3://{bucket}/{prefix}\n{e}")

    if not runs and not log_runs:
        print(f"No output found under s3://{bucket}/{prefix}")
        print("Possible reasons:")
        print("  - Lambda functions have not run yet (check CloudWatch logs)")
        print("  - No resources matched any policy on their last run")
        print("  - Wrong --prefix (try --prefix output for live runs)")
        return

    # ── For each policy+region, select runs to display ───────────────────────────

    total = 0
    for region, policy in sorted(runs):
        sorted_runs = sorted(runs[(region, policy)], reverse=True)  # newest first
        selected = sorted_runs if args.all_runs else [sorted_runs[0]]

        for timestamp, key in selected:
            try:
                resources = read_gz(s3, bucket, key)
            except (ClientError, json.JSONDecodeError, OSError, EOFError) as e:
                print(f"Warning: could not read {key}: {e}", file=sys.stderr)
                continue
            if not resources:
                continue
            run_tag = f"  [{timestamp}]" if args.all_runs else ""
            for r in resources:
                rid, name = extract(policy, r)
                print(f"{region:<18} {policy:<42} {rid:<25} {name}{run_tag}")
                total += 1

    filters = []
    if args.region:
        filters.append(f"region={args.region}")
    if args.policy:
        filters.append(f"policy~={args.policy!r}")
    filter_note = f" (filters: {', '.join(filters)})" if filters else ""
    print(f"\n{total} resource(s) identified{filter_note}.")

    # ── Scan custodian-run.log for ERROR/CRITICAL entries ─────────────────────────

    policy_errors = []
    for region, policy in sorted(log_runs):
        sorted_log_runs = sorted(log_runs[(region, policy)], reverse=True)
        selected = sorted_log_runs if args.all_runs else [sorted_log_runs[0]]

        for timestamp, log_key in selected:
            try:
                content = read_log(s3, bucket, log_key)
            except ClientError as e:
                print(f"Warning: could not read {log_key}: {e}", file=sys.stderr)
                continue
            if not content:
                continue
            errs = extract_errors(content)
            if errs:
                run_tag = f"  [{timestamp}]" if args.all_runs else ""
                policy_errors.append((region, policy, run_tag, errs))

    if policy_errors:
        print(f"\n{'!' * 70}")
        print(f"  POLICY ERRORS — {len(policy_errors)} policy run(s) failed")
        print(f"{'!' * 70}")
        for region, policy, run_tag, errs in policy_errors:
            print(f"\n  {region:<18} {policy}{run_tag}")
            for msg, exc in errs[:5]:
                print(f"    ERROR: {msg}")
                if exc:
                    print(f"           {exc}")
            if len(errs) > 5:
                print(f"    ... and {len(errs) - 5} more error(s)")
        print("\n  Check CloudWatch Logs for full tracebacks:")
        print("  CloudWatch → Log groups → /aws/lambda/<policy-name>")


if __name__ == "__main__":
    main()
