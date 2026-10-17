"""Check fresh identities and input freezing without connecting to a device."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
BASE = HERE.parent


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        package = BASE
        shutil.copytree(package / 'acquisition_source', self.root / 'acquisition_source')
        shutil.copy2(package / 'requirements.txt', self.root / 'requirements.txt')
        for study in ['unified_experiments_20261016', 'completion_experiments_20261017_e']:
            for relative in ['protocol.json','planned_units.json','planned_schedule.json','models/manifest.json']:
                target = self.root / 'studies' / study / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BASE / 'studies' / study / relative, target)
        self.bootstrap = load(HERE / 'new_campaign.py')
        self.bootstrap.ROOT = self.root
        self.output = self.root / 'fresh-workspace'
    def tearDown(self):
        self.temp.cleanup()
    def run_bootstrap(self):
        def device(command, **kwargs):
            if 'sha256sum' in command:
                return 'a'*64 + ' /fixture/base.apk\n'
            if 'getprop' in command:
                return 'fixture-firmware\n'
            if 'dumpsys' in command:
                return 'versionName=0.2.0\n'
            return 'package:/fixture/base.apk\n'
        argv = ['new_campaign.py','--output',str(self.output),'--serial','fixture-device',
                '--template','completion','--campaign-id','fresh-fixture']
        with patch.object(sys, 'argv', argv), patch.object(self.bootstrap.subprocess, 'check_output', side_effect=device):
            self.bootstrap.main()
    def test_new_ids_and_no_inherited_environment_observations(self):
        self.run_bootstrap()
        protocol = json.loads((self.output / 'protocol.json').read_text())
        self.assertEqual(protocol['campaign_id'], 'fresh-fixture')
        self.assertIsNone(protocol['environment']['air_conditioner_setpoint_c'])
        self.assertIsNone(protocol['environment']['ambient_measured_c'])
        units = json.loads((self.output / 'planned_units.json').read_text())
        self.assertEqual(len(units), 3)
        self.assertTrue(all(item['run_id'].startswith('fresh-fixture-') for unit in units for item in unit['members']))
        self.assertIn('fresh-fixture-profile-', (self.output / 'profiling/capture.py').read_text())
        self.assertFalse((self.output / 'acquisition/state.json').exists())
    def test_existing_workspace_is_preserved(self):
        self.run_bootstrap()
        original = (self.output / 'protocol.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_bootstrap()
        self.assertEqual(original, (self.output / 'protocol.json').read_bytes())
    def test_freeze_includes_diagnostic_configuration(self):
        self.run_bootstrap()
        (self.output / 'intake/pilot_acceptance.json').write_text('{"passed":true}')
        freeze = load(BASE / 'acquisition_source/scripts/freeze_inputs.py')
        freeze.ROOT = self.output
        freeze.main()
        manifest = json.loads((self.output / 'intake/frozen_hashes.json').read_text())
        self.assertIn('profiling/capture.py', manifest)
        self.assertIn('profiling/config.txt', manifest)
        self.assertIn('requirements.txt', manifest)


if __name__ == '__main__':
    unittest.main()
