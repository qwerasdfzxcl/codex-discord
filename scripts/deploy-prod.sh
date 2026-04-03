#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MAIN_CHECKOUT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd -- "$MAIN_CHECKOUT/.." && pwd)"
STAGING_CHECKOUT="${ROOT_DIR}/staging"
PROD_SERVICE="${PROD_SERVICE:-codex-discord-main}"
REMOTE_NAME="${REMOTE_NAME:-origin}"
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

git -C "$MAIN_CHECKOUT" remote get-url "$REMOTE_NAME" >/dev/null 2>&1 || die "Missing git remote: $REMOTE_NAME"
git -C "$MAIN_CHECKOUT" fetch "$REMOTE_NAME" --prune

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

if [[ "$(git -C "$MAIN_CHECKOUT" rev-list --count "${MAIN_BRANCH}..${REMOTE_NAME}/${MAIN_BRANCH}")" != "0" ]]; then
  die "Main checkout is behind ${REMOTE_NAME}/${MAIN_BRANCH}"
fi

if [[ "$(git -C "$STAGING_CHECKOUT" rev-list --count "${STAGING_BRANCH}..${REMOTE_NAME}/${STAGING_BRANCH}")" != "0" ]]; then
  die "Staging checkout is behind ${REMOTE_NAME}/${STAGING_BRANCH}"
fi

STAGING_PUSHED="no"
MAIN_PUSHED="no"

if git -C "$MAIN_CHECKOUT" merge-base --is-ancestor "$STAGING_BRANCH" "$MAIN_BRANCH"; then
  if ! git -C "$STAGING_CHECKOUT" diff --quiet "${REMOTE_NAME}/${STAGING_BRANCH}" "$STAGING_BRANCH"; then
    git -C "$STAGING_CHECKOUT" push "$REMOTE_NAME" "$STAGING_BRANCH"
    STAGING_PUSHED="yes"
  fi

  if ! git -C "$MAIN_CHECKOUT" diff --quiet "${REMOTE_NAME}/${MAIN_BRANCH}" "$MAIN_BRANCH"; then
    git -C "$MAIN_CHECKOUT" push "$REMOTE_NAME" "$MAIN_BRANCH"
    MAIN_PUSHED="yes"
  fi

  echo "No new staging commits to deploy."
  echo "staging pushed: ${STAGING_PUSHED}"
  echo "main pushed: ${MAIN_PUSHED}"
  exit 0
fi

PREVIOUS_MAIN="$(git -C "$MAIN_CHECKOUT" rev-parse --short HEAD)"
STAGING_HEAD="$(git -C "$STAGING_CHECKOUT" rev-parse --short HEAD)"

if ! git -C "$STAGING_CHECKOUT" diff --quiet "${REMOTE_NAME}/${STAGING_BRANCH}" "$STAGING_BRANCH"; then
  git -C "$STAGING_CHECKOUT" push "$REMOTE_NAME" "$STAGING_BRANCH"
  STAGING_PUSHED="yes"
fi

git -C "$MAIN_CHECKOUT" merge --no-ff --no-edit "$STAGING_BRANCH"

if ! git -C "$MAIN_CHECKOUT" diff --quiet "${REMOTE_NAME}/${MAIN_BRANCH}" "$MAIN_BRANCH"; then
  git -C "$MAIN_CHECKOUT" push "$REMOTE_NAME" "$MAIN_BRANCH"
  MAIN_PUSHED="yes"
fi

"$RESTART_HELPER" "$PROD_SERVICE"

CURRENT_MAIN="$(git -C "$MAIN_CHECKOUT" rev-parse --short HEAD)"

echo "Deploy complete"
echo "main checkout: $MAIN_CHECKOUT"
echo "staging checkout: $STAGING_CHECKOUT"
echo "merged ${STAGING_BRANCH}@${STAGING_HEAD} into ${MAIN_BRANCH}"
echo "main moved: ${PREVIOUS_MAIN} -> ${CURRENT_MAIN}"
echo "staging pushed: ${STAGING_PUSHED}"
echo "main pushed: ${MAIN_PUSHED}"
echo "restarted service: ${PROD_SERVICE}"
