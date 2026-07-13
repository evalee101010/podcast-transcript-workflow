from pathlib import Path
import json
import tempfile
import unittest

from podcast_tracker.document_patches import (
    PatchBaselineMismatch,
    append_approved_body_patch,
    hash_body_text,
    parse_readable_markdown,
    patches_path_for_readable,
    render_readable_markdown,
    replace_rendered_block_body,
)


READABLE = """# 阅读版

> 元信息

---

**主持人**  `00:00`
你好，今天聊 AI。

**嘉宾**  `00:05`
好的，我们开始。
"""


class DocumentPatchTests(unittest.TestCase):
    def test_parse_readable_blocks_have_stable_ids_and_hashes(self) -> None:
        parsed = parse_readable_markdown(READABLE)

        self.assertEqual([block.block_id for block in parsed.blocks], ["block_0001", "block_0002"])
        self.assertEqual(parsed.blocks[0].speaker, "主持人")
        self.assertEqual(parsed.blocks[0].timestamp, "00:00")
        self.assertEqual(parsed.blocks[0].text, "你好，今天聊 AI。")
        self.assertEqual(parsed.blocks[0].body_hash, hash_body_text("你好，今天聊 AI。"))

    def test_approved_patch_renders_without_mutating_source_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(READABLE, encoding="utf-8")
            block = parse_readable_markdown(READABLE).blocks[0]

            event = append_approved_body_patch(
                readable_path,
                episode_id="ep1",
                block_id=block.block_id,
                before_hash=block.body_hash,
                before_text=block.text,
                after_text="你好，今天聊人工智能。",
            )

            rendered = render_readable_markdown(readable_path)
            patch_data = json.loads(patches_path_for_readable(readable_path).read_text(encoding="utf-8"))

            self.assertEqual(event["episode_id"], "ep1")
            self.assertIn("你好，今天聊人工智能。", rendered)
            self.assertNotIn("你好，今天聊人工智能。", readable_path.read_text(encoding="utf-8"))
            self.assertEqual(patch_data["version"], 1)
            self.assertEqual(patch_data["patches"][0]["status"], "approved")

    def test_replace_rendered_block_body_uses_latest_rendered_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(READABLE, encoding="utf-8")
            block = parse_readable_markdown(READABLE).blocks[0]
            append_approved_body_patch(
                readable_path,
                block_id=block.block_id,
                before_hash=block.body_hash,
                before_text=block.text,
                after_text="第一轮人工修改。",
            )

            candidate = replace_rendered_block_body(readable_path, block.block_id, "第二轮人工修改。")

            self.assertIn("第二轮人工修改。", candidate)
            self.assertNotIn("第一轮人工修改。", candidate)
            self.assertIn("好的，我们开始。", candidate)

    def test_patch_renders_when_unrelated_baseline_text_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(READABLE, encoding="utf-8")
            block = parse_readable_markdown(READABLE).blocks[0]
            append_approved_body_patch(
                readable_path,
                block_id=block.block_id,
                before_hash=block.body_hash,
                before_text=block.text,
                after_text="你好，今天聊人工智能。",
            )
            readable_path.write_text(READABLE.replace("> 元信息", "> 元信息已更新"), encoding="utf-8")

            rendered = render_readable_markdown(readable_path)

            self.assertIn("你好，今天聊人工智能。", rendered)
            self.assertIn("> 元信息已更新", rendered)

    def test_patch_blocks_when_edited_block_baseline_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(READABLE, encoding="utf-8")
            block = parse_readable_markdown(READABLE).blocks[0]
            append_approved_body_patch(
                readable_path,
                block_id=block.block_id,
                before_hash=block.body_hash,
                before_text=block.text,
                after_text="你好，今天聊人工智能。",
            )
            changed = READABLE.replace("你好，今天聊 AI。", "你好，今天聊大模型。")
            readable_path.write_text(changed, encoding="utf-8")

            with self.assertRaises(PatchBaselineMismatch):
                render_readable_markdown(readable_path)

    def test_append_patch_rebases_when_existing_patches_are_still_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(READABLE, encoding="utf-8")
            parsed = parse_readable_markdown(READABLE)
            append_approved_body_patch(
                readable_path,
                block_id=parsed.blocks[0].block_id,
                before_hash=parsed.blocks[0].body_hash,
                before_text=parsed.blocks[0].text,
                after_text="你好，今天聊人工智能。",
            )
            readable_path.write_text(READABLE.replace("> 元信息", "> 元信息已更新"), encoding="utf-8")
            rendered = render_readable_markdown(readable_path)
            second_block = parse_readable_markdown(rendered).blocks[1]

            append_approved_body_patch(
                readable_path,
                block_id=second_block.block_id,
                before_hash=second_block.body_hash,
                before_text=second_block.text,
                after_text="好的，我们马上开始。",
            )
            rendered_again = render_readable_markdown(readable_path)

            self.assertIn("你好，今天聊人工智能。", rendered_again)
            self.assertIn("好的，我们马上开始。", rendered_again)


if __name__ == "__main__":
    unittest.main()
