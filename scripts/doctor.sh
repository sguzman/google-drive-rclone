#!/usr/bin/env bash
set -Eeuo pipefail

printf '== Google Drive archive diagnostics ==\n'
printf 'User: %s\n' "$(id -un)"
printf 'Drive mount: '
findmnt -n --mountpoint /drive -o TARGET,SOURCE,FSTYPE || true
printf 'Destination: '
if [[ -d /drive/gdrive ]]; then
  ls -ld /drive/gdrive
else
  printf 'MISSING\n'
fi
printf '\nRclone: '
if command -v rclone >/dev/null; then
  rclone version | head -n 1
  printf 'Configured gdrive remote: '
  if rclone listremotes 2>/dev/null | grep -Fxq 'gdrive:'; then
    printf 'yes\n'
  else
    printf 'no\n'
  fi
else
  printf 'missing\n'
fi
printf '\nTimer:\n'
systemctl --user list-timers --all google-drive-rclone.timer --no-pager || true
printf '\nService:\n'
systemctl --user show google-drive-rclone.service -p ActiveState -p SubState -p Result --no-pager || true
printf '\nRecent logs:\n'
journalctl --user -u google-drive-rclone.service -n 20 --no-pager || true
