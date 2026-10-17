#!/usr/bin/env python3
"""Recover colliding Google Drive names by immutable Drive file ID (read-only)."""
import argparse
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


def recovery_dir(item, by_id, recovered):
    folder = recovered
    for name, folder_id in folder_components(item, by_id):
        folder = folder / (name + '--' + digest_id(folder_id)[:24])
    return folder / ('id-' + digest_id(item['id']))


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


def run_recovery(items, archive, runner=subprocess.run, dry_run=False):
    by_id, siblings, candidates, duplicate_folders = plan(items)
    recovered = archive / '.rclone-duplicates'
    summary = {'duplicate_folder_ids': len(duplicate_folders),
               'candidate_files': len(candidates), 'downloaded': 0,
               'already_archived': 0, 'already_recovered': 0,
               'identical_source': 0, 'unsupported': 0, 'errors': []}
    for item, reason in unique_candidates(candidates):
        if reason:
            summary['identical_source'] += 1
            continue
        if item.get('mimeType') == SHORTCUT:
            summary['unsupported'] += 1
            summary['errors'].append('Shortcut requires review: ' + item['id'])
            continue
        # The regular copy may already have saved precisely this content.
        if any(file_matches(item, path) for path in local_candidates(item, by_id, archive)):
            summary['already_archived'] += 1
            continue
        destination = recovery_dir(item, by_id, recovered)
        if recovered in destination.parents and (destination.is_symlink() or
                any(p.is_symlink() for p in destination.parents if p != archive)):
            summary['errors'].append('Unsafe symlink at ' + str(destination))
            continue
        manifest = destination / 'recovered.json'
        if manifest.is_file():
            try:
                record = json.loads(manifest.read_text())
                leaf = destination / record['filename']
                if record['id'] == item['id'] and leaf.is_file() and not leaf.is_symlink():
                    if item.get('md5Checksum') and not file_matches(item, leaf):
                        summary['errors'].append('Existing recovered file changed: ' + str(leaf))
                    elif md5_file(leaf) == record['local_md5']:
                        summary['already_recovered'] += 1
                    else:
                        summary['errors'].append('Local checksum changed: ' + str(leaf))
                    continue
            except (KeyError, ValueError, OSError) as exc:
                summary['errors'].append('Bad manifest: ' + str(manifest) + ': ' + str(exc))
                continue
            summary['errors'].append('Incomplete recovery manifest: ' + str(manifest))
            continue
        if destination.exists() and any(destination.iterdir()):
            summary['errors'].append('Unverified recovery directory; refusing overwrite: ' + str(destination))
            continue
        if dry_run:
            print('WOULD RECOVER:', item['id'], item['name'], '=>', destination)
            continue
        destination.mkdir(parents=True, exist_ok=True)
        command = ['rclone', 'backend', 'copyid', 'gdrive:', item['id'],
                   str(destination) + '/', '--immutable', '--bwlimit=512K',
                   '--transfers=1', '--multi-thread-streams=1', '--retries=10',
                   '--low-level-retries=20', '--log-level=ERROR']
        proc = runner(command, text=True, capture_output=True)
        if proc.returncode:
            summary['errors'].append('Copy failed for ' + item['id'] + ': ' + proc.stderr[-700:])
            continue
        leaves = [p for p in destination.iterdir() if p.is_file() and not p.is_symlink() and p.name != 'recovered.json']
        if len(leaves) != 1:
            summary['errors'].append('Unexpected output for ' + item['id'] + ': ' + str(destination))
            continue
        leaf = leaves[0]
        if item.get('md5Checksum') and not file_matches(item, leaf):
            summary['errors'].append('Checksum mismatch for ' + item['id'] + ': ' + str(leaf))
            continue
        record = {'id': item['id'], 'source_name': item['name'],
                  'source_mime': item.get('mimeType'), 'source_md5': item.get('md5Checksum'),
                  'filename': leaf.name, 'local_md5': md5_file(leaf)}
        tmp = destination / 'recovered.json.tmp'
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n')
        os.replace(tmp, manifest)
        summary['downloaded'] += 1
        print('RECOVERED:', item['id'], '=>', leaf)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['scan', 'recover'], help='scan is read-only')
    parser.add_argument('--archive', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--fixture', type=Path, help='offline JSON fixture (for testing only)')
    args = parser.parse_args()
    if args.action == 'recover' and not args.fixture:
        if not is_disk_mounted():
            fail('/drive is not an ext4 mount; refusing to write')
        if args.archive.resolve() != DEFAULT_ROOT or args.archive.is_symlink():
            fail('Archive must be exactly /drive/gdrive and not a symlink')
        if not args.archive.is_dir() or not os.access(args.archive, os.W_OK):
            fail('Archive is missing or not writable')
    items = json.loads(args.fixture.read_text()) if args.fixture else query()
    summary = run_recovery(items, args.archive, dry_run=args.action == 'scan')
    print('SUMMARY:', json.dumps(summary, ensure_ascii=False))
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
