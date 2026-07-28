# The identity GitHub Actions assumes to run terraform apply.
#
# OIDC rather than a stored access key: nothing long-lived is kept in GitHub,
# and the trust policy pins the exact repository and branch, so a fork or a
# feature branch cannot deploy.

data "aws_partition" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # IAM no longer verifies this thumbprint for GitHub's provider, but the API
  # still requires the field.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      # Without this the role would be assumable from any repository on GitHub.
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/${var.github_deploy_branch}"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name                 = "${var.project_name}-github-deploy-${var.environment}"
  description          = "Assumed by GitHub Actions via OIDC to apply the Terraform stack."
  assume_role_policy   = data.aws_iam_policy_document.github_assume_role.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_deploy" {
  # Terraform state. The workflow must read and write the state object and take
  # the lock, and nothing else in that bucket.
  statement {
    sid    = "TerraformState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${var.tf_state_bucket}/usstocks/*"]
  }

  statement {
    sid       = "TerraformStateList"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${var.tf_state_bucket}"]
  }

  # Managing the resources in this configuration. Lightsail has no
  # resource-level ARNs to scope against, so it is action-scoped instead.
  statement {
    sid    = "ManageLightsail"
    effect = "Allow"
    actions = [
      "lightsail:GetInstance*",
      "lightsail:GetStaticIp*",
      "lightsail:GetRegions",
      "lightsail:GetBlueprints",
      "lightsail:GetBundles",
      "lightsail:GetOperation*",
      "lightsail:CreateInstances",
      "lightsail:DeleteInstance",
      "lightsail:PutInstancePublicPorts",
      "lightsail:OpenInstancePublicPorts",
      "lightsail:CloseInstancePublicPorts",
      "lightsail:AllocateStaticIp",
      "lightsail:ReleaseStaticIp",
      "lightsail:AttachStaticIp",
      "lightsail:DetachStaticIp",
      "lightsail:EnableAddOn",
      "lightsail:DisableAddOn",
      "lightsail:TagResource",
      "lightsail:UntagResource",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ManageBackupBucket"
    effect = "Allow"
    actions = [
      "s3:CreateBucket",
      "s3:GetBucket*",
      "s3:PutBucket*",
      "s3:DeleteBucketPolicy",
      "s3:GetLifecycleConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:GetEncryptionConfiguration",
      "s3:PutEncryptionConfiguration",
    ]
    resources = [
      aws_s3_bucket.backup.arn,
      "${aws_s3_bucket.backup.arn}/*",
    ]
  }

  statement {
    sid    = "ManageAlerting"
    effect = "Allow"
    actions = [
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:GetTopicAttributes",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
      "sns:Unsubscribe",
      "sns:GetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:ListTagsForResource",
      "sns:TagResource",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:ListTagsForResource",
      "cloudwatch:TagResource",
      "budgets:ViewBudget",
      "budgets:ModifyBudget",
      "budgets:DescribeBudget*",
      "budgets:CreateBudget*",
      "budgets:UpdateBudget*",
      "budgets:DeleteBudget*",
    ]
    resources = ["*"]
  }

  # Scoped to this project's own names so a compromised workflow cannot mint
  # arbitrary privileged identities.
  statement {
    sid    = "ManageProjectIdentities"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:TagRole",
      "iam:GetUser",
      "iam:GetUserPolicy",
      "iam:ListUserPolicies",
      "iam:ListAttachedUserPolicies",
      "iam:ListAccessKeys",
      "iam:CreateUser",
      "iam:DeleteUser",
      "iam:PutUserPolicy",
      "iam:DeleteUserPolicy",
      "iam:TagUser",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.project_name}-*",
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:user/${var.project_name}-*",
    ]
  }

  statement {
    sid       = "ReadOidcProvider"
    effect    = "Allow"
    actions   = ["iam:GetOpenIDConnectProvider", "iam:TagOpenIDConnectProvider"]
    resources = [local.github_oidc_provider_arn]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "terraform-apply"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
