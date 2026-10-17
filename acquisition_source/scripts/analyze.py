#!/usr/bin/env python3
"""Reconstruct retained pairs, sensor context, monitoring costs and censored sessions."""
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'analysis'
SEED = 20261016


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def write_table(name, data):
    if not data:
        (OUT / name).write_text('')
        return
    fields = list(dict.fromkeys(key for row in data for key in row))
    with (OUT / name).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(data)


def describe(values):
    if not values:
        return dict(n=0)
    median = statistics.median(values)
    return dict(n=len(values), mean=statistics.mean(values), median=median,
                sd=statistics.stdev(values) if len(values) > 1 else None,
                mad=statistics.median(abs(value - median) for value in values))


def block_interval(records, key):
    blocks = sorted({row.get('resampling_block', str(row['block'])) for row in records})
    if len(blocks) < 2:
        return None
    grouped = {block: [row[key] for row in records
                       if row.get('resampling_block', str(row['block'])) == block] for block in blocks}
    rng = np.random.default_rng(SEED)
    estimates = [np.mean([v for block in rng.choice(blocks, len(blocks), replace=True)
                          for v in grouped[block]]) for _ in range(10000)]
    return np.quantile(estimates, [.025, .975]).tolist()


def sensitivity(records):
    if not records:
        return {}
    block_of = lambda row: row.get('resampling_block', str(row['block']))
    blocks = sorted({block_of(row) for row in records})
    first = {block: min(row['index'] for row in records if block_of(row) == block) for block in blocks}
    design = np.array([[1, row['skin_difference_signed'], row['fastvlm_first'],
                        *[int(block_of(row) == block) for block in blocks[1:]]]
                       for row in records], dtype=float)
    design[:, 2:] -= design[:, 2:].mean(axis=0)
    adjusted = None
    if len(records) > design.shape[1] and np.linalg.matrix_rank(design) == design.shape[1]:
        adjusted = float(np.linalg.lstsq(
            design, [row['difference_s'] for row in records], rcond=None,
        )[0][0])
    return dict(
        adjusted_at_equal_start_skin_s=adjusted,
        exclude_first_pair_per_block=describe([
            row['difference_s'] for row in records if row['index'] != first[block_of(row)]
        ]),
        starting_skin_difference_at_most_0_5=describe([
            row['difference_s'] for row in records if abs(row['skin_difference_signed']) <= .5
        ]),
    )


def main():
    OUT.mkdir(exist_ok=True)
    state = json.loads((ROOT / 'acquisition/state.json').read_text())
    events = rows(ROOT / 'acquisition/events.jsonl')
    launches = {event['run']: event for event in events if event.get('event') == 'launch'}
    recoveries = {event['run']: event for event in events if event.get('event') == 'post_session_recovery'}
    points, sessions, sensor_rows = [], [], []
    byrun = {}
    for rec in state['completed'] + state['thermal_stops']:
        rid, item = rec['run'], rec['design']
        directory = ROOT / 'runs' / rid
        validation = json.loads((directory / 'followup_validation.json').read_text())
        if not validation['accepted_observations']:
            raise ValueError('Accepted state conflicts with validation: ' + rid)
        measured = [row for row in rows(directory / 'results.jsonl')
                    if row.get('phase') == 'measure' and row.get('status') == 'ok']
        start = launches[rid]['starting_state']
        segment = launches[rid].get('segment', 'initial')
        byrun[rid] = dict(item=item, rows=measured, start=start, segment=segment)
        for row in measured:
            points.append(dict(
                run=rid, kind=item['kind'], model=item['model'], block=item['block'],
                segment=segment,
                workload=item['workload'], monitoring=item['monitoring'],
                start_ns=row['app_start_ns'], duration_s=row['app_call_elapsed_ns'] / 1e9,
                actual_prefill=row['actual_prefill_tokens'], actual_decode=row['actual_decode_tokens'],
                decode_tps=row['native_decode_tokens_per_s'], start_skin=start['skin'],
                start_battery_hal=start['battery_hal'], start_nsp0=start['nsp0'],
                start_severity=start['severity'],
            ))
        polls = rows(directory / 'host_thermal.jsonl')
        channel_points = collections.defaultdict(list)
        successful_times = []
        for poll in polls:
            if 'device_uptime' not in poll:
                continue
            stamp = float(poll['device_uptime'].split()[0])
            successful_times.append(stamp)
            for sensor in poll['hal_sensors']:
                channel_points[(sensor['name'], sensor['type'])].append((stamp, sensor['value']))
        for (name, sensor_type), values in channel_points.items():
            baseline = start['channels'].get(name)
            rise_auc = None
            if baseline is not None and len(values) > 1:
                rise_auc = sum((b[0] - a[0]) *
                               (max(0, a[1] - baseline) + max(0, b[1] - baseline)) / 2
                               for a, b in zip(values, values[1:]))
            sensor_rows.append(dict(
                run=rid, name=name, android_type=sensor_type, samples=len(values),
                failed_polls=sum('error' in poll for poll in polls),
                missing_channel_polls=len(successful_times) - len(values),
                baseline=baseline, first=values[0][1], final=values[-1][1],
                sampled_peak=max(value for _, value in values),
                rise_from_baseline=max(value for _, value in values) - baseline
                if baseline is not None else None,
                positive_rise_auc=rise_auc,
                maximum_sample_gap_s=max((b[0] - a[0] for a, b in zip(values, values[1:])), default=None),
            ))
        if item['kind'] != 'repeated':
            continue
        done = json.loads((directory / 'done.json').read_text())
        first, last = done['first_request_ns'], done['last_request_end_ns']
        thermal = sorted(rows(directory / 'thermal_events.jsonl'), key=lambda row: row['device_monotonic_ns'])
        severe = [row for row in thermal if row.get('severity', 0) >= 3
                  and first <= row['device_monotonic_ns'] <= last]
        severity_seconds = collections.defaultdict(float)
        cursor, current = first, start['severity']
        for row in thermal:
            stamp = row['device_monotonic_ns']
            if stamp <= first:
                current = row['severity']
            elif stamp <= last:
                severity_seconds[str(current)] += (stamp - cursor) / 1e9
                cursor, current = stamp, row['severity']
        severity_seconds[str(current)] += (last - cursor) / 1e9
        headroom = rows(directory / 'thermal_headroom.jsonl')
        finite_headroom = [row['headroom'] for row in headroom if row.get('available')
                           and isinstance(row.get('headroom'), (int, float))
                           and math.isfinite(row['headroom'])]
        recovery = recoveries.get(rid, {})
        quarter = max(1, len(measured) // 4)
        sessions.append(dict(
            run=rid, model=item['model'], block=item['block'], outcome=done['outcome'],
            segment=segment,
            requests=len(measured), observed_window_s=(last - first) / 1e9,
            severe_event_s=(severe[0]['device_monotonic_ns'] - first) / 1e9 if severe else None,
            severe_event_right_censored=not bool(severe),
            start_skin=start['skin'], start_battery_hal=start['battery_hal'], start_nsp0=start['nsp0'],
            early_decode_tps=statistics.median(row['native_decode_tokens_per_s'] for row in measured[:quarter]),
            late_decode_tps=statistics.median(row['native_decode_tokens_per_s'] for row in measured[-quarter:]),
            severity_seconds=json.dumps(dict(severity_seconds)), headroom_samples=len(headroom),
            finite_headroom_samples=len(finite_headroom), maximum_headroom=max(finite_headroom, default=None),
            matched_recovery_observed=recovery.get('observed'),
            matched_recovery_elapsed_s=recovery.get('elapsed_seconds'),
        ))
    paired, overhead = [], []
    for index, unit in enumerate(state['units']):
        if not unit['paired_complete']:
            continue
        runs = [byrun[member['run']] for member in unit['members']]
        a, b = runs
        if a['segment'] != b['segment']:
            raise ValueError('A pair crosses acquisition segments: ' + unit['id'])
        pair_context = dict(segment=a['segment'],
                            resampling_block=a['segment'] + '/' + str(a['item']['block']))
        if unit['kind'] == 'main':
            f, g = sorted(runs, key=lambda run: 'gemma' in run['item']['model'])
            paired.append(dict(
                unit=unit['id'], block=a['item']['block'], workload=a['item']['workload'], index=index,
                fastvlm_s=f['rows'][0]['app_call_elapsed_ns'] / 1e9,
                gemma_s=g['rows'][0]['app_call_elapsed_ns'] / 1e9,
                difference_s=(f['rows'][0]['app_call_elapsed_ns'] - g['rows'][0]['app_call_elapsed_ns']) / 1e9,
                skin_difference_signed=f['start']['skin'] - g['start']['skin'],
                fastvlm_first=int('fastvlm' in a['item']['model']),
                **pair_context,
            ))
        elif unit['kind'] == 'overhead':
            full, minimal = sorted(runs, key=lambda run: run['item']['monitoring'] != 'full')
            fs = full['rows'][0]['app_call_elapsed_ns'] / 1e9
            ms = minimal['rows'][0]['app_call_elapsed_ns'] / 1e9
            overhead.append(dict(
                unit=unit['id'], block=a['item']['block'], model=a['item']['model'],
                workload=a['item']['workload'], full_s=fs, minimal_s=ms,
                difference_s=fs - ms, difference_percent=100 * (fs - ms) / ms,
                full_first=a['item']['monitoring'] == 'full',
                skin_difference_signed=full['start']['skin'] - minimal['start']['skin'],
                **pair_context,
            ))
    summary = dict(
        campaign=json.loads((ROOT / 'protocol.json').read_text())['campaign_id'], status=state['status'],
        valid_requests_by_kind=dict(collections.Counter(point['kind'] for point in points)),
        completed_main_pairs=len(paired), completed_monitoring_pairs=len(overhead),
        repeated_outcomes=dict(collections.Counter(session['outcome'] for session in sessions)),
        exclusion_reasons=dict(collections.Counter(item['reason'] for item in state['excluded'])),
        bootstrap=dict(method='percentile, whole segment-by-scheduled-block resampling', resamples=10000, seed=SEED),
        main={}, overhead={},
        limits='Single handset, external power, actual ambient temperature unmeasured; separate acquisition with upper-only global baseline bounds and symmetric within-pair multi-sensor matching. Explicit pause/resume creates separate labeled acquisition segments, not independent environmental resets. Minimal telemetry is not zero overhead. No independent NPU placement proof.',
    )
    summary['requests_by_segment_and_kind'] = dict(collections.Counter(
        point['segment'] + '/' + point['kind'] for point in points
    ))
    summary['monitoring_by_segment'] = {
        segment: {model + '/' + workload: describe([
            row['difference_s'] for row in overhead
            if row['segment'] == segment and row['model'] == model and row['workload'] == workload
        ]) for model in ['fastvlm-sm8750', 'gemma4-sm8750'] for workload in ['short', 'long']}
        for segment in sorted({row['segment'] for row in overhead})
    }
    for workload in ['short', 'long']:
        selected = [row for row in paired if row['workload'] == workload]
        summary['main'][workload] = dict(
            differences=describe([row['difference_s'] for row in selected]),
            descriptive_block_interval_95=block_interval(selected, 'difference_s'),
            sensitivity=sensitivity(selected),
        )
    for model in ['fastvlm-sm8750', 'gemma4-sm8750']:
        for workload in ['short', 'long']:
            selected = [row for row in overhead if row['model'] == model and row['workload'] == workload]
            summary['overhead'][model + '/' + workload] = dict(
                full_minus_minimal_s=describe([row['difference_s'] for row in selected]),
                relative_difference_percent=describe([row['difference_percent'] for row in selected]),
                descriptive_block_interval_95=block_interval(selected, 'difference_s'),
            )
    write_table('request_points.csv', sorted(points, key=lambda row: row['start_ns']))
    write_table('main_pairs.csv', paired)
    write_table('monitoring_pairs.csv', overhead)
    write_table('long_sessions.csv', sessions)
    write_table('sensor_summary.csv', sensor_rows)
    write_table('exclusions.csv', [dict(run=item['run'], reason=item['reason'],
                                      kind=item['design']['kind']) for item in state['excluded']])
    (OUT / 'results.json').write_text(json.dumps(summary, indent=2, allow_nan=False))
    (OUT / 'reviewer-followup-status.txt').write_text(
        f"Acquisition status: {state['status']}\n"
        f"Completed main pairs: {len(paired)} / 60\n"
        f"Completed full/minimal pairs: {len(overhead)} / 40\n"
        f"Retained repeated-use trajectories: {len(sessions)} / 12\n"
        f"Repeated outcomes: {dict(collections.Counter(session['outcome'] for session in sessions))}\n"
        "Addresses measurement overhead and thermal/recovery observations; no claim that all reviewer requests are resolved.\n"
        + summary['limits'] + '\n'
    )
    if overhead:
        fig, axes = plt.subplots(1, 2, figsize=(9, 3.5), constrained_layout=True)
        for model, marker in [('fastvlm-sm8750', 'o'), ('gemma4-sm8750', 's')]:
            for axis, workload in zip(axes, ['short', 'long']):
                selected = [row for row in overhead if row['model'] == model and row['workload'] == workload]
                axis.scatter(range(len(selected)), [1000 * row['difference_s'] for row in selected], marker=marker, label=model)
                axis.axhline(0, color='gray', linewidth=.7)
                axis.set(title=workload, xlabel='Accepted pair within stratum', ylabel='Full minus minimal call duration (ms)')
        axes[0].legend(fontsize=8)
        fig.savefig(OUT / 'monitoring_differences.pdf')
        plt.close(fig)
    if sessions:
        fig, axes = plt.subplots(len(sessions), 3, figsize=(10, max(3, len(sessions) * 1.8)), squeeze=False, constrained_layout=True)
        for line, session in zip(axes, sessions):
            rid = session['run']
            measured = byrun[rid]['rows']
            origin = measured[0]['app_start_ns'] / 1e9
            thermal = rows(ROOT / 'runs' / rid / 'host_thermal.jsonl')
            skin = [(float(poll['device_uptime'].split()[0]) - origin, sensor['value'])
                    for poll in thermal if 'device_uptime' in poll
                    for sensor in poll['hal_sensors'] if sensor['name'] == 'skin']
            if skin:
                line[0].plot(*zip(*skin), linewidth=.8)
            severity = rows(ROOT / 'runs' / rid / 'thermal_events.jsonl')
            line[1].step([(row['device_monotonic_ns'] / 1e9 - origin) for row in severity],
                         [row['severity'] for row in severity], where='post', linewidth=.8)
            line[2].scatter([(row['app_start_ns'] / 1e9 - origin) for row in measured],
                            [row['native_decode_tokens_per_s'] for row in measured], s=2)
            line[0].set_ylabel(session['model'] + '\n' + session['outcome'], fontsize=7)
            for axis in line:
                axis.set_xlim(0, 920)
                axis.tick_params(labelsize=7)
        for axis, title in zip(axes[0], ['Skin HAL report (nominal C)', 'Android thermal severity', 'Decode rate (tokens/s)']):
            axis.set_title(title, fontsize=9)
        for axis in axes[-1]:
            axis.set_xlabel('Seconds from first measured invocation')
        fig.savefig(OUT / 'repeated_trajectories.pdf')
        plt.close(fig)
    manifest = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((ROOT / 'runs').glob('*/results.jsonl'))}
    (OUT / 'result_hashes.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({key: summary[key] for key in ['status', 'valid_requests_by_kind', 'repeated_outcomes']}, indent=2))


if __name__ == '__main__':
    main()
