# Terraform, not SAM/CloudFormation.
#
# CloudFormation has no Lightsail resource types at all, so SAM cannot manage
# the instance this system runs on. Terraform's AWS provider does
# (aws_lightsail_*), and it also covers the S3, IAM, SNS and Budgets pieces, so
# the whole footprint stays in one tool and one state file.

terraform {
  # 1.10 is the floor because backend.hcl uses use_lockfile (S3-native state
  # locking). On an older Terraform that setting is silently unknown and the
  # failure surfaces as a confusing backend error instead of a version one.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Remote state. Local state would live only in the CI runner, so two pushes
  # could race and a lost state file would orphan every resource below.
  # Bootstrap this bucket once before the first apply -- see
  # docs/aws-deployment.md.
  backend "s3" {
    key     = "usstocks/terraform.tfstate"
    encrypt = true
    # bucket, region, dynamodb_table/use_lockfile come from backend.hcl so the
    # same code can target another account without edits:
    #   terraform init -backend-config=backend.hcl
  }
}

provider "aws" {
  region = var.aws_region
  # Local runs use the named profile. CI leaves this null and authenticates
  # with the OIDC role instead, since a profile has no meaning on a runner.
  profile = var.aws_profile

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
