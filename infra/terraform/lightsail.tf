# The compute tier: one Lightsail instance running collector + api +
# cloudflared (spec 6.1, 8).
#
# This is the reason the project uses Terraform rather than SAM: CloudFormation
# exposes no Lightsail resources, so an instance created there could only be
# managed by hand.

locals {
  instance_name = "${var.project_name}-${var.environment}"

  # Bootstraps the host only. Application deployment is pull-based and lives in
  # deploy/ -- baking it into user_data would mean rebuilding the instance to
  # ship a code change.
  user_data = templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
    project_name = var.project_name
    github_owner = var.github_owner
    github_repo  = var.github_repo
  })
}

resource "aws_lightsail_instance" "app" {
  name              = local.instance_name
  availability_zone = var.lightsail_availability_zone
  blueprint_id      = var.lightsail_blueprint_id
  bundle_id         = var.lightsail_bundle_id
  key_pair_name     = var.ssh_key_pair_name
  user_data         = local.user_data

  # Whole-disk daily snapshots. These complement, and do not replace, the
  # SQLite-level backup in deploy/backup: a snapshot of a live WAL database is
  # only crash-consistent, whereas VACUUM INTO produces a verified copy
  # (spec 4.4, 10.4).
  add_on {
    type          = "AutoSnapshot"
    snapshot_time = format("%02d:00", var.auto_snapshot_hour_utc)
    status        = "Enabled"
  }

  tags = {
    Name = local.instance_name
    Role = "collector-api"
  }

  lifecycle {
    # Editing the bootstrap script must not silently destroy the instance and
    # take the accumulated SQLite database with it. Changing it is a
    # rebuild-and-restore decision, made deliberately.
    ignore_changes = [user_data]
  }
}

# Ports. Everything except SSH stays shut: the browser reaches the app through
# the Cloudflare tunnel, which is an outbound connection from this instance, so
# 80/443 are never opened inbound (spec 3.5, 4.5, 7.2).
#
# This resource is authoritative -- ports absent here are closed, including the
# 80/443 that Lightsail opens by default on a fresh instance. That default is
# exactly the "unintended exposure" risk the specification lists in section 12.
resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = var.ssh_allowed_cidrs
  }
}

resource "aws_lightsail_static_ip" "app" {
  count = var.enable_static_ip ? 1 : 0
  name  = "${local.instance_name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  count          = var.enable_static_ip ? 1 : 0
  static_ip_name = aws_lightsail_static_ip.app[0].name
  instance_name  = aws_lightsail_instance.app.name
}
