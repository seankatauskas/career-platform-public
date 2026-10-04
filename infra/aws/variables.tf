variable "region" {
  type    = string
  default = "us-east-2"
}
variable "cost_monitor_enabled" {
  type        = bool
  default     = false
  description = "Opt in to daily host-only billing snapshots and ce:GetCostAndUsage read access. Cost Explorer API requests are chargeable. Existing hosts also require the documented monitor setup."
}
variable "name" {
  type    = string
  default = "career-platform"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name))
    error_message = "Use a lowercase resource prefix of 3–31 characters."
  }
}
variable "github_repository" {
  type        = string
  description = "GitHub owner/repository permitted to assume deployment roles."
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use owner/repository."
  }
}
variable "github_environment" {
  type    = string
  default = "production"
}
variable "github_oidc_ids" {
  type = object({
    owner      = number
    repository = number
  })
  description = "Verified GitHub owner and repository IDs for immutable OIDC subjects; null retains the legacy name-only subject."
  default     = null
  validation {
    condition = var.github_oidc_ids == null ? true : alltrue([
      for id in [var.github_oidc_ids.owner, var.github_oidc_ids.repository] : id > 0 && id == floor(id)
    ])
    error_message = "GitHub OIDC IDs must be positive integers obtained from GitHub."
  }
}
variable "github_oidc_provider_arn" {
  type        = string
  description = "Existing GitHub OIDC provider ARN; null creates the account-wide provider."
  default     = null
}
variable "alert_email" {
  type        = string
  description = "Owner email for SNS confirmation and budget alerts."
  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.alert_email))
    error_message = "Provide the owner's real email address."
  }
}
variable "state_bucket" {
  type        = string
  description = "Existing state bucket created by bootstrap."
}
variable "instance_type" {
  type    = string
  default = "t3.large"
}
variable "ami_id" {
  type        = string
  default     = null
  description = "Pin the reviewed Ubuntu AMI after first plan; null discovers latest Canonical Ubuntu 24.04 amd64."
}
variable "data_volume_size" {
  type    = number
  default = 100
}
variable "initialize_data_volume" {
  type        = bool
  default     = false
  description = "Explicit one-time authorization to format this managed volume if genuinely blank. Set false after first bootstrap."
}
variable "cloudwatch_agent_version" {
  type        = string
  default     = "1.300072.0b1766"
  description = "Pinned upstream agent package version. Override only after reviewing its SHA256."
}
variable "cloudwatch_agent_sha256" {
  default     = "05baeadca96c4bb8e43906ed09cf0bebd0f321ff6d41987bdc46ce681de0978d"
  type        = string
  description = "SHA256 of the pinned ubuntu amd64 CloudWatch .deb; obtained independently from the reviewed distribution."
  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.cloudwatch_agent_sha256))
    error_message = "Provide the reviewed 64-character lowercase package SHA256."
  }
}
