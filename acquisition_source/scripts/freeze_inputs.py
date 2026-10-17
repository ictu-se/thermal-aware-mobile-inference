#!/usr/bin/env python3
"""Freeze reviewed new protocol inputs after a successful fresh pilot collection."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    target = ROOT / 'intake/frozen_hashes.json'
    if target.exists() or (ROOT / 'acquisition/state.json').exists():
        raise FileExistsError('Existing frozen or acquired inputs preserved')
    pilots = json.loads((ROOT / 'intake/pilot_acceptance.json').read_text())
    if not pilots['passed']:
        raise RuntimeError('Fresh preflight pilots must pass first')
    paths = [ROOT / name for name in ['protocol.json', 'planned_units.json', 'planned_schedule.json',
        'models/manifest.json', 'profiling/capture.py', 'profiling/config.txt', 'requirements.txt', 'intake/build_identity.json', 'intake/fingerprint.txt',
        'intake/environment.json', 'intake/pilot_acceptance.json']]
    paths.extend(sorted((ROOT / 'scripts').glob('*.py')))
    target.write_text(json.dumps({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in paths}, indent=2) + '\n')
    print('Frozen', len(paths), 'new-campaign inputs')

if __name__ == '__main__':
    main()
