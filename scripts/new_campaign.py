#!/usr/bin/env python3
"""Prepare a new, empty acquisition workspace without running any workload."""
import argparse
import datetime
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--template', choices=['primary', 'completion'], default='primary')
    parser.add_argument('--campaign-id', default='fresh-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.campaign_id):
        raise ValueError('Use an ASCII campaign identifier with letters, digits, hyphens or underscores')
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError('New workspace must not exist: ' + str(output))
    def shell(*command):
        return subprocess.check_output([args.adb, '-s', args.serial, 'shell', *command], text=True).strip()
    package = 'vn.ictu.thermalbench'
    apk = shell('pm', 'path', package).split('package:')[1]
    digest = shell('sha256sum', apk).split()[0]
    firmware = shell('getprop', 'ro.build.fingerprint')
    version = re.search(r'versionName=([^\s]+)', shell('dumpsys', 'package', package))
    if not version or version[1] != '0.2.0':
        raise ValueError('Install the provided version 0.2.0 instrumentation before acquisition')
    output.mkdir(parents=True)
    for name in ['intake', 'runs', 'analysis', 'models', 'acquisition', 'scripts']:
        (output / name).mkdir()
    for path in (ROOT / 'acquisition_source/scripts').glob('*.py'):
        shutil.copy2(path, output / 'scripts' / path.name)
    shutil.copytree(ROOT / 'acquisition_source/profiling', output / 'profiling')
    (output / 'profiling/traces').mkdir()
    source = ROOT / 'studies' / ('unified_experiments_20261016' if args.template == 'primary' else 'completion_experiments_20261017_e')
    for name in ['protocol.json', 'planned_units.json', 'planned_schedule.json', 'models/manifest.json']:
        shutil.copy2(source / name, output / name)
    shutil.copy2(ROOT / 'requirements.txt', output / 'requirements.txt')
    old_prefix = json.loads((source / 'planned_units.json').read_text())[0]['id'].split('-')[0]
    for name in ['planned_units.json', 'planned_schedule.json']:
        path = output / name
        path.write_text(path.read_text().replace(old_prefix + '-', args.campaign_id + '-'))
    profile = output / 'profiling/capture.py'
    old_profile = re.search(r'rid = f"([^\"]+?)-profile-', profile.read_text())
    if old_profile:
        profile.write_text(profile.read_text().replace(old_profile[1] + '-', args.campaign_id + '-'))
    environment = dict(case_state=None, fold_state=None, support_surface=None, ambient_measured_c=None,
                       air_conditioner_setpoint_c=None, source='To be recorded before new acquisition')
    protocol = json.loads((output / 'protocol.json').read_text())
    protocol.update(campaign_id=args.campaign_id, created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), environment=environment)
    protocol.pop('explicit_retry_authorization', None)
    (output / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')
    (output / 'protocol.sha256').write_text(hashlib.sha256((output / 'protocol.json').read_bytes()).hexdigest() + '\n')
    (output / 'intake/build_identity.json').write_text(json.dumps(dict(new_apk_sha256=digest,
        scope='Fresh installed build; not a claim of identity to the recorded APK'), indent=2))
    (output / 'intake/fingerprint.txt').write_text(firmware + '\n')
    (output / 'intake/environment.json').write_text(json.dumps(environment, indent=2))
    print('Prepared', output, '; record conditions, run fresh preflight, then freeze inputs.')


if __name__ == '__main__':
    main()
