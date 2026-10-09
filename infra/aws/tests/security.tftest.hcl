# Mocked provider tests never call AWS or provision resources.
mock_provider "aws" {
  mock_resource "aws_sns_topic" { defaults = { arn = "arn:aws:sns:us-east-2:123456789012:career-platform-alerts" } }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_partition" { defaults = { partition = "aws" } }
  mock_data "aws_availability_zones" { defaults = { names = ["us-east-2a"] } }
  mock_data "aws_ami" { defaults = { id = "ami-0123456789abcdef0" } }
}
variables {
  github_repository = "seankatauskas/career-platform"
  alert_email       = "operator@example.com"
  state_bucket      = "career-platform-test-state"
}
run "single_host_security" {
  command = plan
  assert {
    condition = alltrue([for name in ["interaction-token", "briefing-inference.json", "briefing-api-key"] :
      aws_secretsmanager_secret.runtime[name].name == "${var.name}/${name}" &&
      aws_secretsmanager_secret.runtime[name].recovery_window_in_days == 30
    ])
    error_message = "Chief-of-staff credentials and the dedicated model profile need persistent Terraform-managed secret containers."
  }
  assert {
    condition = alltrue([for name in ["config.json", "portable-master-key", "mcp-token", "inference.json", "runpod-api-key", "resume-model.json", "hermes.env", "hermes.yaml", "tailscale-auth-key"] :
      contains(keys(aws_secretsmanager_secret.runtime), name)
    ])
    error_message = "Adding chief-of-staff secrets must preserve the existing runtime secret inventory."
  }
  assert {
    condition     = length(aws_iam_role_policy.cost_monitor) == 0
    error_message = "Billing access and paid collection must remain opt-in."
  }
  assert {
    condition     = aws_instance.host.instance_type == "t3.large" && aws_instance.host.root_block_device[0].encrypted && aws_instance.host.root_block_device[0].volume_size == 30
    error_message = "Use the small encrypted single-host baseline."
  }
  assert {
    condition     = aws_instance.host.metadata_options[0].http_tokens == "required" && aws_instance.host.metadata_options[0].http_put_response_hop_limit == 1 && aws_instance.host.metadata_options[0].http_protocol_ipv6 == "disabled"
    error_message = "Metadata must require IMDSv2 and limit container access."
  }
  assert {
    condition     = length(aws_security_group.host.ingress) == 0
    error_message = "There must be no public inbound ports."
  }
  assert {
    condition     = aws_ebs_volume.data.encrypted && aws_ebs_volume.data.size == 100
    error_message = "Persistent data needs its own encrypted volume."
  }
  assert {
    condition     = var.initialize_data_volume == false
    error_message = "Formatting must require explicit first-install authorization."
  }
  assert {
    condition     = alltrue([for r in aws_ecr_repository.images : r.image_tag_mutability == "IMMUTABLE"])
    error_message = "Releases must refer to immutable published images."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.backups.block_public_acls && aws_s3_bucket_public_access_block.backups.block_public_policy && aws_s3_bucket_public_access_block.backups.ignore_public_acls && aws_s3_bucket_public_access_block.backups.restrict_public_buckets
    error_message = "Private backups must block every public-access mechanism."
  }
  assert {
    condition     = alltrue([for a in aws_cloudwatch_metric_alarm.operations : a.treat_missing_data == "breaching"])
    error_message = "Monitoring must detect silence as well as explicit failures."
  }
  assert {
    condition     = anytrue([for q in aws_cloudwatch_metric_alarm.operations["domain"].metric_query : anytrue([for m in q.metric : m.metric_name == "DomainReady"])]) && aws_cloudwatch_metric_alarm.operations["domain_stale"].metric_name == "DomainStaleCapabilities" && aws_cloudwatch_metric_alarm.operations["domain_reconciliation"].metric_name == "DomainPendingReconciliation"
    error_message = "Domain progress and uncertain external work need alarms separate from process health."
  }
  assert {
    condition = alltrue([for name in ["health", "domain"] : anytrue([
      for q in aws_cloudwatch_metric_alarm.operations[name].metric_query : q.expression == "IF(FILL(maintenance, 0) > 0, 1, health)"
    ])]) && aws_cloudwatch_metric_alarm.operations["backup"].threshold == 86400
    error_message = "Only bounded maintenance suppresses service health; backup age remains a real 24-hour alarm."
  }
  assert {
    condition     = aws_cloudwatch_log_group.operations.retention_in_days == 14
    error_message = "Retain only fourteen days of operational logs."
  }
  assert {
    condition     = jsondecode(aws_ssm_document.deploy.content).parameters.ReleaseId.interpolationType == "ENV_VAR" && jsondecode(aws_ssm_document.deploy.content).parameters.ManifestSha256.allowedPattern == "^[a-f0-9]{64}$"
    error_message = "Deployment parameters must be validated and passed without shell interpolation."
  }
  assert {
    condition = (
      !contains(keys(jsondecode(aws_ssm_document.release_status.content)), "parameters") &&
      jsondecode(aws_ssm_document.release_status.content).mainSteps[0].inputs.timeoutSeconds == "30" &&
      strcontains(jsondecode(aws_ssm_document.release_status.content).mainSteps[0].inputs.runCommand[0], file("${path.module}/../../deploy/aws/release-status.py"))
    )
    error_message = "Release status must run only the fixed, bounded, parameterless read-only inspector."
  }

}

run "read_only_cost_monitor" {
  command = plan
  variables {
    cost_monitor_enabled = true
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.cost_monitor[0].policy).Statement == [{
      Effect = "Allow", Action = ["ce:GetCostAndUsage"], Resource = "*"
    }]
    error_message = "The cost monitor must only read cost/usage, with no billing writes or new credentials."
  }
}

run "immutable_github_subject" {
  command = plan
  variables {
    github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    github_oidc_ids          = { owner = 12345, repository = 67890 }
  }
  assert {
    condition = alltrue([for role in [aws_iam_role.deploy, aws_iam_role.infrastructure] :
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:seankatauskas@12345/career-platform@67890:environment:production" &&
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    ])
    error_message = "Both GitHub roles must trust only the exact immutable repository identity and production environment."
  }
}

run "legacy_github_subject" {
  command = plan
  variables {
    github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    github_oidc_ids          = null
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:seankatauskas/career-platform:environment:production"
    error_message = "Existing repositories retain their exact legacy subject when immutable IDs are not configured."
  }
}

run "coordinated_release_permissions" {
  command = plan
  override_resource {
    target          = aws_s3_bucket.releases
    override_during = plan
    values          = { arn = "arn:aws:s3:::test-releases" }
  }
  override_resource {
    target          = aws_ecr_repository.images["app"]
    override_during = plan
    values          = { arn = "arn:aws:ecr:us-east-2:123456789012:repository/app" }
  }
  override_resource {
    target          = aws_ecr_repository.images["hermes"]
    override_during = plan
    values          = { arn = "arn:aws:ecr:us-east-2:123456789012:repository/hermes" }
  }
  override_resource {
    target          = aws_ssm_document.deploy
    override_during = plan
    values          = { arn = "arn:aws:ssm:us-east-2:123456789012:document/deploy" }
  }
  override_resource {
    target          = aws_ssm_document.release_status
    override_during = plan
    values          = { arn = "arn:aws:ssm:us-east-2:123456789012:document/release-status" }
  }
  assert {
    condition = anytrue([for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      try(statement.Action == ["s3:ListBucket"] && statement.Resource == aws_s3_bucket.releases.arn && statement.Condition.StringLike["s3:prefix"] == "releases/coordination/*", false)
    ])
    error_message = "Candidate discovery must be limited to the coordination prefix."
  }
  assert {
    condition = anytrue([for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      try(statement.Action == ["ssm:SendCommand"] && toset(statement.Resource) == toset([aws_ssm_document.deploy.arn, aws_ssm_document.release_status.arn]), false)
    ])
    error_message = "Release automation may invoke only the installer and fixed status documents."
  }
}
