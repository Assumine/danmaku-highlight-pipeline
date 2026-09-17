import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import highlight_editor as editor


class CutReviewTests(unittest.TestCase):
    def review(self, words, boundary=108.0):
        return editor.review_speech_boundary(words, boundary=boundary, review_end=120.0)

    def test_recovers_missed_lead_in_crossing_cut(self):
        result = self.review([
            {"start": 105.0, "end": 106.0, "word": "Previous sentence."},
            {"start": 107.74, "end": 107.96, "word": "Someone"},
            {"start": 107.96, "end": 109.2, "word": " already started"},
            {"start": 109.3, "end": 110.0, "word": " talking!"},
        ])
        self.assertAlmostEqual(result['adjusted_end'], 110.2)
        self.assertEqual(result['terminal_turn']['start'], 107.74)

    def test_keeps_clean_pause_unchanged(self):
        self.assertIsNone(self.review([
            {"start": 105.0, "end": 107.0, "word": "Finished!"},
            {"start": 109.0, "end": 111.0, "word": "Later!"},
        ]))

    def test_does_not_pull_in_sentence_starting_after_cut(self):
        self.assertIsNone(self.review([
            {"start": 108.01, "end": 113.0, "word": "New subject!"},
        ]))

    def test_stops_after_crossing_sentence_not_next_sentence(self):
        result = self.review([
            {"start": 107.0, "end": 109.0, "word": "Finish this!"},
            {"start": 110.0, "end": 116.0, "word": "Next topic!"},
        ])
        self.assertAlmostEqual(result['adjusted_end'], 109.2)

    def test_does_not_reopen_sentence_ending_exactly_at_cut(self):
        self.assertIsNone(self.review([
            {"start": 107.0, "end": 108.0, "word": "Done!"},
        ]))

    def test_empty_review_keeps_existing_cut(self):
        self.assertIsNone(self.review([]))

    def test_padding_stays_before_next_sentence(self):
        result = self.review([
            {"start": 107.0, "end": 109.0, "word": "Finish!"},
            {"start": 109.1, "end": 112.0, "word": "Next!"},
        ])
        self.assertAlmostEqual(result['adjusted_end'], 109.02)

    def test_refiner_protects_recovered_words_before_valley_search(self):
        refiner = editor.WhisperRefiner.__new__(editor.WhisperRefiner)
        initial = [{"start": 105.0, "end": 106.0, "word": "Finished!"}]
        reviewed = [
            {"start": 107.74, "end": 110.0, "word": "Actually still talking!"},
            {"start": 113.0, "end": 116.0, "word": "Later!"},
        ]
        refiner.transcribe_window = Mock(side_effect=[([], ''), (initial, 'Initial'), (reviewed, 'Review')])
        highlight = editor.Highlight(1, 60.0, 108.2, 108.2, 100.0, 108.0, 120.0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(editor, 'find_tail_weak_valley', return_value=None) as valley:
                refiner.refine(path/'source.mp4', highlight, path, path, 'test')
            record = json.loads((path/'highlight_01_transcript.json').read_text(encoding='utf-8'))
        self.assertFalse(refiner.transcribe_window.call_args.kwargs['vad_filter'])
        self.assertAlmostEqual(valley.call_args.kwargs['protected_until'], 110.08)
        self.assertAlmostEqual(highlight.final_end, 110.2)
        self.assertAlmostEqual(highlight.speech_content_end, 110.0)
        self.assertAlmostEqual(highlight.next_speech_start, 113.0)
        self.assertEqual(record['cut_review']['text'], 'Review')
        self.assertIn('local-cut-speech-review', highlight.end_basis)


if __name__ == '__main__':
    unittest.main()
