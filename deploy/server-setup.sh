#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04/24.04 VPS. Run from your Mac:
#   ssh root@SERVER_IP 'bash -s' < deploy/server-setup.sh
set -euo pipefail

echo "==> packages"
apt-get update -y
apt-get install -y curl rsync ufw

if ! command -v docker >/dev/null 2>&1; then
  echo "==> docker (official install script)"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> firewall: ssh, http, https"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

mkdir -p /opt/copilot
echo "==> done. Next (on your Mac): ./deploy/push.sh root@SERVER_IP"
