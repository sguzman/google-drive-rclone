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
# The post-copy recovery pass runs only after a SUCCESSFUL normal transfer.
rclone copy 'gdrive:' '/drive/gdrive' \
  --drive-skip-dangling-shortcuts \
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

# Serialized, budgeted duplicate recovery. The main copy process is finished
# before this starts; --after-main-copy bypasses only the in-service status
# check, not the mount safety checks or the recovery file lock.
# Source files without an MD5 are intentionally deferred for manual review.
script_dir="$(cd "$(dirname "$(realpath "$0")")" && pwd -P)"
python3 "$script_dir/recover_duplicates.py" recover \
  --after-main-copy \
  --binary-only \
  --max-downloads 25 \
  --max-total-bytes 134217728
