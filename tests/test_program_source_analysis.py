import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import program_extractor as extractor
import program_source_analysis as source
from program_rule_engine_v2 import Message


class SourceProgramTests(unittest.TestCase):
    def test_boss_and_hero_share_one_display_category(self):
        texts = ("Boss来了", "BOSS技能", "boss很强", "英雄模式", "英雄不能复活", "这个英雄")
        rows = [Message(t, f"douyin:{i+1}", text)
                for i, (t, text) in enumerate(zip((100, 130, 160, 200, 230, 260), texts))]
        nodes = source.analyze(rows)["nodes"]
        self.assertEqual([n["category"] for n in nodes], ["Boss英雄模式"])
        with patch.object(source, "load_messages", return_value=(rows, {"mode": "absolute-time-xml"})), \
             patch.object(extractor, "_trusted_refined_starts", return_value=[]):
            self.assertEqual(extractor.generate_program_list(["example.ass"]), {"Boss英雄模式(测试)": {"P1": [60]}})
        self.assertNotIn("Boss英雄模式", extractor.find_clip_program_nodes({}, [], [(m.time, m.text) for m in rows]))

    def test_boss_short_burst_and_unrelated_titles_are_rejected(self):
        short = self.rows("douyin", "BOSS很强", (100, 110, 120, 130, 140, 150))
        self.assertEqual(source.analyze(short)["nodes"], [])
        for text in ("英雄联盟", "反恐精英OL"):
            rows = self.rows("douyin", text, (100, 130, 160, 200, 230, 260))
            self.assertFalse(any(n["category"] == "Boss英雄模式" for n in source.analyze(rows)["nodes"]))

    def test_supported_boss_discussion_is_valid_evidence(self):
        for text in ("下把英雄", "猎人比boss厉害多了", "变一把boss或者英雄"):
            rows = self.rows("douyin", text, (100, 130, 160, 200, 230, 260))
            self.assertTrue(any(n["category"] == "Boss英雄模式" for n in source.analyze(rows)["nodes"]))

    def rows(self, platform, text, times):
        return [Message(t, f"{platform}:u{i+1}", text) for i, t in enumerate(times)]

    def test_douyin_alone_never_creates_laughter(self):
        rows = self.rows("douyin", "哈哈笑死了", range(100, 120))
        self.assertEqual(source.analyze(rows)["nodes"], [])

    def test_bilibili_seed_uses_douyin_reactions(self):
        rows = self.rows("bilibili", "[嘲笑]", range(100, 106))
        rows += self.rows("douyin", "笑死了", range(110, 116))
        node = source.analyze(rows)["nodes"][0]
        self.assertEqual(node["category"], "下饭坤")
        self.assertEqual(node["trigger_time"], 100)
        self.assertEqual(node["evidence"]["douyin"]["users"], 6)

    def test_single_user_spam_cannot_trigger(self):
        rows = [Message(t, "bilibili:one", "[嘲笑] 自私",) for t in range(100, 130)]
        self.assertEqual(source.analyze(rows)["nodes"], [])

    def test_douyin_can_support_every_keyword_category(self):
        for category, text in (("猎人模式", "这把猎人模式"), ("爽局", "爽局"),
                               ("自私坤", "自私"), ("内鬼坤", "害人精"), ("理财坤", "理财")):
            with self.subTest(category=category):
                times = (100, 120, 160, 210, 250, 280) if category == "猎人模式" else (200, 202, 204, 206)
                rows = self.rows("douyin", text, times)
                nodes = source.analyze(rows)["nodes"]
                self.assertEqual([n["category"] for n in nodes], [category])
                if category == "爽局":
                    self.assertEqual(nodes[0]["time"], 26)

    def test_supported_discussion_can_trigger_hunter(self):
        rows = self.rows("douyin", "什么时候玩猎人", (100, 120, 160, 210, 250, 280))
        result = source.analyze(rows)
        self.assertEqual([node["category"] for node in result["nodes"]], ["猎人模式"])

    def test_same_topic_across_platforms_is_one_node(self):
        rows = self.rows("douyin", "自私", range(100, 104))
        rows += self.rows("bilibili", "自私", range(102, 106))
        self.assertEqual(len(source.analyze(rows)["nodes"]), 1)

    def test_generate_uses_xml_analysis_without_changing_clip_nodes(self):
        rows = self.rows("douyin", "这把猎人模式", (100, 120, 160, 210, 250, 280))
        with patch.object(source, "load_messages", return_value=(rows, {"mode": "absolute-time-xml"})), \
             patch.object(extractor, "_trusted_refined_starts", return_value=[]):
            self.assertEqual(extractor.generate_program_list(["example.ass"]), {"猎人模式(测试)": {"P1": [60]}})
        self.assertNotIn("猎人模式", extractor.find_clip_program_nodes({}, [], [(m.time, m.text) for m in rows]))

    def test_loader_aligns_linked_source_and_rejects_wrong_room(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backup = root / "backup"
            backup.mkdir()
            def write(path, rows, origin):
                tree = ET.Element("i")
                for t, uid, text in rows:
                    ET.SubElement(tree, "d", p=f"{t},1,25,0,{origin+t},0,{uid},0").text = text
                ET.ElementTree(tree).write(path, encoding="utf-8")
            write(backup / "record.xml", [(0, "b1", "开始"), (500, "b2", "结束")], 1700000000)
            for name, room in (("correct", "https://room/1"), ("wrong", "https://room/2")):
                session = backup / "抖音弹幕" / name
                (session / "时间轴").mkdir(parents=True)
                write(session / "record.xml", [(10, "d1", "猎人"), (600, "d2", "猎人")], 1700000050)
                (session / "时间轴" / "record.timeline.json").write_text(json.dumps({
                    "room_url": room, "xml_file": "record.xml", "bilibili_video": "record.flv",
                }), encoding="utf-8")
            messages, report = source.load_messages(backup / "record.ass", root=root, config={"room_url": "https://room/1"})
            dy = [m for m in messages if m.user.startswith("douyin:")]
            self.assertEqual([m.time for m in dy], [60])
            self.assertEqual(len(report["sources"]), 1)


if __name__ == "__main__":
    unittest.main()
