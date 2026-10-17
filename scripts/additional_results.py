#!/usr/bin/env python3
"""Reconstruct separate retry and requested-backend diagnostics from retained observations."""
import argparse
import csv
import json
from pathlib import Path
import statistics


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def csvrows(path):
    if not path.exists():
        return []
    with path.open() as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows, columns):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def profile(root, cohort, item):
    run = item.get('run')
    model = item.get('model') or ('gemma4-sm8750' if 'gemma' in str(run) else 'fastvlm-sm8750')
    usable = item.get('outcome') == 'captured' or bool(item.get('trace_sha256') and item.get('trace_bytes'))
    phases = csvrows(root / 'profiling' / (str(run) + '-phases.csv'))
    cpu = csvrows(root / 'profiling' / (str(run) + '-app_cpu.csv'))
    phases_available = usable and item.get('query_returncodes', {}).get('phases') == 0
    cpu_available = usable and item.get('query_returncodes', {}).get('app_cpu') == 0 and bool(cpu)
    return dict(cohort=cohort, run=run, model=model, backend=item.get('backend', 'NPU'),
                outcome=item['outcome'], phase_slices=len(phases) if phases_available else None,
                trace_available=usable,
                complete_phase_slices=sum(int(row['dur']) >= 0 for row in phases) if phases_available else None,
                application_cpu_available=cpu_available,
                observed_application_cpu_ms=float(cpu[0]['observed_cpu_ms']) if cpu_available else None,
                independent_npu_operator_placement_verified=False)


def condition_summaries(backend, backend_state, cohort):
    profiles = []
    conditions, libraries = [], []
    observed_conditions = {(item['model'], item['backend']): item for item in backend_state.get('results', [])}
    backend_protocol = read(backend / 'protocol.json')
    for planned in backend_protocol['schedule']:
        item = observed_conditions.get((planned['model'], planned['backend']), dict(**planned, outcome='unlaunched_after_stop'))
        item = dict(item)
        item['run'] = item.get('run') or f"{backend_protocol.get('run_prefix','backend17')}-{planned['backend'].lower()}-{planned['model']}"
        value = profile(backend, cohort, item)
        profiles.append(value)
        directory = backend / 'runs' / str(item.get('run'))
        all_results = lines(directory / 'results.jsonl')
        measurements = [row for row in all_results if row.get('phase') == 'measure' and row.get('status') == 'ok']
        events_path = directory / 'events.jsonl'
        started_calls = sum(row.get('event') == 'request_start' and row.get('phase') == 'measure' for row in lines(events_path)) if events_path.exists() else None
        errors = '\n'.join(str(row.get('error','')) for row in all_results if row.get('status') == 'error')
        validation, done = read(directory / 'validation.json', {}), read(directory / 'done.json', {})
        metadata = read(directory / 'metadata.json', {})
        valid = bool(validation.get('passed') and done.get('complete') and len(measurements) == 3 and done.get('measured_requests') == 3)
        valid = valid and metadata.get('backend_requested') == item['backend']
        valid = valid and all(row.get('actual_prefill_tokens') == 128 and row.get('actual_decode_tokens') == 64
                             and row.get('app_call_elapsed_ns', 0) > 0
                             and row.get('app_end_ns', 0)-row.get('app_start_ns', 0) == row['app_call_elapsed_ns'] for row in measurements)
        durations = [row['app_call_elapsed_ns']/1e9 for row in measurements] if valid else []
        library_path = directory / 'loaded_libraries.txt'
        names = sorted({Path(line).name for line in library_path.read_text().splitlines()}) if library_path.exists() else []
        for name in names:
            libraries.append(dict(cohort=cohort, model=item['model'], backend=item['backend'], library=name))
        start = item.get('starting_state') or {}
        if not start:
            eligible = [row for row in lines(backend / 'profiling' / (item['run'] + '-recovery.jsonl')) if row.get('eligible')]
            start = eligible[-1] if eligible else {}
        conditions.append(dict(**value, accepted_requests=len(durations), observed_success_rows=len(measurements),
                               planned_calls=3, started_calls=started_calls,
                               unlaunched_calls=3-started_calls if started_calls is not None else None,
                               initialization_error_tag='input_tensor_not_found' if 'Input tensor not found' in errors else None,
                               median_sdk_call_s=statistics.median(durations) if durations else None,
                               individual_sdk_call_s=durations,
                               start_skin=start.get('skin'), start_battery_hal=start.get('battery_hal'),
                               start_nsp0=start.get('nsp0'), start_severity=start.get('severity'),
                               libraries_recorded=library_path.exists(),
                               qnn_library_loaded=any(name.startswith('libQnn') for name in names) if library_path.exists() else None))
    return conditions, profiles, libraries


def generate(studies, output, retry_study=None, backend_study=None, hardware_study=None, metadata_study=None):
    output.mkdir(parents=True, exist_ok=True)
    retry = retry_study or studies / 'completion_experiments_20261017_e'
    backend = backend_study or studies / 'backend_diagnostics_20261017'
    state = read(retry / 'acquisition/state.json')
    retry_profiles = read(retry / 'profiling/state.json')
    backend_state = read(backend / 'profiling/state.json')
    if state['status'] == 'running' or retry_profiles['status'] in {'running', 'queued_after_primary'} or backend_state['status'] in {'running', 'queued_after_supplementary'}:
        raise RuntimeError('Wait for all device work to close before reconstructing results')
    units = {item['id']: item for item in state['units']}
    pairs = []
    for planned in read(retry / 'planned_units.json'):
        values = {}
        for item in units.get(planned['id'], {}).get('members', []):
            if not item.get('accepted'):
                continue
            design = next(value for value in planned['members'] if value['run_id'] == item['run'])
            row = next(row for row in lines(retry / 'runs' / item['run'] / 'results.jsonl') if row.get('phase') == 'measure')
            key = design['model'] if planned['kind'] == 'main' else design['monitoring']
            values[key] = row['app_call_elapsed_ns'] / 1e9
        main = planned['kind'] == 'main'
        a, b = ('fastvlm-sm8750', 'gemma4-sm8750') if main else ('full', 'minimal')
        pairs.append(dict(unit=planned['id'], condition='Main short' if main else 'Monitoring',
                          packages='FastVLM/Gemma' if main else ('Gemma' if 'gemma' in planned['members'][0]['model'] else 'FastVLM'),
                          first_quantity_s=values.get(a), second_quantity_s=values.get(b),
                          difference_s=values[a]-values[b] if a in values and b in values else None,
                          complete_pair=units.get(planned['id'], {}).get('paired_complete', False)))
    profiles = [profile(retry, 'Retry', item) for item in retry_profiles.get('results', [])]
    conditions, extra_profiles, libraries = condition_summaries(backend, backend_state, 'Grid')
    profiles.extend(extra_profiles)
    hardware = hardware_study or studies / 'hardware_event_diagnostics_20261017'
    hardware_state = read(hardware / 'profiling/state.json', {})
    hardware_conditions = []
    if hardware_state:
        if hardware_state['status'] in {'running', 'queued_after_supplementary'}:
            raise RuntimeError('Hardware-event observations are still running')
        hardware_conditions, extra_profiles, extra_libraries = condition_summaries(hardware, hardware_state, 'Kernel')
        profiles.extend(extra_profiles)
        libraries.extend(extra_libraries)
        original = {(item['model'],item['backend']):item for item in hardware_state.get('results',[])}
        for item in hardware_conditions:
            captured = original.get((item['model'],item['backend']),{})
            query_ok = captured.get('query_returncodes',{}).get('hardware_events') == 0 and item['trace_available']
            rows = csvrows(hardware / 'profiling' / (item['run']+'-hardware_events.csv')) if query_ok else []
            item['observed_application_vendor_events'] = len(rows) if query_ok else None
            item['vendor_event_evidence_available'] = bool(rows)
            item['vendor_event_coverage'] = 'events_observed' if rows else 'unverified_coverage_no_absence_claim'
            item['setup_metadata'] = csvrows(hardware / 'profiling' / (item['run']+'-hardware_setup.csv'))
            power_rows = csvrows(hardware / 'profiling' / (item['run']+'-power_counters.csv')) if captured.get('query_returncodes',{}).get('power_counters') == 0 else []
            item['power_counter_rows'] = len(power_rows)
            item['power_counter_tracks'] = sorted({row['name'] for row in power_rows})
            item['observed_power_rail_series'] = sorted({row['name'] for row in power_rows if row['name'].startswith('power.rails')})
            item['energy_per_request_validated'] = False
    metadata = metadata_study or studies / 'metadata_probe_20261017'
    metadata_state = read(metadata / 'state.json', {})
    metadata_result = read(metadata / 'results/model-metadata.json', {})
    baseline = read(retry / 'acquisition/baseline.json', {})
    metrics = dict(retry=dict(status=state['status'], accepted_requests=sum(item['requests'] for item in state['completed']),
                             complete_pairs=sum(item['paired_complete'] for item in state['units']),
                             excluded_members=len(state['excluded']), baseline=baseline.get('reference'), pairs=pairs),
                   backend=dict(status=backend_state['status'], conditions=conditions), profiles=profiles,
                   policy='Separate prospective cohorts; no pooling, replacement of exclusions or independent-replication claim for calls within a capture')
    metrics['hardware'] = dict(status=hardware_state.get('status','not_available'),conditions=hardware_conditions)
    metrics['model_metadata'] = dict(status=metadata_state.get('status','not_available'),result=metadata_result)
    write_csv(output / 'hardware_conditions.csv', hardware_conditions, ['model','outcome','accepted_requests','started_calls','unlaunched_calls','observed_application_vendor_events','vendor_event_evidence_available','vendor_event_coverage','complete_phase_slices','application_cpu_available','power_counter_rows','power_counter_tracks','observed_power_rail_series','energy_per_request_validated'])
    (output / 'additional_metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    write_csv(output / 'retry_pairs.csv', pairs, ['unit','condition','packages','first_quantity_s','second_quantity_s','difference_s','complete_pair'])
    write_csv(output / 'backend_conditions.csv', conditions, ['model','backend','outcome','accepted_requests','planned_calls','started_calls','unlaunched_calls','initialization_error_tag','median_sdk_call_s','start_skin','start_battery_hal','start_nsp0','start_severity','application_cpu_available','observed_application_cpu_ms','complete_phase_slices','libraries_recorded','qnn_library_loaded'])
    write_csv(output / 'additional_profiles.csv', profiles, ['cohort','run','model','backend','outcome','phase_slices','complete_phase_slices','application_cpu_available','observed_application_cpu_ms','independent_npu_operator_placement_verified'])
    write_csv(output / 'backend_libraries.csv', libraries, ['cohort','model','backend','library'])
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--studies', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--retry-study', type=Path)
    parser.add_argument('--backend-study', type=Path)
    parser.add_argument('--hardware-study', type=Path)
    parser.add_argument('--metadata-study', type=Path)
    args = parser.parse_args()
    metrics = generate(args.studies, args.output, args.retry_study, args.backend_study, args.hardware_study, args.metadata_study)
    print(json.dumps(dict(retry_pairs=metrics['retry']['complete_pairs'],
                          backend_conditions=len(metrics['backend']['conditions']))))
