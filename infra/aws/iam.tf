resource "aws_iam_role" "host" {

  name = "${var.name}-host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = {
        Service = "ec2.amazonaws.com"
      }, Action = "sts:AssumeRole"
    }]
  })

}
resource "aws_iam_instance_profile" "host" {
  name = "${var.name}-host"
  role = aws_iam_role.host.name
}
# A separate, opt-in read action, never a billing administrator role. The primary
# account billing query does not select an explicit billing-view ARN.
resource "aws_iam_role_policy" "cost_monitor" {
  count = var.cost_monitor_enabled ? 1 : 0
  name  = "${var.name}-cost-monitor"
  role  = aws_iam_role.host.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Action = ["ce:GetCostAndUsage"], Resource = "*"
    }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.host.name
  policy_arn = "${local.prefix}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "host" {

  name = "${var.name}-host"
  role = aws_iam_role.host.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect = "Allow", Action = ["s3:ListBucket"], Resource = [aws_s3_bucket.backups.arn, aws_s3_bucket.releases.arn]
      },
      {
        Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:AbortMultipartUpload"], Resource = "${aws_s3_bucket.backups.arn}/backups/*"
      },
      {
        Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.releases.arn}/releases/*"
      },
      {
        Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = [for s in aws_secretsmanager_secret.runtime : s.arn]
      },
      {
        Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*"
      },
      {
        Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"], Resource = [for r in aws_ecr_repository.images : r.arn]
      },
      {
        Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"], Resource = "${aws_cloudwatch_log_group.operations.arn}:*"
      },
      {
        Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "CareerPlatform"
          }
        }
      },
      {
        Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn
      }
    ]
  })

}
resource "aws_iam_openid_connect_provider" "github" {

  count          = var.github_oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

}
locals {

  oidc_arn = var.github_oidc_provider_arn != null ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
  github_subject_prefix = var.github_oidc_ids == null ? "repo:${var.github_repository}" : format(
    "repo:%s@%s/%s@%s", split("/", var.github_repository)[0], var.github_oidc_ids.owner,
    split("/", var.github_repository)[1], var.github_oidc_ids.repository
  )
  github_trust = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = {
        Federated = local.oidc_arn
        }, Action = "sts:AssumeRoleWithWebIdentity", Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "${local.github_subject_prefix}:environment:${var.github_environment}"
        }
      }
    }]
  })

}
resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-deploy"
  assume_role_policy   = local.github_trust
  max_session_duration = 3600
}
resource "aws_iam_role_policy" "deploy" {

  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*"
      },
      {
        Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = [for r in aws_ecr_repository.images : r.arn]
      },
      {
        Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.releases.arn}/releases/*"
      },
      {
        Effect    = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.releases.arn,
        Condition = { StringLike = { "s3:prefix" = "releases/coordination/*" } }
      },
      {
        Effect = "Allow", Action = ["ssm:SendCommand"], Resource = [aws_ssm_document.deploy.arn, aws_ssm_document.release_status.arn]
      },
      {
        Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "${local.prefix}:ec2:${var.region}:${local.account}:instance/*", Condition = {
          StringEquals = {
            "ssm:resourceTag/Project" = var.name
          }
        }
      },
      {
        Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*"
      }
    ]
  })

}
resource "aws_iam_role" "infrastructure" {
  name                 = "${var.name}-infrastructure"
  assume_role_policy   = local.github_trust
  max_session_duration = 3600
}
# IAM creation and policy changes require the operator bootstrap identity. The
# automation role deliberately cannot rewrite its own permissions or the host role.
# Protect its GitHub environment with required reviewers and allowed release branches.
resource "aws_iam_role_policy" "infrastructure" {

  role = aws_iam_role.infrastructure.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect = "Allow", Action = ["ec2:Describe*", "ec2:Get*", "iam:Get*", "iam:List*", "ssm:GetParameter", "sts:GetCallerIdentity", "cloudwatch:DescribeAlarms", "sns:ListTopics", "sns:ListSubscriptions", "logs:DescribeLogGroups", "budgets:ViewBudget"], Resource = "*"
      },
      {
        Effect = "Allow", Action = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:CreateVolume", "ec2:RunInstances", "ec2:CreateTags"], Resource = "*", Condition = {
          StringEquals = {
            "aws:RequestTag/Project" = var.name
          }
        }
      },
      {
        Effect = "Allow", Action = ["ec2:RunInstances"], Resource = ["${local.prefix}:ec2:${var.region}::image/*", "${local.prefix}:ec2:${var.region}:${local.account}:subnet/*", "${local.prefix}:ec2:${var.region}:${local.account}:security-group/*", "${local.prefix}:ec2:${var.region}:${local.account}:network-interface/*"]
      },
      {
        Effect = "Allow", Action = ["ec2:ModifyVpcAttribute", "ec2:ModifySubnetAttribute", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable", "ec2:ReplaceRouteTableAssociation", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress", "ec2:DeleteVpc", "ec2:DeleteSubnet", "ec2:DeleteInternetGateway", "ec2:DeleteRouteTable", "ec2:DeleteSecurityGroup", "ec2:AttachVolume", "ec2:DetachVolume", "ec2:ModifyVolume", "ec2:DeleteVolume", "ec2:StopInstances", "ec2:StartInstances", "ec2:TerminateInstances", "ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions", "ec2:ModifyInstanceCreditSpecification", "ec2:MonitorInstances", "ec2:UnmonitorInstances", "ec2:DeleteTags"], Resource = "*", Condition = {
          StringEquals = {
            "ec2:ResourceTag/Project" = var.name
          }
        }
      },
      {
        Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.host.arn, Condition = {
          StringEquals = {
            "iam:PassedToService" = "ec2.amazonaws.com"
          }
        }
      },
      {
        Effect = "Allow", Action = ["ecr:*"], Resource = "${local.prefix}:ecr:${var.region}:${local.account}:repository/${var.name}/*"
      },
      {
        Effect = "Allow", Action = ["s3:*"], Resource = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*", aws_s3_bucket.releases.arn, "${aws_s3_bucket.releases.arn}/*"]
      },
      {
        Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketVersioning"], Resource = "${local.prefix}:s3:::${var.state_bucket}"
      },
      {
        Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${local.prefix}:s3:::${var.state_bucket}/career-platform/*"
      },
      {
        Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${local.prefix}:s3:::${var.state_bucket}/career-platform/*.tflock"
      },
      {
        Effect = "Allow", Action = ["secretsmanager:CreateSecret", "secretsmanager:DescribeSecret", "secretsmanager:UpdateSecret", "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret", "secretsmanager:TagResource", "secretsmanager:UntagResource", "secretsmanager:GetResourcePolicy"], Resource = "${local.prefix}:secretsmanager:${var.region}:${local.account}:secret:${var.name}/*"
      },
      {
        Effect = "Allow", Action = ["ssm:CreateDocument", "ssm:UpdateDocument", "ssm:UpdateDocumentDefaultVersion", "ssm:DeleteDocument", "ssm:DescribeDocument", "ssm:GetDocument", "ssm:ListDocumentVersions", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource"], Resource = "${local.prefix}:ssm:${var.region}:${local.account}:document/${var.name}-*"
      },
      {
        Effect = "Allow", Action = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:ListTagsForResource"], Resource = "${local.prefix}:cloudwatch:${var.region}:${local.account}:alarm:${var.name}-*"
      },
      {
        Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:ListTagsForResource", "logs:TagResource", "logs:UntagResource"], Resource = "${local.prefix}:logs:${var.region}:${local.account}:log-group:/${var.name}/*"
      },
      {
        Effect = "Allow", Action = ["sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:Subscribe", "sns:ListSubscriptionsByTopic", "sns:TagResource", "sns:UntagResource", "sns:ListTagsForResource"], Resource = "${local.prefix}:sns:${var.region}:${local.account}:${var.name}-*"
      },
      {
        Effect = "Allow", Action = ["sns:GetSubscriptionAttributes", "sns:Unsubscribe"], Resource = "${local.prefix}:sns:${var.region}:${local.account}:${var.name}-*:*"
      },
      {
        Effect = "Allow", Action = ["budgets:ModifyBudget", "budgets:TagResource", "budgets:UntagResource", "budgets:ListTagsForResource"], Resource = "${local.prefix}:budgets::${local.account}:budget/${var.name}-*"
      }
    ]
  })

}
resource "aws_ssm_document" "deploy" {

  name          = "${var.name}-deploy"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2", description = "Install a checksummed Career Platform release; no arbitrary shell interface.", parameters = {

      ReleaseId = {
        type = "String", allowedPattern = "^[a-f0-9]{40}(-[0-9]+)?$", interpolationType = "ENV_VAR"
      },
      ManifestSha256 = {
        type = "String", allowedPattern = "^[a-f0-9]{64}$", interpolationType = "ENV_VAR"
      }

      }, mainSteps = [{
        action = "aws:runShellScript", name = "install", inputs = {
          timeoutSeconds = "10800", runCommand = ["set -eu", "test -n \"$SSM_ReleaseId\" && test -n \"$SSM_ManifestSha256\"", "/usr/local/sbin/job-search-install-release \"$SSM_ReleaseId\" \"$SSM_ManifestSha256\""]
        }
    }]
  })

}

# A fixed read-only inspection command, available before the first application
# release. Callers cannot supply shell, paths, or parameters.
resource "aws_ssm_document" "release_status" {
  name            = "${var.name}-release-status"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2", description = "Read release identity and maintenance state without changing application state.",
    mainSteps = [{
      action = "aws:runShellScript", name = "releaseStatus",
      inputs = {
        timeoutSeconds = "30",
        runCommand     = ["python3 - <<'CAREER_RELEASE_STATUS'\n${file("${path.module}/../../deploy/aws/release-status.py")}\nCAREER_RELEASE_STATUS"]
      }
    }]
  })
}
