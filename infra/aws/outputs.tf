output "instance_id" {
  value = aws_instance.host.id
}
output "data_volume_id" {
  value = aws_ebs_volume.data.id
}
output "image_repositories" {
  value = {
    for name, repository in aws_ecr_repository.images : name => repository.repository_url
  }
}
output "backup_bucket" {
  value = aws_s3_bucket.backups.id
}
output "release_bucket" {
  value = aws_s3_bucket.releases.id
}
output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
output "infrastructure_role_arn" {
  value = aws_iam_role.infrastructure.arn
}
output "secret_arns" {
  value = {
    for name, secret in aws_secretsmanager_secret.runtime : name => secret.arn
  }
}
output "deploy_document_name" {
  value = aws_ssm_document.deploy.name
}
output "notification_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
output "ami_id" {
  value = aws_instance.host.ami
}
