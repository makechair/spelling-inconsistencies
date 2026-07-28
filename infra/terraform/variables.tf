variable "aws_region" {
  description = "Region for every resource. Lightsail availability zones are region-scoped."
  type        = string
  default     = "ap-northeast-1"
}

variable "aws_profile" {
  description = <<-EOT
    Local AWS CLI profile. Defaults to dev01.
    CI passes null: a GitHub Actions runner authenticates through the OIDC
    role and has no profile to read.
  EOT
  type        = string
  default     = "dev01"
}

variable "project_name" {
  description = "Prefix for resource names. Lowercase; used in the S3 bucket name."
  type        = string
  default     = "usstocks"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric with hyphens."
  }
}

variable "environment" {
  description = "Environment name, matching the AWS profile in use."
  type        = string
  default     = "dev01"
}

variable "alert_email" {
  description = <<-EOT
    Address for backup, snapshot and budget alerts. AWS sends a confirmation
    mail; the subscription stays inactive until it is accepted.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a valid address."
  }
}

# ------------------------------------------------------------------ Lightsail
variable "lightsail_bundle_id" {
  description = <<-EOT
    Instance plan. small_3_0 is the 1 GB / 2 vCPU / 40 GB tier the
    specification selected (section 6.1).
    Move to medium_3_0 (2 GB) if the collector starts hitting its memory cap;
    the specification lists 1 GB OOM as a known risk (section 12).
  EOT
  type        = string
  default     = "small_3_0"
}

variable "lightsail_blueprint_id" {
  description = "OS image. Ubuntu LTS, matching docs/deployment.md."
  type        = string
  default     = "ubuntu_24_04"
}

variable "lightsail_availability_zone" {
  description = "Must be a zone inside aws_region, e.g. ap-northeast-1a."
  type        = string
  default     = "ap-northeast-1a"
}

variable "ssh_allowed_cidrs" {
  description = <<-EOT
    Sources allowed to reach SSH. Everything else stays closed: the app is
    reached only through the Cloudflare tunnel, which dials outbound, so ports
    80 and 443 are never opened (spec 3.5, 4.5).

    There is no default, so the value has to be a conscious decision. Set it
    to your own address ("203.0.113.10/32"); "0.0.0.0/0" opens SSH to the
    internet and contradicts the closed-ingress design.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0
    error_message = "ssh_allowed_cidrs must list at least one CIDR, e.g. [\"203.0.113.10/32\"]."
  }

  validation {
    condition     = alltrue([for c in var.ssh_allowed_cidrs : can(cidrnetmask(c))])
    error_message = "Every entry must be a valid IPv4 CIDR, e.g. \"203.0.113.10/32\"."
  }
}

variable "ssh_key_pair_name" {
  description = <<-EOT
    Existing Lightsail key pair to install. Leave null to have Lightsail use
    its default key pair for the region.
  EOT
  type        = string
  default     = null
}

variable "enable_static_ip" {
  description = <<-EOT
    Attach a static IP. It is free while attached to a running instance and
    billed when unattached, so detaching without deleting it costs money.
    Only needed for stable SSH access; the tunnel does not require it.
  EOT
  type        = bool
  default     = true
}

variable "auto_snapshot_hour_utc" {
  description = <<-EOT
    Hour (UTC) for Lightsail automatic daily snapshots, which are a
    whole-disk complement to the SQLite-level backup in deploy/backup
    (spec 4.4). 06:00 UTC = 02:00 ET, after after-hours trading ends.
  EOT
  type        = number
  default     = 6

  validation {
    condition     = var.auto_snapshot_hour_utc >= 0 && var.auto_snapshot_hour_utc <= 23
    error_message = "auto_snapshot_hour_utc must be 0-23."
  }
}

# -------------------------------------------------------------------- backup
variable "backup_retention_days" {
  description = <<-EOT
    Days to keep daily SQLite backups (spec 10.4). The floor is 30 on purpose:
    uploads use STANDARD_IA, which bills a 30-day minimum duration, so deleting
    sooner costs more than keeping the object.
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.backup_retention_days >= 30
    error_message = "backup_retention_days must be at least 30 (STANDARD_IA minimum duration)."
  }
}

# ---------------------------------------------------------------------- cost
variable "monthly_budget_usd" {
  description = "Monthly cost budget. The specification targets about 10 USD (4.1)."
  type        = number
  default     = 12
}

# -------------------------------------------------------------------- GitHub
variable "github_owner" {
  description = "GitHub account allowed to assume the deploy role."
  type        = string
  default     = "makechair"
}

variable "github_repo" {
  description = "Repository allowed to assume the deploy role."
  type        = string
  default     = "us-stock-realtime-chart"
}

variable "github_deploy_branch" {
  description = <<-EOT
    Only pushes to this branch may assume the deploy role. The trust policy
    pins the ref; without it any repository on GitHub could assume the role.
  EOT
  type        = string
  default     = "main"
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    An account holds at most one OIDC provider per URL. Set false if
    token.actions.githubusercontent.com is already registered, otherwise the
    apply fails with EntityAlreadyExists.
  EOT
  type        = bool
  default     = true
}

variable "tf_state_bucket" {
  description = <<-EOT
    Bucket holding the Terraform state, created once by the bootstrap script
    (scripts/bootstrap-tf-state.sh). The deploy role is granted access to only
    this project's key prefix inside it, not the whole bucket.
  EOT
  type        = string
}
