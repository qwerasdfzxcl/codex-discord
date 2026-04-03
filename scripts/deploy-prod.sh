#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MAIN_CHECKOUT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd -- "$MAIN_CHECKOUT/.." && pwd)"
STAGING_CHECKOUT="${ROOT_DIR}/staging"
PROD_SERVICE="${PROD_SERVICE:-codex-discord-main}"
MAIN_BRANCH="${MAIN_BRANCH:-main}"
STAGING_BRANCH="${STAGING_BRANCH:-staging}"
RESTART_HELPER="${RESTART_HELPER:-${SCRIPT_DIR}/systemd-restart-service.sh}"

die() {
  echo "$1" >&2
  exit 1
}

if [[ ! -d "$STAGING_CHECKOUT" ]]; then
  die "Missing staging checkout: $STAGING_CHECKOUT"
fi

if [[ ! -d "$MAIN_CHECKOUT" ]]; then
  die "Missing main checkout: $MAIN_CHECKOUT"
fi

if [[ ! -x "$RESTART_HELPER" ]]; then
  die "Missing restart helper: $RESTART_HELPER"
fi

git -C "$MAIN_CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Main checkout is not a git worktree"
git -C "$STAGING_CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Staging checkout is not a git worktree"

MAIN_CHECKOUT_BRANCH="$(git -C "$MAIN_CHECKOUT" branch --show-current)"
STAGING_CHECKOUT_BRANCH="$(git -C "$STAGING_CHECKOUT" branch --show-current)"

if [[ "$MAIN_CHECKOUT_BRANCH" != "$MAIN_BRANCH" ]]; then
  die "Main checkout must be on ${MAIN_BRANCH}, got ${MAIN_CHECKOUT_BRANCH}"
fi

if [[ "$STAGING_CHECKOUT_BRANCH" != "$STAGING_BRANCH" ]]; then
  die "Staging checkout must be on ${STAGING_BRANCH}, got ${STAGING_CHECKOUT_BRANCH}"
fi

if [[ -n "$(git -C "$MAIN_CHECKOUT" status --porcelain)" ]]; then
  die "Main checkout has uncommitted changes"
fi

if [[ -n "$(git -C "$STAGING_CHECKOUT" status --porcelain)" ]]; then
  die "Staging checkout has uncommitted changes"
fi

if git -C "$MAIN_CHECKOUT" merge-base --is-ancestor "$STAGING_BRANCH" "$MAIN_BRANCH"; then
  echo "No new staging commits to deploy."
  exit 0
fi

PREVIOUS_MAIN="$(git -C "$MAIN_CHECKOUT" rev-parse --short HEAD)"
STAGING_HEAD="$(git -C "$STAGING_CHECKOUT" rev-parse --short HEAD)"

git -C "$MAIN_CHECKOUT" merge --no-ff --no-edit "$STAGING_BRANCH"

"$RESTART_HELPER" "$PROD_SERVICE"

CURRENT_MAIN="$(git -C "$MAIN_CHECKOUT" rev-parse --short HEAD)"

echo "Deploy complete"
echo "main checkout: $MAIN_CHECKOUT"
echo "staging checkout: $STAGING_CHECKOUT"
echo "merged ${STAGING_BRANCH}@${STAGING_HEAD} into ${MAIN_BRANCH}"
echo "main moved: ${PREVIOUS_MAIN} -> ${CURRENT_MAIN}"
echo "restarted service: ${PROD_SERVICE}"
