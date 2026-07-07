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


# ── tag() ─────────────────────────────────────────────────────────────────────


class TestTag:
    def test_returns_name_tag_value(self):
        r = {"Tags": [{"Key": "Name", "Value": "my-instance"}]}
        assert tag(r) == "my-instance"

    def test_returns_empty_string_when_no_tags(self):
        assert tag({}) == ""
        assert tag({"Tags": []}) == ""

    def test_returns_empty_string_when_name_tag_missing(self):
        r = {"Tags": [{"Key": "custodian:exempt", "Value": "true"}]}
        assert tag(r) == ""

    def test_custom_key(self):
        r = {"Tags": [{"Key": "custodian:exempt", "Value": "true"}]}
        assert tag(r, "custodian:exempt") == "true"

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
