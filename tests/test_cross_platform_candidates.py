import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import dual_platform_program as dual
import program_rule_engine_v2 as engine


def candidate(start=100, end=140):
    return engine.Candidate(
        xml_file="bili.xml",
        program_time_seconds=max(0, start - engine.PRE_ROLL_SECONDS),
        trigger_time_seconds=start,
        event_end_seconds=end,
        dominant_reaction="笑点",
        messages=14,
        unique_users=12,
        effective_messages=14,
        density_lift=2.5,
        new_user_ratio=1.0,
        reaction_users=6,
        reaction_ratio=0.5,
        laughter_users=2,
        laughter_messages=2,
        laughter_ratio=0.167,
        score=10.0,
        echo_users=0,
        echo_text="",
        routine_ratio=0.0,
        placeholder_ratio=0.0,
        reaction_breakdown={"笑点": 6},
        evidence=[],
        payoff_start_seconds=start,
    )


class CrossPlatformCandidateTests(unittest.TestCase):
    def test_collective_reaction_is_recovered(self):
        with patch.object(dual.engine, "find_candidates", return_value=[candidate()]):
            rows = dual.supplemental_cross_platform_candidates(
                "bili.xml", [], [], 200,
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end_policy"], "pooled-cross-platform")

    def test_weak_or_routine_reaction_is_rejected(self):
        weak = candidate()
        weak.reaction_users = 3
        weak.reaction_ratio = 0.143
        routine = candidate(200, 240)
        routine.routine_ratio = 0.2
        with patch.object(
            dual.engine, "find_candidates", return_value=[weak, routine],
        ):
            rows = dual.supplemental_cross_platform_candidates(
                "bili.xml", [], [], 300,
            )
        self.assertEqual(rows, [])

    def test_existing_event_is_not_duplicated(self):
        existing = [{"trigger_time_seconds": 110, "event_end_seconds": 130}]
        with patch.object(dual.engine, "find_candidates", return_value=[candidate()]):
            rows = dual.supplemental_cross_platform_candidates(
                "bili.xml", [], existing, 200,
            )
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
