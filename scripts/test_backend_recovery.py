"""Exercise transport-loss handling without touching a device."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parent.parent / 'diagnostic_source'
sys.path.insert(0, str(ROOT.parent / 'acquisition_source/scripts'))
spec = importlib.util.spec_from_file_location('diagnostic_capture', ROOT / 'profiling/capture_backend.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'acquisition').mkdir()
        self.protocol = dict(maximum_recovery_seconds=30, recovery_poll_seconds=5,
                             stable_seconds=10, maximum_sample_gap_seconds=10,
                             stable_window_span_hal_units=1.5, battery_minimum_percent=99,
                             baseline_comparison='upper_only', baseline_tolerances={'skin': 2, 'battery_hal': 2, 'nsp0': 8},
                             pair_tolerances={'skin': 1, 'battery_hal': 1, 'nsp0': 5})
        (self.root / 'protocol.json').write_text(json.dumps(self.protocol))
        (self.root / 'acquisition/baseline.json').write_text(json.dumps({'reference': {'skin': 30, 'battery_hal': 30, 'nsp0': 32}}))
        self.sample = dict(skin=30, battery_hal=30, nsp0=32, severity=0,
                           override=False, externally_powered=True, battery_level=100)
        self.args = SimpleNamespace(adb='unused', serial='unused')
        self.clock = Clock()
        self.patches = [patch.object(capture, 'ROOT', self.root), patch.object(capture, 'HERE', self.root),
                        patch.object(capture, 'time', self.clock)]
        for item in self.patches:
            item.start()
    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()
    def test_reconnection_requires_entire_new_window(self):
        observations = [self.sample, self.sample, RuntimeError('adb: device not found'),
                        self.sample, self.sample, self.sample]
        with patch.object(capture, 'current', side_effect=observations) as current:
            self.assertEqual(capture.recovery(self.args, 'test'), self.sample)
        self.assertEqual(self.clock.now, 25)
        self.assertEqual(current.call_count, 6)
    def test_missing_device_exhausts_original_deadline(self):
        with patch.object(capture, 'current', side_effect=RuntimeError('adb: device not found')):
            self.assertIsNone(capture.recovery(self.args, 'test'))
        self.assertEqual(self.clock.now, 30)
    def test_other_errors_are_not_hidden_as_transport_loss(self):
        with patch.object(capture, 'current', side_effect=RuntimeError('sensor parser failed')):
            with self.assertRaisesRegex(RuntimeError, 'sensor parser failed'):
                capture.recovery(self.args, 'test')
        self.assertEqual(self.clock.now, 0)


if __name__ == '__main__':
    unittest.main()
