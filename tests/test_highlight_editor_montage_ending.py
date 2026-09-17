import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import highlight_editor as editor


def highlight(index):
    return editor.Highlight(
        index=index, start=0.0, coarse_end=10.0, final_end=10.0,
        activity_start=5.0, activity_end=9.0, review_end=15.0,
        speech_content_end=9.8, next_speech_start=13.0,
    )


class MontageEndingTests(unittest.TestCase):
    def test_only_last_clip_extends_and_fade_starts_after_speech(self):
        clips = [highlight(1), highlight(2)]
        first_before = dataclasses.asdict(clips[0])
        ending = editor.prepare_montage_ending(clips, 20.0)
        self.assertEqual(dataclasses.asdict(clips[0]), first_before)
        self.assertEqual(ending.clip_index, 2)
        self.assertAlmostEqual(clips[-1].final_end, 10.6)
        self.assertAlmostEqual(ending.audio_fade_duration, 0.8)
        self.assertGreaterEqual(
            ending.final_end - ending.audio_fade_duration, clips[-1].speech_content_end,
        )

    def test_extension_stops_before_next_speech(self):
        clip = highlight(1)
        clip.next_speech_start = 10.3
        ending = editor.prepare_montage_ending([clip], 20.0)
        self.assertAlmostEqual(ending.final_end, 10.22)
        self.assertAlmostEqual(ending.audio_fade_duration, 0.42)

    def test_source_end_limits_extension(self):
        ending = editor.prepare_montage_ending([highlight(1)], 10.1)
        self.assertAlmostEqual(ending.extension, 0.1)

    def test_reviewed_window_limits_extension(self):
        clip = highlight(1)
        clip.review_end = 10.15
        ending = editor.prepare_montage_ending([clip], 20.0)
        self.assertAlmostEqual(ending.extension, 0.15)

    def test_no_sentence_boundary_does_not_add_unreviewed_audio(self):
        clip = highlight(1)
        clip.speech_content_end = None
        ending = editor.prepare_montage_ending([clip], 20.0)
        self.assertEqual(ending.extension, 0.0)
        self.assertEqual(ending.audio_fade_duration, 0.0)
        self.assertAlmostEqual(ending.video_fade_duration, 0.8)

    def test_empty_selection_has_no_ending(self):
        self.assertIsNone(editor.prepare_montage_ending([], 20.0))

    def test_audio_edge_analysis_excludes_added_tail(self):
        with patch.object(editor, "probe_duration", return_value=12.6), \
             patch.object(editor, "decode_audio_window", return_value=[1000] * 16000) as decode:
            editor.analyze_clip_audio_edges(Path("last.mp4"), 2, duration_limit=12.0)
        self.assertEqual(decode.call_args_list[1].args, (Path("last.mp4"), 6.0, 6.0))

    def render_command(self, *, with_ending=True):
        clips = [highlight(1), highlight(2)]
        transitions = editor.standard_transition_plan(clips, reason="test")
        ending = editor.prepare_montage_ending(clips, 20.0) if with_ending else None
        with patch.object(editor, "probe_duration", side_effect=[clip.duration for clip in clips]), \
             patch.object(editor, "source_label_font", return_value=Path("C:/font.ttf")), \
             patch.object(editor, "run_command", return_value=SimpleNamespace(returncode=0)) as run:
            editor.render_montage(
                [Path("first.mp4"), Path("last.mp4")], clips, Path("out.mp4"),
                source_part=3, profile=editor.build_render_profile(),
                transitions=transitions, ending=ending,
            )
        command = run.call_args.args[0]
        return command[command.index("-filter_complex") + 1]

    def test_fade_is_on_last_input_and_includes_source_label(self):
        filters = self.render_command()
        self.assertEqual(filters.count("afade="), 1)
        self.assertEqual(filters.count("]fade="), 1)
        self.assertIn("[vref1]fade=t=out:st=9.800:d=0.800[v1]", filters)
        self.assertIn("afade=t=out:st=9.400:d=0.800:curve=hsin", filters)
        self.assertIn("[0:a]aresample=async=1:first_pts=0[a0]", filters)
        self.assertIn("acrossfade=d=0.200:c1=tri:c2=tri", filters)

    def test_render_without_ending_keeps_existing_transitions(self):
        filters = self.render_command(with_ending=False)
        self.assertNotIn("afade=", filters)
        self.assertNotIn("]fade=", filters)
        self.assertIn("acrossfade=d=0.200:c1=tri:c2=tri", filters)

    def test_ending_cannot_target_an_intermediate_clip(self):
        clips = [highlight(1), highlight(2)]
        ending = editor.prepare_montage_ending([clips[0]], 20.0)
        with self.assertRaises(ValueError):
            editor.render_montage(
                [Path("first.mp4"), Path("last.mp4")], clips, Path("out.mp4"),
                source_part=3, profile=editor.build_render_profile(),
                transitions=editor.standard_transition_plan(clips, reason="test"),
                ending=ending,
            )


if __name__ == "__main__":
    unittest.main()
