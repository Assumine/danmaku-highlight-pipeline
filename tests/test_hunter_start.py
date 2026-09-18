import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from program_source_analysis import Message, analyze


class HunterStartTests(unittest.TestCase):
    def rows(self, first_text):
        return [Message(t, f"bilibili:{i + 100}", text) for i, (t, text) in enumerate([
            (100, first_text), (210, "猎人跳水会死吗"),
            (240, "装个炮台卡住猎人"), (270, "这把猎人模式"),
            (300, "猎人来了"), (350, "猎人看不见"), (390, "猎人来了")])]

    def hunter(self, rows):
        return next(n for n in analyze(rows)["nodes"] if n["category"] == "猎人模式")

    def test_isolated_point_removed_regardless_of_content(self):
        for text in ("猎人跳水会死吗", "这把猎人模式", "猎人来了"):
            self.assertEqual(self.hunter(self.rows(text))["time"], 170)

    def test_supported_question_keeps_anchor(self):
        rows = self.rows("猎人跳水会死吗")
        rows.append(Message(140, "douyin:900", "猎人来了"))
        rows.append(Message(160, "bilibili:901", "猎人跳水会死吗"))
        self.assertEqual(self.hunter(rows)["time"], 60)

    def test_same_user_cannot_support_isolated_point(self):
        rows = self.rows("猎人来了")
        rows.append(Message(120, rows[0].user, "猎人跳水会死吗"))
        rows.append(Message(140, rows[0].user, "这把猎人模式"))
        self.assertEqual(self.hunter(rows)["time"], 170)

    def test_boss_uses_same_isolation_rule(self):
        rows = [Message(t, f"bilibili:{i + 200}", text) for i, (t, text) in enumerate([
            (100, "恶龙boss是不是被删除了"), (230, "这地图boss坐牢"),
            (260, "BOSS牢底坐穿"), (290, "boss守点了"),
            (330, "boss来了"), (380, "boss很强"), (410, "boss没了")])]
        boss = next(n for n in analyze(rows)["nodes"] if n["category"] == "Boss英雄模式")
        self.assertEqual(boss["time"], 190)

    def test_short_dense_hunter_cluster_is_confirmed_at_end(self):
        times = (6068, 6094, 6121, 6135, 6138, 6145, 6148, 6158, 6174, 6179, 6204)
        rows = [Message(t, f"bilibili:short-{i}", "猎人模式") for i, t in enumerate(times)]
        hunter = self.hunter(rows)
        self.assertEqual(hunter["time"], 6204)
        self.assertEqual(hunter["reason"], "late-confirmed-short-theme")

    def test_delayed_first_keyword_near_recording_start_backfills_to_zero(self):
        rows = [
            Message(20 + i * 10, f"bilibili:context-{i}", "普通弹幕")
            for i in range(20)
        ]
        rows += [
            Message(t, f"bilibili:hunter-{i}", "猎人模式")
            for i, t in enumerate((303, 352, 383, 450, 498, 506))
        ]
        hunter = self.hunter(rows)
        self.assertEqual(hunter["time"], 0)
        self.assertEqual(hunter["reason"], "recording-start-continuation")


if __name__ == "__main__":
    unittest.main()
