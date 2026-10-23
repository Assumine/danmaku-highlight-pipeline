import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline
import submission_worker as submitter
from montage_danmaku import map_comments, write_ass
from pipeline_runtime import digest, job_lock, read_json, save_json


class ClockTests(unittest.TestCase):
    def test_transition_clock_and_half_open_boundaries(self):
        root = ET.fromstring('<i><d p="10,1">one</d><d p="12,1">boundary</d><d p="20,1">two</d></i>')
        mapped = map_comments(root, [
            {"source_time": 10, "output_start": 0, "output_end": 2},
            {"source_time": 20, "output_start": 1.4, "output_end": 3.4}])
        self.assertEqual([n.get("p") for n in mapped], ["0.000,1", "1.400,1"])
        self.assertEqual(root[0].get("p"), "10,1")

    def test_ass_overflow_and_override_escaping(self):
        root = ET.Element("i")
        for _ in range(7):
            ET.SubElement(root, "d", p="0,1").text = "{\\pos(1,1)} test"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.ass"
            counts = write_ass(root, path)
            self.assertEqual(counts, {"rendered": 5, "overflow_dropped": 2})
            self.assertNotIn("{\\pos", path.read_text(encoding="utf-8-sig"))


class DurableTests(unittest.TestCase):
    def test_atomic_json_and_released_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "job.json"
            save_json(path, {"status": "failed"})
            save_json(path, {"status": "completed"})
            self.assertEqual(read_json(path)["status"], "completed")
            with self.assertRaises(RuntimeError):
                with job_lock(path.with_suffix(".lock")):
                    raise RuntimeError("interrupted")
            with job_lock(path.with_suffix(".lock")):
                pass

    def test_failed_editor_retry_preserves_first_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video, xml, timeline = root / "video.mp4", root / "source.xml", root / "timeline.json"
            video.write_bytes(b"fake")
            xml.write_text('<i><d p="60,1">test</d></i>', encoding="utf-8")
            save_json(timeline, [{"xml_file": "source.xml"}])
            with patch.object(pipeline, "validate_media_file", return_value={"duration": 120}), \
                 patch.object(pipeline, "build_program", return_value=(timeline, xml, {"source_errors": []})), \
                 patch.object(pipeline, "run", return_value=SimpleNamespace(returncode=1)):
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        pipeline.execute(video, xml, root / "workspace")
            paths = list((root / "workspace/records").glob("*/job.json"))
            job = read_json(paths[0])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(len(job["attempts"]), 2)
            self.assertNotEqual(job["attempts"][0]["manifest"], job["attempts"][1]["manifest"])


class UploadTests(unittest.TestCase):
    def test_reconcile_ignores_existing_cids_and_rejects_ambiguity(self):
        entry = {"title": "test", "before_cids": [1]}
        self.assertFalse(submitter.reconcile(entry, [{"cid": 1, "title": "test"}]))
        self.assertTrue(submitter.reconcile(entry, [{"cid": 2, "title": "test"}]))
        self.assertEqual(entry["cid"], 2)
        with self.assertRaises(RuntimeError):
            submitter.reconcile(entry, [{"cid": n, "title": "test"} for n in (2, 3)])

    def test_plan_only_never_calls_network(self):
        with patch.object(submitter, "submit") as execute:
            submitter.main(["job.json", "--target", "BVexample", "--biliup", "biliup", "--cookies", "local.json"])
        execute.assert_not_called()

    def test_unknown_upload_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outputs = [root / "raw.mp4", root / "burned.mp4"]
            for output in outputs:
                output.write_bytes(b"media")
            job = root / "job.json"
            save_json(job, {"status": "completed", "duration": 1, "outputs": list(map(str, outputs)),
                            "output_hashes": {str(p): digest(p) for p in outputs}})
            save_json(root / "submission.json", {"target": "BVexample", "entries": {
                submitter.TITLES[0]: {"title": submitter.TITLES[0], "status": "attempting", "before_cids": []}}})
            cli = SimpleNamespace(show_submission=lambda _: ["show"], append=lambda *_: self.fail("duplicate upload"))
            with patch.object(submitter, "validate_media_file"), \
                 patch.object(submitter, "run", return_value=SimpleNamespace(returncode=0, stdout='{"videos": []}')):
                with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
                    submitter.submit(job, "BVexample", cli)


if __name__ == "__main__":
    unittest.main()
