#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${1:-}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"
SUDO_BIN="${SUDO_BIN:-/usr/bin/sudo}"

die() {
  echo "$1" >&2
  exit 1
}

if [[ -z "$SERVICE_NAME" ]]; then
  die "Usage: $0 <service-name>"
fi

if [[ ! -x "$SYSTEMCTL_BIN" ]]; then
  die "Missing systemctl binary: $SYSTEMCTL_BIN"
fi

if "$SYSTEMCTL_BIN" --user show "$SERVICE_NAME" >/dev/null 2>&1; then
  "$SYSTEMCTL_BIN" --user restart "$SERVICE_NAME"
  echo "Restarted user service: $SERVICE_NAME"
  exit 0
fi

if "$SYSTEMCTL_BIN" show "$SERVICE_NAME" >/dev/null 2>&1; then
  if [[ ! -x "$SUDO_BIN" ]]; then
    die "Service '$SERVICE_NAME' is a system service. Install sudo or switch the unit to a user service."
  fi

  if "$SUDO_BIN" -n "$SYSTEMCTL_BIN" restart "$SERVICE_NAME"; then
    echo "Restarted system service with sudo: $SERVICE_NAME"
    exit 0
  fi

  die "Service '$SERVICE_NAME' is a system service. Configure passwordless sudo for: $SYSTEMCTL_BIN restart $SERVICE_NAME"
fi

die "Service '$SERVICE_NAME' was not found as a user service or a system service."
