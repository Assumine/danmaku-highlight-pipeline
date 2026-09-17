import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import highlight_editor as editor


def highlight(index):
    return editor.Highlight(
        index=index,
        start=0.0,
        coarse_end=10.0,
        final_end=10.0,
        activity_start=5.0,
        activity_end=8.0,
        review_end=10.0,
    )


def edge(index, side, level, immediate, votes=0, score=-12.0):
    return editor.AudioEdgeAnalysis(
        clip_index=index,
        side=side,
        immediate_rms_db=immediate,
        score_2s=score,
        score_4s=score,
        score_6s=score,
        level=level,
        intense_votes=votes,
    )


class TransitionPlanTests(unittest.TestCase):
    def test_directional_rules_and_safe_fallback(self):
        highlights = [highlight(index) for index in range(1, 6)]
        edges = [
            edge(1, "tail", "intense", -10.0, votes=3),
            edge(2, "head", "medium", -18.0),
            edge(2, "tail", "medium", -18.0),
            edge(3, "head", "intense", -10.0, votes=3),
            edge(3, "tail", "intense", -9.0, votes=3),
            edge(4, "head", "intense", -11.0, votes=2),
            edge(4, "tail", "intense", -20.0, votes=3),
            edge(5, "head", "calm", -20.0),
        ]

        plans = editor.plan_transitions_from_edges(highlights, edges)

        self.assertEqual(
            [plan.kind for plan in plans],
            ["l-style", "j-style", "intense-to-intense", "standard"],
        )
        self.assertEqual(plans[0].audio_duration, 0.10)
        self.assertEqual(plans[1].audio_duration, 0.45)
        self.assertEqual(plans[2].video_duration, 0.40)
        self.assertEqual(plans[3].audio_duration, 0.20)

    def test_relative_level_classification(self):
        edges = [
            edge(1, "head", "medium", -24.0, score=-20.0),
            edge(1, "tail", "medium", -24.0, score=-19.0),
            edge(2, "head", "medium", -14.0, score=-13.0),
            edge(2, "tail", "medium", -14.0, score=-12.0),
            edge(3, "head", "medium", -8.0, score=-7.0),
            edge(3, "tail", "medium", -8.0, score=-6.0),
        ]

        analysis = editor.classify_audio_edges(edges)

        self.assertEqual(analysis["status"], "classified")
        self.assertEqual([item.level for item in edges], [
            "calm", "calm", "medium", "medium", "intense", "intense"
        ])
        self.assertEqual(edges[-1].intense_votes, 3)

    def test_dynamic_plan_is_unchanged_by_tail_audio_adjustment(self):
        highlights = [highlight(1), highlight(2)]
        highlights[0].tail_adjustment_kind = "audio-weak-valley"
        highlights[0].tail_adjustment_original_end = 10.0
        highlights[0].tail_adjustment_endpoint_db = -10.0
        highlights[0].tail_adjustment_valley_db = -18.0
        highlights[0].tail_adjustment_shift = 0.40
        highlights[0].final_end = 9.60
        edges = [
            edge(1, "tail", "medium", -18.0),
            edge(2, "head", "medium", -18.0),
        ]

        plans = editor.plan_transitions_from_edges(highlights, edges)

        self.assertEqual(plans[0].kind, "standard")
        self.assertAlmostEqual(plans[0].video_duration, 0.60)
        self.assertAlmostEqual(plans[0].audio_duration, 0.20)


if __name__ == "__main__":
    unittest.main()
