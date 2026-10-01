terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

variable "region" {
  type    = string
  default = "us-east-2"
}
variable "availability_zone" { type = string }
variable "size_gib" { type = number }
variable "volume_name" { type = string }

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "career-platform", ManagedBy = "terraform" }
  }
}

# Import an existing volume here before removing its original state address.
# This module must have its own backend key, outside the release CI state prefix.
resource "aws_ebs_volume" "preserved" {
  availability_zone = var.availability_zone
  size              = var.size_gib
  type              = "gp3"
  encrypted         = true
  tags              = { Name = var.volume_name }
  lifecycle {
    prevent_destroy = true
  }
}

output "volume_id" { value = aws_ebs_volume.preserved.id }
