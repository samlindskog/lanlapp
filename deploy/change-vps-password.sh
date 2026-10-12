#!/usr/bin/env bash
# Change your login/sudo password on the VPS interactively (prompts for current, then new password).
#   deploy/change-vps-password.sh [ssh-host]     default: uscentral1
set -euo pipefail
HOST="${1:-uscentral1}"
echo "Changing the password for user 'sam' on $HOST (you'll be asked for the current password first)."
ssh -t "$HOST" passwd
