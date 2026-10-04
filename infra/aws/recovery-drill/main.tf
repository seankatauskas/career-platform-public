terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "career-platform", ManagedBy = "terraform", Purpose = "recovery-drill" }
  }
}

variable "region" { default = "us-east-2" }
variable "source_instance_id" {
  type = string
  validation {
    condition     = can(regex("^i-[0-9a-f]+$", var.source_instance_id))
    error_message = "Provide the existing production instance ID to read its host configuration."
  }
}
variable "backup_bucket" { type = string }
variable "release_bucket" { type = string }
variable "secret_arns" {
  type = map(string)
  validation {
    condition     = alltrue([for arn in values(var.secret_arns) : can(regex("^arn:aws:secretsmanager:", arn))])
    error_message = "Use secret ARNs, never secret values."
  }
}
variable "initialize_data_volume" {
  type        = bool
  default     = false
  description = "Explicit authorization for initial formatting of this new drill volume only."
}
variable "cloudwatch_agent_version" { default = "1.300072.0b1766" }
variable "cloudwatch_agent_sha256" { default = "05baeadca96c4bb8e43906ed09cf0bebd0f321ff6d41987bdc46ce681de0978d" }
variable "operations_log_group" { default = "/career-platform/operations" }

data "aws_instance" "source" { instance_id = var.source_instance_id }

resource "aws_ebs_volume" "drill" {
  availability_zone = data.aws_instance.source.availability_zone
  size              = 100
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "career-platform-restore-drill-data" }
}

resource "aws_instance" "drill" {
  ami                    = data.aws_instance.source.ami
  instance_type          = data.aws_instance.source.instance_type
  subnet_id              = data.aws_instance.source.subnet_id
  vpc_security_group_ids = data.aws_instance.source.vpc_security_group_ids
  iam_instance_profile   = data.aws_instance.source.iam_instance_profile
  monitoring             = false
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_protocol_ipv6          = "disabled"
    instance_metadata_tags      = "disabled"
  }
  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }
  credit_specification { cpu_credits = "standard" }
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(join("\n", [templatefile("${path.module}/../templates/cloud-init.sh.tftpl", {
    region            = var.region
    volume_id         = aws_ebs_volume.drill.id
    initialize_volume = var.initialize_data_volume ? "yes" : "no"
    name              = "career-platform-restore-drill"
    cw_version        = var.cloudwatch_agent_version
    cw_sha256         = var.cloudwatch_agent_sha256
    installer         = file("${path.module}/../../../deploy/aws/install-release")
    cost_service      = file("${path.module}/../../../deploy/aws/job-search-costs.service")
    cost_timer        = file("${path.module}/../../../deploy/aws/job-search-costs.timer")
    operations_json = jsonencode({
      version        = 1, aws_region = var.region, backup_bucket = var.backup_bucket,
      release_bucket = var.release_bucket, data_root = "/var/lib/job-search",
      release_root   = "/opt/job-search", data_volume_id = aws_ebs_volume.drill.id,
      secret_arns    = var.secret_arns, cloudwatch_namespace = "CareerPlatform",
      app_uid        = 10001, app_gid = 10001
    })
    cw_json = jsonencode({
      agent = { metrics_collection_interval = 60, run_as_user = "root" }
      metrics = {
        namespace = "CareerPlatform", append_dimensions = { InstanceId = "$${aws:InstanceId}" }
        metrics_collected = {
          mem  = { measurement = ["mem_used_percent"] }
          disk = { measurement = ["used_percent"], resources = ["/", "/var/lib/job-search"], drop_device = true }
        }
      }
      logs = { logs_collected = { files = { collect_list = [{
        file_path       = "/var/log/job-search/operations.log", log_group_name = var.operations_log_group,
        log_stream_name = "{instance_id}/recovery-drill"
      }] } } }
    })
  }), "systemctl disable --now job-search-status.timer job-search-backup.timer job-search-costs.timer"]))
  tags = { Name = "career-platform-restore-drill" }
}

resource "aws_volume_attachment" "drill" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.drill.id
  instance_id = aws_instance.drill.id
}

output "instance_id" { value = aws_instance.drill.id }
output "data_volume_id" { value = aws_ebs_volume.drill.id }
