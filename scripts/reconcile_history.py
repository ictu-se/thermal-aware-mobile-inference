#!/usr/bin/env python3
"""Recompute historical counts from published component-presence and ledger evidence."""
import collections
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def main():
    inventory = list(csv.DictReader((ROOT / 'historical/container_membership.csv').open()))
    ledger = list(csv.DictReader((ROOT / 'historical/ledger_identifiers.csv').open()))
    scope_counts = {}
    for scope in sorted({row['scope'] for row in inventory}):
        rows = [row for row in inventory if row['scope'] == scope]
        scope_counts[scope] = dict(containers=len(rows),
            six_components=sum(row['six_core_components'] == 'True' for row in rows),
            seven_components=sum(row['seven_components'] == 'True' for row in rows))
    frequency = collections.Counter(row['run_id'] for row in ledger)
    seven = {row['run'] for row in inventory if row['seven_components'] == 'True'}
    report = dict(scopes=scope_counts, current_containers=len(inventory),
        six_components=sum(row['six_core_components'] == 'True' for row in inventory),
        seven_components=len(seven),
        derived_summaries=sum(row['derived_summary'] == 'True' for row in inventory),
        ledger_rows=len(ledger), ledger_distinct_containers=len(frequency),
        repeated_row_excess=len(ledger) - len(frequency),
        ledger_containers_with_seven_components=len(set(frequency) & seven),
        ledger_containers_without_seven_components=sorted(set(frequency) - seven),
        repeated_ledger_identifiers={key: value for key, value in frequency.items() if value > 1})
    expected = json.loads((ROOT / 'historical/accounting.json').read_text())
    for key, value in report.items():
        if expected[key] != value:
            raise ValueError('Reconstruction mismatch: ' + key)
    output = ROOT / 'generated'
    output.mkdir(exist_ok=True)
    (output / 'historical_accounting.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Verified', len(inventory), 'containers and', len(ledger), 'ledger entries')

if __name__ == '__main__':
    main()
