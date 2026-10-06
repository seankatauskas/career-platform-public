"""Check the actual bootstrap shell parses after Terraform-style substitutions."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "infra/aws/templates/cloud-init.sh.tftpl"


class BootstrapTests(unittest.TestCase):
    def render(self):
        text = TEMPLATE.read_text()
        values = {
            "region": "us-east-2", "volume_id": "vol-0123456789abcdef0",
            "initialize_volume": "no", "name": "career-platform",
            "cw_version": "1.300072.0b1766", "cw_sha256": "a" * 64,
            "operations_json": '{"version":1}', "cw_json": '{}',
            "installer": (ROOT / "deploy/aws/install-release").read_text(),
            "cost_service": (ROOT / "deploy/aws/job-search-costs.service").read_text(),
            "cost_timer": (ROOT / "deploy/aws/job-search-costs.timer").read_text(),
        }
        # Protect Terraform's escaped shell interpolation, then resolve variables.
        text = text.replace("$${", "__SHELL_BRACE__")
        text = re.sub(r"\$\{([a-z_]+)\}", lambda m: values[m[1]], text)
        return text.replace("__SHELL_BRACE__", "${")

    def test_rendered_bootstrap_and_embedded_shell_parse(self):
        text = self.render()
        blocks = [text]
        for name in ("FIREWALL", "MOUNT"):
            blocks.append(text.split("<<'" + name + "'\n", 1)[1].split("\n" + name, 1)[0])
        for block in blocks:
            result = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_docker_requires_verified_mount_and_firewall(self):
        text = self.render()
        self.assertIn("Requires=job-search-metadata-firewall.service job-search-data.service", text)
        self.assertIn("ExecStartPre=/usr/local/sbin/job-search-metadata-firewall", text)
        self.assertIn("--uid-owner 10001", text)
        self.assertIn("DOCKER-USER -d 169.254.169.254/32 -j DROP", text)
        self.assertIn('cmp -n "$bytes" "$device" /dev/zero', text)
        self.assertIn('"$(cat "$marker")" = "$expected"', text)
        self.assertNotIn("mkfs.ext4 -F", text)

    def test_cost_monitor_uses_verified_release_and_shared_packaged_units(self):
        text = self.render()
        service = (ROOT / "deploy/aws/job-search-costs.service").read_text()
        timer = (ROOT / "deploy/aws/job-search-costs.timer").read_text()
        self.assertIn(service, text)
        self.assertIn(timer, text)
        self.assertIn("WorkingDirectory=/opt/job-search/current", service)
        self.assertIn("--config /etc/job-search/operations.json", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("Environment=AWS_MAX_ATTEMPTS=1", service)
        self.assertNotIn("api-key", service)
        self.assertIn("job-search-costs.timer", text)
        drill = (ROOT / "infra/aws/recovery-drill/main.tf").read_text()
        self.assertIn("disable --now job-search-status.timer job-search-backup.timer job-search-costs.timer", drill)

    def test_native_aws_cli_is_pinned_and_preserves_cost_service_sandbox(self):
        text = self.render()
        self.assertIn("awscli-exe-linux-x86_64-2.35.21.zip", text)
        self.assertIn("1fe665267a6149dfb8551cec52b419fa6e82533fab6dd7678939209246e792ee", text)
        self.assertLess(text.index("sha256sum -c -"), text.index('"$aws_cli_stage/aws/install"'))
        self.assertNotIn("snap install aws-cli", text)
        self.assertIn("ProtectHome=true", (ROOT / "deploy/aws/job-search-costs.service").read_text())

    def test_release_workflow_aws_calls_are_allowed_by_deploy_role(self):
        workflow = "\n".join((ROOT / ".github/workflows" / name).read_text()
                             for name in ("aws-release.yml", "aws-deploy.yml"))
        iam = (ROOT / "infra/aws/iam.tf").read_text()
        policy = iam.split('resource "aws_iam_role_policy" "deploy"', 1)[1].split('resource "aws_iam_role" "infrastructure"', 1)[0]
        mapping = {
            ("ecr", "get-login-password"): "ecr:GetAuthorizationToken",
            ("ecr", "describe-images"): "ecr:DescribeImages",
            ("s3api", "put-object"): "s3:PutObject",
            ("s3api", "get-object"): "s3:GetObject",
            ("s3api", "list-objects-v2"): "s3:ListBucket",
            ("ssm", "send-command"): "ssm:SendCommand",
            ("ssm", "get-command-invocation"): "ssm:GetCommandInvocation",
        }
        calls = set(re.findall(r"\baws\s+(ecr|s3api|ssm)\s+([a-z-]+)", workflow))
        coordinator = (ROOT / "job_search/release_coordinator.py").read_text()
        calls.update(re.findall(r"self\.aws\('([^']+)', '([^']+)'", coordinator))
        self.assertTrue(calls)
        for call in calls:
            self.assertIn(call, mapping, "new AWS CLI operation needs an IAM contract")
            self.assertIn('"' + mapping[call] + '"', policy)
        ecr_statement = next(line for line in policy.splitlines() if '"ecr:DescribeImages"' in line)
        self.assertIn("Resource = [for r in aws_ecr_repository.images : r.arn]", ecr_statement)
        self.assertNotIn("AWS-RunShellScript", policy)

    def test_signature_probe_failure_never_formats_a_volume(self):
        text = self.render()
        checks = text.split("  # Any partition or signature", 1)[1].split("  new_volume=yes", 1)[0]
        checks = "# Any partition or signature" + checks
        setup = """set -euo pipefail
device=/unused-test-device
lsblk() { echo disk; }
wipefs() { return 2; }
blockdev() { echo 4096; }
cmp() { return 0; }
mkfs.ext4() { echo UNSAFE_FORMAT; }
"""
        result = subprocess.run(["bash"], input=setup + checks, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to inspect volume signatures", result.stderr)
        self.assertNotIn("UNSAFE_FORMAT", result.stdout)

    def test_new_volume_authorization_is_exact_short_lived_and_single_use(self):
        checks = self.render().split("  # Any partition or signature", 1)[1].split("  new_volume=yes", 1)[0]
        checks = "# Any partition or signature" + checks
        for case, grant, status, metadata, signature in (
            ("valid", "vol-0123456789abcdef0 1800000300\n", 1, "0:600:1", ""),
            ("missing", None, 1, "0:600:1", ""),
            ("wrong-volume", "vol-11111111111111111 1800000300\n", 1, "0:600:1", ""),
            ("expired", "vol-0123456789abcdef0 1799999999\n", 1, "0:600:1", ""),
            ("too-long", "vol-0123456789abcdef0 1800003601\n", 1, "0:600:1", ""),
            ("unsafe-owner", "vol-0123456789abcdef0 1800000300\n", 1, "1000:600:1", ""),
            ("read-error", "vol-0123456789abcdef0 1800000300\n", 2, "0:600:1", ""),
            ("signature", "vol-0123456789abcdef0 1800000300\n", 1, "0:600:1", "LUKS"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                authorization = Path(directory) / "initialize-empty-volume"
                if grant is not None:
                    authorization.write_text(grant)
                setup = f"""set -euo pipefail
expected=vol-0123456789abcdef0
device=/unused-test-device
lsblk() {{ echo disk; }}
wipefs() {{ printf '%s' '{signature}'; }}
blockdev() {{ echo 4096; }}
cmp() {{ return {status}; }}
stat() {{ echo '{metadata}'; }}
date() {{ echo 1800000000; }}
sync() {{ :; }}
mv() {{ shift 2; command mv "$@"; }}
mkfs.ext4() {{ test -f '{authorization}.used'; test ! -e '{authorization}'; echo FORMAT; }}
"""
                script = setup + checks.replace("authorization=/etc/job-search/initialize-empty-volume", f"authorization='{authorization}'")
                result = subprocess.run(["bash"], input=script, text=True, capture_output=True)
                if case == "valid":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("FORMAT", result.stdout)
                    # Even a newly written grant cannot repeat initialization.
                    authorization.write_text(grant)
                    retry = subprocess.run(["bash"], input=script, text=True, capture_output=True)
                    self.assertNotEqual(retry.returncode, 0)
                    self.assertNotIn("FORMAT", retry.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("FORMAT", result.stdout)

    def test_local_and_ci_use_same_remote_state_key(self):
        example = (ROOT / "infra/aws/backend.hcl.example").read_text()
        workflow = (ROOT / ".github/workflows/aws-terraform.yml").read_text()
        local_key = re.search(r'key\s*=\s*"([^" ]+)"', example)[1]
        workflow_key = re.search(r"-backend-config='key=([^']+)'", workflow)[1]
        self.assertEqual(local_key, workflow_key)

    def test_root_only_operations_configuration(self):
        text = self.render()
        self.assertIn("chmod 0600 /etc/job-search/operations.json", text)
        self.assertIn("install -d -o root -g 10001 -m 0710 /var/lib/job-search/private", text)
        self.assertNotIn("set -x", text)


if __name__ == "__main__":
    unittest.main()
