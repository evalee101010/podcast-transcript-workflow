from pathlib import Path
import tempfile
import unittest
from unittest import mock

from podcast_tracker.document_patches import append_approved_body_patch, parse_readable_markdown
from podcast_tracker.lark_export import (
    LarkExport,
    LarkExportStore,
    LarkExporter,
    LarkFolderStore,
    _resolve_lark_cli_bin,
    _run_lark,
)
from podcast_tracker.models import Episode


def _episode() -> Episode:
    return Episode(
        id="ep1",
        subscription_id="sub1",
        program_title="节目",
        title="标题",
        source_url="https://example.com/ep1",
        audio_url="https://cdn.example.com/ep1.mp3",
        published_at="2026-06-22T00:00:00+00:00",
        created_at="2026-06-22T00:00:00+00:00",
    )


class LarkExporterTests(unittest.TestCase):
    def test_export_readable_imports_markdown_and_sends_group_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command: list[str], _cwd: Path) -> dict:
                calls.append(command)
                text = " ".join(command)
                if "drive +search" in text:
                    return {"data": {"items": []}}
                if "drive +create-folder" in text:
                    return {"data": {"folder": {"token": "fld1", "name": "Podcast Transcripts"}}}
                if "im +chat-search" in text:
                    return {"data": {"items": [{"chat_name": "Podcast Alerts", "chat_id": "oc1"}]}}
                if "drive +import" in text:
                    return {
                        "data": {
                            "file": {
                                "file_token": "doc1",
                                "url": "https://example.feishu.cn/docx/doc1",
                            }
                        }
                    }
                if "drive permission.members create" in text:
                    return {"ok": True}
                if "im +messages-send" in text:
                    return {"data": {"message_id": "om1"}}
                raise AssertionError(command)

            store = LarkExportStore(exports_file=Path(tmpdir) / "exports.json")
            exporter = LarkExporter(
                export_store=store,
                folder_store=LarkFolderStore(folders_file=Path(tmpdir) / "folders.json"),
                runner=runner,
                chat_name="Podcast Alerts",
                tmp_dir=Path(tmpdir) / "private-tmp",
            )

            export = exporter.export_readable(_episode(), readable_path)

            self.assertEqual(export.folder_token, "fld1")
            self.assertEqual(export.chat_id, "oc1")
            self.assertEqual(export.lark_doc_url, "https://example.feishu.cn/docx/doc1")
            self.assertEqual(export.message_id, "om1")
            self.assertEqual(store.get("ep1").lark_doc_url, export.lark_doc_url)
            import_call = next(call for call in calls if call[:3] == ["lark-cli", "drive", "+import"])
            self.assertIn("--folder-token", import_call)
            self.assertIn("fld1", import_call)
            self.assertIn("--file", import_call)
            self.assertEqual(import_call[import_call.index("--file") + 1], "ep1-标题.md")
            self.assertTrue((Path(tmpdir) / "private-tmp" / "ep1-标题.md").exists())
            grant_call = next(
                call for call in calls if call[:4] == ["lark-cli", "drive", "permission.members", "create"]
            )
            self.assertIn("--yes", grant_call)
            send_call = next(call for call in calls if call[:3] == ["lark-cli", "im", "+messages-send"])
            self.assertEqual(send_call[send_call.index("--as") + 1], "bot")

    def test_sync_readable_overwrites_existing_doc_with_rendered_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(
                "# 阅读版\n\n**主持人**  `00:00`\n这里有一个标点错误，\n",
                encoding="utf-8",
            )
            block = parse_readable_markdown(readable_path.read_text(encoding="utf-8")).blocks[0]
            append_approved_body_patch(
                readable_path,
                episode_id="ep1",
                block_id=block.block_id,
                before_hash=block.body_hash,
                before_text=block.text,
                after_text="这里有一个标点错误。",
            )
            calls: list[tuple[list[str], Path]] = []

            def runner(command: list[str], cwd: Path) -> dict:
                calls.append((command, cwd))
                if command[:3] == ["lark-cli", "docs", "+update"]:
                    return {"data": {"result": "success"}}
                raise AssertionError(command)

            store = LarkExportStore(exports_file=Path(tmpdir) / "exports.json")
            store.upsert(
                LarkExport(
                    episode_id="ep1",
                    readable_path=str(readable_path),
                    folder_name="Podcast Transcripts",
                    folder_token="fld1",
                    chat_name="Podcast Alerts",
                    chat_id="oc1",
                    lark_doc_url="https://example.feishu.cn/docx/doc1",
                    lark_doc_token="doc1",
                    message_id="om1",
                    exported_at="before",
                )
            )
            exporter = LarkExporter(
                export_store=store,
                folder_store=LarkFolderStore(folders_file=Path(tmpdir) / "folders.json"),
                runner=runner,
                tmp_dir=Path(tmpdir) / "private-tmp",
            )

            export = exporter.sync_readable(_episode(), readable_path)

            self.assertEqual(export.lark_doc_url, "https://example.feishu.cn/docx/doc1")
            self.assertEqual(len(calls), 1)
            command, cwd = calls[0]
            self.assertEqual(command[:3], ["lark-cli", "docs", "+update"])
            self.assertEqual(command[command.index("--command") + 1], "overwrite")
            self.assertEqual(command[command.index("--doc-format") + 1], "markdown")
            self.assertEqual(command[command.index("--content") + 1], "@ep1-标题.md")
            synced_markdown = (cwd / "ep1-标题.md").read_text(encoding="utf-8")
            self.assertIn("这里有一个标点错误。", synced_markdown)
            self.assertNotIn("这里有一个标点错误，", synced_markdown)

    def test_export_creates_folder_when_search_scope_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command: list[str], _cwd: Path) -> dict:
                calls.append(command)
                text = " ".join(command)
                if "drive +search" in text:
                    raise RuntimeError("missing required scope(s): search:docs:read")
                if "drive +create-folder" in text:
                    return {"data": {"folder": {"token": "fld1", "name": "Podcast Transcripts"}}}
                if "im +chat-search" in text:
                    return {"data": {"items": [{"chat_name": "Podcast Alerts", "chat_id": "oc1"}]}}
                if "drive +import" in text:
                    return {"data": {"file": {"file_token": "doc1", "url": "https://example.feishu.cn/docx/doc1"}}}
                if "drive permission.members create" in text:
                    return {"ok": True}
                if "im +messages-send" in text:
                    return {"data": {"message_id": "om1"}}
                raise AssertionError(command)

            store = LarkExportStore(exports_file=Path(tmpdir) / "exports.json")
            exporter = LarkExporter(
                export_store=store,
                folder_store=LarkFolderStore(folders_file=Path(tmpdir) / "folders.json"),
                runner=runner,
                chat_name="Podcast Alerts",
                tmp_dir=Path(tmpdir) / "private-tmp",
            )

            export = exporter.export_readable(_episode(), readable_path)

            self.assertEqual(export.folder_token, "fld1")
            self.assertTrue(any(call[:3] == ["lark-cli", "drive", "+create-folder"] for call in calls))

    def test_created_folder_is_cached_even_when_import_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command: list[str], _cwd: Path) -> dict:
                calls.append(command)
                text = " ".join(command)
                if "drive +search" in text:
                    raise RuntimeError("missing required scope(s): search:docs:read")
                if "drive +create-folder" in text:
                    return {"data": {"folder": {"token": "fld1", "name": "Podcast Transcripts"}}}
                if "im +chat-search" in text:
                    return {"data": {"items": [{"chat_name": "Podcast Alerts", "chat_id": "oc1"}]}}
                if "drive +import" in text:
                    raise RuntimeError("import failed")
                raise AssertionError(command)

            store = LarkExportStore(exports_file=Path(tmpdir) / "exports.json")
            folder_store = LarkFolderStore(folders_file=Path(tmpdir) / "folders.json")
            exporter = LarkExporter(
                export_store=store,
                folder_store=folder_store,
                runner=runner,
                chat_name="Podcast Alerts",
                tmp_dir=Path(tmpdir) / "private-tmp",
            )

            with self.assertRaisesRegex(RuntimeError, "import failed"):
                exporter.export_readable(_episode(), readable_path)
            with self.assertRaisesRegex(RuntimeError, "import failed"):
                exporter.export_readable(_episode(), readable_path)

            create_calls = [call for call in calls if call[:3] == ["lark-cli", "drive", "+create-folder"]]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(folder_store.get("Podcast Transcripts"), "fld1")

    def test_run_lark_reports_timeout(self) -> None:
        with mock.patch("podcast_tracker.lark_export.LARK_CLI_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                _run_lark(["/bin/sleep", "3"], Path.cwd())

    def test_resolve_lark_cli_bin_allows_env_override(self) -> None:
        with mock.patch.dict("os.environ", {"PODCAST_TRACKER_LARK_CLI_BIN": "/custom/lark-cli"}):
            self.assertEqual(_resolve_lark_cli_bin(), "/custom/lark-cli")

    def test_run_lark_uses_resolved_lark_cli_path(self) -> None:
        completed = subprocess_completed(returncode=0, stdout="{}\n")
        with mock.patch.dict("os.environ", {"PODCAST_TRACKER_LARK_CLI_BIN": "/custom/lark-cli"}):
            with mock.patch("podcast_tracker.lark_export.subprocess.run", return_value=completed) as run:
                self.assertEqual(_run_lark(["lark-cli", "version", "--json"], Path.cwd()), {})
        self.assertEqual(run.call_args.args[0][0], "/custom/lark-cli")


def subprocess_completed(returncode: int, stdout: str = "", stderr: str = ""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


if __name__ == "__main__":
    unittest.main()
