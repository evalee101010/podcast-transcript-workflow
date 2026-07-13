from pathlib import Path
import tempfile
import unittest

from podcast_tracker.models import Episode
from podcast_tracker.store import Store


class StoreTests(unittest.TestCase):
    def test_upsert_episode_preserves_transcript_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = Store(
                data_dir=data_dir,
                subscriptions_file=data_dir / "subscriptions.json",
                episodes_file=data_dir / "episodes.json",
            )
            original = Episode(
                id="ep1",
                subscription_id="sub1",
                program_title="节目",
                title="标题",
                source_url="https://example.com/1",
                audio_url=None,
                published_at="2026-06-22",
                created_at="2026-06-22T00:00:00+00:00",
                transcript_status="transcribed",
                transcript_path="/tmp/transcript.md",
            )
            refreshed = Episode(
                id="ep1",
                subscription_id="sub1",
                program_title="节目",
                title="标题更新",
                source_url="https://example.com/1",
                audio_url="https://example.com/audio.mp3",
                published_at="2026-06-22",
                created_at="2026-06-23T00:00:00+00:00",
            )

            store.upsert_episode(original)
            self.assertFalse(store.upsert_episode(refreshed))
            saved = store.get_episode("ep1")

            self.assertEqual(saved.title, "标题更新")
            self.assertEqual(saved.audio_url, "https://example.com/audio.mp3")
            self.assertEqual(saved.transcript_status, "transcribed")
            self.assertEqual(saved.transcript_path, "/tmp/transcript.md")
            self.assertEqual(saved.created_at, "2026-06-22T00:00:00+00:00")

    def test_upsert_episode_merges_same_source_url_with_different_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = Store(
                data_dir=data_dir,
                subscriptions_file=data_dir / "subscriptions.json",
                episodes_file=data_dir / "episodes.json",
            )
            original = Episode(
                id="manual-id",
                subscription_id="old-sub",
                program_title="节目",
                title="旧标题",
                source_url="https://example.com/episode",
                audio_url=None,
                published_at="2026-06-22",
                created_at="2026-06-22T00:00:00+00:00",
                transcript_status="transcribed",
                transcript_path="/tmp/transcript.md",
            )
            refreshed = Episode(
                id="feed-id",
                subscription_id="formal-sub",
                program_title="节目",
                title="新标题",
                source_url="https://example.com/episode",
                audio_url="https://cdn.example.com/audio.mp3",
                published_at="2026-06-23",
                created_at="2026-06-23T00:00:00+00:00",
            )

            self.assertTrue(store.upsert_episode(original))
            self.assertFalse(store.upsert_episode(refreshed))
            episodes = store.load_episodes()
            saved = episodes["manual-id"]

            self.assertEqual(set(episodes), {"manual-id"})
            self.assertEqual(saved.subscription_id, "formal-sub")
            self.assertEqual(saved.title, "新标题")
            self.assertEqual(saved.audio_url, "https://cdn.example.com/audio.mp3")
            self.assertEqual(saved.transcript_status, "transcribed")
            self.assertEqual(saved.transcript_path, "/tmp/transcript.md")
            self.assertEqual(saved.created_at, "2026-06-22T00:00:00+00:00")

    def test_upsert_episode_collapses_existing_duplicate_source_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = Store(
                data_dir=data_dir,
                subscriptions_file=data_dir / "subscriptions.json",
                episodes_file=data_dir / "episodes.json",
            )
            transcribed = Episode(
                id="transcribed-id",
                subscription_id="old-sub",
                program_title="节目",
                title="旧标题",
                source_url="https://example.com/episode",
                audio_url=None,
                published_at="2026-06-22",
                created_at="2026-06-22T00:00:00+00:00",
                transcript_status="transcribed",
                transcript_path="/tmp/transcript.md",
            )
            pending = Episode(
                id="pending-id",
                subscription_id="formal-sub",
                program_title="节目",
                title="新标题",
                source_url="https://example.com/episode",
                audio_url="https://cdn.example.com/audio.mp3",
                published_at="2026-06-23",
                created_at="2026-06-23T00:00:00+00:00",
            )
            store.save_episodes({"transcribed-id": transcribed, "pending-id": pending})

            self.assertFalse(store.upsert_episode(pending))
            episodes = store.load_episodes()
            saved = episodes["transcribed-id"]

            self.assertEqual(set(episodes), {"transcribed-id"})
            self.assertEqual(saved.subscription_id, "formal-sub")
            self.assertEqual(saved.title, "新标题")
            self.assertEqual(saved.transcript_status, "transcribed")
            self.assertEqual(saved.transcript_path, "/tmp/transcript.md")


if __name__ == "__main__":
    unittest.main()
