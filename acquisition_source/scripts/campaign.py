#!/usr/bin/env python3
"""One locked acquisition with prespecified, independently bounded study arms."""
import argparse
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from acquire import Acquisition, event, save, utc
from run_device import ROOT, PACKAGE, call


def verify_freeze():
    manifest = json.loads((ROOT / 'intake/frozen_hashes.json').read_text())
    for relative, expected in manifest.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError('Frozen input changed: ' + relative)


def remaining_schedule(state, schedule, run_exists=lambda rid: False):
    """Resume only a paused prefix of fully accounted units, never a partial pair."""
    if state['status'] != 'paused':
        raise ValueError('Only an explicitly paused campaign can be resumed')
    closed = state['units']
    if [unit['id'] for unit in closed] != [unit['id'] for unit in schedule[:len(closed)]]:
        raise ValueError('Checkpoint units do not match the frozen schedule prefix')
    covered = {item['run_id'] for unit in schedule[:len(closed)] for item in unit['members']}
    known = set(state['attempted']) | {
        rec['run'] for key in ['completed', 'thermal_stops', 'excluded'] for rec in state[key]
    }
    if known - covered:
        raise ValueError('Unfinished or interrupted pair requires separate adjudication')
    remaining = schedule[len(closed):]
    if any(run_exists(item['run_id']) for unit in remaining for item in unit['members']):
        raise ValueError('Unaccounted future run artifacts exist; refusing to overwrite')
    return remaining


def consecutive_failures(units):
    failures = collections.Counter()
    for unit in units:
        if unit.get('not_attempted') or unit.get('interrupted_by_user'):
            continue
        failures[unit['kind']] = 0 if unit['paired_complete'] else failures[unit['kind']] + 1
    return failures


class Campaign(Acquisition):
    def persist(self, **change):
        super().persist(**change)
        # Regenerate the descriptive summary only when an outcome changes.
        records = len(self.state['completed']) + len(self.state['thermal_stops'])
        key = (records, len(self.state['units']), self.state['status'])
        if key != getattr(self, '_summary_key', None):
            self._summary_key = key
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts/summarize.py')],
                capture_output=True, text=True,
            )
            if result.returncode:
                save(ROOT / 'analysis/summary_error.json', dict(error=result.stderr))

    def run(self):
        if self.args.resume_from:
            self.prepare_resume()
            self.run_remaining()
            return
        if (ROOT / 'acquisition/state.json').exists():
            raise RuntimeError('Existing campaign preserved; no automatic restart')
        self.persist()
        self.verify_identity()
        if not json.loads((ROOT / 'intake/pilot_acceptance.json').read_text())['passed']:
            raise RuntimeError('Fresh pilots failed')
        call(self.args.adb, self.args.serial, 'shell', 'am', 'force-stop', PACKAGE)
        self.calibrate()
        self.state['segment_id'] = 'initial'
        self.run_remaining()

    def prepare_resume(self):
        checkpoint = (ROOT / self.args.resume_from).resolve()
        review = json.loads((checkpoint / 'review.json').read_text())
        for relative, expected in review['source_hashes'].items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Checkpoint source changed: ' + relative)
        old = json.loads((ROOT / 'acquisition/state.json').read_text())
        schedule = json.loads((ROOT / 'planned_units.json').read_text())
        remaining_schedule(old, schedule, lambda rid: (ROOT / 'runs' / rid).exists())
        for relative, expected in json.loads((checkpoint / 'retained_raw_hashes.json').read_text()).items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Retained observation changed: ' + relative)
        self.verify_identity()
        self.baseline = json.loads((ROOT / 'acquisition/baseline.json').read_text())['reference']
        self.state = old
        self.state.setdefault('resumptions', []).append(dict(
            segment_id=review['segment_id'], checkpoint=str(checkpoint.relative_to(ROOT)),
            resumed_utc=utc(), previous_finished_utc=old.get('finished_utc'),
            closed_units_before_resume=len(old['units']),
            completed_pairs_before_resume=sum(unit['paired_complete'] for unit in old['units']),
        ))
        self.state.pop('error', None)
        self.state.pop('finished_utc', None)
        self.persist(pid=os.getpid(), status='running', phase='resume_preflight',
                     segment_id=review['segment_id'])
        event(event='explicit_resume', segment=review['segment_id'],
              checkpoint=str(checkpoint.relative_to(ROOT)), baseline_unchanged=True)

    def run_remaining(self):
        failures = consecutive_failures(self.state['units'])
        disabled = set(self.state.get('disabled_study_arms', []))
        previous_block = None
        schedule = json.loads((ROOT / 'planned_units.json').read_text())[len(self.state['units']):]
        segment = self.state.get('segment_id', 'initial')
        for unit in schedule:
            self.guard()
            verify_freeze()
            kind = unit['kind']
            if kind in disabled:
                for item in unit['members']:
                    self.state['excluded'].append(dict(
                        run=item['run_id'], reason='study_arm_stopped_before_attempt',
                        design=item,
                    ))
                self.state['units'].append(dict(
                    id=unit['id'], kind=kind, members=[], paired_complete=False,
                    starting_skin_difference=None, not_attempted=True,
                ))
                self.persist()
                continue
            reference = None
            starts, members = [], []
            rest = self.protocol['minimum_block_rest_seconds'] if (
                unit['block'] != previous_block or kind == 'repeated'
            ) else 0
            for index, item in enumerate(unit['members']):
                rid = item['run_id']
                start = self.recovery(rid, reference, rest if index == 0 else 0)
                self.state['attempted'].append(rid)
                if start is None:
                    self.state['excluded'].append(dict(
                        run=rid, reason='recovery_timeout', design=item,
                    ))
                    event(event='excluded', run=rid, reason='recovery_timeout')
                    if index == 0:
                        other = unit['members'][1]
                        self.state['excluded'].append(dict(
                            run=other['run_id'], reason='unlaunched_pair_partner',
                            design=other,
                        ))
                    break
                if reference is None:
                    reference = start
                    event(event='pair_reference', unit=unit['id'], reference=start)
                starts.append(start)
                event(event='launch', run=rid, design=item, starting_state=start, segment=segment)
                report = self.measure(item)
                members.append(dict(
                    run=rid, accepted=report['accepted_observations'],
                    outcome=report['outcome'],
                ))
                if report['accepted_observations']:
                    key = 'thermal_stops' if report['outcome'] == 'thermal_stop' else 'completed'
                    self.state[key].append(dict(
                        run=rid, requests=report['valid_request_count'], design=item, segment=segment,
                    ))
                else:
                    self.state['excluded'].append(dict(
                        run=rid, reason=report['outcome'], design=item,
                    ))
                event(event='finish', run=rid, report=report)
                self.persist()
                if not report['accepted_observations']:
                    if index == 0:
                        other = unit['members'][1]
                        self.state['excluded'].append(dict(
                            run=other['run_id'], reason='unlaunched_pair_partner',
                            design=other,
                        ))
                    break
                if kind == 'repeated':
                    before = time.monotonic()
                    recovered = self.recovery(rid + '-post-recovery', start)
                    event(
                        event='post_session_recovery', run=rid,
                        observed=recovered is not None,
                        elapsed_seconds=time.monotonic() - before,
                        end_state=recovered,
                        definition='first full eligible matched window; deadline-censored otherwise',
                    )
            paired = len(members) == 2 and all(m['accepted'] for m in members)
            self.state['units'].append(dict(
                id=unit['id'], kind=kind, members=members, paired_complete=paired,
                segment=segment,
                starting_skin_difference=abs(starts[1]['skin'] - starts[0]['skin'])
                if len(starts) == 2 else None,
            ))
            failures[kind] = 0 if paired else failures[kind] + 1
            if failures[kind] >= 3:
                disabled.add(kind)
                event(event='study_arm_stopped', kind=kind, consecutive_failed_pairs=3)
            self.persist(disabled_study_arms=sorted(disabled))
            previous_block = unit['block']
        self.persist(
            status='complete', phase='complete', current_run=None,
            schedule_exhausted=True, targets_all_observed=not self.state['excluded'],
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--resume-from', default=None,
                        help='Explicit reviewed checkpoint; original protocol and raw data must match')
    args = parser.parse_args()
    lock_name = 'thermalbench-' + hashlib.sha256(args.serial.encode()).hexdigest()[:16] + '.lock'
    with open(Path(tempfile.gettempdir()) / lock_name, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (ROOT / 'acquisition/state.json').exists() and not args.resume_from:
            raise RuntimeError('Existing acquisition preserved; no automatic restart')
        verify_freeze()
        app = Campaign(args)
        if args.resume_from:
            # Preserve the original observations even if preflight rejects resumption.
            app.state = json.loads((ROOT / 'acquisition/state.json').read_text())
        try:
            app.run()
        except BaseException as error:
            app.persist(
                status='paused' if isinstance(error, (InterruptedError, KeyboardInterrupt))
                else 'stopped_with_error', error=str(error),
            )
            event(event='controller_error', error=str(error))
            raise
        finally:
            app.persist(finished_utc=utc())
            call(args.adb, args.serial, 'shell', 'am', 'force-stop', PACKAGE,
                 timeout=10, check=False)
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts/analyze.py')],
                capture_output=True, text=True,
            )
            save(ROOT / 'analysis/final_analysis_status.json', dict(
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
            ))


if __name__ == '__main__':
    main()
