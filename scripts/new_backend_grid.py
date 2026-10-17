#!/usr/bin/env python3
"""Prepare an empty, independently identified requested-backend diagnostic workspace."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-study', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--template', choices=['backend','hardware'], default='backend')
    args = parser.parse_args()
    baseline, root = args.baseline_study.resolve(), args.workspace.resolve()
    package = Path(__file__).resolve().parents[1]
    source = package / ('diagnostic_source' if args.template == 'backend' else 'hardware_source')
    if root.exists():
        raise FileExistsError('Choose a new workspace; existing observations are preserved')
    state = json.loads((baseline / 'acquisition/state.json').read_text())
    if state['status'] != 'complete' or not (baseline / 'acquisition/baseline.json').exists():
        raise ValueError('The source acquisition must be complete with an admitted idle baseline')
    expected_models = {'fastvlm-sm8750', 'gemma4-sm8750'}
    observed_models = json.loads((baseline / 'models/manifest.json').read_text())
    if {item['id'] for item in observed_models} != expected_models:
        raise ValueError('This grid prescribes the two pinned model packages')
    pinned_models = json.loads((package / 'studies/unified_experiments_20261016/models/manifest.json').read_text())
    if {item['id']:item['sha256'] for item in observed_models} != {item['id']:item['sha256'] for item in pinned_models}:
        raise ValueError('Model digests must match the pinned diagnostic packages')
    for directory in ['scripts','models','intake','acquisition','analysis','runs','profiling/traces','profiling/tools']:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in ['run_device.py','acquire.py']:
        shutil.copy2(package / 'acquisition_source/scripts' / name, root / 'scripts' / name)
    for relative in ['scripts/run_backend_diagnostics.py','profiling/capture_backend.py','profiling/config.txt','protocol.json']:
        shutil.copy2(source / relative, root / relative)
    shutil.copy2(package / 'acquisition_source/profiling/tools/trace_processor', root / 'profiling/tools/trace_processor')
    shutil.copy2(package / 'requirements.txt', root / 'requirements.txt')
    for relative in ['models/manifest.json','intake/build_identity.json','intake/fingerprint.txt','intake/environment.json']:
        shutil.copy2(baseline / relative, root / relative)
    prefix = ('backend-' if args.template == 'backend' else 'hardware-') + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    protocol = json.loads((root / 'protocol.json').read_text())
    capture = root / 'profiling/capture_backend.py'
    capture.write_text(capture.read_text().replace(protocol.get('run_prefix','backend17')+'-', prefix + '-'))
    protocol.update(campaign_id=prefix, run_prefix=prefix, baseline_source=baseline.name,
                    created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (root / 'protocol.json').write_text(json.dumps(protocol, indent=2) + '\n')
    (root / 'intake/baseline-source.json').write_text(json.dumps({'baseline_study': str(baseline)}, indent=2) + '\n')
    (root / 'planned_schedule.json').write_text('[]\n')
    paths = [root / 'protocol.json',root / 'models/manifest.json',root / 'profiling/config.txt',root / 'profiling/capture_backend.py',*sorted((root / 'scripts').glob('*.py'))]
    (root / 'intake/frozen_hashes.json').write_text(json.dumps({str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, indent=2) + '\n')
    print('Prepared new workspace; no device workload has started:', root)


if __name__ == '__main__':
    main()
