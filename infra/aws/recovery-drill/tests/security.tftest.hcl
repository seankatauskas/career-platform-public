mock_provider "aws" {
  mock_data "aws_instance" {
    defaults = {
      ami                    = "ami-0123456789abcdef0", instance_type = "t3.large",
      availability_zone      = "us-east-2a", subnet_id = "subnet-0123456789abcdef0",
      vpc_security_group_ids = ["sg-0123456789abcdef0"], iam_instance_profile = "career-platform-host"
    }
  }
}
variables {
  source_instance_id = "i-0123456789abcdef0"
  backup_bucket      = "career-platform-test-backups"
  release_bucket     = "career-platform-test-releases"
  secret_arns        = {}
}
run "isolated_replacement" {
  command = plan
  assert {
    condition     = aws_instance.drill.ami == data.aws_instance.source.ami && aws_instance.drill.subnet_id == data.aws_instance.source.subnet_id && aws_instance.drill.iam_instance_profile == data.aws_instance.source.iam_instance_profile
    error_message = "Reuse the reviewed host configuration without changing production infrastructure."
  }
  assert {
    condition     = aws_instance.drill.metadata_options[0].http_tokens == "required" && aws_instance.drill.metadata_options[0].http_put_response_hop_limit == 1 && aws_instance.drill.credit_specification[0].cpu_credits == "standard"
    error_message = "Retain metadata isolation and bounded burst-credit billing."
  }
  assert {
    condition     = aws_ebs_volume.drill.encrypted && aws_ebs_volume.drill.size == 100 && aws_instance.drill.root_block_device[0].encrypted
    error_message = "Recovery uses a separate encrypted data volume and encrypted root disk."
  }
  assert {
    condition     = !var.initialize_data_volume
    error_message = "Formatting still needs explicit first-use authorization."
  }
}
