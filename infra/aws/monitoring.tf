resource "aws_cloudwatch_log_group" "operations" {

  name              = "/${var.name}/operations"
  retention_in_days = 14

}
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}
resource "aws_sns_topic_subscription" "email" {

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

}
resource "aws_budgets_budget" "monthly" {

  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = "100"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  # Account-wide spending is intentional: include untaggable transfer/tax costs.
  dynamic "notification" {

    for_each = [80, 100]
    content {

      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]

    }

  }

}
locals {

  alarms = {

    instance = {
      namespace = "AWS/EC2", metric = "StatusCheckFailed", threshold = 0, comparison = "GreaterThanThreshold", statistic = "Maximum", period = 60, evaluations = 3, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    memory = {
      namespace = "CareerPlatform", metric = "mem_used_percent", threshold = 90, comparison = "GreaterThanThreshold", statistic = "Average", period = 60, evaluations = 5, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    credits = {
      namespace = "AWS/EC2", metric = "CPUCreditBalance", threshold = 20, comparison = "LessThanThreshold", statistic = "Minimum", period = 300, evaluations = 3, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    health = {
      namespace = "CareerPlatform", metric = "Healthy", threshold = 1, comparison = "LessThanThreshold", statistic = "Minimum", period = 300, evaluations = 2, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    domain = {
      namespace = "CareerPlatform", metric = "DomainReady", threshold = 1, comparison = "LessThanThreshold", statistic = "Minimum", period = 300, evaluations = 2, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    domain_stale = {
      namespace = "CareerPlatform", metric = "DomainStaleCapabilities", threshold = 0, comparison = "GreaterThanThreshold", statistic = "Maximum", period = 300, evaluations = 2, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    domain_reconciliation = {
      namespace = "CareerPlatform", metric = "DomainPendingReconciliation", threshold = 0, comparison = "GreaterThanThreshold", statistic = "Maximum", period = 300, evaluations = 1, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    backup_attempt = {
      namespace = "CareerPlatform", metric = "BackupAttemptFailed", threshold = 0, comparison = "GreaterThanThreshold", statistic = "Maximum", period = 300, evaluations = 1, dimensions = {
        InstanceId = aws_instance.host.id
      }
    },
    backup = {
      namespace = "CareerPlatform", metric = "BackupAgeSeconds", threshold = 86400, comparison = "GreaterThanThreshold", statistic = "Maximum", period = 300, evaluations = 2, dimensions = {
        InstanceId = aws_instance.host.id
      }
    }

  }

}
resource "aws_cloudwatch_metric_alarm" "operations" {

  for_each            = local.alarms
  alarm_name          = "${var.name}-${each.key}"
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  comparison_operator = each.value.comparison
  threshold           = each.value.threshold
  statistic           = each.value.statistic
  period              = each.value.period
  evaluation_periods  = each.value.evaluations
  dimensions          = each.value.dimensions
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

}
# Agent disk metrics have path/fstype dimensions; query aggregates by path and
# instance so EBS device-name changes do not silently disable alarms.
resource "aws_cloudwatch_metric_alarm" "disk" {

  for_each            = toset(["/", "/var/lib/job-search"])
  alarm_name          = "${var.name}-disk-${each.key == "/" ? "root" : "data"}"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  evaluation_periods  = 3
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  metric_query {

    id          = "disk"
    expression  = "SELECT MAX(disk_used_percent) FROM CareerPlatform WHERE InstanceId = '${aws_instance.host.id}' AND path = '${each.key}'"
    period      = 60
    return_data = true

  }

}
