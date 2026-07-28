output "instance_name" {
  description = "Lightsail instance name."
  value       = aws_lightsail_instance.app.name
}

output "instance_public_ip" {
  description = "Address for SSH. Not used by browsers -- they arrive via the Cloudflare tunnel."
  value       = var.enable_static_ip ? aws_lightsail_static_ip.app[0].ip_address : aws_lightsail_instance.app.public_ip_address
}

output "instance_username" {
  description = "SSH user for the chosen blueprint."
  value       = aws_lightsail_instance.app.username
}

output "open_ports" {
  description = "Confirms only SSH is reachable; 80/443 stay closed by design (spec 4.5)."
  value       = [for p in aws_lightsail_instance_public_ports.app.port_info : "${p.protocol}/${p.from_port}-${p.to_port} from ${join(",", p.cidrs)}"]
}

output "backup_bucket_name" {
  description = "Backup bucket."
  value       = aws_s3_bucket.backup.id
}

output "backup_s3_uri" {
  description = "Value for USSTOCKS_BACKUP_S3_URI in the instance env file."
  value       = "s3://${aws_s3_bucket.backup.id}"
}

output "backup_uploader_user_name" {
  description = <<-EOT
    Create one access key for this user and place it on the instance. The key
    is deliberately not created by Terraform, which would write the secret into
    state in plaintext:
      aws iam create-access-key --user-name <this> --profile dev01
  EOT
  value       = aws_iam_user.backup_uploader.name
}

output "alert_topic_arn" {
  description = "SNS topic carrying budget and burst-capacity alerts."
  value       = aws_sns_topic.alerts.arn
}

output "github_deploy_role_arn" {
  description = <<-EOT
    Set this as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub so the
    workflow can assume it via OIDC.
  EOT
  value       = aws_iam_role.github_deploy.arn
}
