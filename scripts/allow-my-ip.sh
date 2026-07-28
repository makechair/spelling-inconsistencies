#!/usr/bin/env bash
#
# Point ssh_allowed_cidrs at whatever address this machine currently has.
#
# Only needed if you want SSH from a terminal on a connection whose address
# changes -- a normal home line. The alternative, and the default, is
# allow_lightsail_browser_ssh: the Lightsail console reaches the instance
# without any address of yours being involved, so nothing has to be updated
# when the ISP reassigns.
#
# Usage:
#   scripts/allow-my-ip.sh            # show what would change
#   scripts/allow-my-ip.sh --apply    # apply it
set -Eeuo pipefail

cd "$(dirname "$0")/../infra/terraform"

APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true

# checkip.amazonaws.com is AWS's own responder over TLS. Using it rather than
# a random third-party service keeps the trust boundary where it already is.
IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"

if [[ ! "${IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "could not determine a global IPv4 address (got: '${IP}')" >&2
  echo "on an IPv6-only connection, use the Lightsail console's browser SSH instead" >&2
  exit 1
fi

echo "current global address: ${IP}"
echo

if [[ "${APPLY}" == "true" ]]; then
  terraform apply -var="ssh_allowed_cidrs=[\"${IP}/32\"]"
  echo
  echo "SSH now admits ${IP}/32 only. Re-run this after the address changes."
else
  terraform plan -var="ssh_allowed_cidrs=[\"${IP}/32\"]"
  echo
  echo "Nothing was changed. Re-run with --apply to commit."
fi
