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

    def test_payoff_after_stable_tail_is_rejected(self):
        row = self.row(end=120)
        row.update(
            dominant_reaction="笑点铺垫",
            payoff_start_seconds=140,
            payoff_end_seconds=150,
            delayed_laughter_payoff=True,
        )
        rows = [row]
        rejected = dual.reject_unwatchable_candidates(rows)
        self.assertEqual(rows, [])
        self.assertEqual(rejected[0]["reason"], "payoff-after-stable-tail")

    def test_weak_douyin_laughter_without_payoff_is_rejected(self):
        row = self.row(policy="absolute-time-douyin", end=120)
        row.update(
            dominant_reaction="笑点",
            laughter_users=2,
            payoff_start_seconds=None,
        )
        rows = [row]
        rejected = dual.reject_unwatchable_candidates(rows)
        self.assertEqual(rows, [])
        self.assertEqual(rejected[0]["reason"], "weak-douyin-laughter-without-payoff")

    def test_bilibili_non_payoff_event_is_still_kept(self):
        row = self.row(end=120)
        row.update(
            dominant_reaction="笑点",
            laughter_users=2,
            payoff_start_seconds=None,
        )
        rows = [row]
        self.assertEqual(dual.reject_unwatchable_candidates(rows), [])
        self.assertEqual(rows, [row])

    def test_weak_douyin_lead_in_to_nearby_bilibili_event_is_kept(self):
        douyin = self.row(policy="absolute-time-douyin", end=120)
        douyin.update(
            dominant_reaction="笑点",
            laughter_users=2,
            payoff_start_seconds=None,
        )
        bilibili = self.row(end=180)
        bilibili.update(program_time_seconds=130, trigger_time_seconds=170)
        rows = [douyin, bilibili]
        self.assertEqual(dual.reject_unwatchable_candidates(rows), [])
        self.assertEqual(rows, [douyin, bilibili])


if __name__ == "__main__":
    unittest.main()
