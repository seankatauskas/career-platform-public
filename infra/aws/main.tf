resource "aws_vpc" "main" {

  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {
    Name = var.name
  }

}
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}
resource "aws_subnet" "main" {

  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

}
resource "aws_route_table" "main" {

  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

}
resource "aws_route_table_association" "main" {
  subnet_id      = aws_subnet.main.id
  route_table_id = aws_route_table.main.id
}
resource "aws_security_group" "host" {

  name        = var.name
  description = "No public inbound ports; Tailscale relay and SSM use outbound connections."
  ingress     = []
  vpc_id      = aws_vpc.main.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

}
resource "aws_ebs_volume" "data" {

  availability_zone = aws_subnet.main.availability_zone
  size              = var.data_volume_size
  type              = "gp3"
  encrypted         = true
  tags = {
    Name = "${var.name}-data"
  }
  lifecycle {
    prevent_destroy = true
  }

}
resource "aws_instance" "host" {

  ami                    = coalesce(var.ami_id, data.aws_ami.ubuntu.id)
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.main.id
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name
  monitoring             = true
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
  credit_specification {
    cpu_credits = "standard"
  }
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(templatefile("${path.module}/templates/cloud-init.sh.tftpl", {

    region            = var.region
    volume_id         = aws_ebs_volume.data.id
    initialize_volume = var.initialize_data_volume ? "yes" : "no"
    name              = var.name
    cw_version        = var.cloudwatch_agent_version
    cw_sha256         = var.cloudwatch_agent_sha256
    installer         = file("${path.module}/../../deploy/aws/install-release")
    operations_json = jsonencode({

      version        = 1, aws_region = var.region, backup_bucket = aws_s3_bucket.backups.id,
      release_bucket = aws_s3_bucket.releases.id,
      data_root      = "/var/lib/job-search", release_root = "/opt/job-search",
      data_volume_id = aws_ebs_volume.data.id,
      secret_arns = {
        for k, v in aws_secretsmanager_secret.runtime : k => v.arn
      },
      cloudwatch_namespace = "CareerPlatform", notification_topic_arn = aws_sns_topic.alerts.arn,
      app_uid              = 10001, app_gid = 10001

    })
    cw_json = jsonencode({

      agent = {
        metrics_collection_interval = 60, run_as_user = "root"
      },
      metrics = {
        namespace = "CareerPlatform", append_dimensions = {
          InstanceId = "$${aws:InstanceId}"
          }, metrics_collected = {

          mem = {
            measurement = ["mem_used_percent"]
          },
          disk = {
            measurement = ["used_percent"], resources = ["/", "/var/lib/job-search"], drop_device = true
          }

        }
      },
      logs = {
        logs_collected = {
          files = {
            collect_list = [{
              file_path = "/var/log/job-search/operations.log", log_group_name = aws_cloudwatch_log_group.operations.name, log_stream_name = "{instance_id}/operations"
            }]
          }
        }
      }

    })

  }))
  tags = {
    Name = var.name
  }
  depends_on = [aws_iam_role_policy.host]

}
resource "aws_volume_attachment" "data" {

  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.host.id
  stop_instance_before_detaching = true

}
resource "aws_ecr_repository" "images" {

  for_each             = toset(["app", "hermes"])
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }

}
resource "aws_ecr_lifecycle_policy" "images" {

  for_each   = aws_ecr_repository.images
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1, description = "Expire only untagged build intermediates after 14 days", selection = {
        tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14
        }, action = {
        type = "expire"
      }
    }]
  })

}
resource "aws_s3_bucket" "backups" {

  bucket = "${var.name}-${local.account}-${var.region}-backups"
  lifecycle {
    prevent_destroy = true
  }

}
resource "aws_s3_bucket_public_access_block" "backups" {

  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

}
resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {

  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }

}
resource "aws_s3_bucket_versioning" "backups" {

  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }

}
resource "aws_s3_bucket_lifecycle_configuration" "backups" {

  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "backup-retention"
    status = "Enabled"
    filter {
      prefix = "backups/"
    }
    expiration {
      days = 14
    }
    noncurrent_version_expiration {
      noncurrent_days = 14
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

}
resource "aws_s3_bucket_policy" "backups" {

  bucket = aws_s3_bucket.backups.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Sid = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"], Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }
      }
    }]
  })

}
resource "aws_secretsmanager_secret" "runtime" {

  for_each                = local.secret_names
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 30
  description             = "Provision value out of band; never store credentials in Terraform state."
  lifecycle {
    prevent_destroy = true
  }

}
resource "aws_s3_bucket" "releases" {

  bucket = "${var.name}-${local.account}-${var.region}-releases"
  lifecycle {
    prevent_destroy = true
  }

}
resource "aws_s3_bucket_public_access_block" "releases" {

  bucket                  = aws_s3_bucket.releases.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

}
resource "aws_s3_bucket_server_side_encryption_configuration" "releases" {

  bucket = aws_s3_bucket.releases.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }

}
resource "aws_s3_bucket_versioning" "releases" {

  bucket = aws_s3_bucket.releases.id
  versioning_configuration {
    status = "Enabled"
  }

}
resource "aws_s3_bucket_policy" "releases" {

  bucket = aws_s3_bucket.releases.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Sid = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.releases.arn, "${aws_s3_bucket.releases.arn}/*"], Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }
      }
    }]
  })

}
