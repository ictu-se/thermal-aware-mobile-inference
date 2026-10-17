#!/usr/bin/env python3
"""Run independent diagnostic traces after the locked measurement campaign."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'profiling'
sys.path.insert(0, str(ROOT / 'scripts'))
from acquire import Acquisition, current, save, utc, window_ok
from run_device import PACKAGE, call


def state(**data):
    save(HERE / 'state.json', dict(utc=utc(), pid=os.getpid(), **data))


def guard():
    if (ROOT / 'PAUSE').exists():
        raise InterruptedError('Pause requested')


def recovery(args, name):
    protocol = json.loads((ROOT / 'protocol.json').read_text())
    baseline = json.loads((ROOT / 'acquisition/baseline.json').read_text())['reference']
    started = time.monotonic()
    window = []
    with (HERE / (name + '-recovery.jsonl')).open('w') as log:
        while time.monotonic() - started < protocol['maximum_recovery_seconds']:
            guard()
            sample = current(args.adb, args.serial)
            now = time.monotonic()
            window.append((now, sample))
            while len(window) > 1 and window[1][0] <= now - protocol['stable_seconds']:
                window.pop(0)
            eligible = window_ok(window, protocol, baseline)
            log.write(json.dumps(dict(utc=utc(), eligible=eligible, **sample)) + '\n')
            log.flush()
            if eligible:
                return sample
            time.sleep(protocol['recovery_poll_seconds'])
    return None


def capture(args, mode, model):
    rid = f"c17d-profile-{mode}-{model['id']}"
    start = recovery(args, rid)
    if start is None:
        return dict(run=rid, outcome='prelaunch_recovery_timeout')
    Acquisition(args).verify_identity()
    remote_config = '/data/misc/perfetto-configs/' + rid + '.txt'
    remote_trace = '/data/misc/perfetto-traces/' + rid + '.pftrace'
    call(args.adb, args.serial, 'push', str(HERE / 'config.txt'), remote_config)
    trace_log = (HERE / (rid + '-capture.txt')).open('w')
    tracer = subprocess.Popen(
        [args.adb, '-s', args.serial, 'shell', 'perfetto', '--txt',
         '-c', remote_config, '-o', remote_trace],
        stdout=trace_log, stderr=subprocess.STDOUT,
    )
    report = dict(run=rid, starting_state=start)
    try:
        time.sleep(2)
        if tracer.poll() is not None:
            raise RuntimeError('Perfetto ended before application launch')
        command = [
            sys.executable, str(ROOT / 'scripts/run_device.py'), '--adb', args.adb,
            '--serial', args.serial, '--run-id', rid, '--mode', mode,
            '--model', f'/sdcard/Android/data/{PACKAGE}/files/models/' + model['filename'],
            '--prefill', '128', '--decode', '64', '--iterations', '3',
            '--warmup', '1' if mode == 'natural' else '0', '--timeout', '180',
        ]
        with (HERE / (rid + '-application.txt')).open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        tracer.wait(timeout=40)
        if tracer.returncode:
            raise RuntimeError('Perfetto capture failed')
        target = HERE / 'traces' / (rid + '.pftrace')
        call(args.adb, args.serial, 'pull', remote_trace, str(target))
        validation_path = ROOT / 'runs' / rid / 'validation.json'
        validation = json.loads(validation_path.read_text()) if validation_path.exists() else {}
        report.update(
            outcome='captured' if result.returncode == 0 and validation.get('passed') else 'application_or_validation_failure',
            trace_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            trace_bytes=target.stat().st_size, application_validation=validation,
            independent_npu_operator_placement_verified=False,
        )
        queries = {
            'phases': "SELECT s.ts,s.dur,s.name,t.name AS thread_name FROM slice s JOIN thread_track tt ON tt.id=s.track_id JOIN thread t ON t.utid=tt.utid JOIN process p ON p.upid=t.upid WHERE p.name='vn.ictu.thermalbench' AND (s.name GLOB 'engine_*' OR s.name GLOB 'fixed_*') ORDER BY s.ts",
            'app_cpu': "SELECT p.name,count(*) AS scheduler_slices,sum(sc.dur)/1e6 AS observed_cpu_ms FROM sched sc JOIN thread t ON t.utid=sc.utid JOIN process p ON p.upid=t.upid WHERE p.name='vn.ictu.thermalbench' AND sc.dur>=0 GROUP BY p.name",
            'loss_stats': "SELECT name,idx,value FROM stats WHERE value>0 AND (name GLOB '*loss*' OR name GLOB '*overrun*' OR name GLOB '*drop*')",
            'frequency': "SELECT ct.name,count(*) AS samples,min(c.value) AS minimum,max(c.value) AS maximum FROM counter c JOIN counter_track ct ON ct.id=c.track_id WHERE ct.name GLOB '*freq*' GROUP BY ct.name",
        }
        report['query_returncodes'] = {}
        for name, sql in queries.items():
            result = subprocess.run(
                [sys.executable, str(HERE / 'tools/trace_processor'), str(target), '-Q', sql],
                capture_output=True, text=True, timeout=120,
            )
            (HERE / (rid + '-' + name + '.csv')).write_text(result.stdout)
            (HERE / (rid + '-' + name + '-stderr.txt')).write_text(result.stderr)
            report['query_returncodes'][name] = result.returncode
        return report
    finally:
        if tracer.poll() is None:
            tracer.terminate()
            tracer.wait(timeout=10)
        trace_log.close()
        call(args.adb, args.serial, 'shell', 'am', 'force-stop', PACKAGE, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--modes', nargs='+', choices=['fixed', 'natural'], default=['fixed'])
    args = parser.parse_args()
    results = []
    try:
        state(status='queued_after_primary')
        while True:
            guard()
            primary = json.loads((ROOT / 'acquisition/state.json').read_text())
            if primary['status'] == 'complete':
                break
            if primary['status'] != 'running':
                state(status='not_started_primary_incomplete', primary_status=primary['status'])
                return
            time.sleep(15)
        lock_name = 'thermalbench-' + hashlib.sha256(args.serial.encode()).hexdigest()[:16] + '.lock'
        with open(Path(tempfile.gettempdir()) / lock_name, 'w') as lock:
            while True:
                guard()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(5)
            for mode in args.modes:
                for model in json.loads((ROOT / 'models/manifest.json').read_text()):
                    guard()
                    state(status='running', mode=mode, model=model['id'], results=results)
                    try:
                        result = capture(args, mode, model)
                    except Exception as error:
                        result = dict(mode=mode, model=model['id'], outcome='capture_error', error=str(error))
                    results.append(result)
            state(status='complete', results=results)
    except InterruptedError as error:
        state(status='paused', error=str(error), results=results)
    except BaseException as error:
        state(status='stopped_with_error', error=str(error), results=results)
        raise


if __name__ == '__main__':
    main()
