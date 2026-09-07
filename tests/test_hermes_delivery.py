import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('delivery', Path(__file__).resolve().parents[1] / 'hermes_delivery.py')
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class DeliveryTests(unittest.TestCase):
    def test_preserves_actual_content_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            root.mkdir()
            source = root / 'feature.py'
            source.write_text('dirty but deployed\n')
            bundle = Path(temp) / 'bundle'
            manifest = delivery.capture([root], bundle)
            self.assertEqual(delivery.verify(bundle), [])
            blob = bundle / 'blobs' / manifest['sources'][0]['files']['feature.py']
            self.assertEqual(blob.read_bytes(), source.read_bytes())
            source.write_text('lost registration\n')
            self.assertTrue(delivery.verify(bundle))
            source.unlink()
            (root / 'other.py').write_text('new\n')
            self.assertTrue(delivery.verify(bundle))

    def test_corrupt_backup_fails_even_with_unchanged_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            root.mkdir()
            (root / 'feature.py').write_text('same\n')
            bundle = Path(temp) / 'bundle'
            manifest = delivery.capture([root], bundle)
            (bundle / 'blobs' / manifest['sources'][0]['files']['feature.py']).write_text('corrupt')
            self.assertTrue(delivery.verify(bundle))

    def test_excludes_runtime_and_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.env').write_text('secret')
            (root / 'test_durations.json').write_text('{}')
            (root / '.venv').mkdir()
            (root / '.venv' / 'package.py').write_text('runtime')
            (root / 'feature.py').write_text('code')
            self.assertEqual(set(delivery.inventory(root)['files']), {'feature.py'})
            (root / 'link.py').symlink_to(root / 'feature.py')
            with self.assertRaises(ValueError):
                delivery.inventory(root)

    def test_refuses_empty_root_and_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                delivery.inventory(temp)
            with self.assertRaises(FileExistsError):
                delivery.capture([temp], temp)

    def test_runtime_gate_rejects_dependency_drift_and_unpatched_sqlite(self):
        baseline = dict(python=[3, 11, 15], packages=[['fastembed', '0.8.0']],
                        lazy_deps_source='/source/tools/lazy_deps.py',
                        memory_vault_spec=['fastembed>=0.8.0,<1'], sqlite='3.50.4')
        self.assertTrue(delivery.compare_runtime(baseline, baseline, True))
        candidate = dict(baseline, sqlite='3.51.3')
        self.assertEqual(delivery.compare_runtime(baseline, candidate, True), [])
        candidate['packages'] = []
        self.assertTrue(delivery.compare_runtime(baseline, candidate, True))
        for version in ('3.50.7', '3.44.6', '3.53.4'):
            self.assertEqual(delivery.compare_runtime(baseline, dict(baseline, sqlite=version), True), [])
        for version in ('3.50.4', '3.51.2', '3.45.0'):
            self.assertTrue(delivery.compare_runtime(baseline, dict(baseline, sqlite=version), True))
