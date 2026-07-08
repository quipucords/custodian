#!/usr/bin/env python3
"""
Render policy.yml.j2 into a deployable Cloud Custodian policy file.

Used directly by deploy.sh, but also useful for inspecting what will be
deployed or running local custodian commands without going through deploy.sh.

Usage:
    # Render live policy to stdout
    uv run render-policy.py

    # Render dry-run policy to stdout
    uv run render-policy.py --dryrun

    # Write to a file
    uv run render-policy.py --dryrun -o /tmp/policy-dryrun.yml

    # Local dry-run test using the rendered output
    uv run render-policy.py --dryrun | \
        uv run custodian run --dryrun -r us-east-1 --output-dir ./local-dryrun /dev/stdin
"""

import argparse
import os
import sys

try:
    import jinja2
except ImportError:
    sys.exit(
        "Jinja2 is not installed.\n"
        "Run: pip install jinja2\n"
        "(It should already be present — try: source .venv/bin/activate)"
    )

import boto3


def main():
    ap = argparse.ArgumentParser(
        description="Render policy.yml.j2 to a deployable custodian policy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--dryrun",
        action="store_true",
        help="Render in dry-run mode (no destructive actions, -dryrun name suffix)",
    )
    ap.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Write rendered policy to FILE instead of stdout",
    )
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

    template_dir = os.path.dirname(os.path.abspath(__file__))
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )

    try:
        template = env.get_template("policy.yml.j2")
    except jinja2.TemplateNotFound:
        sys.exit(f"Template not found: {os.path.join(template_dir, 'policy.yml.j2')}")

    try:
        rendered = template.render(account_id=account_id, dryrun=args.dryrun)
    except jinja2.UndefinedError as e:
        sys.exit(f"Template variable error in policy.yml.j2: {e}")

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(rendered)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
