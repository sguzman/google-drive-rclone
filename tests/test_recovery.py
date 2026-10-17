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
                target = Path(command[5]); target.mkdir(parents=True, exist_ok=True)
                (target / 'resume.pdf').write_bytes({'1': a, '2': b, '3': c}[command[4]])
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
        paths = [rd.recovery_dir(item, {o['id']: o for o in items}, Path('/archive'))
                 for item in candidates]
        self.assertNotEqual(*paths)


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
            destination = rd.recovery_dir(items[0], {i['id']: i for i in items}, archive / '.rclone-duplicates')
            destination.mkdir(parents=True)
            (destination / 'img.jpg').write_bytes(b'bad')
            def cant_copy(command, **_kwargs):
                if command[4] == 'a':
                    self.fail('Should never overwrite unknown local state')
                folder = Path(command[5])
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'img.jpg').write_bytes(b'xyz')
                return FakeProcess()
            summary = rd.run_recovery(items, archive, runner=cant_copy)
            self.assertEqual(len(summary['errors']), 1)
            self.assertEqual((destination / 'img.jpg').read_bytes(), b'bad')

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
                directory = Path(command[5])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / 'duplicate').write_bytes(command[4].encode())
                return FakeProcess()
            summary = rd.run_recovery(items, Path(temp), runner=fake)
            self.assertEqual(summary['downloaded'], 1)
            self.assertEqual(summary['attempted'], 1)
            self.assertEqual(len(seen), 1)

    def test_scan_has_no_side_effects(self):
        items = [obj('a', 'x', 'root', b'a'), obj('b', 'x', 'root', b'b')]
        with tempfile.TemporaryDirectory() as temp:
            summary = rd.run_recovery(items, Path(temp), dry_run=True)
            self.assertEqual(summary['downloaded'], 0)
            self.assertFalse((Path(temp) / '.rclone-duplicates').exists())


if __name__ == '__main__':
    unittest.main()
