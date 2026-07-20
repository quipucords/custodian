"""
Tests for the extract() and tag() helper functions in s3-summary.py.

These functions are pure Python (no AWS calls), so they can be tested
without credentials or mocking.
"""

import importlib.util
import sys
from pathlib import Path

# s3-summary.py has a hyphen — load it via importlib
_spec = importlib.util.spec_from_file_location(
    "s3_summary", Path(__file__).parent.parent / "s3-summary.py"
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["s3_summary"] = _mod
_spec.loader.exec_module(_mod)

tag = _mod.tag
extract = _mod.extract
extract_errors = _mod.extract_errors


# ── tag() ─────────────────────────────────────────────────────────────────────


class TestTag:
    def test_returns_name_tag_value(self):
        r = {"Tags": [{"Key": "Name", "Value": "my-instance"}]}
        assert tag(r) == "my-instance"

    def test_returns_empty_string_when_no_tags(self):
        assert tag({}) == ""
        assert tag({"Tags": []}) == ""

    def test_returns_empty_string_when_name_tag_missing(self):
        r = {"Tags": [{"Key": "custodian:ignore", "Value": "true"}]}
        assert tag(r) == ""

    def test_custom_key(self):
        r = {"Tags": [{"Key": "custodian:ignore", "Value": "true"}]}
        assert tag(r, "custodian:ignore") == "true"

    def test_returns_first_match_only(self):
        r = {"Tags": [{"Key": "Name", "Value": "first"}, {"Key": "Name", "Value": "second"}]}
        assert tag(r) == "first"


# ── extract() ─────────────────────────────────────────────────────────────────


class TestExtract:
    def test_ec2_running(self):
        r = {"InstanceId": "i-0abc", "Tags": [{"Key": "Name", "Value": "web-01"}]}
        rid, name = extract("terminate-stale-running-ec2", r)
        assert rid == "i-0abc"
        assert name == "web-01"

    def test_ec2_stopped(self):
        r = {"InstanceId": "i-0def", "Tags": []}
        rid, name = extract("terminate-stale-stopped-ec2", r)
        assert rid == "i-0def"
        assert name == ""

    def test_ec2_pet(self):
        r = {"InstanceId": "i-0pet", "Tags": [{"Key": "Name", "Value": "dev-box"}]}
        rid, name = extract("stop-long-running-pet-ec2", r)
        assert rid == "i-0pet"
        assert name == "dev-box"

    def test_ebs_volume(self):
        r = {"VolumeId": "vol-0abc", "Tags": [{"Key": "Name", "Value": "data"}]}
        rid, name = extract("delete-unattached-volumes", r)
        assert rid == "vol-0abc"
        assert name == "data"

    def test_snapshot_uses_snapshot_id_not_volume_id(self):
        # Snapshot records contain both SnapshotId and VolumeId (the source volume).
        # extract() must return SnapshotId, not VolumeId.
        r = {
            "SnapshotId": "snap-0abc",
            "VolumeId": "vol-0abc",
            "Description": "Created by CreateImage",
        }
        rid, name = extract("delete-old-snapshots", r)
        assert rid == "snap-0abc"
        assert name == "Created by CreateImage"

    def test_ami(self):
        r = {"ImageId": "ami-0abc", "Name": "my-ami"}
        rid, name = extract("deregister-old-amis", r)
        assert rid == "ami-0abc"
        assert name == "my-ami"

    def test_elastic_ip(self):
        r = {"AllocationId": "eipalloc-0abc", "PublicIp": "1.2.3.4"}
        rid, name = extract("release-unassociated-eips", r)
        assert rid == "eipalloc-0abc"
        assert name == "1.2.3.4"

    def test_eni(self):
        r = {"NetworkInterfaceId": "eni-0abc", "Description": "my ENI"}
        rid, name = extract("delete-detached-enis", r)
        assert rid == "eni-0abc"
        assert name == "my ENI"

    def test_nat_gateway(self):
        r = {
            "NatGatewayId": "nat-0abc",
            "Tags": [{"Key": "Name", "Value": "egress-gw"}],
        }
        rid, name = extract("delete-old-nat-gateways", r)
        assert rid == "nat-0abc"
        assert name == "egress-gw"

    def test_security_group(self):
        r = {"GroupId": "sg-0abc", "GroupName": "launch-wizard-1"}
        rid, name = extract("delete-unused-security-groups", r)
        assert rid == "sg-0abc"
        assert name == "launch-wizard-1"

    def test_key_pair(self):
        r = {"KeyPairId": "key-0abc", "KeyName": "my-key"}
        rid, name = extract("delete-unused-key-pairs", r)
        assert rid == "key-0abc"
        assert name == "my-key"

    def test_log_group(self):
        r = {"logGroupName": "/aws/lambda/my-function"}
        rid, name = extract("delete-stale-log-groups", r)
        assert rid == "/aws/lambda/my-function"
        assert name == ""

    def test_dryrun_policy_names_route_correctly(self):
        # Dryrun policies have the same name with -dryrun suffix —
        # extract() should still dispatch on the resource type substring.
        r = {"InstanceId": "i-0abc", "Tags": []}
        rid, _ = extract("terminate-stale-stopped-ec2-dryrun", r)
        assert rid == "i-0abc"

    def test_unknown_policy_returns_question_mark(self):
        rid, name = extract("some-unknown-policy", {"SomeField": "value"})
        assert rid == "?"
        assert name == ""


# ── extract_errors() ──────────────────────────────────────────────────────────


class TestExtractErrors:
    # Real-world log snippet pulled from S3 after the AccessDenied failure.
    ACCESSDENIED_LOG = """\
2026-07-08 20:06:44,180 - custodian.policy - DEBUG - Running policy:delete-unused-key-pairs-dryrun resource:key-pair region:us-east-2 c7n:0.9.51
2026-07-08 20:06:44,495 - custodian.resources.ec2 - DEBUG - Filtered from 23 to 23 ec2
2026-07-08 20:06:44,570 - custodian.output - DEBUG - metric:PolicyException Count:1 policy:delete-unused-key-pairs-dryrun restype:key-pair
2026-07-08 20:06:44,570 - custodian.output - ERROR - Error while executing policy
Traceback (most recent call last):
  File "/var/task/c7n/policy.py", line 330, in run
    resources = self.policy.resource_manager.resources()
  File "/var/task/c7n/query.py", line 549, in resources
    resources = self.filter_resources(resources)
botocore.exceptions.ClientError: An error occurred (AccessDenied) when calling the DescribeAutoScalingGroups operation: User: arn:aws:sts::123456789012:assumed-role/custodian-cleanup-role/custodian-delete-unused-key-pairs-dryrun is not authorized to perform: autoscaling:DescribeAutoScalingGroups because no identity-based policy allows the autoscaling:DescribeAutoScalingGroups action
"""  # noqa: E501

    def test_finds_error_message_and_exception(self):
        errs = extract_errors(self.ACCESSDENIED_LOG)
        assert len(errs) == 1
        msg, exc = errs[0]
        assert msg == "Error while executing policy"
        assert "AccessDenied" in exc
        assert "DescribeAutoScalingGroups" in exc

    def test_clean_log_returns_no_errors(self):
        log = """\
2026-07-08 20:00:00,000 - custodian.policy - DEBUG - Running policy:terminate-stale-running-ec2
2026-07-08 20:00:01,000 - custodian.policy - INFO - policy:terminate-stale-running-ec2 matched 3 resources
"""  # noqa: E501
        assert extract_errors(log) == []

    def test_critical_level_is_captured(self):
        log = """\
2026-07-08 20:00:00,000 - custodian.policy - CRITICAL - Unrecoverable error
FatalException: something went wrong
"""
        errs = extract_errors(log)
        assert len(errs) == 1
        msg, exc = errs[0]
        assert msg == "Unrecoverable error"
        assert exc == "FatalException: something went wrong"

    def test_error_without_traceback(self):
        log = "2026-07-08 20:00:00,000 - custodian.output - ERROR - Standalone error message\n"
        errs = extract_errors(log)
        assert len(errs) == 1
        msg, exc = errs[0]
        assert msg == "Standalone error message"
        assert exc is None

    def test_multiple_errors_all_captured(self):
        log = """\
2026-07-08 20:00:00,000 - custodian.output - ERROR - First error
FirstException: boom
2026-07-08 20:00:01,000 - custodian.output - ERROR - Second error
SecondException: bang
"""
        errs = extract_errors(log)
        assert len(errs) == 2
        assert errs[0][0] == "First error"
        assert errs[1][0] == "Second error"
