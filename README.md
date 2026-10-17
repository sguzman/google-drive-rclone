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


## Duplicate-file recovery (new; initially scan-only)

Google Drive permits different file objects (with distinct IDs and possibly distinct bytes) to share the same name in a folder. An ordinary rclone copy may ignore all but one of those objects. Duplicate folder names can also conceal files inside the folders.

The tool assumes rclone's default Google Docs export extensions (`docx,xlsx,pptx,svg`) when finding output-name collisions; custom export-format configurations are not yet supported. The new Python tool enumerates IDs with `rclone backend query` and plans recovery for file-name collisions and descendants of colliding folder IDs. It compares available **MD5 checksums**, not just names or sizes. When a source has no hash (for example, Google-native Docs), it never assumes two objects are byte-identical.

The primary archive remains at `/drive/gdrive`. Recovered files go to `/drive/gdrive/.rclone-duplicates/` in **separate directories keyed by their immutable Drive file IDs**, with `recovered.json` checksums and provenance. The tool never renames, deletes, or uploads anything on Google Drive. It refuses to overwrite uncertain local state.

### Step 1: Inventory, without downloading

```fish
python3 scripts/recover_duplicates.py scan
```

The scan prints a maximum of eight `WOULD RECOVER` samples by default, then a JSON summary. Add `--verbose` to print every candidate. **The candidate count includes descendants of duplicate-named folders; it is NOT a count of missing files.** It makes no local modifications. It can take time because it queries the Google Drive file inventory.

### Step 2: Explicitly recover

```fish
python3 scripts/recover_duplicates.py recover --max-downloads 1
```

Recovery uses `rclone backend copyid` and shares the 512 KiB/s cap; it also checks that `/drive` is actually an ext4 mount. **Recovery defaults to at most one new file per run and refuses to start while the regular downloader is active.** Wait for the original copy to finish or pause that service first. This recovery step has not yet been live-validated against your Drive; automatic scheduling will be added after the read-only scan and one live copy are verified.

The recovery state is stored on the 18 TB drive, **never in this public repository**. Google-native Docs are converted through rclone's normal export settings; their bytes cannot be compared to a cloud MD5 hash. Dangling shortcuts are flagged for review and are not silently marked as archived. If a transfer is interrupted leaving uncertain local output, recovery stops for that ID rather than deleting or overwriting data.

Offline tests (do not require Google credentials):

```fish
python3 -m unittest discover -s tests -v
```

### Single-file live probe

While the normal downloader continues, run:

```fish
python3 scripts/recover_duplicates.py probe
```

This queries the Drive inventory, chooses **one checksum-bearing binary at most 1 MiB** that appears not to be archived, and downloads it by immutable ID at **64 KiB/s**. It verifies the cloud-provided MD5, writes a local provenance manifest and leaves Google Drive untouched. The one-file probe can coexist with the regular 512 KiB/s download; it does not stop or restart that service. If no eligible small candidate exists, it fails without downloading anything. A completed `probe` is not evidence that all other candidates can be recovered; it only validates one end-to-end copy.
