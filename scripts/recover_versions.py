#!/usr/bin/env python3
"""Preserve changed Drive sources as separate, immutable archive versions.

The primary /drive/gdrive tree is never modified. This companion scanner
handles ordinary, non-colliding file IDs only; name collisions are handled
separately by recover_duplicates.py.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from recover_duplicates import (
    DEFAULT_ROOT, FOLDER, GOOGLE_EXPORT_EXTENSIONS, SHORTCUT,
    digest_id, file_matches, folder_components, is_disk_mounted,
    md5_file, output_name, plan, query, safe_name, safety_check,
)


def source_token(item):
    """Stable token for this cloud content revision, not a mutable filename."""
    if item.get('md5Checksum'):
        return 'md5:' + str(item['md5Checksum']).lower()
    if item.get('modifiedTime'):
        return 'mtime:' + str(item['modifiedTime'])
    return None


def cloud_time(item):
    raw = item.get('modifiedTime')
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError):
        return None


def is_changed(item, original):
    """True only with evidence that the current cloud version differs."""
    if original.is_symlink() or not original.is_file():
        return False
    if item.get('md5Checksum'):
        return not file_matches(item, original)
    # Cloud-native exports lack hashes and may lack sizes. Use the source's
    # actual modification time, never the export's present-day download time.
    when = cloud_time(item)
    return when is not None and abs(original.stat().st_mtime - when) > 2


def original_path(item, by_id, archive):
    parent = archive
    for name, _ in folder_components(item, by_id):
        parent /= name
    return parent / safe_name(output_name(item))


def version_path(item, by_id, archive):
    """Include both file ID and source revision; never overwrite prior revisions."""
    root = archive / '.rclone-versions'
    parent = root
    for name, _ in folder_components(item, by_id):
        parent /= name
    filename = safe_name(output_name(item))
    stem, ext = os.path.splitext(filename)
    token = source_token(item)
    if token is None:
        raise ValueError('Source version token unavailable')
    revision = hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]
    return parent / (stem + '--' + digest_id(item['id'])[:16] + '--' + revision + ext)


def discover(items, archive):
    """Return current cloud revisions missing from a pre-existing local snapshot."""
    by_id, _, duplicates, _ = plan(items)
    excluded = {item['id'] for item in duplicates}
    found, uncertain = [], 0
    for item in items:
        if item['id'] in excluded or item.get('mimeType') in (FOLDER, SHORTCUT):
            continue
        mime = item.get('mimeType', '')
        if mime.startswith('application/vnd.google-apps.') and mime not in GOOGLE_EXPORT_EXTENSIONS:
            continue
        original = original_path(item, by_id, archive)
        if not safety_check(original, archive):
            uncertain += 1
            continue
        if not original.exists() and not original.is_symlink():
            continue  # Not a conflict; the ordinary copy handles new objects.
        if original.is_symlink() or not original.is_file():
            uncertain += 1
            continue
        if not source_token(item):
            uncertain += 1
            continue
        if is_changed(item, original):
            found.append(item)
    found.sort(key=lambda item: (item.get('name', ''), item['id']))
    return by_id, found, uncertain


def snapshot_present(item, destination):
    sidecar = destination.with_name(destination.name + '.source.json')
    if not destination.exists() and not sidecar.exists():
        return False
    if destination.is_symlink() or sidecar.is_symlink() or not destination.is_file() or not sidecar.is_file():
        raise ValueError('Partial or unsafe versioned snapshot at ' + str(destination))
    record = json.loads(sidecar.read_text(encoding='utf-8'))
    if (record.get('id') != item['id']
            or record.get('version') != source_token(item)
            or record.get('local_md5') != md5_file(destination)):
        raise ValueError('Versioned snapshot provenance mismatch at ' + str(destination))
    if item.get('md5Checksum') and not file_matches(item, destination):
        raise ValueError('Stored version hash differs from current Drive content')
    return True


def recover(items, archive, *, runner=subprocess.run, dry_run=True,
            max_downloads=4, max_bytes=128 * 1024 * 1024):
    by_id, changed, uncertain = discover(items, archive)
    summary = dict(changed=len(changed), uncertain=uncertain, existing=0,
                   downloaded=0, bytes_downloaded=0, pending=0, errors=[])
    spent = 0
    for item in changed:
        destination = version_path(item, by_id, archive)
        if not safety_check(destination, archive):
            summary['errors'].append('Unsafe destination for cloud ID ' + item['id'])
            continue
        try:
            if snapshot_present(item, destination):
                summary['existing'] += 1
                continue
        except (ValueError, OSError) as exc:
            summary['errors'].append(str(exc))
            continue
        summary['pending'] += 1
        if dry_run or summary['downloaded'] >= max_downloads:
            continue
        size = item.get('size')
        try:
            count = int(size) if size is not None else None
            if count is not None and count < 0:
                count = None
        except (TypeError, ValueError):
            count = None
        if spent >= max_bytes or (count is not None and count > max_bytes - spent):
            continue  # Not an unlimited oversize downloader; defer large revisions.
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            command = ['rclone', 'backend', 'copyid', 'gdrive:', item['id'],
                       str(destination), '--immutable', '--bwlimit=512K',
                       '--transfers=1', '--multi-thread-streams=1', '--retries=10',
                       '--low-level-retries=20', '--log-level=ERROR']
            proc = runner(command, text=True, capture_output=True)
            if proc.returncode:
                raise RuntimeError('Rclone copyid failed (exit ' + str(proc.returncode) + ')')
            if not destination.is_file() or destination.is_symlink():
                raise RuntimeError('Versioned copy was not produced')
            if item.get('md5Checksum') and not file_matches(item, destination):
                raise RuntimeError('Versioned copy did not match source checksum')
            record = dict(id=item['id'], version=source_token(item),
                          source_mime=item.get('mimeType'),
                          source_name=item['name'], local_md5=md5_file(destination))
            sidecar = destination.with_name(destination.name + '.source.json')
            with sidecar.open('x', encoding='utf-8') as handle:
                json.dump(record, handle, ensure_ascii=False, sort_keys=True)
                handle.write('\\n')
                handle.flush()
                os.fsync(handle.fileno())
            summary['downloaded'] += 1
            actual = destination.stat().st_size
            spent += actual
            summary['bytes_downloaded'] += actual
            summary['pending'] -= 1
        except (OSError, RuntimeError, ValueError) as exc:
            summary['errors'].append('Could not preserve cloud revision '
                                     + item['id'] + ': ' + str(exc))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('scan', 'recover'))
    parser.add_argument('--archive', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--max-downloads', type=int, default=4)
    parser.add_argument('--max-total-bytes', type=int, default=128 * 1024 * 1024)
    parser.add_argument('--after-main-copy', action='store_true')
    args = parser.parse_args()
    if args.max_downloads < 1 or args.max_total_bytes < 1:
        parser.error('Limits must be positive')
    if args.after_main_copy and args.action != 'recover':
        parser.error('--after-main-copy requires recover')
    lock = None
    if args.action == 'recover':
        if not is_disk_mounted() or args.archive.resolve() != DEFAULT_ROOT or args.archive.is_symlink():
            raise RuntimeError('Expected /drive/gdrive on the ext4 mount')
        if not args.archive.is_dir() or not os.access(args.archive, os.W_OK):
            raise RuntimeError('Archive directory is not writable')
        if not args.after_main_copy:
            active = subprocess.run(['systemctl', '--user', 'is-active',
                                     '--quiet', 'google-drive-rclone.service'],
                                    capture_output=True)
            if active.returncode == 0:
                raise RuntimeError('Ordinary Google Drive copy is active')
        lockroot = args.archive / '.rclone-duplicates'
        if lockroot.is_symlink():
            raise RuntimeError('Recovery lock directory is a symlink')
        lockroot.mkdir(exist_ok=True)
        lock = (lockroot / '.recovery.lock').open('a+')
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another recovery pass holds the lock; skipping')
            return 0
    items = query()
    summary = recover(items, args.archive, dry_run=args.action == 'scan',
                      max_downloads=args.max_downloads, max_bytes=args.max_total_bytes)
    print('VERSION SUMMARY:', json.dumps(summary, ensure_ascii=False))
    if summary['uncertain']:
        print('WARNING: Some existing files lack safe metadata or path matching; '
              'these need manual review.', file=sys.stderr)
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
