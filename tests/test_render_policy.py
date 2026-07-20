"""
Tests for policy.yml.j2 template rendering.

Verifies that both live and dry-run renders produce valid YAML containing
exactly the expected policies with correct names, account IDs, and
action blocks for each mode.
"""

from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).parent.parent
FAKE_ACCOUNT = "123456789012"

EXPECTED_POLICY_NAMES = [
    "stop-long-running-pet-ec2",
    "terminate-stale-running-ec2",
    "terminate-stale-stopped-ec2",
    "delete-unattached-volumes",
    "delete-old-snapshots",
    "deregister-stale-protected-launched-amis",
    "deregister-stale-protected-never-launched-amis",
    "deregister-old-unprotected-amis",
    "release-unassociated-eips",
    "delete-detached-enis",
    "delete-old-nat-gateways",
    "delete-unused-security-groups",
    "delete-unused-key-pairs",
    "delete-stale-log-groups",
]


def render(*, dryrun: bool, account_id: str = FAKE_ACCOUNT) -> tuple[str, dict]:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT)),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )
    rendered = env.get_template("policy.yml.j2").render(
        account_id=account_id,
        dryrun=dryrun,
    )
    return rendered, yaml.safe_load(rendered)


@pytest.fixture(scope="module")
def live():
    text, parsed = render(dryrun=False)
    return text, parsed, {p["name"]: p for p in parsed["policies"]}


@pytest.fixture(scope="module")
def dryrun():
    text, parsed = render(dryrun=True)
    return text, parsed, {p["name"]: p for p in parsed["policies"]}


# ── Live render ───────────────────────────────────────────────────────────────


class TestLiveRender:
    def test_produces_valid_yaml(self, live):
        _, parsed, _ = live
        assert isinstance(parsed, dict)
        assert "policies" in parsed

    def test_all_14_policies_present(self, live):
        _, _, policies = live
        for name in EXPECTED_POLICY_NAMES:
            assert name in policies, f"missing policy: {name}"

    def test_policy_count(self, live):
        _, _, policies = live
        assert len(policies) == len(EXPECTED_POLICY_NAMES)

    def test_no_dryrun_suffix_on_any_name(self, live):
        _, _, policies = live
        for name in policies:
            assert not name.endswith("-dryrun"), f"{name} has unexpected -dryrun suffix"

    def test_all_policies_have_non_empty_actions(self, live):
        _, _, policies = live
        for name, policy in policies.items():
            assert policy.get("actions"), f"{name} has no actions in live mode"

    def test_account_id_substituted_in_role(self, live):
        _, parsed, _ = live
        role = parsed["vars"]["role"]
        assert FAKE_ACCOUNT in role
        assert "YOUR_ACCOUNT_ID" not in role

    def test_no_placeholder_in_output(self, live):
        text, _, _ = live
        assert "YOUR_ACCOUNT_ID" not in text

    def test_all_policies_have_periodic_mode(self, live):
        _, _, policies = live
        for name, policy in policies.items():
            assert policy.get("mode", {}).get("type") == "periodic", f"{name} is not periodic mode"

    def test_all_policies_have_python311_runtime(self, live):
        _, _, policies = live
        for name, policy in policies.items():
            assert policy["mode"]["runtime"] == "python3.11", f"{name} has unexpected runtime"


# ── Dry-run render ────────────────────────────────────────────────────────────


class TestDryrunRender:
    def test_produces_valid_yaml(self, dryrun):
        _, parsed, _ = dryrun
        assert isinstance(parsed, dict)
        assert "policies" in parsed

    def test_all_14_policies_present_with_suffix(self, dryrun):
        _, _, policies = dryrun
        for name in EXPECTED_POLICY_NAMES:
            assert f"{name}-dryrun" in policies, f"missing policy: {name}-dryrun"

    def test_policy_count(self, dryrun):
        _, _, policies = dryrun
        assert len(policies) == len(EXPECTED_POLICY_NAMES)

    def test_all_names_have_dryrun_suffix(self, dryrun):
        _, _, policies = dryrun
        for name in policies:
            assert name.endswith("-dryrun"), f"{name} is missing -dryrun suffix"

    def test_all_actions_are_empty(self, dryrun):
        _, _, policies = dryrun
        for name, policy in policies.items():
            actions = policy.get("actions", [])
            assert actions == [], f"{name} has non-empty actions in dry-run mode: {actions}"

    def test_account_id_substituted_in_role(self, dryrun):
        _, parsed, _ = dryrun
        role = parsed["vars"]["role"]
        assert FAKE_ACCOUNT in role
        assert "YOUR_ACCOUNT_ID" not in role

    def test_no_placeholder_in_output(self, dryrun):
        text, _, _ = dryrun
        assert "YOUR_ACCOUNT_ID" not in text


# ── Both modes ────────────────────────────────────────────────────────────────


class TestBothModes:
    def test_live_and_dryrun_names_are_disjoint(self, live, dryrun):
        _, _, live_policies = live
        _, _, dryrun_policies = dryrun
        assert set(live_policies.keys()).isdisjoint(set(dryrun_policies.keys()))

    def test_ec2_stop_policy_has_stop_action_in_live(self, live):
        _, _, policies = live
        actions = policies["stop-long-running-pet-ec2"]["actions"]
        assert any(
            (a == "stop" or (isinstance(a, dict) and a.get("type") == "stop")) for a in actions
        )

    def test_ec2_terminate_policy_has_terminate_action_in_live(self, live):
        _, _, policies = live
        actions = policies["terminate-stale-running-ec2"]["actions"]
        assert any(
            (a == "terminate" or (isinstance(a, dict) and a.get("type") == "terminate"))
            for a in actions
        )

    # ── deregister-stale-protected-launched-amis ──────────────────────────────

    def test_deregister_stale_protected_launched_has_protect_tag_filter(self, live):
        _, _, policies = live
        filters = policies["deregister-stale-protected-launched-amis"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("key") == "tag:custodian:protect-recently-launched-ami"
            and f.get("value") == "true"
            and f.get("op") == "eq"
            for f in filters
        )

    def test_deregister_stale_protected_launched_has_last_launched_age_filter(self, live):
        _, _, policies = live
        filters = policies["deregister-stale-protected-launched-amis"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("type") == "image-attribute"
            and f.get("attribute") == "lastLaunchedTime"
            and f.get("value_type") == "age"
            and f.get("op") == "gte"
            and f.get("value") == 30
            for f in filters
        )

    def test_deregister_stale_protected_launched_has_delete_snapshots_action(self, live):
        _, _, policies = live
        actions = policies["deregister-stale-protected-launched-amis"]["actions"]
        assert any(
            isinstance(a, dict)
            and a.get("type") == "deregister"
            and a.get("delete-snapshots") is True
            for a in actions
        )

    def test_deregister_stale_protected_launched_runs_daily(self, live):
        _, _, policies = live
        schedule = policies["deregister-stale-protected-launched-amis"]["mode"]["schedule"]
        assert schedule == "rate(1 day)"

    # ── deregister-stale-protected-never-launched-amis ────────────────────────

    def test_deregister_stale_protected_never_launched_has_protect_tag_filter(self, live):
        _, _, policies = live
        filters = policies["deregister-stale-protected-never-launched-amis"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("key") == "tag:custodian:protect-recently-launched-ami"
            and f.get("value") == "true"
            and f.get("op") == "eq"
            for f in filters
        )

    def test_deregister_stale_protected_never_launched_has_absent_filter(self, live):
        _, _, policies = live
        filters = policies["deregister-stale-protected-never-launched-amis"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("type") == "image-attribute"
            and f.get("attribute") == "lastLaunchedTime"
            and f.get("value") == "absent"
            for f in filters
        )

    def test_deregister_stale_protected_never_launched_has_30_day_image_age_filter(self, live):
        _, _, policies = live
        filters = policies["deregister-stale-protected-never-launched-amis"]["filters"]
        assert any(
            isinstance(f, dict) and f.get("type") == "image-age" and f.get("days") == 30
            for f in filters
        )

    def test_deregister_stale_protected_never_launched_has_delete_snapshots_action(self, live):
        _, _, policies = live
        actions = policies["deregister-stale-protected-never-launched-amis"]["actions"]
        assert any(
            isinstance(a, dict)
            and a.get("type") == "deregister"
            and a.get("delete-snapshots") is True
            for a in actions
        )

    def test_deregister_stale_protected_never_launched_runs_daily(self, live):
        _, _, policies = live
        schedule = policies["deregister-stale-protected-never-launched-amis"]["mode"]["schedule"]
        assert schedule == "rate(1 day)"

    # ── deregister-old-unprotected-amis ───────────────────────────────────────

    def test_deregister_old_unprotected_excludes_protect_tag(self, live):
        _, _, policies = live
        filters = policies["deregister-old-unprotected-amis"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("key") == "tag:custodian:protect-recently-launched-ami"
            and f.get("value") == "true"
            and f.get("op") == "ne"
            for f in filters
        )

    def test_deregister_old_unprotected_has_7_day_image_age_filter(self, live):
        _, _, policies = live
        filters = policies["deregister-old-unprotected-amis"]["filters"]
        assert any(
            isinstance(f, dict) and f.get("type") == "image-age" and f.get("days") == 7
            for f in filters
        )

    def test_deregister_old_unprotected_has_delete_snapshots_action(self, live):
        _, _, policies = live
        actions = policies["deregister-old-unprotected-amis"]["actions"]
        assert any(
            isinstance(a, dict)
            and a.get("type") == "deregister"
            and a.get("delete-snapshots") is True
            for a in actions
        )

    def test_deregister_old_unprotected_runs_daily(self, live):
        _, _, policies = live
        schedule = policies["deregister-old-unprotected-amis"]["mode"]["schedule"]
        assert schedule == "rate(1 day)"

    def test_terminate_running_ec2_has_stop_only_filter(self, live):
        # Instances tagged custodian:no-terminate=true must never be terminated.
        # Removing this filter from the template would silently break the guarantee.
        _, _, policies = live
        filters = policies["terminate-stale-running-ec2"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("key") == "tag:custodian:no-terminate"
            and f.get("op") == "ne"
            for f in filters
        ), "terminate-stale-running-ec2 is missing the no-terminate-filter"

    def test_terminate_stopped_ec2_has_stop_only_filter(self, live):
        # Instances tagged custodian:no-terminate=true must never be terminated.
        _, _, policies = live
        filters = policies["terminate-stale-stopped-ec2"]["filters"]
        assert any(
            isinstance(f, dict)
            and f.get("key") == "tag:custodian:no-terminate"
            and f.get("op") == "ne"
            for f in filters
        ), "terminate-stale-stopped-ec2 is missing the no-terminate-filter"

    def test_delete_old_snapshots_runs_daily(self, live):
        _, _, policies = live
        assert policies["delete-old-snapshots"]["mode"]["schedule"] == "rate(1 day)"

    def test_delete_old_snapshots_uses_7_day_threshold(self, live):
        _, _, policies = live
        filters = policies["delete-old-snapshots"]["filters"]
        assert any(
            isinstance(f, dict) and f.get("type") == "age" and f.get("days") == 7 for f in filters
        )

    def test_all_policies_have_keep_filter(self, live):
        # Every policy must honour custodian:ignore=true. A missing keep-filter
        # means tagged resources would be modified/deleted despite the ignoreion.
        _, _, policies = live
        for name, policy in policies.items():
            filters = policy.get("filters", [])
            assert any(
                isinstance(f, dict)
                and f.get("key") == "tag:custodian:ignore"
                and f.get("op") == "ne"
                for f in filters
            ), f"{name} is missing *keep-filter (custodian:ignore check)"
