"""Native core portability preserves raw experience and uses primitive semantics."""
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))
from goblin_core import memory_cores
from goblin_core.vendor_aethermind.aethermind import core


def make_core(root):
    mind = core.AetherMind(root, create=True)
    first = mind.write_layer(type='discovery', body='The native write path owns the source.',
                             ctx='work', conf=.9, markers=[], author='test')
    mind.write_layer(type='correction', body='!Verify source ownership before changing it.',
                     ctx='work', conf=1.0, markers=['!'], author='test', corrects=[first.id])
    mind.write_texture('Quiet attention to the owning source. 漢字', 'work', '~', author='test')
    mind.write_event('observation', 'An observed event.', 'work', author='test')
    mind.archive([first.id], reason='Retain original experience as archived history')
    metadata = json.loads((root / '.aethermind/project.json').read_text())
    identity = {'schema_version': 1, 'project_id': metadata['project_id']}
    (root / '.aethermind/project.identity.json').write_text(json.dumps(identity))
    texture = root / '.aethermind/texture.aem'
    texture.write_bytes(texture.read_bytes().replace(b'\n', b'\r\n'))
    (root / '.aethermind' / 'unknown-extension.bin').write_bytes(b'\x00\xffnative extension')
    return mind


def read_bytes(root):
    result = {}
    for path in (root / '.aethermind').rglob('*'):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def package(path, files):
    manifest = {'schema': memory_cores.PACKAGE_SCHEMA, 'files': {}}
    for name, data in files.items():
        manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    with zipfile.ZipFile(path, 'w') as output:
        output.writestr('manifest.json', json.dumps(manifest))
        for name, data in files.items():
            output.writestr(name, data)


class MemoryCoreTests(unittest.TestCase):
    def test_round_trip_preserves_all_native_bytes_identity_relations_debt_and_texture(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'original'
            make_core(source)
            original = read_bytes(source)
            inspection = memory_cores.inspect_core(source)
            self.assertTrue(inspection['compatible'])
            self.assertTrue(inspection['healthy'], inspection['issues'])
            self.assertTrue(inspection['corrections'][0]['corrects'])
            self.assertTrue(inspection['unabsorbed_debt'])
            archive = base / 'core.zip'
            memory_cores.export_core(source, archive)
            archive_bytes = archive.read_bytes()
            imported = memory_cores.import_core(archive, base / 'cores')
            copied = Path(imported['project_root'])
            self.assertNotEqual(copied, source)
            self.assertEqual(read_bytes(copied), original)
            self.assertEqual(read_bytes(source), original)
            self.assertEqual(archive.read_bytes(), archive_bytes)
            self.assertEqual(imported['inspection']['currentness'], inspection['currentness'])
            self.assertEqual(imported['inspection']['corrections'], inspection['corrections'])
            self.assertEqual(imported['inspection']['unabsorbed_debt'], inspection['unabsorbed_debt'])
            self.assertEqual(imported['inspection']['texture'], inspection['texture'])
            self.assertEqual(imported['repairs'], [])
            with self.assertRaises(FileExistsError):
                memory_cores.export_core(source, archive)
            self.assertEqual(archive.read_bytes(), archive_bytes)

    def test_empty_native_directory_survives_export_and_import(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'source'
            (source / '.aethermind').mkdir(parents=True)
            memory_cores.export_core(source, base / 'empty.zip')
            result = memory_cores.import_core(base / 'empty.zip', base / 'cores')
            self.assertTrue(result['inspection']['healthy'])
            self.assertTrue(result['inspection']['compatible'])
            self.assertEqual(read_bytes(Path(result['project_root'])), {})

    def test_inspection_of_missing_store_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            absent = Path(temp) / 'absent'
            report = memory_cores.inspect_core(absent)
            self.assertFalse(report['store_exists'])
            self.assertFalse(report['compatible'])
            self.assertFalse(absent.exists())

    def test_unknown_runtime_version_with_current_format_reads_and_future_format_stays_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'source'
            make_core(source)
            metadata_path = source / '.aethermind/project.json'
            metadata = json.loads(metadata_path.read_text())
            metadata['runtime_version'] = 'future-runtime-build'
            metadata_path.write_text(json.dumps(metadata))
            self.assertTrue(memory_cores.inspect_core(source)['compatible'])
            metadata['format_version'] = 'future-format'
            metadata_path.write_text(json.dumps(metadata))
            original = read_bytes(source)
            archive = base / 'future.zip'
            memory_cores.export_core(source, archive)
            result = memory_cores.import_core(archive, base / 'cores')
            self.assertFalse(result['inspection']['compatible'])
            self.assertEqual(read_bytes(Path(result['project_root'])), original)
            self.assertEqual(read_bytes(source), original)

    def test_corrupt_derived_tree_is_retained_and_authored_core_remains_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'source'
            make_core(source)
            (source / '.aethermind/tree.aem').write_bytes(b'[[node]\ninvalid')
            archive = base / 'core.zip'
            memory_cores.export_core(source, archive)
            result = memory_cores.import_core(archive, base / 'cores')
            inspection = result['inspection']
            self.assertTrue(inspection['healthy'], inspection['issues'])
            self.assertTrue(inspection['derived_tree']['rebuild_pending'])
            self.assertEqual(inspection['derived_tree']['state'], 'unreadable')
            self.assertEqual(read_bytes(Path(result['project_root'])), read_bytes(source))
            self.assertEqual(result['repairs'], [])

    def test_corrupt_authored_ledger_is_reported_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            make_core(source)
            path = source / '.aethermind/layers.aem'
            with path.open('ab') as output:
                output.write(b'\n[[layer]]\nid = "broken"\nbody = "unfinished')
            before = read_bytes(source)
            report = memory_cores.inspect_core(source)
            self.assertFalse(report['healthy'])
            self.assertTrue(report['issues'])
            self.assertEqual(read_bytes(source), before)

    def test_native_writer_lock_protects_export_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'source'
            make_core(source)
            attempted = threading.Event()
            finished = threading.Event()
            errors = []
            real_lock = core._FileLock

            class ObservedLock(real_lock):
                def __enter__(self):
                    attempted.set()
                    return super().__enter__()

            def export():
                try:
                    memory_cores.export_core(source, base / 'copy.zip')
                except Exception as error:
                    errors.append(error)
                finally:
                    finished.set()

            with (source / '.aethermind/layers.aem').open('rb') as handle:
                with real_lock(handle):
                    with patch.object(core, '_FileLock', ObservedLock):
                        worker = threading.Thread(target=export)
                        worker.start()
                        self.assertTrue(attempted.wait(2))
                        self.assertFalse(finished.is_set())
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue((base / 'copy.zip').is_file())

    def test_source_change_during_export_is_detected_and_no_archive_is_published(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'source'
            make_core(source)
            real_files = memory_cores._files
            calls = 0

            def changed(store):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (store / 'unknown-extension.bin').write_bytes(b'concurrent replacement')
                return real_files(store)

            with patch.object(memory_cores, '_files', changed):
                with self.assertRaisesRegex(ValueError, 'changed during export'):
                    memory_cores.export_core(source, base / 'copy.zip')
            self.assertFalse((base / 'copy.zip').exists())

    def test_archive_rejects_traversal_symlink_and_bad_hash_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for name in ('../escape', '/absolute', '.aethermind/../escape', '.aethermind/./alias'):
                archive = base / 'unsafe.zip'
                package(archive, {name: b'original'})
                original = archive.read_bytes()
                with self.assertRaises(ValueError):
                    memory_cores.import_core(archive, base / 'cores')
                self.assertEqual(archive.read_bytes(), original)
            archive = base / 'symlink.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                item = zipfile.ZipInfo('.aethermind/link')
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(item, '../../escape')
            with self.assertRaises(ValueError):
                memory_cores.import_core(archive, base / 'cores')
            archive = base / 'bad-hash.zip'
            package(archive, {'.aethermind/layers.aem': b'original'})
            with zipfile.ZipFile(archive, 'r') as original:
                manifest = original.read('manifest.json')
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('manifest.json', manifest)
                output.writestr('.aethermind/layers.aem', b'changed')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                memory_cores.import_core(archive, base / 'cores')
            self.assertFalse((base / 'cores').exists())


if __name__ == '__main__':
    unittest.main()
