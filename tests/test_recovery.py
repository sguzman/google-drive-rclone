import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recover_duplicates as rd


def obj(oid, name, parent, data=None, mime='application/octet-stream'):
    result = {'id': oid, 'name': name, 'parents': [parent], 'mimeType': mime}
    if data is not None:
        result['md5Checksum'] = hashlib.md5(data).hexdigest()
        result['size'] = str(len(data))
    return result


class FakeProcess:
    def __init__(self, rc=0, stderr=''):
        self.returncode = rc
        self.stderr = stderr


class RecoveryTests(unittest.TestCase):
    def test_colliding_names_preserve_distinct_bytes_and_skip_same_hash(self):
        a, b, c = b'Aaaa', b'Bbbb', b'Aaaa'
        items = [obj('1', 'resume.pdf', 'root', a), obj('2', 'resume.pdf', 'root', b),
                 obj('3', 'resume.pdf', 'root', c)]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'resume.pdf').write_bytes(a)
            commands = []
            def runner(command, **kwargs):
                commands.append(command)
                target = Path(command[5]); target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes({'1': a, '2': b, '3': c}[command[4]])
                return FakeProcess()
            summary = rd.run_recovery(items, base, runner=runner)
            self.assertEqual(summary['already_archived'], 1)
            self.assertEqual(summary['identical_source'], 1)
            self.assertEqual(summary['downloaded'], 1)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][4], '2')
            self.assertEqual(rd.run_recovery(items, base, runner=runner)['already_recovered'], 1)
            self.assertEqual(len(commands), 1)
            self.assertEqual((base / 'resume.pdf').read_bytes(), a)

    def test_duplicate_folders_include_unique_child_names(self):
        items = [obj('f1', 'Photos', 'root', mime=rd.FOLDER),
                 obj('f2', 'Photos', 'root', mime=rd.FOLDER),
                 obj('first', 'unique1.jpg', 'f1', b'a'),
                 obj('second', 'unique2.jpg', 'f2', b'b')]
        _, _, candidates, folder_ids = rd.plan(items)
        self.assertEqual(folder_ids, {'f1', 'f2'})
        self.assertEqual({item['id'] for item in candidates}, {'first', 'second'})
        paths = [rd.compact_destination(item, {o['id']: o for o in items},
                                        folder_ids, Path('/archive'))
                 for item in candidates]
        self.assertNotEqual(*paths)
        self.assertNotEqual(paths[0].parent, paths[1].parent)
        self.assertEqual(paths[0].parent.parent, paths[1].parent.parent)


    def test_export_name_collision_with_regular_file(self):
        doc = obj('native', 'Resume', 'root', mime='application/vnd.google-apps.document')
        file = obj('binary', 'Resume.docx', 'root', b'binary')
        _, _, candidates, _ = rd.plan([doc, file])
        self.assertEqual({o['id'] for o in candidates}, {'native', 'binary'})
        self.assertEqual(rd.output_name(doc), 'Resume.docx')

    def test_no_hash_never_infers_equal_content(self):
        items = [obj('a', 'Untitled document', 'root', mime='application/vnd.google-apps.document'),
                 obj('b', 'Untitled document', 'root', mime='application/vnd.google-apps.document')]
        _, _, candidates, _ = rd.plan(items)
        self.assertEqual(len(list(rd.unique_candidates(candidates))), 2)

    def test_existing_conflicting_sidecar_never_overwritten(self):
        items = [obj('a', 'img.jpg', 'root', b'abc'), obj('b', 'img.jpg', 'root', b'xyz')]
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp)
            destination = rd.compact_destination(items[0],
                {i['id']: i for i in items}, set(), archive / '.rclone-duplicates')
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b'bad')
            def cant_copy(command, **_kwargs):
                if command[4] == 'a':
                    self.fail('Should never overwrite unknown local state')
                target = Path(command[5])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'xyz')
                return FakeProcess()
            summary = rd.run_recovery(items, archive, runner=cant_copy)
            self.assertEqual(len(summary['errors']), 1)
            self.assertEqual(destination.read_bytes(), b'bad')

    def test_legacy_recovery_remains_intact_and_detected(self):
        items = [obj('a', 'photo.jpg', 'root', b'aaa'),
                 obj('b', 'photo.jpg', 'root', b'bbb')]
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp)
            directory = rd.legacy_recovery_dir(items[0],
                {i['id']: i for i in items}, archive / '.rclone-duplicates')
            directory.mkdir(parents=True)
            (directory / 'photo.jpg').write_bytes(b'aaa')
            (directory / 'recovered.json').write_text(__import__('json').dumps({
                'id': 'a', 'filename': 'photo.jpg', 'local_md5':
                    hashlib.md5(b'aaa').hexdigest()}))
            def runner(command, **_kwargs):
                if command[4] == 'a':
                    self.fail('Legacy file should not be downloaded again')
                destination = Path(command[5])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b'bbb')
                return FakeProcess()
            result = rd.run_recovery(items, archive, runner=runner)
            self.assertEqual(result['already_recovered'], 1)
            self.assertTrue((directory / 'photo.jpg').is_file())
            self.assertTrue((directory / 'recovered.json').is_file())

    def test_compact_layout_has_no_one_file_directory(self):
        items = [obj('f', 'Photos', 'root', mime=rd.FOLDER),
                 obj('a', 'nice.jpg', 'f', b'abc'),
                 obj('b', 'nice.jpg', 'f', b'def')]
        by_id = {i['id']: i for i in items}
        target = rd.compact_destination(items[1], by_id, set(), Path('/archive'))
        self.assertEqual(target.parent, Path('/archive/Photos'))
        self.assertTrue(target.name.startswith('nice--'))
        self.assertTrue(target.name.endswith('.jpg'))

    def test_safe_paths(self):
        for name in ('../x', '/etc/passwd', '..', 'bad\\name', '\x00foo'):
            piece = rd.safe_name(name)
            self.assertNotIn('/', piece)
            self.assertNotIn('\\', piece)
            self.assertNotIn('\x00', piece)
            self.assertNotIn(piece, ('.', '..'))


    def test_scan_short_output_and_accurate_count(self):
        import io
        from contextlib import redirect_stdout
        items = [obj(str(i), 'duplicate', 'root', str(i).encode()) for i in range(24)]
        with tempfile.TemporaryDirectory() as temp:
            stream = io.StringIO()
            with redirect_stdout(stream):
                summary = rd.run_recovery(items, Path(temp), dry_run=True)
            self.assertEqual(summary['candidate_files'], 24)
            self.assertEqual(summary['would_download'], 24)
            self.assertEqual(stream.getvalue().count('WOULD RECOVER:'), 8)

    def test_recover_only_one_by_default(self):
        items = [obj(str(i), 'duplicate', 'root', str(i).encode()) for i in range(3)]
        with tempfile.TemporaryDirectory() as temp:
            seen = []
            def fake(command, **kwargs):
                seen.append(command[4])
                destination = Path(command[5])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(command[4].encode())
                return FakeProcess()
            summary = rd.run_recovery(items, Path(temp), runner=fake)
            self.assertEqual(summary['downloaded'], 1)
            self.assertEqual(summary['attempted'], 1)
            self.assertEqual(len(seen), 1)


    def test_probe_selects_small_distinct_binary_and_bounds_size(self):
        items = [
            obj('folder1', 'Photos', 'root', mime=rd.FOLDER),
            obj('folder2', 'Photos', 'root', mime=rd.FOLDER),
            obj('large', 'large.bin', 'folder1', b'A' * 104),
            obj('small', 'small.bin', 'folder2', b'B' * 5),
            obj('tiny', 'tiny.bin', 'folder1', b'C' * 2),
            obj('duplicate-name-a', 'dup.bin', 'root', b'XX'),
            obj('duplicate-name-b', 'dup.bin', 'root', b'YY'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp)
            (local / 'Photos').mkdir()
            (local / 'Photos' / 'tiny.bin').write_bytes(b'C' * 2)
            choice = rd.probe_candidate(items, local, max_bytes=100)
            self.assertEqual(choice['id'], 'duplicate-name-a')
            self.assertIsNone(rd.probe_candidate(items, local, max_bytes=1))

    def test_probe_only_id_and_bandwidth(self):
        items = [obj('one', 'collide', 'root', b'X'),
                 obj('two', 'collide', 'root', b'Y')]
        with tempfile.TemporaryDirectory() as temp:
            seen = []
            def fake(command, **kwargs):
                seen.append(command)
                destination = Path(command[5])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b'Y')
                return FakeProcess()
            summary = rd.run_recovery(items, Path(temp), runner=fake,
                                      only_ids={'two'}, bandwidth='64K')
            self.assertEqual(summary['downloaded'], 1)
            self.assertEqual(seen[0][4], 'two')
            self.assertIn('--bwlimit=64K', seen[0])


    def test_auto_pass_is_bounded_by_bytes_and_count(self):
        items = [obj('a', 'same', 'root', b'A' * 4),
                 obj('b', 'same', 'root', b'B' * 4),
                 obj('c', 'same', 'root', b'C' * 4)]
        with tempfile.TemporaryDirectory() as temp:
            seen = []
            def fake(cmd, **kwargs):
                seen.append(cmd[4])
                dest = Path(cmd[5])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(cmd[4].upper().encode() * 4)
                return FakeProcess()
            result = rd.run_recovery(items, Path(temp), runner=fake,
                                     max_downloads=25, max_total_bytes=8,
                                     binary_only=True)
            self.assertEqual(result['downloaded'], 2)
            self.assertEqual(result['planned_bytes'], 8)
            self.assertEqual(result['skipped_budget'], 1)
            self.assertEqual(len(seen), 2)

    def test_auto_skips_hashless_but_normal_mode_keeps_it(self):
        native = obj('a', 'Title', 'root', mime='application/vnd.google-apps.document')
        binary = obj('b', 'Title.docx', 'root', b'binary')
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            def fake(cmd, **kwargs):
                paths.append(cmd[4])
                dest = Path(cmd[5])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b'binary')
                return FakeProcess()
            result = rd.run_recovery([native, binary], Path(temp), runner=fake,
                                     binary_only=True, max_downloads=1)
            self.assertEqual(result['skipped_unverifiable'], 1)
            self.assertEqual(result['downloaded'], 1)
            self.assertEqual(paths, ['b'])

    def test_scan_has_no_side_effects(self):
        items = [obj('a', 'x', 'root', b'a'), obj('b', 'x', 'root', b'b')]
        with tempfile.TemporaryDirectory() as temp:
            summary = rd.run_recovery(items, Path(temp), dry_run=True)
            self.assertEqual(summary['downloaded'], 0)
            self.assertFalse((Path(temp) / '.rclone-duplicates').exists())


if __name__ == '__main__':
    unittest.main()
