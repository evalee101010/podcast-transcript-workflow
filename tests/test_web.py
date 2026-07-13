from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from podcast_tracker.models import Episode, Subscription
from podcast_tracker.document_patches import parse_readable_markdown, render_readable_markdown
from podcast_tracker.jobs import ReadableJob
from podcast_tracker.store import Store
from podcast_tracker.web import (
    _delete_glossary_entry,
    _health_payload,
    _make_handler,
    _parse_glossary_sources,
    _render_document_conflict_page,
    _render_document_page,
    _render_glossary_page,
    _save_document_body_patch,
    _state_payload,
    _upsert_episode_canonical,
    _upsert_episode_correction,
    _upsert_glossary_entries,
    _upsert_global_canonical,
)


def _store(data_dir: Path) -> Store:
    return Store(
        data_dir=data_dir,
        subscriptions_file=data_dir / "subscriptions.json",
        episodes_file=data_dir / "episodes.json",
    )


def _episode(episode_id: str, subscription_id: str, title: str) -> Episode:
    return Episode(
        id=episode_id,
        subscription_id=subscription_id,
        program_title="十字路口Crossing",
        title=title,
        source_url=f"https://example.com/{episode_id}",
        audio_url=f"https://cdn.example.com/{episode_id}.mp3",
        published_at="2026-06-22T00:00:00+00:00",
        created_at="2026-06-22T00:00:00+00:00",
    )


class _FakeJobManager:
    def __init__(self, jobs: dict[str, ReadableJob]) -> None:
        self.jobs = jobs

    def latest_by_episode(self) -> dict[str, ReadableJob]:
        return self.jobs


class _CapturingJobManager:
    completion_hook = None

    def __init__(self, _store: Store, completion_hook=None, **_kwargs) -> None:
        type(self).completion_hook = completion_hook

    def latest_by_episode(self) -> dict[str, ReadableJob]:
        return {}


class WebStateTests(unittest.TestCase):
    def test_default_handler_does_not_auto_publish_to_lark(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            _CapturingJobManager.completion_hook = "unset"

            with mock.patch.dict("os.environ", {"PODCAST_TRACKER_ENABLE_LARK": ""}):
                with mock.patch("podcast_tracker.web.ReadableJobManager", _CapturingJobManager):
                    _make_handler(store)

            self.assertIsNone(_CapturingJobManager.completion_hook)

    def test_lark_auto_publish_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            _CapturingJobManager.completion_hook = None

            with mock.patch.dict("os.environ", {"PODCAST_TRACKER_ENABLE_LARK": "1"}):
                with mock.patch("podcast_tracker.web.ReadableJobManager", _CapturingJobManager):
                    _make_handler(store)

            self.assertIsNotNone(_CapturingJobManager.completion_hook)

    def test_health_payload_loads_local_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(
                Subscription(
                    id="sub1",
                    title="测试节目",
                    feed_url="https://example.com/feed",
                    source_url="https://example.com",
                    created_at="2026-06-22T00:00:00+00:00",
                )
            )
            store.upsert_episode(_episode("ep1", "sub1", "第一期"))

            health = _health_payload(store)

            self.assertTrue(health["ok"])
            self.assertEqual(health["service"], "podcast-tracker-web")
            self.assertEqual(health["counts"]["subscriptions"], 1)
            self.assertEqual(health["counts"]["episodes"], 1)

    def test_orphan_episodes_are_grouped_by_program_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_episode(_episode("ep1", "old-sub-1", "第一期"))
            store.upsert_episode(_episode("ep2", "old-sub-2", "第二期"))

            state = _state_payload(store)

            self.assertEqual(len(state["subscriptions"]), 1)
            source = state["subscriptions"][0]
            self.assertEqual(source["title"], "十字路口Crossing")
            self.assertTrue(source["inferred"])
            self.assertEqual(source["episode_count"], 2)
            self.assertEqual({item["source_id"] for item in state["episodes"]}, {source["id"]})

    def test_formal_subscription_claims_orphan_episodes_with_same_program_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(
                Subscription(
                    id="formal-sub",
                    title="十字路口Crossing",
                    feed_url="https://example.com/feed",
                    source_url="https://example.com",
                    created_at="2026-06-22T00:00:00+00:00",
                )
            )
            store.upsert_episode(_episode("ep1", "old-sub-1", "第一期"))

            state = _state_payload(store)

            self.assertEqual(len(state["subscriptions"]), 1)
            self.assertFalse(state["subscriptions"][0]["inferred"])
            self.assertEqual(state["episodes"][0]["source_id"], "formal-sub")

    def test_episode_is_pending_until_readable_document_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            transcript_path.write_text("# 原始逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(
                replace(
                    _episode("ep1", "sub1", "第一期"),
                    transcript_status="transcribed",
                    transcript_path=str(transcript_path),
                )
            )

            state = _state_payload(store)
            episode = state["episodes"][0]

            self.assertEqual(episode["display_status"], "pending")
            self.assertFalse(episode["has_readable"])
            self.assertIsNone(episode["document_url"])
            self.assertNotIn("transcript_path", episode)
            self.assertNotIn("transcript_status", episode)
            self.assertNotIn("transcribe_command", episode)
            self.assertNotIn("readable_command", episode)
            self.assertEqual(state["counts"]["pending"], 1)
            self.assertEqual(state["counts"]["readable"], 0)

    def test_episode_has_readable_action_when_readable_document_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            transcript_path.write_text("# 原始逐字稿\n", encoding="utf-8")
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(
                replace(
                    _episode("ep1", "sub1", "第一期"),
                    transcript_status="transcribed",
                    transcript_path=str(transcript_path),
                )
            )

            state = _state_payload(store)
            episode = state["episodes"][0]

            self.assertEqual(episode["display_status"], "readable")
            self.assertTrue(episode["has_readable"])
            self.assertEqual(episode["document_url"], "/docs/ep1/readable")
            self.assertEqual(state["counts"]["pending"], 0)
            self.assertEqual(state["counts"]["readable"], 1)

    def test_episode_shows_generating_when_readable_job_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_episode(_episode("ep1", "sub1", "第一期"))
            job_manager = _FakeJobManager(
                {
                    "ep1": ReadableJob(
                        id="job1",
                        episode_id="ep1",
                        status="running",
                        stage="transcribe_audio",
                        progress=45,
                        stage_started_at="2026-06-22T00:00:01+00:00",
                        created_at="now",
                    )
                }
            )

            state = _state_payload(store, job_manager)
            episode = state["episodes"][0]

            self.assertEqual(episode["display_status"], "generating")
            self.assertEqual(episode["job_id"], "job1")
            self.assertEqual(episode["job_status"], "running")
            self.assertEqual(episode["job_stage"], "transcribe_audio")
            self.assertEqual(episode["job_stage_label"], "ASR处理中")
            self.assertEqual(episode["job_progress"], 45)
            self.assertEqual(episode["job_stage_started_at"], "2026-06-22T00:00:01+00:00")

    def test_episode_shows_failed_when_readable_job_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_episode(_episode("ep1", "sub1", "第一期"))
            job_manager = _FakeJobManager(
                {
                    "ep1": ReadableJob(
                        id="job1",
                        episode_id="ep1",
                        status="failed",
                        stage="failed",
                        progress=90,
                        created_at="now",
                        error="boom",
                    )
                }
            )

            state = _state_payload(store, job_manager)
            episode = state["episodes"][0]

            self.assertEqual(episode["display_status"], "failed")
            self.assertEqual(episode["job_error"], "boom")
            self.assertEqual(episode["job_stage"], "failed")
            self.assertEqual(episode["job_stage_label"], "失败")
            self.assertEqual(episode["job_progress"], 90)

    def test_document_page_has_visible_lark_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")

            html = _render_document_page(_episode("ep1", "sub1", "第一期"), readable_path)

            self.assertIn('id="larkButton"', html)
            self.assertIn('id="larkStatus"', html)
            self.assertIn("正在导入飞书文档并发送到群", html)
            self.assertIn("发到飞书", html)
            self.assertIn("setLarkStatus(error.message", html)

    def test_document_page_can_sync_existing_lark_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")

            html = _render_document_page(
                _episode("ep1", "sub1", "第一期"),
                readable_path,
                lark_doc_url="https://example.feishu.cn/docx/doc1",
            )

            self.assertIn("同步飞书", html)
            self.assertIn("打开飞书", html)
            self.assertIn('id="openLarkButton"', html)
            self.assertIn('action = existingUrl ? "sync" : "export"', html)
            self.assertIn("正在同步当前版本到飞书文档", html)

    def test_document_page_shows_local_glossary_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            candidates_path = Path(tmpdir) / "episode-阅读版.glossary-candidates.json"
            markdown = "# 阅读版\n\n正文。"
            readable_path.write_text(markdown, encoding="utf-8")
            candidates_path.write_text(
                json.dumps(
                    {
                        "unknown_tokens": [
                            {"token": "codepilot", "count": 3, "blocks": [12, 13]},
                            {"token": "tokengrant", "count": 2, "blocks": [20]},
                        ],
                        "variant_clusters": [["tokengrant", "tokengrand", "tokenground"]],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            html = _render_document_page(_episode("ep1", "sub1", "第一期"), readable_path)

            self.assertIn("疑似错词", html)
            self.assertIn("仅本地显示，不进入飞书文档", html)
            self.assertIn("修正为", html)
            self.assertIn("这是正确词", html)
            self.assertIn("本集修改", html)
            self.assertIn('id="glossaryDock"', html)
            self.assertIn("错词修正", html)
            self.assertIn("选中正文里的词会自动填入", html)
            self.assertIn("reloadKeepingScroll", html)
            self.assertIn('id="settingsButton"', html)
            self.assertIn('href="/glossary"', html)
            self.assertIn("id=\"glossarySource\"", html)
            self.assertIn("codepilot", html)
            self.assertIn('data-review-action="correct"', html)
            self.assertIn('data-review-action="canonical"', html)
            self.assertIn('data-review-action="episode_correct"', html)
            self.assertNotIn("本集忽略", html)
            self.assertNotIn("已保存热词库", html)
            self.assertIn("tokengrant / tokengrand / tokenground", html)
            self.assertEqual(readable_path.read_text(encoding="utf-8"), markdown)

    def test_document_page_hides_saved_episode_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            sidecar_path = Path(tmpdir) / "episode-阅读版.glossary.json"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            sidecar_path.write_text(
                json.dumps(
                    {
                        "canonical": ["Agent"],
                        "corrections": {"a 证": "Agent"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            html = _render_document_page(_episode("ep1", "sub1", "第一期"), readable_path)

            self.assertNotIn("已保存热词库", html)
            self.assertNotIn("本集词库", html)
            self.assertNotIn('data-fill-source="a 证"', html)
            self.assertNotIn('data-fill-target="Agent"', html)
            self.assertIn('id="settingsButton"', html)
            self.assertIn('href="/glossary"', html)
            self.assertIn('id="glossaryDock"', html)

    def test_glossary_page_shows_global_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))

            html = _render_glossary_page(store)

            self.assertIn("热词库", html)
            self.assertIn("全局热词库", html)
            self.assertIn("Claude Code", html)
            self.assertIn("a 证", html)
            self.assertIn("Agent", html)
            self.assertIn("data-glossary-delete", html)
            self.assertIn("/api/glossary/delete", html)

    def test_document_page_renders_editable_transcript_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text(
                "# 阅读版\n\n**主持人**  `00:00`\n这里有一个标点错误，\n",
                encoding="utf-8",
            )

            html = _render_document_page(_episode("ep1", "sub1", "第一期"), readable_path)

            self.assertIn('class="transcript-block"', html)
            self.assertIn('data-block-id="block_0001"', html)
            self.assertIn("data-block-edit", html)
            self.assertIn("/api/docs/patches", html)
            self.assertIn("这里有一个标点错误，", html)

    def test_document_conflict_page_uses_utf8_body_not_status_message(self) -> None:
        html = _render_document_conflict_page("阅读版基线已变更：中文文件名.md")

        self.assertIn("阅读版暂时不能打开", html)
        self.assertIn("阅读版基线已变更：中文文件名.md", html)
        self.assertIn('href="/"', html)

    def test_document_body_patch_saves_sidecar_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            markdown = "# 阅读版\n\n**主持人**  `00:00`\n这里有一个标点错误，\n"
            readable_path.write_text(markdown, encoding="utf-8")
            block = parse_readable_markdown(markdown).blocks[0]

            result = _save_document_body_patch(
                _episode("ep1", "sub1", "第一期"),
                readable_path,
                block_id=block.block_id,
                before_hash=block.body_hash,
                after_text="这里有一个标点错误。",
            )

            self.assertTrue(result["changed"])
            self.assertEqual(readable_path.read_text(encoding="utf-8"), markdown)
            self.assertIn("这里有一个标点错误。", render_readable_markdown(readable_path))

    def test_document_body_patch_allows_unrelated_glossary_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            sidecar_glossary = data_dir / "episode-阅读版.glossary.json"
            transcript_path.write_text(
                "# 标题\n\n## 逐字稿\n\n"
                "### 主持人\n\n[00:00:00 - 00:00:02]\n\nbadterm 还没改。\n\n"
                "### 主持人\n\n[00:00:02 - 00:00:04]\n\n这里有一个标点错误，\n",
                encoding="utf-8",
            )
            markdown = (
                "# 阅读版\n\n"
                "**主持人**  `00:00`\n"
                "badterm 还没改。\n\n"
                "**主持人**  `00:02`\n"
                "这里有一个标点错误，\n"
            )
            readable_path.write_text(markdown, encoding="utf-8")
            sidecar_glossary.write_text(
                json.dumps(
                    {"canonical": ["GoodTerm"], "corrections": {"badterm": "GoodTerm"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            block = parse_readable_markdown(markdown).blocks[1]
            episode = replace(_episode("ep1", "sub1", "第一期"), transcript_path=str(transcript_path))

            result = _save_document_body_patch(
                episode,
                readable_path,
                block_id=block.block_id,
                before_hash=block.body_hash,
                after_text="这里有一个标点错误。",
            )

            self.assertTrue(result["changed"])
            self.assertEqual(result["verify"]["exit_code"], 0, result["verify"]["output"])
            self.assertIn("known ASR error", result["verify"]["output"])
            self.assertIn("这里有一个标点错误。", render_readable_markdown(readable_path))

    def test_delete_glossary_entry_removes_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            glossary_path = Path(tmpdir) / "glossary.json"
            glossary_path.write_text(
                json.dumps({"canonical": ["Agent", "Claude"], "corrections": {}}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = _delete_glossary_entry(glossary_path, "canonical", "Agent")
            data = json.loads(glossary_path.read_text(encoding="utf-8"))

            self.assertEqual(result["type"], "canonical")
            self.assertEqual(data["canonical"], ["Claude"])

    def test_delete_glossary_entry_removes_correction_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            glossary_path = Path(tmpdir) / "glossary.json"
            glossary_path.write_text(
                json.dumps(
                    {"canonical": ["Agent"], "corrections": {"a 证": "Agent", "b 证": "Agent"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = _delete_glossary_entry(glossary_path, "correction", "a 证", "Agent")
            data = json.loads(glossary_path.read_text(encoding="utf-8"))

            self.assertEqual(result["type"], "correction")
            self.assertEqual(data["canonical"], ["Agent"])
            self.assertEqual(data["corrections"], {"b 证": "Agent"})

    def test_delete_last_episode_glossary_entry_removes_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            glossary_path = Path(tmpdir) / "episode-阅读版.glossary.json"
            glossary_path.write_text(
                json.dumps({"canonical": ["Agent"], "corrections": {}}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = _delete_glossary_entry(glossary_path, "canonical", "Agent")

            self.assertTrue(result["removed_file"])
            self.assertFalse(glossary_path.exists())

    def test_glossary_update_adds_correction_and_canonical_hotword(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            glossary_path = Path(tmpdir) / "glossary.json"
            glossary_path.write_text(
                json.dumps({"canonical": ["Claude Code"], "corrections": {}}, ensure_ascii=False),
                encoding="utf-8",
            )

            sources = _parse_glossary_sources("a 证, A证")
            result = _upsert_glossary_entries(sources, "agent", glossary_path)
            data = json.loads(glossary_path.read_text(encoding="utf-8"))

            self.assertEqual(sources, ["a 证", "A证"])
            self.assertTrue(result["added_canonical"])
            self.assertIn("agent", data["canonical"])
            self.assertEqual(data["corrections"]["a 证"], "agent")
            self.assertEqual(data["corrections"]["A证"], "agent")

    def test_global_canonical_adds_hotword_without_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            glossary_path = Path(tmpdir) / "glossary.json"
            glossary_path.write_text(
                json.dumps({"canonical": [], "corrections": {}}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = _upsert_global_canonical(["TikTok", "FFmpeg"], glossary_path)
            data = json.loads(glossary_path.read_text(encoding="utf-8"))

            self.assertEqual(result["scope"], "global")
            self.assertEqual(data["canonical"], ["TikTok", "FFmpeg"])
            self.assertEqual(data["corrections"], {})

    def test_episode_ignore_adds_sidecar_canonical_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")

            result = _upsert_episode_canonical(readable_path, ["maybeok"])
            sidecar = Path(tmpdir) / "episode-阅读版.glossary.json"
            data = json.loads(sidecar.read_text(encoding="utf-8"))

            self.assertEqual(result["scope"], "episode")
            self.assertEqual(data["canonical"], ["maybeok"])
            self.assertEqual(data["corrections"], {})

    def test_episode_correction_adds_sidecar_correction_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")

            result = _upsert_episode_correction(readable_path, ["a 证"], "agent")
            sidecar = Path(tmpdir) / "episode-阅读版.glossary.json"
            data = json.loads(sidecar.read_text(encoding="utf-8"))

            self.assertEqual(result["scope"], "episode")
            self.assertEqual(data["canonical"], ["agent"])
            self.assertEqual(data["corrections"], {"a 证": "agent"})


if __name__ == "__main__":
    unittest.main()
