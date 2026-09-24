import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from media_integrity import MediaIntegrityError, validate_probe_payload


def valid_payload(
    duration=100.0,
    video_packets=6000,
    audio_packets=4688,
    video_start=0.020,
    video_end=99.980,
    audio_start=0.000,
    audio_end=99.990,
):
    return {
        "format": {"start_time": "0", "duration": str(duration)},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "avg_frame_rate": "60/1",
                "r_frame_rate": "60/1",
                "nb_read_packets": str(video_packets),
                "packet_first_dts": str(video_start),
                "packet_last_dts": str(video_end),
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "nb_read_packets": str(audio_packets),
                "packet_first_dts": str(audio_start),
                "packet_last_dts": str(audio_end),
            },
        ],
    }


class MediaIntegrityTests(unittest.TestCase):
    def test_accepts_complete_audio_and_video_packet_coverage(self):
        report = validate_probe_payload(valid_payload(), expected_duration=100.0)
        self.assertEqual(report["video_packets"], 6000)
        self.assertEqual(report["audio_packets"], 4688)
        self.assertAlmostEqual(report["av_end_delta"], 0.01)

    def test_rejects_av_end_drift_even_when_packet_counts_pass(self):
        with self.assertRaisesRegex(MediaIntegrityError, "音视频边界不同步"):
            validate_probe_payload(valid_payload(video_end=97.530, audio_end=99.990))

    def test_rejects_video_track_that_ends_far_before_container(self):
        with self.assertRaisesRegex(MediaIntegrityError, "视频轨覆盖不足"):
            validate_probe_payload(valid_payload(video_packets=1200))

    def test_rejects_known_h264_parser_failure_even_when_counts_look_complete(self):
        stderr = "[h264] missing picture in access unit with size 227\n"
        with self.assertRaisesRegex(MediaIntegrityError, "码流结构异常"):
            validate_probe_payload(valid_payload(), stderr)

    def test_rejects_timestamp_failure(self):
        stderr = "Application provided invalid, non monotonically increasing dts\n"
        with self.assertRaisesRegex(MediaIntegrityError, "码流结构异常"):
            validate_probe_payload(valid_payload(), stderr)


if __name__ == "__main__":
    unittest.main()
