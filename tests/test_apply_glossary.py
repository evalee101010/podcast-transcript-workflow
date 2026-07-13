from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "apply_glossary.py"
SPEC = importlib.util.spec_from_file_location("apply_glossary", SCRIPT_PATH)
apply_glossary = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = apply_glossary
SPEC.loader.exec_module(apply_glossary)


class ApplyGlossaryTests(unittest.TestCase):
    def test_applies_known_errors_to_body_only_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = Path(tmpdir) / "episode.md"
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            original_path.write_text(
                "# cloudcode 标题\n\n## 逐字稿\n\n### 主持人\n\n"
                "[00:00:00 - 00:00:02]\n\n我们在用 cloudcode，看 UIUX。\n",
                encoding="utf-8",
            )
            readable_path.write_text(
                "# cloudcode 标题\n\n**主持人**  `00:00`\n"
                "我们在用 cloudcode，看 UIUX。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                first = apply_glossary.main([str(readable_path)])
                second = apply_glossary.main([str(readable_path)])

            text = readable_path.read_text(encoding="utf-8")
            self.assertEqual(first, 0)
            self.assertEqual(second, 0)
            self.assertIn("# cloudcode 标题", text)
            self.assertIn("**主持人**  `00:00`", text)
            self.assertIn("我们在用 Claude Code，看 UI/UX。", text)
            self.assertEqual(text.count("Claude Code"), 1)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(
                "# 标题\n\n**主持人**  `00:00`\n我们在用 cloudcode。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()) as out:
                code = apply_glossary.main([str(readable_path), "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn("cloudcode -> Claude Code", out.getvalue())
            self.assertIn("cloudcode", readable_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
