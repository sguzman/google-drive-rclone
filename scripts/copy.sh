#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'google-drive-rclone: %s\n' "$*" >&2
  exit 1
}

# Fail closed if the large disk is absent. A plain /drive directory on the
# root filesystem must NEVER become the download destination.
[[ "$(findmnt -n --mountpoint /drive -o TARGET 2>/dev/null || true)" == "/drive" ]] ||
  die "/drive is not a mounted filesystem; refusing to write"

[[ "$(findmnt -n --mountpoint /drive -o FSTYPE 2>/dev/null || true)" == "ext4" ]] ||
  die "/drive is not the expected ext4 filesystem; refusing to write"

[[ ! -L /drive/gdrive ]] || die "/drive/gdrive is a symlink; refusing to follow it"
[[ -d /drive/gdrive && -w /drive/gdrive ]] ||
  die "/drive/gdrive is missing or not writable; run scripts/install.sh"

command -v rclone >/dev/null || die "rclone is not installed"

# Immutable archive: no uploads, no deletions, no overwriting local files.
exec rclone copy 'gdrive:' '/drive/gdrive' \
  --immutable \
  --bwlimit=512K \
  --transfers=1 \
  --checkers=2 \
  --multi-thread-streams=1 \
  --retries=10 \
  --low-level-retries=20 \
  --contimeout=30s \
  --timeout=5m \
  --stats=15m \
  --stats-log-level=NOTICE \
  --log-level=NOTICE
