import array
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import highlight_editor as editor


def write_test_wav(path, amplitude_at, duration=4.0, sample_rate=16000):
    samples = array.array(
        "h",
        (
            round(
                amplitude_at(index / sample_rate)
                * math.sin(2.0 * math.pi * 200.0 * index / sample_rate)
            )
            for index in range(round(duration * sample_rate))
        ),
    )
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())


class TailAudioValleyAdjustmentTests(unittest.TestCase):
    def run_adjustment(self, amplitude_at, *, lower_bound=101.0, protected_until=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "tail.wav"
            write_test_wav(wav_path, amplitude_at)
            return editor.find_tail_weak_valley(
                wav_path,
                window_start=100.0,
                original_end=103.0,
                lower_bound=lower_bound,
                protected_until=protected_until,
            )

    def test_moves_existing_cut_to_latest_qualified_valley(self):
        def amplitude_at(time):
            if 1.30 <= time <= 1.45:
                return 500
            if 2.20 <= time <= 2.35:
                return 700
            return 12000

        adjustment = self.run_adjustment(amplitude_at)

        self.assertIsNotNone(adjustment)
        self.assertGreater(adjustment["adjusted_end"], 102.15)
        self.assertLess(adjustment["adjusted_end"], 102.40)
        self.assertGreaterEqual(
            adjustment["endpoint_db"] - adjustment["valley_db"],
            editor.TAIL_ADJUST_MIN_DROP_DB,
        )
        self.assertAlmostEqual(adjustment["original_end"], 103.0)

    def test_keeps_existing_cut_without_clear_valley(self):
        adjustment = self.run_adjustment(lambda _time: 12000)
        self.assertIsNone(adjustment)

    def test_keeps_existing_cut_when_endpoint_is_already_quiet(self):
        def amplitude_at(time):
            return 300 if 2.20 <= time <= 2.35 else 3000

        adjustment = self.run_adjustment(amplitude_at)
        self.assertIsNone(adjustment)

    def test_does_not_cross_activity_lower_bound(self):
        def amplitude_at(time):
            return 500 if 2.20 <= time <= 2.35 else 12000

        adjustment = self.run_adjustment(amplitude_at, lower_bound=102.50)
        self.assertIsNone(adjustment)

    def test_valley_inside_selected_speech_is_not_a_cut(self):
        adjustment = self.run_adjustment(
            lambda time: 500 if 2.20 <= time <= 2.35 else 12000,
            protected_until=102.70,
        )
        self.assertIsNone(adjustment)

    def test_valley_after_selected_speech_is_still_usable(self):
        adjustment = self.run_adjustment(
            lambda time: 500 if 2.20 <= time <= 2.35 else 12000,
            protected_until=102.10,
        )
        self.assertIsNotNone(adjustment)
        self.assertGreaterEqual(adjustment["adjusted_end"], 102.10)

    def test_speech_boundary_still_runs_before_tail_adjustment(self):
        boundary = editor.choose_speech_boundary(
            [
                {"start": 107.0, "end": 109.2, "word": "这是完整的一句话。"},
            ],
            activity_start=105.0,
            activity_end=108.0,
            review_end=115.0,
        )

        self.assertAlmostEqual(boundary, 109.2 + editor.AUDIO_TRANSITION_DURATION)


class SpeechEndGuardTests(unittest.TestCase):
    def choose(self, words, anchor=108.0):
        return editor.choose_speech_boundary(
            words, activity_start=105.0, activity_end=anchor, review_end=120.0,
        )

    def test_short_lead_in_keeps_its_immediate_continuation(self):
        words = [
            {"start": 108.0, "end": 108.34, "word": "出去"},
            {"start": 108.98, "end": 110.68, "word": "就被打死了"},
            {"start": 112.0, "end": 114.0, "word": "下一件事"},
        ]
        self.assertAlmostEqual(self.choose(words), 110.88)

    def test_short_complete_sentence_does_not_follow_later_talk(self):
        words = [
            {"start": 108.0, "end": 108.34, "word": "走。"},
            {"start": 108.98, "end": 110.68, "word": "这是下一句话。"},
        ]
        self.assertAlmostEqual(self.choose(words), 108.54)

    def test_long_pause_does_not_extend_short_turn(self):
        words = [
            {"start": 108.0, "end": 108.34, "word": "出去"},
            {"start": 110.0, "end": 112.0, "word": "后面的内容"},
        ]
        self.assertAlmostEqual(self.choose(words), 108.54)

    def test_short_continuations_do_not_chain(self):
        words = [
            {"start": 108.0, "end": 108.3, "word": "出去"},
            {"start": 108.9, "end": 109.2, "word": "看看"},
            {"start": 109.8, "end": 110.1, "word": "再来"},
        ]
        self.assertAlmostEqual(self.choose(words), 109.4)

    def test_padding_does_not_include_first_word_of_next_turn(self):
        words = [
            {"start": 105.0, "end": 106.0, "word": "前一句。"},
            {"start": 108.05, "end": 108.21, "word": "来"},
            {"start": 108.21, "end": 110.0, "word": "了个平地无敌"},
        ]
        self.assertAlmostEqual(self.choose(words), 107.97)

    def test_padding_guard_cannot_cut_previous_word(self):
        words = [
            {"start": 107.0, "end": 108.0, "word": "前一句。"},
            {"start": 108.03, "end": 110.0, "word": "后一句。"},
        ]
        self.assertAlmostEqual(self.choose(words), 108.0)

    def test_normal_sentence_timing_is_unchanged(self):
        words = [
            {"start": 107.0, "end": 109.2, "word": "这是完整的一句话。"},
            {"start": 111.0, "end": 114.0, "word": "后续讨论。"},
        ]
        self.assertAlmostEqual(self.choose(words), 109.4)


if __name__ == "__main__":
    unittest.main()
