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
import tempfile
import time

from recover_duplicates import (
    DEFAULT_ROOT, FOLDER, GOOGLE_EXPORT_EXTENSIONS, SHORTCUT,
    digest_id, file_matches, folder_components, is_disk_mounted,
    md5_file, output_name, plan, query, safe_name, safety_check,
)


SNAPSHOT_NAME = '.version-inventory.json'
SNAPSHOT_SECONDS = 30 * 60


def snapshot_path(archive):
    return archive / '.rclone-versions' / SNAPSHOT_NAME


def save_snapshot(items, archive, *, clock=time.time):
    """Opt-in local metadata cache, never inside Git or the main archive."""
    parent = archive / '.rclone-versions'
    if archive.is_symlink() or parent.is_symlink():
        raise RuntimeError('Refusing symlink in version snapshot directory')
    parent.mkdir(mode=0o700, exist_ok=True)
    path = snapshot_path(archive)
    if path.is_symlink():
        raise RuntimeError('Refusing symlink version snapshot')
    fd, temporary = tempfile.mkstemp(prefix='.inventory-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'created': clock(), 'items': items},
                      stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_snapshot(archive, *, clock=time.time):
    """Reject missing, stale, symlinked, or malformed source metadata."""
    parent = archive / '.rclone-versions'
    path = snapshot_path(archive)
    if archive.is_symlink() or parent.is_symlink() or path.is_symlink():
        raise RuntimeError('Refusing symlink in version snapshot')
    if not path.is_file():
        raise RuntimeError('Version snapshot missing; run scan --save-snapshot first')
    metadata = path.stat()
    if metadata.st_mode & 0o077:
        raise RuntimeError('Version snapshot permissions are too broad')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise RuntimeError('Invalid version snapshot format')
    created = data.get('created')
    if (not isinstance(created, (float, int)) or isinstance(created, bool)
            or not 0 <= clock() - created <= SNAPSHOT_SECONDS):
        raise RuntimeError('Version snapshot stale or future-dated; rerun the scan')
    items = data.get('items')
    if (not isinstance(items, list)
            or any(not isinstance(x, dict) or not isinstance(x.get('id'), str)
                   or not x['id'] or not isinstance(x.get('name'), str)
                   for x in items)):
        raise RuntimeError('Invalid version snapshot contents')
    return items


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
    if destination.is_symlink() or sidecar.is_symlink():
        raise ValueError('Refusing symlink in versioned archive')
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
            max_downloads=4, max_bytes=128 * 1024 * 1024,
            only_hashed=False):
    by_id, changed, uncertain = discover(items, archive)
    summary = dict(changed=len(changed), uncertain=uncertain,
                   hashed_changed=sum(bool(i.get('md5Checksum')) for i in changed),
                   hashless_changed=sum(not i.get('md5Checksum') for i in changed),
                   existing=0, attempted=0, downloaded=0,
                   bytes_downloaded=0, pending=0,
                   skipped_hashless=0, errors=[])
    spent = 0
    planned = 0
    for item in changed:
        if only_hashed and not item.get('md5Checksum'):
            summary['skipped_hashless'] += 1
            continue
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
        if dry_run or summary['attempted'] >= max_downloads:
            continue
        size = item.get('size')
        try:
            count = int(size) if size is not None else None
            if count is not None and count < 0:
                count = None
        except (TypeError, ValueError):
            count = None
        if planned >= max_bytes or (count is not None and count > max_bytes - planned):
            continue  # Not an unlimited oversize downloader; defer large revisions.
        if count is None and only_hashed:
            continue  # The limited sample requires a known finite byte count.
        summary['attempted'] += 1
        planned += count if count is not None else max_bytes
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
                handle.write('\n')
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


def complete_summary(summary):
    """Fail closed before a copy that ignores existing destination files."""
    return not (summary['pending'] or summary['uncertain'] or
                summary['errors'] or summary['skipped_hashless'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('scan', 'recover'))
    parser.add_argument('--archive', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--max-downloads', type=int, default=4)
    parser.add_argument('--max-total-bytes', type=int, default=128 * 1024 * 1024)
    parser.add_argument('--only-hashed', action='store_true',
                        help='recover only binary versions verifiable against Drive MD5')
    parser.add_argument('--save-snapshot', action='store_true',
                        help='scan: save private short-lived inventory for a later recovery')
    parser.add_argument('--use-snapshot', action='store_true',
                        help='recover: reuse recent scan without a second Drive API query')
    parser.add_argument('--after-main-copy', action='store_true')
    parser.add_argument('--in-service', action='store_true',
                        help='called by the serialized systemd copy service')
    parser.add_argument('--require-complete', action='store_true',
                        help='fail unless all detected revisions have been preserved')
    args = parser.parse_args()
    if args.max_downloads < 1 or args.max_total_bytes < 1:
        parser.error('Limits must be positive')
    if (args.after_main_copy or args.in_service or args.require_complete) and args.action != 'recover':
        parser.error('service and completion flags require recover')
    if args.save_snapshot and args.action != 'scan':
        parser.error('--save-snapshot is only valid for scan')
    if args.use_snapshot and args.action != 'recover':
        parser.error('--use-snapshot is only valid for recover')
    lock = None
    if args.action == 'recover' or args.save_snapshot:
        if not is_disk_mounted() or args.archive.resolve() != DEFAULT_ROOT or args.archive.is_symlink():
            raise RuntimeError('Expected /drive/gdrive on the ext4 mount')
        if not args.archive.is_dir() or not os.access(args.archive, os.W_OK):
            raise RuntimeError('Archive directory is not writable')
        if args.action == 'recover' and not (args.after_main_copy or args.in_service):
            active = subprocess.run(['systemctl', '--user', 'is-active',
                                     '--quiet', 'google-drive-rclone.service'],
                                    capture_output=True)
            if active.returncode == 0:
                raise RuntimeError('Ordinary Google Drive copy is active')
        if args.action != 'recover':
            items = query()
            save_snapshot(items, args.archive)
            summary = recover(items, args.archive, dry_run=True,
                              max_downloads=args.max_downloads,
                              max_bytes=args.max_total_bytes,
                              only_hashed=args.only_hashed)
            print('VERSION SUMMARY:', json.dumps(summary, ensure_ascii=False))
            print('Saved private version inventory (expires in 30 minutes)')
            return 1 if summary['errors'] else 0
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
    items = read_snapshot(args.archive) if args.use_snapshot else query()
    summary = recover(items, args.archive, dry_run=args.action == 'scan',
                      max_downloads=args.max_downloads, max_bytes=args.max_total_bytes,
                      only_hashed=args.only_hashed)
    print('VERSION SUMMARY:', json.dumps(summary, ensure_ascii=False))
    if summary['uncertain']:
        print('WARNING: Some existing files lack safe metadata or path matching; '
              'these need manual review.', file=sys.stderr)
    if args.require_complete and not complete_summary(summary):
        print('ERROR: Refusing ordinary copy while version preservation is incomplete '
              '(pending={pending}, uncertain={uncertain}, skipped_hashless={skipped_hashless}, '
              'errors={errors})'.format(**summary), file=sys.stderr)
        return 1
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
