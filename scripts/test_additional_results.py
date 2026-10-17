import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('additional_results', Path(__file__).with_name('additional_results.py'))
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AdditionalResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.retry = self.root / 'completion_experiments_20261017_e'
        self.grid = self.root / 'backend_diagnostics_20261017'
        self.output = self.root / 'generated'
        self.write(self.retry / 'acquisition/state.json', {'status':'complete','completed':[{'requests':1}], 'excluded':[],
                   'units':[{'id':'pair','paired_complete':False,'members':[{'run':'minimal','accepted':True}]}]})
        self.write(self.retry / 'planned_units.json', [{'id':'pair','kind':'overhead','members':[
                   {'run_id':'minimal','model':'fastvlm-sm8750','monitoring':'minimal'},
                   {'run_id':'full','model':'fastvlm-sm8750','monitoring':'full'}]}])
        self.jsonlines(self.retry / 'runs/minimal/results.jsonl', [{'phase':'measure','app_call_elapsed_ns':1700000000}])
        self.write(self.retry / 'profiling/state.json', {'status':'complete','results':[]})
        self.schedule = [{'model':model,'backend':backend} for model in ['fastvlm-sm8750','gemma4-sm8750'] for backend in ['NPU','CPU','GPU']]
        self.write(self.grid / 'protocol.json', {'schedule':self.schedule})
        self.write(self.grid / 'profiling/state.json', {'status':'complete','results':[]})
    def tearDown(self):
        self.temp.cleanup()
    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    def jsonlines(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    def backend_request(self, metadata_backend='CPU'):
        self.write(self.grid / 'profiling/state.json', {'status':'complete','results':[
            {'model':'fastvlm-sm8750','backend':'CPU','run':'cpu','outcome':'capture_error'}]})
        directory = self.grid / 'runs/cpu'
        self.write(directory / 'validation.json', {'passed':True})
        self.write(directory / 'done.json', {'complete':True,'measured_requests':3})
        self.write(directory / 'metadata.json', {'backend_requested':metadata_backend})
        self.jsonlines(directory / 'results.jsonl', [{'phase':'measure','status':'ok','actual_prefill_tokens':128,
            'actual_decode_tokens':64,'app_call_elapsed_ns':1000000000,'app_start_ns':i*2000000000,
            'app_end_ns':i*2000000000+1000000000} for i in range(3)])
    def test_unmatched_partner_and_unlaunched_conditions_remain_missing(self):
        metrics = analysis.generate(self.root, self.output)
        pair = metrics['retry']['pairs'][0]
        self.assertIsNone(pair['first_quantity_s'])
        self.assertEqual(pair['second_quantity_s'], 1.7)
        self.assertIsNone(pair['difference_s'])
        self.assertEqual(len(metrics['backend']['conditions']), 6)
        self.assertTrue(all(row['median_sdk_call_s'] is None for row in metrics['backend']['conditions']))
        json.dumps(metrics, allow_nan=False)
    def test_valid_native_requests_remain_distinct_from_failed_trace(self):
        self.backend_request()
        metrics = analysis.generate(self.root, self.output)
        cpu = next(row for row in metrics['backend']['conditions'] if row['run']=='cpu')
        self.assertEqual(cpu['accepted_requests'], 3)
        self.assertEqual(cpu['median_sdk_call_s'], 1)
        self.assertFalse(cpu['trace_available'])
        self.assertIsNone(cpu['phase_slices'])
    def test_misassigned_requested_backend_is_not_accepted(self):
        self.backend_request(metadata_backend='NPU')
        metrics = analysis.generate(self.root, self.output)
        cpu = next(row for row in metrics['backend']['conditions'] if row['run']=='cpu')
        self.assertEqual(cpu['accepted_requests'], 0)
        self.assertIsNone(cpu['median_sdk_call_s'])
    def test_empty_vendor_and_power_streams_are_not_zero_work_or_energy(self):
        hardware = self.root / 'hardware_event_diagnostics_20261017'
        self.write(hardware / 'protocol.json', {'run_prefix':'hardware17','schedule':[{'model':'fastvlm-sm8750','backend':'NPU'}]})
        self.write(hardware / 'profiling/state.json', {'status':'complete','results':[{
            'model':'fastvlm-sm8750','backend':'NPU','run':'hardware17-npu-fastvlm-sm8750','outcome':'captured',
            'trace_sha256':'synthetic-fixture','trace_bytes':1,'query_returncodes':{'hardware_events':0,'power_counters':0}}]})
        metrics = analysis.generate(self.root, self.output)
        item = metrics['hardware']['conditions'][0]
        self.assertEqual(item['observed_application_vendor_events'], 0)
        self.assertFalse(item['vendor_event_evidence_available'])
        self.assertEqual(item['vendor_event_coverage'], 'unverified_coverage_no_absence_claim')
        self.assertFalse(item['energy_per_request_validated'])
        self.assertFalse(item['independent_npu_operator_placement_verified'])


if __name__ == '__main__':
    unittest.main()
