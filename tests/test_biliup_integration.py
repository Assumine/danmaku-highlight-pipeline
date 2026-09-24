import unittest
from pathlib import Path

from biliup_integration import (
    BiliupCLI,
    archive_edit_payload,
    broadcast_day,
    desired_remote_parts,
    ordered_source_inputs,
    parse_list_output,
    parse_show_output,
    replacement_payload,
)


def part(cid, title, *, ready=True):
    return {
        "cid": cid, "title": title, "filename": f"file-{cid}", "desc": "",
        "status": 0, "xcode_state": 6 if ready else 2,
    }


class BiliupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cli = BiliupCLI(Path("biliup.exe"), Path("cookies.json"))
        self.inputs = [
            {"date_part": "20260923", "base_name": "23时场00_20260923"},
            {"date_part": "20260923", "base_name": "19时场00_20260923"},
        ]

    def test_cli_commands_use_current_submit_options(self):
        self.assertEqual(self.cli.list_submissions()[-4:], ["--from-page", "1", "--max-pages", "1"])
        self.assertEqual(self.cli.show_submission("BVexample")[-2:], ["show", "BVexample"])
        upload = self.cli.upload(
            title="Example", description="Program", tags="game,stream",
            category_id=171, video_paths=["raw.flv", "danmaku.flv"],
        )
        self.assertEqual(upload[:4], ["biliup.exe", "--user-cookie", "cookies.json", "upload"])
        self.assertEqual(upload[-2:], ["raw.flv", "danmaku.flv"])
        self.assertEqual(upload[upload.index("--submit") + 1], "web")
        self.assertEqual(upload[upload.index("--charging-pay") + 1], "0")
        self.assertNotIn("--open-elec", upload)
        append = self.cli.append("BVexample", ["highlight.mp4"])
        self.assertEqual(append[-3:], ["--vid", "BVexample", "highlight.mp4"])

    def test_list_and_show_output_parsing(self):
        rows = parse_list_output("notice\n\x1b[32mBVexample Example title  2026-09-24\x1b[0m\n")
        self.assertEqual(rows, [("BVexample", "Example title")])
        self.assertEqual(parse_show_output('notice\n{"videos": []}'), {"videos": []})
        with self.assertRaises(ValueError):
            parse_show_output("no JSON")

    def test_07_boundary_and_source_part_numbers_are_local(self):
        next_morning = {"date_part": "20260924", "base_name": "01时场10_20260924"}
        self.assertEqual(broadcast_day(next_morning), "20260923")
        self.assertEqual(broadcast_day({"date_part": "20260924", "base_name": "07时场00_20260924"}), "20260924")
        ordered = ordered_source_inputs([next_morning, *self.inputs])
        self.assertEqual([item["base_name"][:2] for item in ordered], ["19", "23", "01"])
        self.assertEqual([item["source_part"] for item in ordered], [1, 2, 3])

    def test_raw_parts_precede_danmaku_parts_and_highlight(self):
        videos = [
            part(1, "[无弹幕版] 19时场00_20260923"),
            part(2, "[有弹幕版] 19时场00_20260923", ready=False),
            part(3, "[无弹幕版] 23时场00_20260923"),
            part(4, "[有弹幕版] 23时场00_20260923", ready=False),
            part(5, "下饭片段"),
        ]
        ordered = desired_remote_parts(videos, self.inputs)
        self.assertEqual([video["cid"] for video in ordered], [1, 3, 2, 4, 5])
        payload = archive_edit_payload({"archive": {"title": "Example", "limited_free": False}, "videos": videos}, ordered)
        self.assertEqual([video["filename"] for video in payload["videos"]], ["file-1", "file-3", "file-2", "file-4", "file-5"])
        self.assertNotIn("limited_free", payload)
        self.assertEqual(payload["title"], "Example")

    def test_missing_danmaku_part_is_allowed_but_unknown_raw_is_not(self):
        videos = [part(1, "[无弹幕版] 19时场00_20260923"), part(2, "[无弹幕版] 23时场00_20260923")]
        self.assertEqual(len(desired_remote_parts(videos, self.inputs)), 2)
        videos.append(part(3, "[无弹幕版]unregistered"))
        with self.assertRaises(ValueError):
            desired_remote_parts(videos, self.inputs)

    def test_daily_highlight_replacement_requires_ready_part(self):
        videos = [part(1, "[无弹幕版] 19时场00_20260923"), part(2, "下饭片段"), part(3, "下饭片段2"), part(4, "下饭片段", ready=False)]
        submission = {"archive": {"title": "Example"}, "videos": videos}
        with self.assertRaises(ValueError):
            replacement_payload(submission, old_cids=[2, 3], replacement_cid=4)
        with self.assertRaises(ValueError):
            replacement_payload(submission, old_cids=[1], replacement_cid=4)
        videos[3]["xcode_state"] = 6
        payload = replacement_payload(submission, old_cids=[2, 3], replacement_cid=4)
        self.assertEqual([video["filename"] for video in payload["videos"]], ["file-1", "file-4"])
        self.assertEqual(payload["videos"][1]["title"], "下饭片段")


if __name__ == "__main__":
    unittest.main()
