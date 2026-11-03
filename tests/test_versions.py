import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('recover_versions', SCRIPTS / 'recover_versions.py')
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


def source(oid='id1', name='document.bin', data=b'new'):
    import hashlib
    return {'id': oid, 'name': name, 'parents': [], 'mimeType': 'application/octet-stream',
            'size': str(len(data)), 'md5Checksum': hashlib.md5(data).hexdigest(),
            'modifiedTime': '2026-10-01T10:00:00Z'}


class FakeCopy:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        destination = Path(cmd[5])
        destination.write_bytes(self.data)
        return type('Result', (), {'returncode': 0, 'stderr': ''})()


class VersionTests(unittest.TestCase):
    def test_same_cloud_version_skipped(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            item = source(data=b'new')
            (archive / item['name']).write_bytes(b'new')
            _, found, unsure = versions.discover([item], archive)
            self.assertEqual(found, [])
            self.assertEqual(unsure, 0)

    def test_changed_binary_archived_separately_and_immutable(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            item = source(data=b'new')
            original = archive / item['name']
            original.write_bytes(b'old')
            runner = FakeCopy(b'new')
            result = versions.recover([item], archive, runner=runner,
                                      dry_run=True)
            self.assertEqual(result['changed'], 1)
            self.assertEqual(result['pending'], 1)
            self.assertEqual(len(list(archive.glob('.rclone-versions/**/*'))), 0)
            result = versions.recover([item], archive, runner=runner, dry_run=False)
            self.assertEqual(result['downloaded'], 1)
            self.assertEqual(original.read_bytes(), b'old')
            self.assertIn('--immutable', runner.calls[0])
            self.assertIn('--bwlimit=512K', runner.calls[0])
            target = versions.version_path(item, {item['id']: item}, archive)
            self.assertEqual(target.read_bytes(), b'new')
            self.assertEqual(json.loads(
                target.with_name(target.name + '.source.json').read_text())['id'], item['id'])
            self.assertEqual(versions.recover([item], archive, runner=runner,
                                             dry_run=False)['existing'], 1)
            self.assertEqual(len(runner.calls), 1)

    def test_two_source_revisions_preserve_both(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            original = archive / 'document.bin'
            original.write_bytes(b'old')
            first = source(data=b'new')
            second = source(data=b'next')
            versions.recover([first], archive, runner=FakeCopy(b'new'), dry_run=False)
            versions.recover([second], archive, runner=FakeCopy(b'next'), dry_run=False)
            a = versions.version_path(first, {first['id']: first}, archive)
            b = versions.version_path(second, {second['id']: second}, archive)
            self.assertNotEqual(a, b)
            self.assertEqual(a.read_bytes(), b'new')
            self.assertEqual(b.read_bytes(), b'next')
            self.assertEqual(original.read_bytes(), b'old')

    def test_native_document_uses_modification_time_without_hash(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            item = {'id': 'native', 'name': 'Sheet',
                    'mimeType': 'application/vnd.google-apps.spreadsheet',
                    'parents': [], 'modifiedTime': '2026-10-01T10:00:00Z'}
            original = archive / 'Sheet.xlsx'
            original.write_bytes(b'old export')
            os.utime(original, (0, 0))
            _, changed, unsure = versions.discover([item], archive)
            self.assertEqual(changed, [item])
            self.assertEqual(unsure, 0)
            runner = FakeCopy(b'new export')
            report = versions.recover([item], archive, runner=runner, dry_run=False)
            self.assertEqual(report['downloaded'], 1)
            self.assertEqual(original.read_bytes(), b'old export')

    def test_duplicate_ids_not_confused_with_version_conflicts(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            a = source(oid='a', data=b'a')
            b = source(oid='b', data=b'b')
            (archive / 'document.bin').write_bytes(b'old')
            _, conflicts, _ = versions.discover([a, b], archive)
            self.assertEqual(conflicts, [])

    def test_untrusted_symlinks_not_downloaded(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            item = source()
            (archive / item['name']).symlink_to('/etc/hosts')
            _, conflicts, uncertain = versions.discover([item], archive)
            self.assertEqual(conflicts, [])
            self.assertEqual(uncertain, 1)

    def test_tampered_manifest_never_overwritten(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory)
            item = source()
            (archive / item['name']).write_bytes(b'old')
            runner = FakeCopy(b'new')
            versions.recover([item], archive, runner=runner, dry_run=False)
            target = versions.version_path(item, {item['id']: item}, archive)
            target.write_bytes(b'CORRUPTED')
            report = versions.recover([item], archive, runner=runner, dry_run=False)
            self.assertEqual(report['downloaded'], 0)
            self.assertTrue(report['errors'])
            self.assertEqual(target.read_bytes(), b'CORRUPTED')
            self.assertEqual(len(runner.calls), 1)


if __name__ == '__main__':
    unittest.main()
