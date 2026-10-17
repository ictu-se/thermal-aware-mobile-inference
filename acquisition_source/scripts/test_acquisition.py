import json,tempfile,unittest
from pathlib import Path
from acquire import window_ok,classify

class GateTests(unittest.TestCase):
 def setUp(self):
  self.p=dict(stable_seconds=60,maximum_sample_gap_seconds=10,battery_minimum_percent=99,stable_window_span_hal_units=1.5,baseline_tolerances=dict(skin=2,battery_hal=2,nsp0=8),pair_tolerances=dict(skin=1,battery_hal=1,nsp0=5))
  self.s=dict(skin=28,battery_hal=31,nsp0=35,override=False,severity=0,externally_powered=True,battery_level=100)
  self.w=[(t,dict(self.s)) for t in range(0,61,5)]
 def test_observed_window(self):self.assertTrue(window_ok(self.w,self.p,self.s,self.s))
 def test_not_yet_full_window(self):self.assertFalse(window_ok(self.w[:-1],self.p))
 def test_gap(self):self.assertFalse(window_ok([self.w[0],*self.w[3:]],self.p))
 def test_charge_override_and_severity(self):
  for key,val in [('battery_level',98),('override',True),('severity',1),('externally_powered',False)]:
   w=[(t,dict(s)) for t,s in self.w];w[5][1][key]=val;self.assertFalse(window_ok(w,self.p))
 def test_missing_secondary_sensor(self):
  self.w[5][1]['nsp0']=None;self.assertFalse(window_ok(self.w,self.p))
 def test_secondary_mismatch(self):
  self.w[5][1]['battery_hal']=33;self.assertFalse(window_ok(self.w,self.p,self.s,self.s))
 def test_cooling_below_pair_band_is_failure(self):
  self.w=[(t,dict(s,skin=26)) for t,s in self.w];self.assertFalse(window_ok(self.w,self.p,self.s,self.s))

class CompletionTests(unittest.TestCase):
 def report(self,outcome='duration_complete',seconds=900,severe=False,error=False,missing=False):
  with tempfile.TemporaryDirectory() as name:
   p=Path(name);start=1_000_000_000;end=start+int(seconds*1e9)
   row=dict(status='ok',phase='measure',actual_prefill_tokens=128,actual_decode_tokens=64,native_prefill_tokens_per_s=1280,native_decode_tokens_per_s=64,native_ttft_s=.115625,app_start_ns=start,app_end_ns=end,app_call_elapsed_ns=end-start)
   (p/'results.jsonl').write_text(json.dumps(row)+'\n')
   if not missing:(p/'done.json').write_text(json.dumps(dict(complete=True,outcome=outcome,measured_requests=1,first_request_ns=start,last_request_end_ns=end)))
   if severe:(p/'thermal_events.jsonl').write_text(json.dumps(dict(severity=3))+'\n')
   if error:(p/'runner_error.json').write_text('{}')
   return classify(p,dict(kind='repeated',prefill_tokens=128,decode_tokens=64,duration_seconds=900))
 def test_complete(self):self.assertTrue(self.report()['accepted_observations'])
 def test_short_window_not_complete(self):self.assertFalse(self.report(seconds=800)['accepted_observations'])
 def test_thermal_event_retained(self):self.assertTrue(self.report('thermal_stop',100,True)['accepted_observations'])
 def test_unverified_thermal_stop(self):self.assertFalse(self.report('thermal_stop',100)['accepted_observations'])
 def test_missing_completion(self):self.assertFalse(self.report(missing=True)['accepted_observations'])
 def test_runner_error(self):self.assertFalse(self.report(error=True)['accepted_observations'])
 def test_iteration_limit(self):self.assertFalse(self.report('iteration_limit')['accepted_observations'])
if __name__=='__main__':unittest.main()
