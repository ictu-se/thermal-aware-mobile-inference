#!/usr/bin/env python3
"""Generate manuscript evidence from retained handset measurements, without pooling cohorts."""
from pathlib import Path
import collections
import csv
import json
import statistics as st
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / 'studies'
PRIMARY = ROOT / 'unified_experiments_20261016'
SUPPLEMENT = ROOT / 'completion_experiments_20261017_d'
PAPER = HERE.parent / 'generated'
PAPER.mkdir(exist_ok=True)
TABLES = PAPER / 'tables'
FIGURES = PAPER / 'figures'
NAMES = {'fastvlm-sm8750': 'FastVLM', 'gemma4-sm8750': 'Gemma'}
COLORS = {'fastvlm-sm8750': '#146b96', 'gemma4-sm8750': '#d65e29'}


def csvrows(path):
    return list(csv.DictReader(path.open()))


def jsonrows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def table(name, rows):
    with (TABLES / (name + '.csv')).open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(json.loads((HERE / 'table_columns.json').read_text())[name])
        writer.writerows(rows)


def main():
    TABLES.mkdir(exist_ok=True)
    FIGURES.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size': 10.5, 'axes.labelsize': 10.5, 'axes.titlesize': 11,
                         'legend.fontsize': 9.5, 'xtick.labelsize': 9.5, 'ytick.labelsize': 9.5})
    points = csvrows(PRIMARY / 'analysis/request_points.csv')
    main_points = [row for row in points if row['kind'] == 'main']
    pairs = csvrows(PRIMARY / 'analysis/main_pairs.csv')
    overhead = csvrows(PRIMARY / 'analysis/monitoring_pairs.csv')
    long = csvrows(PRIMARY / 'analysis/long_sessions.csv')
    sensors = csvrows(PRIMARY / 'analysis/sensor_summary.csv')
    summary = json.loads((PRIMARY / 'analysis/results.json').read_text())
    state = json.loads((PRIMARY / 'acquisition/state.json').read_text())
    assert len(main_points) == 119 and len(pairs) == 59 and len(overhead) == 38 and len(long) == 12
    metrics = dict(main={}, thermal={}, memory={}, repeated={}, monitoring={}, profiling=[])
    rows, thermal_rows = [], []
    by_sensor = {(row['run'], row['name']): row for row in sensors}
    for model in NAMES:
        for workload, budget in [('short', '128/64'), ('long', '512/256')]:
            selected = [row for row in main_points if row['model'] == model and row['workload'] == workload]
            durations = [float(row['duration_s']) for row in selected]
            rate = [float(row['decode_tps']) for row in selected]
            rows.append([NAMES[model], budget, len(selected), f'{st.mean(durations):.3f}',
                         f'{st.median(durations):.3f}', f'{st.stdev(durations):.3f}',
                         f'{st.mean(rate):.1f}'])
            skin = [by_sensor[row['run'], 'skin'] for row in selected]
            pss = []
            for row in selected:
                values = [sample['total_pss_kb'] / 1024 for sample in
                          jsonrows(PRIMARY / 'runs' / row['run'] / 'device_telemetry.jsonl')
                          if isinstance(sample.get('total_pss_kb'), (int, float))]
                if values:
                    pss.append(max(values))
            key = model + '/' + workload
            metrics['main'][key] = dict(n=len(selected), mean_s=st.mean(durations), median_s=st.median(durations), sd_s=st.stdev(durations))
            metrics['memory'][key] = dict(n=len(pss), mean_sampled_peak_mib=st.mean(pss), minimum=min(pss), maximum=max(pss))
            metrics['thermal'][key] = {field: st.mean(float(row[field]) for row in skin)
                                       for field in ['baseline', 'final', 'sampled_peak', 'rise_from_baseline', 'positive_rise_auc']}
            thermal_rows.append([NAMES[model], workload, *[f"{metrics['thermal'][key][field]:.1f}"
                for field in ['baseline', 'final', 'rise_from_baseline', 'positive_rise_auc']],
                f'{st.mean(pss):.0f}'])
    table('performance', rows)
    table('thermal_memory', thermal_rows)
    rows = []
    for workload in ['short', 'long']:
        result = summary['main'][workload]
        low, high = result['descriptive_block_interval_95']
        rows.append([workload.title(), result['differences']['n'],
                     f"{result['differences']['mean']:.3f}", f'[{low:.3f}, {high:.3f}]',
                     f"{result['sensitivity']['exclude_first_pair_per_block']['mean']:.3f}",
                     f"{result['sensitivity']['adjusted_at_equal_start_skin_s']:.3f}"])
    table('paired', rows)
    rows = []
    for model in NAMES:
        for workload in ['short', 'long']:
            value = summary['overhead'][model + '/' + workload]
            low, high = value['descriptive_block_interval_95']
            rows.append([NAMES[model], workload, value['full_minus_minimal_s']['n'],
                         f"{1000*value['full_minus_minimal_s']['mean']:.1f}",
                         f'[{1000*low:.1f}, {1000*high:.1f}]',
                         f"{value['relative_difference_percent']['mean']:.2f}"])
            metrics['monitoring'][model + '/' + workload] = value
    table('monitoring', rows)
    rows = []
    for model in NAMES:
        selected = sorted([row for row in long if row['model'] == model], key=lambda row: int(row['block']))
        events = [float(row['severe_event_s']) for row in selected]
        recovery = [float(row['matched_recovery_elapsed_s']) for row in selected]
        metrics['repeated'][model] = dict(n=len(selected), requests=sum(int(row['requests']) for row in selected),
            severe_seconds=events, recovery_seconds=recovery, median_severe_s=st.median(events),
            median_recovery_s=st.median(recovery))
        for row in selected:
            rows.append([int(row['block']) + 1, NAMES[model], row['requests'],
                         f"{float(row['severe_event_s']):.1f}",
                         f"{float(row['matched_recovery_elapsed_s']):.1f}",
                         row['finite_headroom_samples'], f"{float(row['maximum_headroom']):.2f}"])
    rows.sort(key=lambda row: (row[0], row[1]))
    table('repeated', rows)
    severity_rows = []
    for model in NAMES:
        totals = collections.Counter()
        for row in long:
            if row['model'] == model:
                totals.update(json.loads(row['severity_seconds']))
        metrics['repeated'][model]['severity_seconds_total'] = dict(totals)
        severity_rows.append([NAMES[model], *[f'{totals[str(level)]:.1f}' for level in range(4)]])
    table('severity', severity_rows)
    main_sensors = [row for row in sensors if row['run'] in {point['run'] for point in main_points}]
    metrics['sampling'] = dict(main_sensor_rows=len(main_sensors), maximum_gap_s=max(float(row['maximum_sample_gap_s']) for row in main_sensors if row['maximum_sample_gap_s']),
        failed_polls=sum(int(row['failed_polls']) for row in main_sensors if row['name'] == 'skin'),
        missing_channel_polls_all=sum(int(row['missing_channel_polls']) for row in main_sensors),
        missing_skin_polls=sum(int(row['missing_channel_polls']) for row in main_sensors if row['name'] == 'skin'))
    figure, axes = plt.subplots(2, 2, figsize=(6.2, 5), constrained_layout=True)
    for column, workload in enumerate(['short', 'long']):
        for model, offset in [('fastvlm-sm8750', -.12), ('gemma4-sm8750', .12)]:
            selected = [row for row in main_points if row['model'] == model and row['workload'] == workload]
            position = np.array([int(row['block']) + 1 for row in selected], float)
            jitter = np.linspace(-.07, .07, len(selected))
            axes[0, column].scatter(position + offset + jitter, [float(row['duration_s']) for row in selected], s=12, alpha=.8, color=COLORS[model], label=NAMES[model])
            axes[1, column].scatter(position + offset + jitter, [float(row['start_skin']) for row in selected], s=12, alpha=.8, color=COLORS[model])
        axes[0, column].set_title(workload.title() + ' fixed work')
        axes[0, column].set_ylabel('SDK call duration (s)')
        axes[1, column].set(xlabel='Scheduled block', ylabel='Starting skin HAL report (nominal °C)')
        for axis in axes[:, column]:
            axis.set_xticks(range(1, 7))
    axes[0, 0].legend(frameon=False)
    figure.savefig(FIGURES / 'fixed_work.png')
    plt.close(figure)
    figure, axis = plt.subplots(figsize=(6.2, 3.4), constrained_layout=True)
    groups = [(model, workload) for model in NAMES for workload in ['short', 'long']]
    for index, (model, workload) in enumerate(groups):
        selected = [row for row in overhead if row['model'] == model and row['workload'] == workload]
        vals = np.array([1000 * float(row['difference_s']) for row in selected])
        axis.scatter(index + np.linspace(-.12, .12, len(vals)), vals, s=20, color=COLORS[model], alpha=.8)
        low, high = summary['overhead'][model + '/' + workload]['descriptive_block_interval_95']
        mean = vals.mean()
        axis.errorbar(index + .22, mean, yerr=[[mean - 1000*low], [1000*high - mean]], fmt='ks', capsize=4, markersize=4)
    axis.axhline(0, color='gray', linewidth=.8)
    axis.set_xticks(range(4), [NAMES[model] + '\n' + workload for model, workload in groups])
    axis.set_ylabel('Full minus minimal duration (ms)')
    figure.savefig(FIGURES / 'monitoring.png')
    plt.close(figure)
    figure, axes = plt.subplots(4, 2, figsize=(6.2, 8.2), constrained_layout=True)
    palette = plt.get_cmap('tab10')
    for column, model in enumerate(NAMES):
        for row in [row for row in long if row['model'] == model]:
            directory = PRIMARY / 'runs' / row['run']
            measured = [point for point in jsonrows(directory / 'results.jsonl') if point.get('phase') == 'measure' and point.get('status') == 'ok']
            origin = measured[0]['app_start_ns'] / 1e9
            color = palette(int(row['block']))
            thermal = jsonrows(directory / 'host_thermal.jsonl')
            skin = [(float(poll['device_uptime'].split()[0]) - origin, sensor['value']) for poll in thermal if 'device_uptime' in poll for sensor in poll['hal_sensors'] if sensor['name'] == 'skin']
            if skin:
                axes[0, column].plot(*zip(*skin), color=color, linewidth=.9, label='B' + str(int(row['block']) + 1))
            headroom = [point for point in jsonrows(directory / 'thermal_headroom.jsonl') if point.get('available')]
            axes[1, column].plot([point['device_monotonic_ns'] / 1e9 - origin for point in headroom], [point['headroom'] for point in headroom], color=color, linewidth=.9)
            status = jsonrows(directory / 'thermal_events.jsonl')
            axes[2, column].step([point['device_monotonic_ns'] / 1e9 - origin for point in status], [point['severity'] for point in status], where='post', color=color, linewidth=.9)
            axes[3, column].scatter([point['app_start_ns'] / 1e9 - origin for point in measured], [point['native_decode_tokens_per_s'] for point in measured], color=color, s=3)
        axes[0, column].set_title(NAMES[model])
        axes[0, column].legend(ncol=3, frameon=False, fontsize=9.5, handlelength=1.2)
        for index, ylabel in enumerate(['Skin HAL (nominal °C)', 'Headroom API value', 'Thermal severity', 'Decode tokens/s']):
            axes[index, column].set_ylabel(ylabel)
            axes[index, column].set_xlim(0, 185)
        axes[2, column].set_yticks([0, 1, 2, 3])
        axes[3, column].set_xlabel('Seconds from first invocation')
    figure.savefig(FIGURES / 'repeated.png')
    plt.close(figure)
    rows = []
    for cohort, directory in [('Original', PRIMARY), ('Diagnostic', SUPPLEMENT)]:
        profile_state = directory / 'profiling/state.json'
        if not profile_state.exists():
            continue
        for item in json.loads(profile_state.read_text()).get('results', []):
            rid = item.get('run') or f"c17d-profile-{item['mode']}-{item['model']}"
            label = 'Gemma' if 'gemma' in rid else 'FastVLM'
            mode = 'stream' if '-natural-' in rid else 'fixed'
            phase_file = directory / 'profiling' / (rid + '-phases.csv')
            phases = csvrows(phase_file) if phase_file.exists() else []
            cpu_file = directory / 'profiling' / (rid + '-app_cpu.csv')
            cpu = csvrows(cpu_file) if cpu_file.exists() else []
            usable = item.get('outcome') == 'captured'
            phase_count = len(phases) if usable and item.get('query_returncodes', {}).get('phases') == 0 else None
            cpu_available = usable and bool(cpu) and item.get('query_returncodes', {}).get('app_cpu') == 0
            value = dict(cohort=cohort, run=rid, model=label, mode=mode, phase_slices=phase_count, app_cpu_available=cpu_available, outcome=item['outcome'])
            metrics['profiling'].append(value)
            rows.append([cohort, label, mode, phase_count if phase_count is not None else '--', 'yes' if cpu_available else 'unavailable'])
    table('profiles', rows)
    supplement_state = json.loads((SUPPLEMENT / 'acquisition/state.json').read_text())
    metrics['supplement_status'] = supplement_state['status']
    gaps, pending = [], {}
    for item in jsonrows(SUPPLEMENT / 'acquisition/events.jsonl'):
        label = item.get('before')
        if item.get('event') == 'connection_wait':
            pending[label] = item['utc']
        elif item.get('event') == 'connection_restored' and label in pending:
            gaps.append(dict(before=label, start_utc=pending.pop(label), restored_utc=item['utc']))
    for label, started in pending.items():
        gaps.append(dict(before=label, start_utc=started, restored_utc=None,
                         observed_until_utc=supplement_state.get('finished_utc')))
    metrics['supplement_transport_gaps'] = gaps
    if supplement_state['status'] != 'running':
        rows = []
        units = {unit['id']: unit for unit in supplement_state['units']}
        schedule = json.loads((SUPPLEMENT / 'planned_units.json').read_text())
        for planned in schedule:
            observed = units.get(planned['id'], {}).get('members', [])
            values = {}
            for member in observed:
                if not member.get('accepted'):
                    continue
                result = next(row for row in jsonrows(SUPPLEMENT / 'runs' / member['run'] / 'results.jsonl') if row.get('phase') == 'measure')
                design = next(item for item in planned['members'] if item['run_id'] == member['run'])
                key = design['model'] if planned['kind'] == 'main' else design['monitoring']
                values[key] = result['app_call_elapsed_ns'] / 1e9
            if planned['kind'] == 'main':
                keys = ['fastvlm-sm8750', 'gemma4-sm8750']
                labels = ['Main short', 'FastVLM/Gemma']
            else:
                keys = ['full', 'minimal']
                labels = ['Monitoring', NAMES[planned['members'][0]['model']]]
            durations = [f'{values[key]:.3f}' if key in values else '--' for key in keys]
            difference = f'{values[keys[0]]-values[keys[1]]:+.3f}' if all(key in values for key in keys) else '--'
            rows.append(labels + durations + [difference])
        table('supplement', rows)
        metrics['supplement_rows'] = rows
        metrics['supplement_complete_pairs'] = sum(unit['paired_complete'] for unit in supplement_state['units'])
    (PAPER / 'publication_metrics.json').write_text(json.dumps(metrics, indent=2))
    print(json.dumps({key: metrics[key] for key in ['main', 'memory', 'sampling', 'repeated', 'supplement_status']}, indent=2))


if __name__ == '__main__':
    main()
