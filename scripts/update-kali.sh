#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Kyan-vd-anker/SAGE.git"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="sage.service"
BACKUP_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sage/backups"

fail() { echo "SAGE update stopped: $*" >&2; exit 1; }
for tool in git python3 tar systemctl; do command -v "$tool" >/dev/null 2>&1 || fail "$tool is required."; done
[[ -d "$ROOT_DIR/.git" ]] || fail "This is not a Git checkout. Follow the first-update steps in UPDATE.md once, then run this script again."
origin="$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)"
case "$origin" in
  "$REPO_URL"|git@github.com:Kyan-vd-anker/SAGE.git|ssh://git@github.com/Kyan-vd-anker/SAGE.git) ;;
  *) fail "The origin remote does not match the SAGE GitHub repository. No files were changed." ;;
esac
branch="$(git -C "$ROOT_DIR" branch --show-current)"
[[ -n "$branch" ]] || fail "Detached Git checkout. Check out the release branch first."
[[ "$(systemctl --user show "$SERVICE" --property=LoadState --value 2>/dev/null || true)" == loaded ]] || fail "Install Sage's user service first with bash scripts/install-kali.sh."
if ! git -C "$ROOT_DIR" diff --quiet || ! git -C "$ROOT_DIR" diff --cached --quiet; then
  fail "Tracked files have local edits. Commit or safely copy them elsewhere before updating."
fi

was_active=0
if systemctl --user is-active --quiet "$SERVICE"; then was_active=1; fi
old_head="$(git -C "$ROOT_DIR" rev-parse HEAD)"
updated=0
restarted=0
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
backup="$BACKUP_DIR/sage-data-$stamp.tar.gz"

restore_service() {
  if [[ "$was_active" -eq 1 && "$restarted" -eq 0 ]]; then
    systemctl --user restart "$SERVICE" || true
  fi
}
rollback_code() {
  if [[ "$updated" -eq 1 ]]; then
    git -C "$ROOT_DIR" reset --hard "$old_head" || true
    updated=0
  fi
}

if [[ -d "$ROOT_DIR/data" ]]; then
  tar -czf "$backup" -C "$ROOT_DIR" data || fail "Could not create the data backup; update was not started."
  chmod 600 "$backup"
  echo "Protected local data: $backup"
else
  echo "No in-project data folder exists; Sage will create it when it starts."
fi
if [[ "$was_active" -eq 1 ]]; then systemctl --user stop "$SERVICE" || fail "Could not stop Sage cleanly."; fi
if ! git -C "$ROOT_DIR" fetch --prune origin; then
  restore_service
  fail "Could not fetch the update. Your code and data were left in place."
fi
if ! git -C "$ROOT_DIR" merge-base --is-ancestor "$old_head" "origin/$branch"; then
  restore_service
  fail "The remote branch diverged or is behind this checkout. Nothing was reset; resolve Git history manually."
fi
if ! git -C "$ROOT_DIR" merge --ff-only "origin/$branch"; then
  restore_service
  fail "Fast-forward update failed. Your data backup is at $backup."
fi
updated=1
if ! python3 -m compileall -q "$ROOT_DIR/sage"; then
  rollback_code
  restore_service
  fail "Python syntax validation failed. The previous code was restored; data backup: $backup"
fi
if [[ "$was_active" -eq 1 ]]; then
  if ! systemctl --user start "$SERVICE"; then
    rollback_code
    systemctl --user start "$SERVICE" || true
    restarted=1
    fail "The new service did not start. Previous code was restored; data backup: $backup"
  fi
  restarted=1
fi
echo "SAGE update complete: $(cat "$ROOT_DIR/VERSION" 2>/dev/null || echo unknown)"
if [[ -f "$backup" ]]; then echo "Data backup kept at: $backup"; fi
if [[ "$was_active" -eq 0 ]]; then echo "Sage was stopped before the update and remains stopped. Start it with: systemctl --user start sage"; fi
