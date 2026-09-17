import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_platform_program as dual


class StableTailTests(unittest.TestCase):
    def row(self, policy="absolute-time-bilibili", end=180):
        return dict(end_policy=policy, trigger_time_seconds=100,
                    program_time_seconds=60, event_end_seconds=end,
                    clip_group=1, platform_evidence={})

    def test_first_fall_not_delayed_wave(self):
        row = self.row()
        original = copy.deepcopy(row)
        dual.restore_stable_tails([row], [dual.EnergyInterval(98, 120, {}),
                                         dual.EnergyInterval(140, 160, {})], [], [])
        self.assertEqual(row["event_end_seconds"], 120)
        for key in ("program_time_seconds", "trigger_time_seconds", "clip_group", "end_policy"):
            self.assertEqual(row[key], original[key])

    def test_source_platform_and_supplement(self):
        for policy, expected in (("absolute-time-douyin", 125),
                                 ("dual-confirmed-bilibili-boundary", 120),
                                 ("watchable-shuangju", 130)):
            row = self.row(policy)
            dual.restore_stable_tails([row], [dual.EnergyInterval(98, 120, {})],
                                     [dual.EnergyInterval(98, 125, {})],
                                     [dual.EnergyInterval(98, 130, {})])
            self.assertEqual(row["event_end_seconds"], expected)

    def test_never_extend(self):
        row = self.row(end=115)
        dual.restore_stable_tails([row], [dual.EnergyInterval(98, 120, {})], [], [])
        self.assertEqual(row["event_end_seconds"], 115)

    def test_missing_interval_keeps_candidate(self):
        row = self.row()
        rows = [row]
        dual.restore_stable_tails(rows, [dual.EnergyInterval(200, 220, {})], [], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(row["event_end_seconds"], 180)
        self.assertEqual(row["platform_evidence"]["tail_boundary"]["policy"],
                         "unchanged-no-stable-interval")


if __name__ == "__main__":
    unittest.main()
