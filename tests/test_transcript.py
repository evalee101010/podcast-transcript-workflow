from pathlib import Path
import tempfile
import unittest

from podcast_tracker.models import Episode, TranscriptSegment
from podcast_tracker.transcript import safe_filename, write_transcript_markdown


class TranscriptTests(unittest.TestCase):
    def test_write_verbatim_speaker_segments_without_summary(self) -> None:
        episode = Episode(
            id="ep1",
            subscription_id="sub1",
            program_title="测试节目",
            title="第一期",
            source_url="https://example.com/ep1",
            audio_url="https://example.com/ep1.mp3",
            published_at="2026-06-22",
            created_at="2026-06-22T00:00:00+00:00",
        )
        segments = [
            TranscriptSegment(speaker="说话人 A", start="00:00:01", text="嗯，我们先说第一点。"),
            TranscriptSegment(speaker="说话人 B", start="00:00:05", text="好的，我补充一个细节。"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_transcript_markdown(episode, segments, output_dir=Path(tmpdir))
            content = path.read_text(encoding="utf-8")

        self.assertIn("## 逐字稿", content)
        self.assertIn("### 说话人 A", content)
        self.assertIn("嗯，我们先说第一点。", content)
        self.assertIn("### 说话人 B", content)
        self.assertIn("好的，我补充一个细节。", content)
        self.assertNotIn("## 摘要", content)
        self.assertNotIn("## 重点", content)

    def test_safe_filename_removes_path_separators(self) -> None:
        self.assertEqual(safe_filename("A/B:C*D?"), "A-B-C-D")


if __name__ == "__main__":
    unittest.main()
