# google-drive-rclone

A **one-way, low-bandwidth Google Drive archive** for Linux. The source is the configured rclone `gdrive:` remote and the destination is **`/drive/gdrive`**.

This is not a bidirectional sync. It intentionally never uploads to Google Drive and never deletes files locally. Existing local files are immutable: if an upstream file changes, rclone reports a conflict rather than overwriting the archival copy.

## Prerequisites

- Linux with systemd user services, Bash, `findmnt`, and `rclone` installed.
- Your 18 TB ext4 filesystem mounted at exactly `/drive`.
- An already authorized rclone remote named `gdrive` (preferably with the `drive.readonly` scope).
- Permission to create/write `/drive/gdrive`.

Do not commit your OAuth credentials, `rclone.conf`, Takeout ZIPs, or downloaded personal files into this repository.

## Install

From this repository checkout (currently `~/Code/Sync/google-drive-rclone`):

```fish
git pull --ff-only
bash scripts/install.sh
```

The installer is idempotent. It checks the mount, creates the destination, validates that `gdrive:` exists, installs **symlinks** to this repository's script and systemd units, reloads systemd, and enables the user timer. It does **not** require sudo and never changes the remote configuration.

If the destination is not writable, the installer exits and prints a one-time ownership command. It does not run sudo automatically.

## Behavior

| Setting | Value |
| --- | --- |
| Remote | `gdrive:` |
| Destination | `/drive/gdrive` |
| Direction | Google Drive to local only |
| Bandwidth cap | 512 KiB/s (~4.2 Mbps) |
| Concurrent transfers | 1 |
| Checkers | 2 |
| Multi-thread download streams | 1 |
| First automatic run | ~1–2 minutes after timer activation |
| Later runs | ~1 hour after each run stops |
| Updates of existing local files | Prohibited (`--immutable`) |
| Deletes | None |

Jobs can take many hours. A second run will not overlap the running service. Completed files are skipped on the next pass. **An interrupted individual file may need to download again from the beginning.**

Rclone exports Google-native Docs to standard formats when copying them; download behavior differs from ordinary files. Google Takeout exports placed in Google Drive are handled as ordinary ZIPs.

**Important:** the shared OAuth client ID for Google Drive is being retired in 2026. Replace it with your own OAuth client credentials for dependable long-term use: https://rclone.org/drive/#making-your-own-client-id . Keep these credentials in `~/.config/rclone/rclone.conf`, not Git.

## Control

```fish
systemctl --user status google-drive-rclone.timer
systemctl --user status google-drive-rclone.service
journalctl --user -u google-drive-rclone.service -n 50 --no-pager
```

Start a pass immediately:

```fish
systemctl --user start google-drive-rclone.service
```

Pause future runs (and stop any in-progress download):

```fish
systemctl --user disable --now google-drive-rclone.timer
systemctl --user stop google-drive-rclone.service
```

Resume:

```fish
systemctl --user enable --now google-drive-rclone.timer
```

Update the repository:

```fish
git pull --ff-only
bash scripts/install.sh
```

The timer runs while your systemd **user manager** is running (normally while logged in). You can separately enable linger if you want it to operate while logged out, but that is deliberately **not** changed by the installer.

## Files

- `scripts/install.sh`: safe, repeatable installer (run once, or after repo updates).
- `scripts/copy.sh`: mount-guarded archive transfer, invoked by systemd.
- `scripts/doctor.sh`: read-only diagnostic summary.
- `systemd/user/google-drive-rclone.service`: one-shot, low-priority transfer.
- `systemd/user/google-drive-rclone.timer`: automatic recurring execution.

This project does not store or manage Google Photos directly. To migrate a static Photos library, first send selected **Google Takeout** exports to Drive, then let the downloader archive the resulting ZIP files.

## Security and safety

The installer does not delete local files or modify your Google Drive; `rclone copy` itself does not delete destination-only files. The installer refuses to work if `/drive` is not mounted. The service also has a mount condition and a second runtime mount check. Logs go to the user journal; no web server, GPU use, or separate daemon is required.

The timer and scripts are linked to your clone. If you move the repo, run the installer again from the new path.
