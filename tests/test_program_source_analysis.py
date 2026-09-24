import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import program_extractor as extractor
import program_rule_engine_v2 as engine
import program_source_analysis as source
from program_rule_engine_v2 import Message


class SourceProgramTests(unittest.TestCase):
    def test_routine_chat_topics_do_not_form_echo_evidence(self):
        for text in ("老板糊涂", "准备下播", "这是录播", "失踪人口回归"):
            with self.subTest(text=text):
                rows = [Message(float(i), f"u{i}", text) for i in range(8)]
                metrics = engine.window_metrics(rows, set(), 0.05)
                self.assertEqual(metrics["echo_users"], 0)

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

    def test_shuangju_display_category_is_marked_as_testing(self):
        analysis = {"nodes": [{"category": "爽局", "time": 90}], "sources": {"warnings": []}}
        with patch.object(extractor, "analyze_source_program", return_value=analysis), \
             patch.object(extractor, "refine_program_list_starts", side_effect=lambda result, *_args, **_kwargs: result):
            self.assertEqual(extractor.generate_program_list(["example.ass"]), {"爽局(测试)": {"P1": [90]}})

    def test_boss_short_burst_and_unrelated_titles_are_rejected(self):
        short = self.rows("douyin", "BOSS很强", (100, 110, 120, 130, 140, 150))
        self.assertEqual(source.analyze(short)["nodes"], [])
        for text in ("英雄联盟", "反恐精英OL"):
            rows = self.rows("douyin", text, (100, 130, 160, 200, 230, 260))
            self.assertFalse(any(n["category"] == "Boss英雄模式" for n in source.analyze(rows)["nodes"]))

    def test_noncurrent_boss_discussion_is_rejected(self):
        for text in ("下把英雄", "猎人比boss厉害多了", "变一把boss或者英雄",
                     "盲猜这把boss", "想看你变英雄", "好久没看坤变BOSS了",
                     "下次再来个英雄", "速来boss", "让管理员开英雄模式"):
            rows = self.rows("douyin", text, (100, 130, 160, 200, 230, 260))
            result = source.analyze(rows)
            self.assertFalse(any(n["category"] == "Boss英雄模式" for n in result["nodes"]))
            self.assertTrue(any(r["reason"] == "non-current-request-prediction-or-history"
                                for r in result["rejected"]))

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
                    self.assertEqual(nodes[0]["time"], 190)

    def test_weak_shuangju_reactions_after_streamer_failure_are_rejected(self):
        cases = (
            ("舒服了", "理财"),
            ("僵尸爽了", "一枪不开"),
        )
        for reaction, failure in cases:
            with self.subTest(reaction=reaction):
                rows = [
                    Message(190, "bilibili:failure", failure),
                    Message(200, "bilibili:b1", reaction),
                    Message(202, "bilibili:b2", reaction),
                    Message(204, "douyin:d1", reaction),
                    Message(206, "douyin:d2", reaction),
                ]
                self.assertFalse(any(
                    node["category"] == "爽局" for node in source.analyze(rows)["nodes"]
                ))

    def test_shuangju_requires_explicit_streamer_benefit(self):
        for anchor in ("让坤爽了", "这下主播舒服了", "鱼刺坤赢麻了"):
            with self.subTest(valid=anchor):
                rows = [Message(200, "douyin:anchor", anchor)]
                rows += [
                    Message(202, "douyin:support-1", "爽了"),
                    Message(204, "bilibili:support-2", "舒服了"),
                    Message(206, "douyin:support-3", "抓爽了"),
                ]
                nodes = source.analyze(rows)["nodes"]
                self.assertEqual([node["category"] for node in nodes], ["爽局"])

        for text in ("舒服了", "赢麻", "碾压", "僵尸爽了", "坤我想起飞"):
            with self.subTest(ambiguous=text):
                rows = self.rows("douyin", text, (200, 202, 204, 206))
                self.assertFalse(any(node["category"] == "爽局"
                                     for node in source.analyze(rows)["nodes"]))

    def test_request_cannot_trigger_hunter(self):
        rows = self.rows("douyin", "什么时候玩猎人", (100, 120, 160, 210, 250, 280))
        result = source.analyze(rows)
        self.assertFalse(any(node["category"] == "猎人模式" for node in result["nodes"]))

    def test_prediction_does_not_suppress_a_later_mode_hit(self):
        cases = (
            ("猎人模式", "盲猜这把猎人", "这把猎人模式"),
            ("Boss英雄模式", "下把预测是 boss", "Boss 来了"),
        )
        for category, prediction, confirmation in cases:
            with self.subTest(category=category):
                rows = [Message(100, "douyin:prediction", prediction)]
                rows += self.rows("douyin", confirmation, (300, 330, 360, 400, 430, 480))
                result = source.analyze(rows)
                node = next(node for node in result["nodes"] if node["category"] == category)
                self.assertEqual(node["trigger_time"], 300)
                self.assertEqual(node["time"], 260)

    def test_noncurrent_hunter_lead_is_removed_before_strong_island(self):
        rows = [
            Message(100, "douyin:lead-1", "能打猎人吗"),
            Message(130, "douyin:lead-2", "想看猎人"),
        ]
        rows += [
            Message(time, f"douyin:strong-{index}", "窥屏猎人")
            for index, time in enumerate((300, 324, 348, 372, 396, 420), 1)
        ]
        node = source.analyze(rows)["nodes"][0]
        self.assertEqual(node["category"], "猎人模式")
        self.assertEqual(node["time"], 260)

        isolated = rows[:2]
        self.assertFalse(any(node["category"] == "猎人模式" for node in source.analyze(isolated)["nodes"]))

    def test_short_hunter_mechanics_discussion_uses_longer_pre_roll(self):
        texts = ("猎人模式是什么咋玩的", "猎人能买啥", "猎人模式是啥机制", "猎人能看到人吗")
        rows = [Message(time, f"douyin:u{index}", text)
                for index, (time, text) in enumerate(zip((100, 146, 157, 165), texts), 1)]
        node = source.analyze(rows)["nodes"][0]
        self.assertEqual(node["time"], 10)
        self.assertEqual(node["reason"], "short-mechanics-theme")

    def test_hunter_role_names_support_a_short_current_mode(self):
        texts = ("小鬼猎人", "打手猎人", "毒液猎人", "信息猎人")
        rows = [Message(time, f"douyin:u{index}", text)
                for index, (time, text) in enumerate(zip((100, 125, 150, 155), texts), 1)]
        node = source.analyze(rows)["nodes"][0]
        self.assertEqual(node["category"], "猎人模式")
        self.assertEqual(node["time"], 10)
        self.assertEqual(node["reason"], "short-mechanics-theme")

    def test_noncurrent_hunter_role_names_are_still_rejected(self):
        rows = self.rows("douyin", "下次想看小鬼猎人", (100, 125, 150, 155))
        self.assertFalse(any(node["category"] == "猎人模式" for node in source.analyze(rows)["nodes"]))

    def test_standalone_role_words_only_support_a_nearby_current_seed(self):
        current = [Message(100, "douyin:seed-1", "这猎人很帅"),
                   Message(150, "douyin:seed-2", "打猎人加币子吗")]
        roles = [Message(time, f"douyin:role-{index}", text)
                 for index, (time, text) in enumerate(zip(
                     (220, 240, 260, 270), ("小鬼能死吗", "毒液好强", "这个地震厉害", "迅捷很快")), 1)]
        node = source.analyze(current + roles)["nodes"][0]
        self.assertEqual(node["category"], "猎人模式")
        self.assertEqual(node["trigger_time"], 100)
        self.assertFalse(any(node["category"] == "猎人模式" for node in source.analyze(roles)["nodes"]))

        request = [Message(100, "douyin:request", "下次想看猎人")]
        self.assertFalse(any(node["category"] == "猎人模式"
                             for node in source.analyze(request + roles)["nodes"]))

    def test_short_boss_role_event_uses_special_mode_context(self):
        rows = [
            Message(100, "douyin:mode", "特殊模式了"),
            Message(112, "douyin:boss", "Boss来了"),
            Message(124, "douyin:hero", "这是什么英雄"),
        ]
        node = source.analyze(rows)["nodes"][0]
        self.assertEqual(node["category"], "Boss英雄模式")
        self.assertEqual(node["time"], 60)
        self.assertEqual(node["reason"], "short-current-role-event")

    def test_bare_role_predictions_do_not_create_short_boss_event(self):
        rows = [
            Message(100, "douyin:mode", "特殊模式了"),
            Message(112, "douyin:boss", "变boss"),
            Message(124, "douyin:hero", "变个英雄又有钱了"),
        ]
        self.assertFalse(any(
            node["category"] == "Boss英雄模式" for node in source.analyze(rows)["nodes"]
        ))

    def test_context_weapons_cannot_bridge_separate_boss_discussions(self):
        rows = [
            Message(100, "douyin:warning", "英雄警告"),
            Message(110, "douyin:generic", "boss"),
            Message(120, "douyin:weapon-1", "直接加特林"),
            Message(130, "douyin:weapon-2", "炮塔激光医药箱"),
            Message(310, "douyin:weapon-3", "男枪大狙"),
            Message(330, "douyin:weapon-4", "买次加特林吧"),
            Message(350, "douyin:mode", "英雄模式"),
        ]
        self.assertFalse(any(
            node["category"] == "Boss英雄模式" for node in source.analyze(rows)["nodes"]
        ))

    def test_each_boss_role_name_is_current_mode_evidence(self):
        for role in (
            "夜魔", "夜行者", "麦叔", "水果刀", "火箭筒", "RPG", "rpg",
            "追迹者", "复仇之神", "复仇女神", "双子星", "恶龙",
        ):
            with self.subTest(role=role):
                rows = [
                    Message(100, "douyin:role", role),
                    Message(112, "douyin:boss-1", "变boss了"),
                    Message(124, "douyin:boss-2", "这是什么boss"),
                ]
                node = source.analyze(rows)["nodes"][0]
                self.assertEqual(node["category"], "Boss英雄模式")
                self.assertEqual(node["reason"], "short-current-role-event")

    def test_noncurrent_new_boss_role_mentions_remain_rejected(self):
        rows = [
            Message(100, "douyin:next", "下一局复仇之神"),
            Message(112, "douyin:hypothetical", "要是复仇女神就好了"),
            Message(124, "douyin:request", "想看追迹者"),
            Message(136, "douyin:prediction", "预测是RPG"),
        ]
        result = source.analyze(rows)
        self.assertFalse(any(
            node["category"] == "Boss英雄模式" for node in result["nodes"]
        ))
        self.assertTrue(any(
            item["category"] == "Boss英雄模式"
            and item["reason"] == "non-current-request-prediction-or-history"
            for item in result["rejected"]
        ))

    def test_nonexclusive_hero_professions_are_context_only(self):
        for role in ("地雷", "机枪", "加特林", "大狙", "激光"):
            with self.subTest(role=role):
                rows = self.rows("douyin", role, (100, 110, 120, 130))
                self.assertFalse(any(node["category"] == "Boss英雄模式"
                                     for node in source.analyze(rows)["nodes"]))

        contextual = [
            Message(100, "douyin:hero-1", "英雄来了"),
            Message(112, "douyin:hero-2", "这是什么英雄"),
            Message(124, "douyin:role", "大狙"),
        ]
        node = source.analyze(contextual)["nodes"][0]
        self.assertEqual(node["category"], "Boss英雄模式")
        self.assertEqual(node["reason"], "short-current-role-event")

    def test_exclusive_hero_professions_are_current_mode_evidence(self):
        for role in ("狙击手", "等离子"):
            with self.subTest(role=role):
                rows = self.rows("douyin", role, (100, 112, 124))
                node = source.analyze(rows)["nodes"][0]
                self.assertEqual(node["category"], "Boss英雄模式")
                self.assertEqual(node["reason"], "short-current-role-event")

    def test_human_hero_and_zombie_boss_are_explicit_faction_roles(self):
        rows = [
            Message(100, "douyin:human", "人类英雄"),
            Message(112, "douyin:zombie", "僵尸boss"),
            Message(124, "douyin:mode", "特殊模式"),
        ]
        node = source.analyze(rows)["nodes"][0]
        self.assertEqual(node["category"], "Boss英雄模式")
        self.assertEqual(node["reason"], "short-current-role-event")

        standalone = self.rows("douyin", "人类僵尸", (100, 110, 120, 130))
        self.assertFalse(any(node["category"] == "Boss英雄模式"
                             for node in source.analyze(standalone)["nodes"]))

    def test_special_mode_or_one_role_reaction_cannot_create_boss_event(self):
        special_only = self.rows("douyin", "多重感染模式", (100, 110, 120, 130))
        self.assertFalse(any(node["category"] == "Boss英雄模式"
                             for node in source.analyze(special_only)["nodes"]))

        sparse = [Message(100, "douyin:warning", "英雄警告"),
                  Message(101, "douyin:generic", "boss"),
                  Message(280, "douyin:request", "下把英雄模式")]
        self.assertFalse(any(node["category"] == "Boss英雄模式"
                             for node in source.analyze(sparse)["nodes"]))

    def test_short_peek_hunter_burst_remains_rejected(self):
        rows = self.rows("douyin", "窥屏猎人", (100, 106, 112, 118, 124, 130, 136, 142, 152))
        self.assertFalse(any(node["category"] == "猎人模式" for node in source.analyze(rows)["nodes"]))

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
