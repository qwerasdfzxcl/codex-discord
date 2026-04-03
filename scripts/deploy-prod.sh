#!/usr/bin/env bash
set -euo pipefail

DEV_CHECKOUT="/srv/codex-discord/dev"
PROD_CHECKOUT="/srv/codex-discord/prod"
PROD_SERVICE="codex-discord-main"

if [[ ! -d "$DEV_CHECKOUT" ]]; then
  echo "Missing dev checkout: $DEV_CHECKOUT" >&2
  exit 1
fi

if [[ ! -d "$PROD_CHECKOUT" ]]; then
  echo "Missing prod checkout: $PROD_CHECKOUT" >&2
  exit 1
fi

/usr/bin/rsync -a --delete \
  --exclude ".env" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  --exclude "config/config.json" \
  "$DEV_CHECKOUT/" "$PROD_CHECKOUT/"

/usr/bin/systemctl restart "$PROD_SERVICE"

echo "Deploy complete: $DEV_CHECKOUT -> $PROD_CHECKOUT"
