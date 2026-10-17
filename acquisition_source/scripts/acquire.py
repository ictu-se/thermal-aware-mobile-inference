#!/usr/bin/env python3
"""Frozen, paired single-handset acquisition; all attempts and outcomes are retained."""
import argparse, datetime, fcntl, hashlib, json, math, os, re, subprocess, sys, tempfile, time
from pathlib import Path
from run_device import ROOT, PACKAGE, call, sensors, validate


def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save(path, data):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2));temp.replace(path)
def event(**data):
    with (ROOT/'acquisition/events.jsonl').open('a') as f:f.write(json.dumps(dict(utc=utc(),**data))+'\n')
def current(adb,serial):
    raw=call(adb,serial,'shell','dumpsys','thermalservice');bat=call(adb,serial,'shell','dumpsys','battery')
    channels={x['name']:x['value'] for x in sensors(raw)}
    severity=re.search(r'Thermal Status:\s*(\d+)',raw);level=re.search(r'\blevel:\s*(\d+)',bat)
    return dict(skin=channels.get('skin'),battery_hal=channels.get('battery'),nsp0=channels.get('nsp0'),
        channels=channels,severity=int(severity[1]) if severity else None,
        override='IsStatusOverride: true' in raw,battery_level=int(level[1]) if level else None,
        externally_powered=bool(re.search(r'(?:AC|USB|Wireless|Dock) powered:\s*true',bat)))
def window_ok(window, protocol, baseline=None, reference=None):
    if len(window)<2 or window[-1][0]-window[0][0]<protocol['stable_seconds']:return False
    if any(b[0]-a[0]>protocol['maximum_sample_gap_seconds'] for a,b in zip(window,window[1:])):return False
    for _,s in window:
        if s['override'] or s['severity']!=0 or not s['externally_powered'] or (s['battery_level'] or 0)<protocol['battery_minimum_percent']:return False
        for key in protocol['baseline_tolerances']:
            if s.get(key) is None or not math.isfinite(s[key]):return False
            if baseline is not None:
                difference=s[key]-baseline[key]
                if protocol.get('baseline_comparison')!='upper_only':difference=abs(difference)
                if difference>protocol['baseline_tolerances'][key]:return False
            if reference is not None and abs(s[key]-reference[key])>protocol['pair_tolerances'][key]:return False
    return max(s['skin'] for _,s in window)-min(s['skin'] for _,s in window)<=protocol['stable_window_span_hal_units']
def classify(directory,item):
    donepath=directory/'done.json';done=json.loads(donepath.read_text()) if donepath.exists() else {}
    report=validate(directory,item.get('mode','fixed'),item['prefill_tokens'],item['decode_tokens'])
    rows=[json.loads(x) for x in (directory/'results.jsonl').read_text().splitlines()] if (directory/'results.jsonl').exists() else []
    measured=[r for r in rows if r.get('phase')=='measure' and r.get('status')=='ok']
    outcome=done.get('outcome','missing_completion')
    accepted=done.get('complete') is True and report['passed'] and not (directory/'runner_error.json').exists()
    accepted=accepted and done.get('measured_requests')==len(measured)
    if measured:
        accepted=accepted and done.get('first_request_ns')==measured[0].get('app_start_ns')
        for row in measured:
            if item.get('mode','fixed')=='fixed':
                accepted=accepted and row.get('app_end_ns',0)-row.get('app_start_ns',0)==row.get('app_call_elapsed_ns')
        if item.get('mode','fixed')=='fixed':accepted=accepted and done.get('last_request_end_ns')==measured[-1].get('app_end_ns')
    if item['kind']=='repeated':
        accepted=accepted and outcome in ('duration_complete','thermal_stop') and len(measured)>0
        if outcome=='duration_complete':
            accepted=accepted and (done.get('last_request_end_ns',0)-done.get('first_request_ns',0))/1e9>=item['duration_seconds']
        if outcome=='thermal_stop':
            events=directory/'thermal_events.jsonl'
            severe=events.exists() and any(json.loads(x).get('severity',0)>=3 for x in events.read_text().splitlines())
            accepted=accepted and severe
    else:accepted=accepted and outcome=='completed' and len(measured)==item.get('iterations',1)
    result=dict(accepted_observations=bool(accepted),outcome=outcome if accepted else 'runner_or_validation_failure',valid_request_count=len(measured) if accepted else 0,validation=report,app_outcome=outcome)
    save(directory/'followup_validation.json',result);return result

def transient_connection_error(error):
    if isinstance(error,subprocess.TimeoutExpired):return True
    message=str(error).lower()
    return any(x in message for x in ('no devices/emulators','device offline','device not found','transport error','connection closed')) or ('device' in message and 'not found' in message)

class Acquisition:
    def __init__(self,args):
        self.args=args;self.protocol=json.loads((ROOT/'protocol.json').read_text());self.baseline=None
        self.state=dict(started_utc=utc(),pid=os.getpid(),status='running',phase='preflight',completed=[],thermal_stops=[],excluded=[],attempted=[],units=[],reference_skin=None)
    def persist(self,**change):self.state.update(change);save(ROOT/'acquisition/state.json',self.state)
    def snapshot(self):return current(self.args.adb,self.args.serial)
    def guard(self):
        if (ROOT/'PAUSE').exists():raise InterruptedError('Pause requested; no further measurement launched')
    def recovery(self,label,reference=None,minimum_rest=0,deadline=None):
        self.persist(phase='recovery',current_run=label,reference_skin=reference['skin'] if reference else None)
        started=time.monotonic();window=[];deadline=deadline or self.protocol['maximum_recovery_seconds']
        while time.monotonic()-started<deadline:
            self.guard()
            try:
                sample=self.snapshot()
            except (RuntimeError,subprocess.TimeoutExpired) as error:
                if not transient_connection_error(error):raise
                window=[]
                if self.state.get('phase')!='waiting_connection':
                    event(event='connection_wait',before=label,error=str(error))
                    self.persist(phase='waiting_connection')
                time.sleep(self.protocol['recovery_poll_seconds'])
                continue
            now=time.monotonic()
            if self.state.get('phase')=='waiting_connection':
                event(event='connection_restored',before=label)
                self.persist(phase='recovery')
            if sample['override']:raise RuntimeError('Thermal status override detected')
            if now-started>=minimum_rest:
                window.append((now,sample))
                while len(window)>1 and window[1][0]<=now-self.protocol['stable_seconds']:window.pop(0)
            ok=window_ok(window,self.protocol,self.baseline,reference)
            event(event='recovery_sample',before=label,reference=reference['skin'] if reference else None,eligible=ok,**sample)
            if ok:return sample
            time.sleep(self.protocol['recovery_poll_seconds'])
        return None
    def verify_identity(self):
        expected=json.loads((ROOT/'intake/build_identity.json').read_text())
        apk=call(self.args.adb,self.args.serial,'shell','pm','path',PACKAGE).strip().split('package:')[1]
        actual=call(self.args.adb,self.args.serial,'shell','sha256sum',apk).split()[0]
        if actual!=expected['new_apk_sha256']:raise RuntimeError('Installed APK differs from pinned build')
        old=(ROOT/'intake/fingerprint.txt').read_text().strip()
        if call(self.args.adb,self.args.serial,'shell','getprop','ro.build.fingerprint').strip()!=old:raise RuntimeError('Firmware changed')
    def measure(self,item):
        self.guard();self.verify_identity();rid=item['run_id'];self.persist(phase='measuring',current_run=rid)
        manifest={m['id']:m for m in json.loads((ROOT/'models/manifest.json').read_text())}
        duration=item.get('duration_seconds',0)
        cmd=[sys.executable,str(ROOT/'scripts/run_device.py'),'--adb',self.args.adb,'--serial',self.args.serial,'--mode',item.get('mode','fixed'),'--model',f'/sdcard/Android/data/{PACKAGE}/files/models/'+manifest[item['model']]['filename'],'--run-id',rid,'--prefill',str(item['prefill_tokens']),'--decode',str(item['decode_tokens']),'--iterations',str(item.get('iterations',1)),'--duration',str(duration),'--timeout',str(duration+180),'--monitoring',item.get('monitoring','full')]
        try:
            with (ROOT/'acquisition'/(rid+'.txt')).open('w') as log:
                subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=duration+300)
            return classify(ROOT/'runs'/rid,item)
        finally:call(self.args.adb,self.args.serial,'shell','am','force-stop',PACKAGE,check=False)
    def calibrate(self):
        # Calibration is separate from all performance outcomes and after the charge gate.
        import statistics
        if self.recovery('charge-and-idle',minimum_rest=300,deadline=28800) is None:raise RuntimeError('Charge/idle gate not reached within eight hours')
        samples=[];start=time.monotonic();self.persist(phase='calibration',current_run='idle-calibration')
        while time.monotonic()-start<self.protocol['calibration_seconds']:
            self.guard();s=self.snapshot();event(event='calibration_sample',**s);samples.append(s)
            if s['severity']!=0 or s['override'] or not s['externally_powered'] or (s['battery_level'] or 0)<self.protocol['battery_minimum_percent']:raise RuntimeError('Idle calibration conditions changed')
            time.sleep(5)
        self.baseline={key:statistics.median(s[key] for s in samples if s[key] is not None) for key in self.protocol['baseline_tolerances']}
        save(ROOT/'acquisition/baseline.json',dict(reference=self.baseline,samples=len(samples),utc=utc(),method='median of 600-second near-full-charge idle calibration'))
    def run(self):
        path=ROOT/'acquisition/state.json'
        if path.exists():raise RuntimeError('Existing state: explicit checkpoint review required before resuming')
        self.persist();self.verify_identity()
        pilots=json.loads((ROOT/'intake/pilot_acceptance.json').read_text());assert pilots['passed']
        call(self.args.adb,self.args.serial,'shell','am','force-stop',PACKAGE)
        self.calibrate();failed=0;previous=None
        for unit in json.loads((ROOT/'planned_units.json').read_text()):
            self.guard();reference=None;members=[];starts=[]
            rest=300 if unit['block']!=previous or unit['kind']=='repeated' else 0
            for index,item in enumerate(unit['members']):
                rid=item['run_id'];start=self.recovery(rid,reference,rest if index==0 else 0)
                self.state['attempted'].append(rid)
                if start is None:
                    self.state['excluded'].append(dict(run=rid,reason='recovery_timeout',design=item));event(event='excluded',run=rid,reason='recovery_timeout')
                    if index==0:
                        other=unit['members'][1];self.state['excluded'].append(dict(run=other['run_id'],reason='unlaunched_pair_partner',design=other))
                    break
                if reference is None:reference=start;event(event='pair_reference',unit=unit['id'],reference_skin=start['skin'])
                starts.append(start);event(event='launch',run=rid,design=item,starting_state=start)
                report=self.measure(item);members.append(dict(run=rid,accepted=report['accepted_observations'],outcome=report['outcome']))
                key='thermal_stops' if report['outcome']=='thermal_stop' else 'completed'
                if report['accepted_observations']:self.state[key].append(dict(run=rid,requests=report['valid_request_count'],design=item))
                else:self.state['excluded'].append(dict(run=rid,reason=report['outcome'],design=item))
                event(event='finish',run=rid,report=report);self.persist()
                if not report['accepted_observations']:
                    if index==0:
                        other=unit['members'][1];self.state['excluded'].append(dict(run=other['run_id'],reason='unlaunched_pair_partner',design=other))
                    break
                if item['kind']=='repeated':
                    # Return to pre-session band; retain the first full eligible window or censor at deadline.
                    t=time.monotonic();recovered=self.recovery(rid+'-post-recovery',start)
                    event(event='post_session_recovery',run=rid,observed=recovered is not None,elapsed_seconds=time.monotonic()-t,end_state=recovered)
            paired=len(members)==2 and all(m['accepted'] for m in members)
            self.state['units'].append(dict(id=unit['id'],kind=unit['kind'],members=members,paired_complete=paired,starting_skin_difference=abs(starts[1]['skin']-starts[0]['skin']) if len(starts)==2 else None))
            self.persist();previous=unit['block'];failed=0 if paired else failed+1
            if failed>=3:self.persist(status='stopped_after_consecutive_failed_pairs');return
        self.persist(status='complete',phase='complete',current_run=None)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--adb',required=True);ap.add_argument('--serial',required=True);a=ap.parse_args()
    lock=open(Path(tempfile.gettempdir())/('thermalbench-'+hashlib.sha256(a.serial.encode()).hexdigest()[:16]+'.lock'),'w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if (ROOT/'acquisition/state.json').exists():raise RuntimeError('Existing acquisition state preserved; review checkpoint before resuming')
    expected=(ROOT/'protocol.sha256').read_text().strip()
    assert hashlib.sha256((ROOT/'protocol.json').read_bytes()).hexdigest()==expected,'Protocol hash changed'
    app=Acquisition(a)
    save(ROOT/'intake/acquisition_code_hashes.json',{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in [ROOT/'protocol.json',ROOT/'planned_units.json',*sorted((ROOT/'scripts').glob('*.py'))]})
    try:app.run()
    except BaseException as e:
        app.persist(status='paused' if isinstance(e,(InterruptedError,KeyboardInterrupt)) else 'stopped_with_error',error=str(e));event(event='controller_error',error=str(e));raise
    finally:
        app.persist(finished_utc=utc());call(a.adb,a.serial,'shell','am','force-stop',PACKAGE,timeout=10,check=False)
if __name__=='__main__':main()
