#!/usr/bin/env python3
"""Recover colliding Google Drive names by immutable Drive file ID (read-only)."""
import argparse
import fcntl
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

FOLDER = 'application/vnd.google-apps.folder'
SHORTCUT = 'application/vnd.google-apps.shortcut'
DEFAULT_ROOT = Path('/drive/gdrive')


def fail(message):
    raise RuntimeError(message)


def safe_name(name):
    # Each Drive path component must be one local component, never an absolute path.
    name = str(name).replace('/', '_').replace('\\', '_').replace('\x00', '_')
    name = ''.join('_' if ord(c) < 32 else c for c in name)
    if name in ('', '.', '..'):
        name = '_'
    return name[:90]


def digest_id(item_id):
    return hashlib.sha256(item_id.encode('utf-8')).hexdigest()


def is_disk_mounted():
    result = subprocess.run(['findmnt', '-n', '--mountpoint', '/drive', '-o', 'TARGET,FSTYPE'],
                            capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == '/drive ext4'


def md5_file(path):
    result = hashlib.md5()
    with path.open('rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def query(runner=subprocess.run):
    cmd = ['rclone', 'backend', 'query', 'gdrive:', 'trashed = false']
    proc = runner(cmd, text=True, capture_output=True)
    if proc.returncode:
        fail('Google Drive query failed: ' + proc.stderr[-2000:])
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        fail(f'Invalid JSON from rclone backend query: {error}')
    if not isinstance(items, list):
        fail('Unexpected query response, expected JSON array')
    return [item for item in items if isinstance(item, dict) and item.get('id') and
            isinstance(item.get('name'), str)]


# Default rclone Drive document exports; customized --drive-export-formats
# settings will require adapting this mapping before automatic recovery.
GOOGLE_EXPORT_EXTENSIONS = {
    'application/vnd.google-apps.document': '.docx',
    'application/vnd.google-apps.spreadsheet': '.xlsx',
    'application/vnd.google-apps.presentation': '.pptx',
    'application/vnd.google-apps.drawing': '.svg',
}


def output_name(item):
    return item['name'] + GOOGLE_EXPORT_EXTENSIONS.get(item.get('mimeType'), '')


def plan(items):
    by_id = {item['id']: item for item in items}
    children = defaultdict(list)
    sibling_dirs = defaultdict(list)
    sibling_files = defaultdict(list)
    for item in items:
        for parent_id in (item.get('parents') or ['__no_parent__']):
            children[parent_id].append(item)
            if item.get('mimeType') == FOLDER:
                sibling_dirs[(parent_id, item['name'])].append(item)
            else:
                sibling_files[(parent_id, output_name(item))].append(item)

    colliding_folders = set()
    colliding_files = set()
    for group in sibling_dirs.values():
        if len(group) > 1:
            colliding_folders.update(item['id'] for item in group)
    for group in sibling_files.values():
        if len(group) > 1:
            colliding_files.update(item['id'] for item in group)

    # A colliding directory can conceal files whose own names are unique.
    hidden = set()
    pending = list(colliding_folders)
    while pending:
        for item in children.get(pending.pop(), []):
            if item.get('mimeType') == FOLDER:
                if item['id'] not in hidden:
                    hidden.add(item['id'])
                    pending.append(item['id'])
            else:
                hidden.add(item['id'])

    candidates = [item for item in items if item['id'] in (colliding_files | hidden)
                  and item.get('mimeType') != FOLDER]
    candidates.sort(key=lambda obj: (obj['name'], obj['id']))
    return by_id, sibling_files, candidates, colliding_folders


def folder_components(item, by_id):
    parts, seen = [], set()
    parent_ids = item.get('parents') or []
    while parent_ids:
        parent_id = parent_ids[0]
        if parent_id in seen:
            fail('Folder-parent cycle at ' + parent_id)
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            break
        if parent.get('mimeType') != FOLDER:
            fail('Parent is not a directory: ' + parent_id)
        parts.append((safe_name(parent['name']), parent_id))
        parent_ids = parent.get('parents') or []
    return list(reversed(parts))


def local_candidates(item, by_id, archive):
    folder = archive
    for component, _ in folder_components(item, by_id):
        folder = folder / component
    return [folder / safe_name(output_name(item))]


def legacy_recovery_dir(item, by_id, recovered):
    """Original per-file directory layout; only checked, never deleted or rewritten."""
    folder = recovered
    for name, folder_id in folder_components(item, by_id):
        folder = folder / (name + '--' + digest_id(folder_id)[:24])
    return folder / ('id-' + digest_id(item['id']))


def compact_destination(item, by_id, collision_ids, recovered):
    """Mirror the source tree, disambiguating only colliding dirs and filenames.

    File IDs go into the *filename*, not a new one-file directory.
    """
    folder = recovered
    for name, folder_id in folder_components(item, by_id):
        if folder_id in collision_ids:
            name = name + '--' + digest_id(folder_id)[:16]
        folder = folder / name
    filename = safe_name(output_name(item))
    stem, ext = os.path.splitext(filename)
    return folder / (stem + '--' + digest_id(item['id'])[:16] + ext)


def safety_check(path, archive):
    """Never follow symlinks within the recovery tree or out of the archive."""
    if archive.is_symlink():
        return False
    current = path
    while current != archive:
        if current.is_symlink():
            return False
        if archive not in current.parents:
            return False
        current = current.parent
    return True


def verified_manifest(item, file_path, manifest_path):
    """Return True only if a manifest and its file are both verified.

    Raise ValueError on damaged or inconsistent prior state, without changing it.
    """
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError('Missing or unsafe provenance sidecar: ' + str(manifest_path))
    record = json.loads(manifest_path.read_text())
    if record.get('id') != item['id'] or not file_path.is_file() or file_path.is_symlink():
        raise ValueError('Incorrect ID or missing recovered file: ' + str(file_path))
    local_md5 = md5_file(file_path)
    if record.get('local_md5') != local_md5:
        raise ValueError('Recovered bytes changed: ' + str(file_path))
    if item.get('md5Checksum') and not file_matches(item, file_path):
        raise ValueError('Recovered bytes differ from Drive: ' + str(file_path))
    return True


def file_matches(item, path):
    md5 = item.get('md5Checksum')
    size = item.get('size')
    if not md5 or path.is_symlink() or not path.is_file():
        return False
    if size is not None and path.stat().st_size != int(size):
        return False
    return md5_file(path) == md5.lower()


def unique_candidates(candidates):
    # Content-identical duplicates at the same parent/name need only one copy.
    # A hashless file is not proven identical to anything else.
    seen = set()
    for item in candidates:
        key = (tuple(item.get('parents') or []), output_name(item))
        checksum = item.get('md5Checksum')
        content_key = (key, str(item.get('size')), checksum.lower()) if checksum else None
        if content_key and content_key in seen:
            yield item, 'identical-source'
        else:
            if content_key:
                seen.add(content_key)
            yield item, None


def probe_candidate(items, archive, max_bytes=1024 * 1024):
    """Choose the smallest not-yet-backed-up binary file with a cloud checksum."""
    by_id, _, candidates, collision_ids = plan(items)
    options = []
    for item, reason in unique_candidates(candidates):
        if reason or not item.get('md5Checksum'):
            continue
        try:
            size = int(item.get('size', ''))
        except (ValueError, TypeError):
            continue
        if not 0 < size <= max_bytes:
            continue
        if any(file_matches(item, candidate)
               for candidate in local_candidates(item, by_id, archive)):
            continue
        recovered = archive / '.rclone-duplicates'
        destination = compact_destination(item, by_id, collision_ids, recovered)
        legacy = legacy_recovery_dir(item, by_id, recovered)
        if destination.exists() or legacy.exists():
            continue
        options.append((size, item['id'], item))
    return min(options, default=(None, None, None))[-1]


def run_recovery(items, archive, runner=subprocess.run, dry_run=False,
                 max_downloads=1, sample_limit=8, verbose=False,
                 only_ids=None, bandwidth='512K',
                 binary_only=False, max_total_bytes=0):
    by_id, _, candidates, duplicate_folders = plan(items)
    recovered = archive / '.rclone-duplicates'
    folder_names = sorted({by_id[oid]['name'] for oid in duplicate_folders})
    summary = {'duplicate_folder_ids': len(duplicate_folders),
               'duplicate_folder_names': folder_names,
               'candidate_files': len(candidates),
               'would_download': 0, 'downloaded': 0, 'attempted': 0,
               'already_archived': 0, 'already_recovered': 0,
               'identical_source': 0, 'unsupported': 0,
               'skipped_unverifiable': 0, 'skipped_budget': 0,
               'planned_bytes': 0, 'downloaded_bytes': 0,
               'pending_files': 0, 'pending_bytes_known': 0,
               'pending_unknown_size': 0, 'oversize_files': 0,
               'errors': []}

    def source_bytes(obj):
        try:
            value = int(obj['size'])
            return value if value >= 0 else None
        except (KeyError, ValueError, TypeError):
            return None

    entries = list(unique_candidates(candidates))
    # Recover the largest eligible files first so a 128-MiB pass actually
    # transfers bytes, instead of spending it on 25 tiny alphabetic files.
    # Oversize files follow eligible files; once nothing smaller remains,
    # download one indivisible large file (still at 512 KiB/s) rather than
    # silently excluding it forever. Unmeasured Google Docs are last.
    if max_total_bytes and not dry_run:
        def priority(entry):
            item, _ = entry
            size = source_bytes(item)
            if size is None:
                return (2, 0, item['id'])
            return (1 if size > max_total_bytes else 0, -size, item['id'])
        entries.sort(key=priority)

    for item, reason in entries:
        if only_ids is not None and item['id'] not in only_ids:
            continue
        if reason:
            summary['identical_source'] += 1
            continue
        if item.get('mimeType') == SHORTCUT:
            # Shortcuts are references, not a downloadable source payload.
            summary['unsupported'] += 1
            continue
        if item.get('mimeType', '').startswith('application/vnd.google-apps.') and (
                item.get('mimeType') not in GOOGLE_EXPORT_EXTENSIONS):
            # Unsupported Google-native items (e.g. Forms) need separate
            # export handling. Never claim to have backed them up.
            summary['unsupported'] += 1
            continue
        if binary_only and not item.get('md5Checksum'):
            summary['skipped_unverifiable'] += 1
            continue
        if any(file_matches(item, path) for path in local_candidates(item, by_id, archive)):
            summary['already_archived'] += 1
            continue

        destination = compact_destination(item, by_id, duplicate_folders, recovered)
        sidecar = destination.with_name(destination.name + '.source.json')
        legacy = legacy_recovery_dir(item, by_id, recovered)
        legacy_manifest = legacy / 'recovered.json'

        if not safety_check(destination, archive) or not safety_check(legacy, archive):
            summary['errors'].append('Unsafe recovery destination for ' + item['id'])
            continue

        # Recognize the legacy single-file directory from the previously successful
        # live probe. Never move/delete that copy without a separate migration.
        if legacy_manifest.exists() or legacy.exists():
            try:
                if not legacy_manifest.is_file() or legacy_manifest.is_symlink():
                    raise ValueError('Legacy directory has no valid manifest')
                old_record = json.loads(legacy_manifest.read_text())
                leaf_name = old_record['filename']
                if Path(leaf_name).name != leaf_name or leaf_name in ('', '.', '..'):
                    raise ValueError('Legacy manifest filename is unsafe')
                legacy_file = legacy / leaf_name
                verified_manifest(item, legacy_file, legacy_manifest)
                summary['already_recovered'] += 1
            except (OSError, ValueError, KeyError) as error:
                summary['errors'].append('Unverified old recovery ' + item['id'] + ': ' + str(error))
            continue

        if sidecar.exists() or sidecar.is_symlink() or destination.exists() or destination.is_symlink():
            try:
                verified_manifest(item, destination, sidecar)
                summary['already_recovered'] += 1
            except (OSError, ValueError, KeyError) as error:
                summary['errors'].append('Unverified recovery ' + item['id'] + ': ' + str(error))
            continue

        size = source_bytes(item)
        summary['pending_files'] += 1
        if size is None:
            summary['pending_unknown_size'] += 1
        else:
            summary['pending_bytes_known'] += size
            if max_total_bytes and size > max_total_bytes:
                summary['oversize_files'] += 1

        if dry_run:
            summary['would_download'] += 1
            if verbose or summary['would_download'] <= sample_limit:
                print('WOULD RECOVER:', item['id'], item['name'], '=>', destination)
            continue

        # Keep scanning for an accurate backlog count after the transfer cap.
        if summary['attempted'] >= max_downloads:
            continue
        if max_total_bytes:
            if summary['planned_bytes'] >= max_total_bytes:
                summary['skipped_budget'] += 1
                continue
            # A single oversized source may run beyond the nominal per-pass
            # byte budget, but only when no smaller transfer was attempted.
            # Skipping it forever would make the archive permanently partial.
            if size is not None and size > max_total_bytes - summary['planned_bytes']:
                if summary['attempted'] > 0 or summary['planned_bytes'] > 0:
                    summary['skipped_budget'] += 1
                    continue
                print('OVERSIZE: one indivisible file exceeds the per-run target:',
                      item['id'], size, 'bytes')
            summary['planned_bytes'] += size or 0
        summary['attempted'] += 1
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            summary['errors'].append('Cannot create destination: ' + str(error))
            continue
        # copyid is given an explicit leaf name. --immutable prevents overwrites.
        command = ['rclone', 'backend', 'copyid', 'gdrive:', item['id'],
                   str(destination), '--immutable', '--bwlimit=' + bandwidth,
                   '--transfers=1', '--multi-thread-streams=1', '--retries=10',
                   '--low-level-retries=20', '--log-level=ERROR']
        proc = runner(command, text=True, capture_output=True)
        if proc.returncode:
            summary['errors'].append('Copy failed for ' + item['id'] + ': ' + proc.stderr[-700:])
            continue
        if not destination.is_file() or destination.is_symlink():
            summary['errors'].append('Expected output not produced: ' + str(destination))
            continue
        if item.get('md5Checksum') and not file_matches(item, destination):
            summary['errors'].append('Checksum mismatch for ' + item['id'] + ': ' + str(destination))
            continue

        record = {'id': item['id'], 'source_name': item['name'],
                  'source_mime': item.get('mimeType'),
                  'source_md5': item.get('md5Checksum'),
                  'filename': destination.name, 'local_md5': md5_file(destination)}
        temp = sidecar.with_name(sidecar.name + '.tmp')
        # Exclusive creation: do not clobber any unverified sidecar/tmp file.
        try:
            with temp.open('x') as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            if sidecar.exists():
                raise ValueError('Another manifest already exists: ' + str(sidecar))
            os.replace(temp, sidecar)
        except (OSError, ValueError) as error:
            summary['errors'].append('Could not save provenance for ' + item['id'] + ': ' + str(error))
            continue

        summary['downloaded'] += 1
        actual_bytes = destination.stat().st_size
        summary['downloaded_bytes'] += actual_bytes
        summary['pending_files'] -= 1
        if size is None:
            summary['pending_unknown_size'] -= 1
            # Workspace exports do not expose their eventual byte size via
            # Google Drive metadata. Charge actual exported bytes to budget.
            summary['planned_bytes'] += actual_bytes
        else:
            summary['pending_bytes_known'] -= size
            if max_total_bytes and size > max_total_bytes:
                summary['oversize_files'] -= 1
        print('RECOVERED:', item['id'], '=>', destination)

    if dry_run and not verbose and summary['would_download'] > sample_limit:
        print(f"... {summary['would_download'] - sample_limit} more candidates omitted; "
              'use --verbose for every file.')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['scan', 'probe', 'recover'],
                        help='scan is read-only; probe copies one small checksum-verifiable file')
    parser.add_argument('--archive', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--fixture', type=Path, help='offline JSON fixture (for testing only)')
    parser.add_argument('--verbose', action='store_true', help='show all scan candidates')
    parser.add_argument('--max-downloads', type=int, default=1,
                        help='max file-ID downloads per recovery run (default: 1)')
    parser.add_argument('--binary-only', action='store_true',
                        help='exclude Google-native Docs and sources without MD5')
    parser.add_argument('--max-total-bytes', type=int, default=0,
                        help='max source bytes per run (0 = unlimited)')
    parser.add_argument('--after-main-copy', action='store_true',
                        help='run sequentially inside systemd copy service, not concurrently')
    args = parser.parse_args()
    if args.max_downloads < 1 or args.max_total_bytes < 0:
        parser.error('--max-downloads must be positive and --max-total-bytes nonnegative')
    if args.after_main_copy and args.action != 'recover':
        parser.error('--after-main-copy is valid only for recover')
    if args.action == 'recover' and not args.fixture and not args.after_main_copy:
        active = subprocess.run(
            ['systemctl', '--user', 'is-active', '--quiet', 'google-drive-rclone.service'],
            capture_output=True)
        if active.returncode == 0:
            fail('Regular Google Drive downloader still running; wait for it to finish')
    if args.action in ('recover', 'probe') and not args.fixture:
        if not is_disk_mounted():
            fail('/drive is not an ext4 mount; refusing to write')
        if args.archive.resolve() != DEFAULT_ROOT or args.archive.is_symlink():
            fail('Archive must be exactly /drive/gdrive and not a symlink')
        if not args.archive.is_dir() or not os.access(args.archive, os.W_OK):
            fail('Archive is missing or not writable')
    # Serialize all writers, including a manual probe during the automatic pass.
    # A scan remains entirely read-only and never creates a lockfile.
    lock = None
    if args.action in ('recover', 'probe') and not args.fixture:
        lock_path = args.archive / '.rclone-duplicates'
        if lock_path.is_symlink():
            fail('Recovery directory is a symlink')
        lock_path.mkdir(exist_ok=True)
        lock = (lock_path / '.recovery.lock').open('a+')
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Recovery busy: another pass holds the lock; skipping.')
            return 0
    items = json.loads(args.fixture.read_text()) if args.fixture else query()
    only_ids = None
    bandwidth = '512K'
    if args.action == 'probe':
        item = probe_candidate(items, args.archive)
        if item is None:
            fail('No eligible binary collision at or below 1 MiB remains to test')
        print('PROBE:', item['id'], item['name'], item.get('size'), 'bytes')
        only_ids = {item['id']}
        bandwidth = '64K'  # coexist quietly with the normal 512 KiB/s downloader
    summary = run_recovery(items, args.archive, dry_run=args.action == 'scan',
                           max_downloads=1 if args.action == 'probe' else args.max_downloads,
                           verbose=args.verbose, only_ids=only_ids, bandwidth=bandwidth,
                           binary_only=args.binary_only,
                           max_total_bytes=args.max_total_bytes)
    print('SUMMARY:', json.dumps(summary, ensure_ascii=False))
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
