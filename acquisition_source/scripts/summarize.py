#!/usr/bin/env python3
"""Summarize only accepted observations; keep unit outcomes and acquisitions separate."""
import json,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
 state=json.loads((ROOT/'acquisition/state.json').read_text());records=state['completed']+state['thermal_stops'];groups={};long=[];byrun={};overhead=[]
 for rec in records:
  item=rec['design'];rid=rec['run'];directory=ROOT/'runs'/rid
  rows=[json.loads(line) for line in (directory/'results.jsonl').read_text().splitlines()];rows=[r for r in rows if r.get('status')=='ok' and r.get('phase')=='measure'];byrun[rid]=rows
  if item['kind']=='repeated':
   done=json.loads((directory/'done.json').read_text());events=[json.loads(x) for x in (directory/'thermal_events.jsonl').read_text().splitlines()];severe=[e for e in events if e.get('severity',0)>=3];q=max(1,len(rows)//4)
   long.append(dict(run=rid,model=item['model'],outcome=done['outcome'],requests=len(rows),window_s=(done['last_request_end_ns']-done['first_request_ns'])/1e9,severe_s=(severe[0]['device_monotonic_ns']-done['first_request_ns'])/1e9 if severe else None,early_tps=statistics.median(r['native_decode_tokens_per_s'] for r in rows[:q]),late_tps=statistics.median(r['native_decode_tokens_per_s'] for r in rows[-q:])))
  elif item['kind']=='main':groups.setdefault(item['model']+' / '+item['workload'],[]).extend(r['app_call_elapsed_ns']/1e9 for r in rows)
 for unit in state['units']:
  if unit['kind']=='overhead' and unit['paired_complete']:
   pair={json.loads((ROOT/'runs'/m['run']/'metadata.json').read_text())['monitoring']:byrun[m['run']][0]['app_call_elapsed_ns']/1e9 for m in unit['members']}
   overhead.append(dict(unit=unit['id'],full_s=pair['full'],minimal_s=pair['minimal'],difference_s=pair['full']-pair['minimal']))
 summary=dict(acquisition_status=state['status'],fixed_work_individual={key:dict(n=len(vals),mean_s=statistics.mean(vals),median_s=statistics.median(vals),sd_s=statistics.stdev(vals) if len(vals)>1 else None) for key,vals in groups.items()},long_sessions=long,overhead_pairs=overhead,excluded=state['excluded'],units=state['units'],note='Descriptive live summary; unmatched main points are individual-only. Paired/block analysis and figure regeneration required after acquisition.')
 out=ROOT/'analysis';out.mkdir(exist_ok=True);tmp=out/'summary.tmp';tmp.write_text(json.dumps(summary,indent=2));tmp.replace(out/'summary.json')
if __name__=='__main__':main()
