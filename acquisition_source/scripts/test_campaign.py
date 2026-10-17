import collections
import json
import unittest

from acquire import window_ok
from campaign import remaining_schedule, consecutive_failures
from run_device import ROOT


class AmendedGateTests(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads((ROOT / 'protocol.json').read_text())
        self.baseline = dict(skin=30, battery_hal=31, nsp0=34)
        self.sample = dict(
            skin=27, battery_hal=28, nsp0=29, override=False,
            severity=0, externally_powered=True, battery_level=100,
        )
        self.window = [(t, dict(self.sample)) for t in range(0, 61, 5)]

    def test_colder_first_start_is_permitted(self):
        self.assertTrue(window_ok(self.window, self.protocol, self.baseline))

    def test_colder_second_start_still_requires_matching(self):
        reference = dict(self.sample, skin=29)
        self.assertFalse(window_ok(self.window, self.protocol, self.baseline, reference))

    def test_hotter_than_baseline_cap_is_rejected(self):
        self.window[4][1]['battery_hal'] = 34
        self.assertFalse(window_ok(self.window, self.protocol, self.baseline))

    def test_secondary_channel_pair_mismatch_is_rejected(self):
        reference = dict(self.sample, nsp0=35)
        self.assertFalse(window_ok(self.window, self.protocol, self.baseline, reference))

    def test_nonfinite_sensor_cannot_pass(self):
        self.window[4][1]['nsp0'] = float('nan')
        self.assertFalse(window_ok(self.window, self.protocol, self.baseline))


class FrozenScheduleTests(unittest.TestCase):
    def test_counts_unique_identifiers_and_balance(self):
        units = json.loads((ROOT / 'planned_units.json').read_text())
        counts = collections.Counter(unit['kind'] for unit in units)
        self.assertEqual(counts, dict(main=60, repeated=6, overhead=40))
        members = [member for unit in units for member in unit['members']]
        self.assertEqual(len({member['run_id'] for member in members}), 212)
        directions = collections.defaultdict(collections.Counter)
        for unit in units:
            self.assertEqual(len(unit['members']), 2)
            a, b = unit['members']
            if unit['kind'] == 'overhead':
                self.assertEqual(a['model'], b['model'])
                self.assertEqual(a['workload'], b['workload'])
                self.assertEqual({a['monitoring'], b['monitoring']}, {'full', 'minimal'})
                directions[(a['model'], a['workload'])][a['monitoring']] += 1
        for counts in directions.values():
            self.assertEqual(counts, dict(full=5, minimal=5))


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.schedule = [dict(id='pair0', members=[dict(run_id='a'), dict(run_id='b')]),
                         dict(id='pair1', members=[dict(run_id='c'), dict(run_id='d')])]
        self.state = dict(status='paused', units=[dict(id='pair0')], attempted=['a', 'b'],
                          completed=[dict(run='a'), dict(run='b')], thermal_stops=[], excluded=[])

    def test_continues_without_repeating_accepted_members(self):
        self.assertEqual(remaining_schedule(self.state, self.schedule), self.schedule[1:])

    def test_incomplete_pair_cannot_cross_resume_boundary(self):
        self.state['completed'].append(dict(run='c'))
        self.assertRaises(ValueError, remaining_schedule, self.state, self.schedule)

    def test_already_attempted_member_cannot_be_retried(self):
        self.state['attempted'].append('c')
        self.assertRaises(ValueError, remaining_schedule, self.state, self.schedule)

    def test_changed_schedule_rejected(self):
        self.state['units'][0]['id'] = 'other'
        self.assertRaises(ValueError, remaining_schedule, self.state, self.schedule)

    def test_terminal_campaign_cannot_resume(self):
        self.state['status'] = 'complete'
        self.assertRaises(ValueError, remaining_schedule, self.state, self.schedule)

    def test_unaccounted_run_directory_is_preserved(self):
        self.assertRaises(ValueError, remaining_schedule, self.state, self.schedule,
                          lambda rid: rid == 'c')

    def test_user_pause_does_not_increment_or_reset_gate_failures(self):
        units = [dict(kind='overhead', paired_complete=False),
                 dict(kind='overhead', paired_complete=False, interrupted_by_user=True)]
        self.assertEqual(consecutive_failures(units)['overhead'], 1)
        units.append(dict(kind='overhead', paired_complete=False))
        self.assertEqual(consecutive_failures(units)['overhead'], 2)

    def test_completed_pair_resets_only_its_study_failure_count(self):
        units = [dict(kind='overhead', paired_complete=False),
                 dict(kind='main', paired_complete=False),
                 dict(kind='overhead', paired_complete=True)]
        self.assertEqual(dict(consecutive_failures(units)), dict(overhead=0, main=1))


if __name__ == '__main__':
    unittest.main()
