# Off-instance backup storage (spec 4.4, 10.4).

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "backup" {
  bucket = "${var.project_name}-backup-${var.environment}-${data.aws_caller_identity.current.account_id}"

  # This holds the only off-instance copy of the accumulated bar history, which
  # is the whole point of the system (spec 2.1). Destroying the stack must not
  # take it with them.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  bucket                  = aws_s3_bucket.backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    apply_server_side_encryption_by_default {
      # SSE-S3 rather than KMS: a customer-managed key costs about 1 USD/month
      # plus per-request charges, against a ~10 USD/month target (spec 4.1).
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id
  versioning_configuration {
    # Suspended by design: every backup is written under a unique timestamped
    # key, so versioning would duplicate storage cost without adding a recovery
    # path.
    status = "Suspended"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    id     = "expire-daily-backups"
    status = "Enabled"

    filter {
      prefix = "daily/"
    }

    expiration {
      days = var.backup_retention_days
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.backup]
}

resource "aws_s3_bucket_policy" "backup" {
  bucket = aws_s3_bucket.backup.id
  policy = data.aws_iam_policy_document.backup_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.backup]
}

data "aws_iam_policy_document" "backup_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.backup.arn,
      "${aws_s3_bucket.backup.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# ---------------------------------------------- credentials used on the host
# Lightsail instances are not EC2 instances and cannot assume an instance role,
# so the backup script needs a long-lived key. The permissions are therefore
# cut to the bone: write-only, one prefix, one bucket. A leaked key cannot read
# back or delete the history.
resource "aws_iam_user" "backup_uploader" {
  name = "${var.project_name}-backup-uploader-${var.environment}"
}

data "aws_iam_policy_document" "backup_uploader" {
  statement {
    sid       = "PutBackupObjects"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backup.arn}/daily/*"]
  }

  statement {
    sid       = "ListOwnPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.backup.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["daily/*"]
    }
  }
}

resource "aws_iam_user_policy" "backup_uploader" {
  name   = "write-daily-backups-only"
  user   = aws_iam_user.backup_uploader.name
  policy = data.aws_iam_policy_document.backup_uploader.json
}

# No aws_iam_access_key resource on purpose: Terraform would write the secret
# into state in plaintext, and the state bucket is a different blast radius
# from the instance. Create and rotate the key out of band:
#   aws iam create-access-key --user-name <output> --profile dev01
# See docs/aws-deployment.md.
