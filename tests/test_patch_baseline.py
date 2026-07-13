import tempfile
import unittest
from pathlib import Path

from podcast_tracker.document_patches import (
    PatchBaselineMismatch,
    append_approved_body_patch,
    hash_body_text,
    load_patch_file,
    render_readable_markdown,
)

BASELINE = "**说话人 1** `00:00:01`\n\n旧的正文。\n"


class PatchBaselineTests(unittest.TestCase):
    def test_baseline_hash_recorded_and_protects_render_and_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable = Path(tmpdir) / "e-阅读版.md"
            readable.write_text(BASELINE, encoding="utf-8")

            append_approved_body_patch(
                readable,
                episode_id="ep",
                block_id="block_0001",
                before_hash=hash_body_text("旧的正文。"),
                before_text="旧的正文。",
                after_text="新的正文。",
            )
            events, baseline_hash = load_patch_file(readable)
            self.assertEqual(len(events), 1)
            self.assertTrue(baseline_hash)
            self.assertIn("新的正文。", render_readable_markdown(readable))

            # Baseline regenerated in place: render and append must refuse.
            readable.write_text(
                "**说话人 1** `00:00:01`\n\n完全不同的基线。\n", encoding="utf-8"
            )
            with self.assertRaises(PatchBaselineMismatch):
                render_readable_markdown(readable)
            with self.assertRaises(PatchBaselineMismatch):
                append_approved_body_patch(
                    readable,
                    episode_id="ep",
                    block_id="block_0001",
                    before_hash="stale",
                    before_text="",
                    after_text="任意",
                )

    def test_legacy_patch_file_without_baseline_hash_still_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable = Path(tmpdir) / "e-阅读版.md"
            readable.write_text(BASELINE, encoding="utf-8")
            legacy = readable.with_name(readable.stem + ".patches.json")
            legacy.write_text(
                '{"version": 1, "patches": [{"field": "body", "status": "approved",'
                ' "block_id": "block_0001", "after_text": "legacy 正文。"}]}',
                encoding="utf-8",
            )
            self.assertIn("legacy 正文。", render_readable_markdown(readable))


if __name__ == "__main__":
    unittest.main()
