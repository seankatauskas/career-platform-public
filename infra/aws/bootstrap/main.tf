terraform {

  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source = "hashicorp/aws", version = "~> 6.0"
    }
  }

}
provider "aws" {
  region = var.region
}
variable "region" {
  type    = string
  default = "us-east-2"
}
variable "bucket_name" {
  type        = string
  description = "Globally unique state bucket name."
}
resource "aws_s3_bucket" "state" {

  bucket = var.bucket_name
  tags = {
    Project = "career-platform", ManagedBy = "terraform-bootstrap"
  }
  lifecycle {
    prevent_destroy = true
  }

}
resource "aws_s3_bucket_public_access_block" "state" {

  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {

  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }

}
resource "aws_s3_bucket_versioning" "state" {

  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }

}
resource "aws_s3_bucket_policy" "state" {

  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"], Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }
      }
    }]
  })

}
output "bucket" {
  value = aws_s3_bucket.state.id
}
