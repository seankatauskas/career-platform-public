terraform {

  required_version = ">= 1.10, < 2.0"
  required_providers {

    aws = {
      source = "hashicorp/aws", version = "~> 6.0"
    }

  }
  backend "s3" {

  }

}
provider "aws" {

  region = var.region
  default_tags {
    tags = {
      Project = var.name, ManagedBy = "terraform"
    }
  }

}
data "aws_caller_identity" "current" {

}
data "aws_partition" "current" {

}
data "aws_availability_zones" "available" {
  state = "available"
}
data "aws_ami" "ubuntu" {

  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

}
locals {

  account      = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition
  prefix       = "arn:${local.partition}"
  secret_names = toset(["config.json", "portable-master-key", "mcp-token", "inference.json", "runpod-api-key", "resume-model.json", "hermes.env", "hermes.yaml", "tailscale-auth-key"])

}
