import unittest

from podcast_tracker.asr import (
    audio_suffix_from_url,
    format_timestamp,
    merge_adjacent_segments,
    segments_from_diarized_payload,
)
from podcast_tracker.models import TranscriptSegment


class AsrTests(unittest.TestCase):
    def test_segments_from_diarized_payload_preserves_speaker_text_and_offsets(self) -> None:
        payload = {
            "segments": [
                {
                    "speaker": "speaker_0",
                    "start": 1.25,
                    "end": 3.5,
                    "text": "嗯，我们先说第一点。",
                },
                {
                    "speaker": "speaker_1",
                    "start": 4,
                    "end": 8,
                    "text": "好的，我补充一个细节。",
                },
            ]
        }

        segments = segments_from_diarized_payload(payload, offset_seconds=1200)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].speaker, "speaker_0")
        self.assertEqual(segments[0].start, "00:20:01.250")
        self.assertEqual(segments[0].end, "00:20:03.500")
        self.assertEqual(segments[0].text, "嗯，我们先说第一点。")
        self.assertEqual(segments[1].speaker, "speaker_1")
        self.assertEqual(segments[1].start, "00:20:04.000")

    def test_segments_from_plain_text_payload_falls_back_to_single_speaker(self) -> None:
        segments = segments_from_diarized_payload({"text": "完整文字稿"})

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].speaker, "Speaker")
        self.assertEqual(segments[0].text, "完整文字稿")

    def test_merge_adjacent_segments_keeps_distinct_speaker_turns(self) -> None:
        segments = [
            TranscriptSegment(speaker="A", text="第一句", start="00:00:01", end="00:00:02"),
            TranscriptSegment(speaker="A", text="第二句", start="00:00:02", end="00:00:03"),
            TranscriptSegment(speaker="B", text="第三句", start="00:00:03", end="00:00:04"),
        ]

        merged = merge_adjacent_segments(segments)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].speaker, "A")
        self.assertEqual(merged[0].text, "第一句\n第二句")
        self.assertEqual(merged[0].start, "00:00:01")
        self.assertEqual(merged[0].end, "00:00:03")
        self.assertEqual(merged[1].speaker, "B")

    def test_audio_suffix_from_url_defaults_to_m4a(self) -> None:
        self.assertEqual(audio_suffix_from_url("https://example.com/a.MP3?x=1"), ".mp3")
        self.assertEqual(audio_suffix_from_url("https://example.com/audio"), ".m4a")

    def test_format_timestamp_preserves_non_numeric_values(self) -> None:
        self.assertEqual(format_timestamp("00:01:02"), "00:01:02")


if __name__ == "__main__":
    unittest.main()
