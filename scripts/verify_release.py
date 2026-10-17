#!/usr/bin/env python3
"""Check distributed file digests using only the Python standard library."""
from pathlib import Path
import hashlib
import json

def main():
    root = Path(__file__).resolve().parent.parent
    entries = json.loads((root / 'inventory.json').read_text())['files']
    for entry in entries:
        path = root / entry['path']
        if not path.is_file() or path.stat().st_size != entry['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise SystemExit('Missing or modified distributed file: ' + entry['path'])
    print(str(len(entries)) + ' distributed files verified')

if __name__ == '__main__':
    main()
