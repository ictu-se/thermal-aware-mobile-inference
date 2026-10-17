#!/usr/bin/env python3
"""Verify installed identities, then collect fresh, excluded measurement pilots."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from acquire import Acquisition, classify, utc
from run_device import ROOT, PACKAGE, call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', required=True)
    args = parser.parse_args()
    lock_name = 'thermalbench-' + hashlib.sha256(args.serial.encode()).hexdigest()[:16] + '.lock'
    with open(Path(tempfile.gettempdir()) / lock_name, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = Acquisition(args)
        app.verify_identity()
        models = json.loads((ROOT / 'models/manifest.json').read_text())
        hashes = {}
        for model in models:
            path = f'/sdcard/Android/data/{PACKAGE}/files/models/' + model['filename']
            actual = call(args.adb, args.serial, 'shell', 'sha256sum', path, timeout=180).split()[0]
            if actual != model['sha256']:
                raise RuntimeError('Model changed: ' + model['id'])
            hashes[model['id']] = actual
        for name, command in {
            'thermal': ['dumpsys', 'thermalservice'],
            'battery': ['dumpsys', 'battery'],
            'power': ['dumpsys', 'power'],
            'processes': ['ps', '-A'],
            'firmware': ['getprop'],
            'screen': ['dumpsys', 'display'],
        }.items():
            (ROOT / 'intake' / (name + '-preflight.txt')).write_text(
                call(args.adb, args.serial, 'shell', *command)
            )
        reports = []
        jobs = [dict(run_id='u16-pilot-clock', mode='selftest', duration=0)]
        jobs.append(dict(run_id='u16-pilot-idle', mode='idle', duration=30))
        for model in models:
            for prefill, decode in [(128, 64), (512, 256)]:
                for monitoring in ['full', 'minimal']:
                    jobs.append(dict(
                        run_id=f"u16-pilot-{model['id']}-{prefill}-{decode}-{monitoring}",
                        mode='fixed', model=model, prefill=prefill, decode=decode,
                        monitoring=monitoring, duration=0,
                    ))
        for job in jobs:
            command = [
                sys.executable, str(ROOT / 'scripts/run_device.py'), '--adb', args.adb,
                '--serial', args.serial, '--run-id', job['run_id'], '--mode', job['mode'],
                '--duration', str(job['duration']), '--timeout', '180',
            ]
            if job['mode'] == 'fixed':
                command += [
                    '--model', f'/sdcard/Android/data/{PACKAGE}/files/models/' + job['model']['filename'],
                    '--prefill', str(job['prefill']), '--decode', str(job['decode']),
                    '--monitoring', job['monitoring'],
                ]
            subprocess.run(command, check=True)
            directory = ROOT / 'runs' / job['run_id']
            report = json.loads((directory / 'validation.json').read_text())
            if not report['passed']:
                raise RuntimeError('Pilot failed: ' + job['run_id'])
            if job['mode'] == 'fixed':
                classified = classify(directory, dict(
                    kind='pilot', mode='fixed', prefill_tokens=job['prefill'],
                    decode_tokens=job['decode'],
                ))
                if not classified['accepted_observations']:
                    raise RuntimeError('Pilot completion failed: ' + job['run_id'])
            reports.append(dict(run=job['run_id'], validation=report))
            call(args.adb, args.serial, 'shell', 'am', 'force-stop', PACKAGE)
        (ROOT / 'intake/pilot_acceptance.json').write_text(json.dumps(dict(
            passed=True, utc=utc(), verified_model_hashes=hashes, reports=reports,
            scope='Fresh clock, 30-second headroom/telemetry idle, both budgets and monitoring modes on both packages. All excluded from primary comparisons.',
        ), indent=2))


if __name__ == '__main__':
    main()
