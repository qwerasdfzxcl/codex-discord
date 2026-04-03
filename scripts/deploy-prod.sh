#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROD_CHECKOUT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd -- "$PROD_CHECKOUT/.." && pwd)"
DEV_CHECKOUT="${ROOT_DIR}/dev"
PROD_SERVICE="${PROD_SERVICE:-codex-discord-main}"
MAIN_BRANCH="${MAIN_BRANCH:-main}"
STAGING_BRANCH="${STAGING_BRANCH:-staging}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"

die() {
  echo "$1" >&2
  exit 1
}

if [[ ! -d "$DEV_CHECKOUT" ]]; then
  die "Missing dev checkout: $DEV_CHECKOUT"
fi

if [[ ! -d "$PROD_CHECKOUT" ]]; then
  die "Missing prod checkout: $PROD_CHECKOUT"
fi

if [[ ! -x "$SYSTEMCTL_BIN" ]]; then
  die "Missing systemctl binary: $SYSTEMCTL_BIN"
fi

git -C "$PROD_CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Prod checkout is not a git worktree"
git -C "$DEV_CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Dev checkout is not a git worktree"

PROD_BRANCH="$(git -C "$PROD_CHECKOUT" branch --show-current)"
DEV_BRANCH="$(git -C "$DEV_CHECKOUT" branch --show-current)"

if [[ "$PROD_BRANCH" != "$MAIN_BRANCH" ]]; then
  die "Prod checkout must be on ${MAIN_BRANCH}, got ${PROD_BRANCH}"
fi

if [[ "$DEV_BRANCH" != "$STAGING_BRANCH" ]]; then
  die "Dev checkout must be on ${STAGING_BRANCH}, got ${DEV_BRANCH}"
fi

if [[ -n "$(git -C "$PROD_CHECKOUT" status --porcelain)" ]]; then
  die "Prod checkout has uncommitted changes"
fi

if [[ -n "$(git -C "$DEV_CHECKOUT" status --porcelain)" ]]; then
  die "Dev checkout has uncommitted changes"
fi

if git -C "$PROD_CHECKOUT" merge-base --is-ancestor "$STAGING_BRANCH" "$MAIN_BRANCH"; then
  echo "No new staging commits to deploy."
  exit 0
fi

PREVIOUS_MAIN="$(git -C "$PROD_CHECKOUT" rev-parse --short HEAD)"
STAGING_HEAD="$(git -C "$DEV_CHECKOUT" rev-parse --short HEAD)"

git -C "$PROD_CHECKOUT" merge --no-ff --no-edit "$STAGING_BRANCH"

"$SYSTEMCTL_BIN" restart "$PROD_SERVICE"

CURRENT_MAIN="$(git -C "$PROD_CHECKOUT" rev-parse --short HEAD)"

echo "Deploy complete"
echo "prod checkout: $PROD_CHECKOUT"
echo "dev checkout: $DEV_CHECKOUT"
echo "merged ${STAGING_BRANCH}@${STAGING_HEAD} into ${MAIN_BRANCH}"
echo "main moved: ${PREVIOUS_MAIN} -> ${CURRENT_MAIN}"
echo "restarted service: ${PROD_SERVICE}"
