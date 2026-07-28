# Alerting and cost guardrails (spec 3.6, 4.1).

resource "aws_sns_topic" "alerts" {
  name         = "${var.project_name}-alerts-${var.environment}"
  display_name = "US stock chart alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

  lifecycle {
    # AWS reports the subscription as "pending confirmation" until the
    # recipient clicks the link, and that arn never becomes the confirmed one.
    # Without this, every plan shows a spurious replacement.
    ignore_changes = [id]
  }
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid    = "AllowBudgetsToPublish"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# Lightsail is a flat monthly charge, so this exists to catch the metered
# parts -- S3 storage and anything added later by accident (spec 4.1).
resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly-${var.environment}"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  notification {
    # Forecast catches a runaway before the money is spent, which an
    # actual-spend alert by definition cannot.
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

# Instance-level alarms. Lightsail publishes its own metrics, and CPU burst
# capacity is the one that actually predicts trouble on a burstable 1 GB plan:
# once the balance is drained the instance is throttled and the collector
# starts dropping behind the feed.
resource "aws_cloudwatch_metric_alarm" "burst_capacity" {
  alarm_name        = "${var.project_name}-burst-capacity-${var.environment}"
  alarm_description = "Lightsail CPU burst capacity is nearly exhausted; the collector will be throttled."
  namespace         = "AWS/Lightsail"
  metric_name       = "BurstCapacityPercentage"
  dimensions = {
    InstanceName = aws_lightsail_instance.app.name
  }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 20
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
