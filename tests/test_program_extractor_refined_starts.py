import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import program_extractor as extractor


class DisplayOnlyProgramTests(unittest.TestCase):
    def test_sustained_hunter_repetition_is_display_only(self):
        times = (100, 110, 120, 160, 180, 210, 220, 250)
        comments = [(time, f"猎人模式 {index}") for index, time in enumerate(times)]

        display_nodes = extractor.find_display_only_program_nodes(comments)
        clip_nodes = extractor.find_clip_program_nodes({}, [], comments)
        points = extractor.find_program_points({}, [], comments)

        self.assertNotIn("猎人模式", clip_nodes)
        self.assertEqual(points["猎人模式"], [60])
        node = display_nodes["猎人模式"][0]
        self.assertTrue(node["display_only"])
        self.assertEqual(node["duration_seconds"], 150)
        self.assertEqual(node["message_count"], 8)
        self.assertEqual(node["section_message_counts"], [4, 4])

    def test_hunter_rejects_short_burst_and_one_sided_repetition(self):
        short = [(100 + index, "猎人") for index in range(6)]
        one_sided = [
            (time, "猎人")
            for time in (100, 105, 110, 115, 120, 260)
        ]
        self.assertEqual(extractor.find_display_only_program_nodes(short), {})
        self.assertEqual(extractor.find_display_only_program_nodes(one_sided), {})

    def test_sparse_but_continuous_hunter_topic_is_retained(self):
        comments = [
            (time, "猎人")
            for time in (100, 110, 120, 305, 315, 325)
        ]
        nodes = extractor.find_display_only_program_nodes(comments)["猎人模式"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["section_message_counts"], [3, 3])


class RefinedProgramStartsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.records = self.root / "records"
        self.records.mkdir()
        self.target_ass = self.root / "示例主播 22时场19_20260904.ass"
        self.target_ass.write_text("target", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_manifest(self, *, completed=True, selected=(1,), date="20260904"):
        record = self.records / f"示例主播 22时场19_{date}_下饭片段"
        record.mkdir()
        source_ass = record / f"示例主播 22时场19_{date}.ass"
        source_ass.write_text("analysis", encoding="utf-8")
        ass_hash = hashlib.sha256(source_ass.read_bytes()).hexdigest()
        payload = {
            "ass": str(source_ass),
            "automation_audit": {
                "decision_mode": "automatic-only",
                "manual_overrides": [],
                "selected_indexes": list(selected),
                "speech_mode": "whisper",
                "render_completed": completed,
                "quality_profile": "production",
                "output_resolution": "source",
                "output_fps": "source",
                "ass_sha256": ass_hash,
            },
            "settings": {
                "start_boundary_policy": "speech-turn-containing-40s-anchor",
            },
            "highlights": [
                {
                    "index": 1,
                    "initial_start": 100.0,
                    "start": 96.5,
                    "start_basis": "speech-turn-containing-40s-anchor",
                },
                {
                    "index": 2,
                    "initial_start": 210.0,
                    "start": 205.0,
                    "start_basis": "speech-turn-containing-40s-anchor",
                },
            ],
        }
        (record / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_reuses_only_selected_nearby_refined_start(self):
        self.write_manifest()
        program = {
            "下饭坤": {"P1": [102.0, 210.0, 400.0]},
            "自私坤": {"P1": [99.0]},
        }

        result = extractor.refine_program_list_starts(
            program,
            [self.target_ass],
            records_root=self.records,
        )

        self.assertEqual(result["下饭坤"]["P1"], [96.5, 210.0, 400.0])
        self.assertEqual(result["自私坤"]["P1"], [96.5])

    def test_incomplete_or_wrong_date_record_is_ignored(self):
        self.write_manifest(completed=False)
        program = {"下饭坤": {"P1": [102.0]}}

        result = extractor.refine_program_list_starts(
            program,
            [self.target_ass],
            records_root=self.records,
        )

        self.assertEqual(result, {"下饭坤": {"P1": [102.0]}})

    def test_no_record_keeps_existing_program_list(self):
        program = {"下饭坤": {"P1": [102.0]}}

        result = extractor.refine_program_list_starts(
            program,
            [self.target_ass],
            records_root=self.records,
        )

        self.assertEqual(result, {"下饭坤": {"P1": [102.0]}})

    def test_display_only_category_never_reuses_clip_start(self):
        self.write_manifest()
        program = {"猎人模式": {"P1": [102.0]}}

        result = extractor.refine_program_list_starts(
            program,
            [self.target_ass],
            records_root=self.records,
        )

        self.assertEqual(result, {"猎人模式": {"P1": [102.0]}})


if __name__ == "__main__":
    unittest.main()
