#!/usr/bin/env python3
"""Read declared model metadata through a temporary instrumentation APK after device work."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time

TARGET = 'vn.ictu.thermalbench'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--study-root', type=Path, required=True)
    parser.add_argument('--baseline-study', type=Path, required=True)
    parser.add_argument('--after-grid', type=Path)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--aapt', type=Path, required=True)
    parser.add_argument('--resume-queued', action='store_true')
    args = parser.parse_args()
    root = args.study_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for directory in ['intake','results','models']:
        (root / directory).mkdir(exist_ok=True)
    state_path = root / 'state.json'
    if state_path.exists():
        previous = json.loads(state_path.read_text())
        if not args.resume_queued or previous['status'] != 'queued_after_device_arms' or (root / 'intake/verification.json').exists():
            raise FileExistsError('Existing metadata attempt preserved')
    if not (root / 'protocol.json').exists():
        (root / 'protocol.json').write_text(json.dumps(dict(
            created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            target_package=TARGET, purpose='Read declared model metadata through the installed pinned SDK',
            inference_executed=False, original_apk_must_remain_unchanged=True,
            baseline_study=args.baseline_study.name),indent=2)+'\n')
    def utc():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    def state(status, **extra):
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(utc=utc(), pid=os.getpid(), status=status, **extra), indent=2)+'\n')
        temporary.replace(state_path)
    def guard():
        for study in [root,args.baseline_study,args.after_grid]:
            if study and (study / 'PAUSE').exists():
                raise InterruptedError('User pause requested')
    def adb(*command, timeout=30):
        return subprocess.check_output([args.adb,'-s',args.serial,*command], text=True, stderr=subprocess.STDOUT, timeout=timeout)
    def shell(*command, timeout=30):
        return adb('shell', shlex.join(command), timeout=timeout)
    installed_test = None
    try:
        state('queued_after_device_arms')
        while True:
            guard()
            acquisition = json.loads((args.baseline_study / 'acquisition/state.json').read_text())
            profiling = json.loads((args.baseline_study / 'profiling/state.json').read_text())
            grid = json.loads((args.after_grid / 'profiling/state.json').read_text()) if args.after_grid else {'status':'complete'}
            if acquisition['status'] != 'running' and profiling['status'] not in {'running','queued_after_primary'} and grid['status'] not in {'running','queued_after_supplementary'}:
                break
            time.sleep(5)
        if args.after_grid:
            started = time.monotonic()
            while not (args.after_grid / 'intake/display-setting-restoration.json').exists():
                guard()
                if time.monotonic()-started > 60:
                    raise RuntimeError('Previous device cleanup did not reach a recorded outcome')
                time.sleep(1)
        badging = subprocess.check_output([str(args.aapt),'dump','badging',str(args.apk)], text=True, timeout=30)
        test_package = re.search(r"^package: name='([^']+)'", badging, re.M)
        if not test_package or not test_package[1].startswith('vn.ictu.thermalbenchprobe') or test_package[1] == TARGET:
            raise ValueError('Only the dedicated metadata instrumentation APK may be installed')
        test_package = test_package[1]
        lock_name = 'thermalbench-' + hashlib.sha256(args.serial.encode()).hexdigest()[:16] + '.lock'
        with (Path(tempfile.gettempdir()) / lock_name).open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            guard()
            state('verifying_installed_target')
            remote_apk = shell('pm','path',TARGET).strip().split('package:')[1]
            before = shell('sha256sum',remote_apk).split()[0]
            identity = json.loads((args.baseline_study / 'intake/build_identity.json').read_text())
            if before != identity['new_apk_sha256']:
                raise RuntimeError('Installed target differs from the acquisition binary')
            models = json.loads((args.baseline_study / 'models/manifest.json').read_text())
            observed_hashes, arguments = {}, []
            for model in models:
                if Path(model['filename']).name != model['filename']:
                    raise ValueError('Model filename must be a basename')
                path = '/sdcard/Android/data/'+TARGET+'/files/models/'+model['filename']
                digest = shell('sha256sum',path,timeout=180).split()[0]
                if digest != model['sha256']:
                    raise RuntimeError('Pinned model digest mismatch: '+model['id'])
                observed_hashes[model['id']] = digest
                arguments.append(dict(id=model['id'],path=path))
            shutil.copy2(args.baseline_study / 'models/manifest.json', root / 'models/manifest.json')
            (root / 'intake/verification.json').write_text(json.dumps(dict(utc=utc(), target_apk_before=before,
                test_apk_sha256=hashlib.sha256(args.apk.read_bytes()).hexdigest(), models=observed_hashes),indent=2)+'\n')
            state('installing_metadata_instrumentation')
            (root / 'intake/install.txt').write_text(adb('install','-r','-t',str(args.apk),timeout=90))
            installed_test = test_package
            if shell('sha256sum',remote_apk).split()[0] != before:
                raise RuntimeError('Target APK changed during instrumentation installation')
            listed = shell('pm','list','instrumentation')
            component = next(line.split()[0].split(':',1)[1] for line in listed.splitlines()
                             if line.startswith('instrumentation:'+test_package+'/') and '(target='+TARGET+')' in line)
            guard()
            state('reading_model_metadata')
            output = shell('am','instrument','-w','-e','models_json',json.dumps(arguments,separators=(',',':')),component,timeout=180)
            (root / 'results/instrumentation-output.txt').write_text(output)
            matched = re.search(r'^INSTRUMENTATION_RESULT: metadata_json=(.*)$',output,re.M)
            if not matched:
                raise RuntimeError('Instrumentation returned no structured model metadata')
            result = json.loads(matched[1])
            after = shell('sha256sum',remote_apk).split()[0]
            if after != before:
                raise RuntimeError('Target APK changed during metadata extraction')
            result.update(target_apk_unchanged=True, target_apk_sha256=after,
                          runtime='LiteRT-LM 0.18.0', inference_executed=False)
            (root / 'results/model-metadata.json').write_text(json.dumps(result,indent=2)+'\n')
            state('complete', observations=len(result.get('observations',[])), target_apk_unchanged=True,
                  inference_executed=False)
    except InterruptedError as error:
        state('paused',error=str(error))
    except BaseException as error:
        state('failed',error=str(error))
        raise
    finally:
        if installed_test:
            try:
                result = adb('uninstall',installed_test,timeout=45)
                cleanup = dict(utc=utc(),status='test_package_removed',output=result)
            except Exception as error:
                cleanup = dict(utc=utc(),status='cleanup_pending',error=str(error))
            (root / 'intake/cleanup.json').write_text(json.dumps(cleanup,indent=2)+'\n')


if __name__ == '__main__':
    main()
