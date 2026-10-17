#!/usr/bin/env python3
"""Run the frozen diagnostic grid after the supplementary device queue closes."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent
PREVIOUS = Path(json.loads((ROOT / 'intake/baseline-source.json').read_text())['baseline_study']).resolve()
sys.path.insert(0, str(ROOT / 'profiling'))
sys.path.insert(0, str(ROOT / 'scripts'))
from capture_backend import capture
from acquire import save, utc
from run_device import call, PACKAGE


def verify_inputs():
    for relative, expected in json.loads((ROOT / 'intake/frozen_hashes.json').read_text()).items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Frozen diagnostic input changed: ' + relative)


def guard():
    if (ROOT / 'PAUSE').exists() or (PREVIOUS / 'PAUSE').exists():
        raise InterruptedError('User pause requested')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', required=True)
    args = parser.parse_args()
    if (ROOT / 'profiling/state.json').exists():
        raise RuntimeError('Existing diagnostic attempt preserved; no automatic restart')
    results = []
    original = None
    def state(status, **extra):
        save(ROOT / 'profiling/state.json', dict(utc=utc(), pid=os.getpid(), status=status,
                                                results=results, **extra))
    try:
        state('queued_after_supplementary')
        while True:
            guard()
            acquisition = json.loads((PREVIOUS / 'acquisition/state.json').read_text())
            profile = json.loads((PREVIOUS / 'profiling/state.json').read_text())
            if acquisition['status'] != 'running' and profile['status'] not in {'running', 'queued_after_primary'}:
                break
            time.sleep(5)
        if acquisition['status'] != 'complete' or not (PREVIOUS / 'acquisition/baseline.json').exists():
            state('not_started_no_admitted_baseline', previous_status=acquisition['status'])
            return
        # Allow the previous restoration worker to finish before taking ownership.
        started = time.monotonic()
        while not (PREVIOUS / 'intake/display-setting-restoration.json').exists():
            guard()
            if time.monotonic() - started > 60:
                raise RuntimeError('Previous device cleanup has not reached a recorded outcome')
            time.sleep(1)
        lock_name = 'thermalbench-' + hashlib.sha256(args.serial.encode()).hexdigest()[:16] + '.lock'
        with (Path(tempfile.gettempdir()) / lock_name).open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            guard()
            verify_inputs()
            shutil.copy2(PREVIOUS / 'acquisition/baseline.json', ROOT / 'acquisition/baseline.json')
            save(ROOT / 'acquisition/state.json', dict(started_utc=utc(), pid=os.getpid(),
                 status='complete', phase='complete', completed=[], thermal_stops=[], excluded=[],
                 attempted=[], units=[], current_run=None, reference_skin=None,
                 scope='Diagnostic conditions are recorded in profiling state, outside all timing cohorts'))
            (BASE / 'ACTIVE_FOLLOWUP.txt').write_text(str(ROOT) + '\n')
            state('running', phase='waiting_connection')
            started = time.monotonic()
            while True:
                guard()
                devices = subprocess.run([args.adb, 'devices'], capture_output=True, text=True, timeout=15)
                if any(line.split() == [args.serial, 'device'] for line in devices.stdout.splitlines()):
                    break
                if time.monotonic() - started >= 1800:
                    raise RuntimeError('ADB did not return within the 30-minute connection bound')
                time.sleep(5)
            original = call(args.adb, args.serial, 'shell', 'settings', 'get', 'global', 'stay_on_while_plugged_in').strip()
            call(args.adb, args.serial, 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', '3')
            save(ROOT / 'intake/temporary-display-setting.json', dict(utc=utc(), original=original, temporary='3'))
            call(args.adb, args.serial, 'shell', 'am', 'force-stop', PACKAGE)
            call(args.adb, args.serial, 'shell', 'input', 'keyevent', 'KEYCODE_HOME')
            probe = subprocess.run([args.adb, '-s', args.serial, 'shell', 'perfetto', '--query'],
                                   capture_output=True, text=True, timeout=30)
            (ROOT / 'intake/perfetto-capabilities.txt').write_text(probe.stdout)
            save(ROOT / 'intake/perfetto-capabilities-status.json', dict(utc=utc(), returncode=probe.returncode,
                 stderr=probe.stderr, independently_verified_npu_operator_placement=False))
            models = {item['id']: item for item in json.loads((ROOT / 'models/manifest.json').read_text())}
            references = {}
            for item in json.loads((ROOT / 'protocol.json').read_text())['schedule']:
                guard()
                verify_inputs()
                model, backend = item['model'], item['backend']
                if backend != 'NPU' and model not in references:
                    results.append(dict(**item, outcome='unlaunched_no_control_start_reference'))
                    state('running', phase='recovery', **item)
                    continue
                state('running', phase='recovery', **item)
                def on_capture_start(run, start):
                    if backend == 'NPU':
                        references[model] = start
                    state('running', phase='capturing', run=run, **item)
                args.on_capture_start = on_capture_start
                try:
                    result = capture(args, 'fixed', models[model], backend, references.get(model))
                    if backend == 'NPU' and 'starting_state' in result:
                        references[model] = result['starting_state']
                    results.append(dict(**item, **result))
                except Exception as error:
                    results.append(dict(**item, outcome='capture_error', error=str(error)))
                state('running', phase='between_conditions', **item)
            state('complete', phase='complete')
    except InterruptedError as error:
        state('paused', error=str(error))
    except BaseException as error:
        state('stopped_with_error', error=str(error))
        raise
    finally:
        if original is not None:
            outcome = dict(utc=utc())
            try:
                current = call(args.adb, args.serial, 'shell', 'settings', 'get', 'global', 'stay_on_while_plugged_in').strip()
                if current == '3':
                    call(args.adb, args.serial, 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', original)
                observed = call(args.adb, args.serial, 'shell', 'settings', 'get', 'global', 'stay_on_while_plugged_in').strip()
                outcome.update(status='restored' if observed == original else 'external_change_preserved', observed=observed)
                call(args.adb, args.serial, 'shell', 'am', 'force-stop', PACKAGE, check=False)
            except Exception as error:
                outcome.update(status='restore_pending_device_unavailable', error=str(error))
            save(ROOT / 'intake/display-setting-restoration.json', outcome)


if __name__ == '__main__':
    main()
