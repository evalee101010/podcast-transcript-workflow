from pathlib import Path
import json
import os
import threading
import tempfile
import time
import unittest
from unittest import mock

from podcast_tracker.jobs import (
    ReadableJobManager,
    _is_retryable_asr_startup_failure,
    _positive_int_env,
    _read_progress_stage,
    _resolve_codex_bin,
    _run_verify_readable,
)
from podcast_tracker.models import Episode
from podcast_tracker.store import Store


def _store(data_dir: Path) -> Store:
    return Store(
        data_dir=data_dir,
        subscriptions_file=data_dir / "subscriptions.json",
        episodes_file=data_dir / "episodes.json",
    )


def _episode(transcript_path: Path) -> Episode:
    return Episode(
        id="ep1",
        subscription_id="sub1",
        program_title="节目",
        title="标题",
        source_url="https://example.com/ep1",
        audio_url="https://cdn.example.com/ep1.mp3",
        published_at="2026-06-22T00:00:00+00:00",
        created_at="2026-06-22T00:00:00+00:00",
        transcript_status="transcribed",
        transcript_path=str(transcript_path),
    )


def _pending_episode() -> Episode:
    return _pending_episode_with_id("ep1")


def _pending_episode_with_id(episode_id: str) -> Episode:
    return Episode(
        id=episode_id,
        subscription_id="sub1",
        program_title="节目",
        title=f"标题 {episode_id}",
        source_url=f"https://example.com/{episode_id}",
        audio_url=f"https://cdn.example.com/{episode_id}.mp3",
        published_at="2026-06-22T00:00:00+00:00",
        created_at="2026-06-22T00:00:00+00:00",
    )


class ReadableJobManagerTests(unittest.TestCase):
    def test_funasr_subprocess_retries_cloud_startup_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            episode = _episode(transcript_path)
            store.upsert_episode(episode)
            attempts = 0

            class FinishedProcess:
                def __init__(self, returncode: int) -> None:
                    self.returncode = returncode

                def poll(self) -> int:
                    return self.returncode

            def popen(*_args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    kwargs["stdout"].write(
                        "init_import_site: Failed to import the site module\n"
                        "OSError: [Errno 11] Resource deadlock avoided\n"
                    )
                    kwargs["stdout"].flush()
                    return FinishedProcess(1)
                return FinishedProcess(0)

            manager = ReadableJobManager(store, run_async=False)
            with (
                mock.patch("podcast_tracker.jobs.JOB_DIR", data_dir / "jobs"),
                mock.patch("podcast_tracker.jobs.subprocess.Popen", side_effect=popen),
                mock.patch("podcast_tracker.jobs.time.sleep"),
            ):
                result = manager._run_funasr_transcript_subprocess(episode, lambda _stage: None)

            self.assertEqual(result, transcript_path)
            self.assertEqual(attempts, 2)

    def test_only_retries_known_cloud_startup_failures(self) -> None:
        self.assertTrue(
            _is_retryable_asr_startup_failure(
                "init_import_site: Failed to import the site module; "
                "Resource deadlock avoided"
            )
        )
        self.assertTrue(
            _is_retryable_asr_startup_failure(
                "WATCHDOG: stage load_model made no progress for 600 seconds"
            )
        )
        self.assertFalse(_is_retryable_asr_startup_failure("model download failed"))

    def test_funasr_subprocess_retries_stuck_startup_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            episode = _episode(transcript_path)
            store.upsert_episode(episode)
            attempts = 0

            class StuckProcess:
                returncode = None

                def poll(self):
                    return self.returncode

                def kill(self) -> None:
                    self.returncode = -9

                def wait(self, timeout=None) -> int:
                    return -9

            class FinishedProcess:
                returncode = 0

                def poll(self) -> int:
                    return 0

            def popen(*_args, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    command = _args[0]
                    progress_path = Path(command[command.index("--progress-file") + 1])
                    progress_path.write_text('{"stage": "load_model"}\n', encoding="utf-8")
                return StuckProcess() if attempts == 1 else FinishedProcess()

            manager = ReadableJobManager(store, run_async=False)
            with (
                mock.patch("podcast_tracker.jobs.JOB_DIR", data_dir / "jobs"),
                mock.patch("podcast_tracker.jobs.subprocess.Popen", side_effect=popen),
                mock.patch("podcast_tracker.jobs.time.monotonic", side_effect=[0, 0, 3000, 3001]),
                mock.patch("podcast_tracker.jobs.time.sleep"),
            ):
                result = manager._run_funasr_transcript_subprocess(episode, lambda _stage: None)

            self.assertEqual(result, transcript_path)
            self.assertEqual(attempts, 2)

    def test_positive_int_env_uses_safe_default(self) -> None:
        with mock.patch.dict(os.environ, {"TEST_TIMEOUT": "600"}):
            self.assertEqual(_positive_int_env("TEST_TIMEOUT", 2700), 600)
        with mock.patch.dict(os.environ, {"TEST_TIMEOUT": "not-a-number"}):
            self.assertEqual(_positive_int_env("TEST_TIMEOUT", 2700), 2700)

    def test_start_readable_job_runs_codex_skill_and_detects_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(_episode(transcript_path))
            commands: list[list[str]] = []

            def runner(command: list[str], _cwd: Path, _log_path: Path) -> int:
                commands.append(command)
                readable_path.write_text("# 阅读版\n", encoding="utf-8")
                return 0

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                runner=runner,
                verify_runner=lambda _transcript, _readable, _log: 0,
                now=lambda: "now",
                run_async=False,
            )

            job = manager.start_readable_job("ep1")

            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.stage, "succeeded")
            self.assertEqual(job.progress, 100)
            self.assertEqual(job.stage_label, "完成")
            self.assertEqual(len(commands), 1)
            command_text = " ".join(commands[0])
            self.assertIn("/bin/codex", commands[0][0])
            self.assertEqual(commands[0][1], "exec")
            self.assertIn("--skip-git-repo-check", commands[0])
            self.assertIn("--add-dir", commands[0])
            self.assertNotIn("-q", commands[0])
            self.assertNotIn("--full-auto", commands[0])
            self.assertIn("podcast-readable", command_text)
            self.assertIn("ep1", command_text)

    def test_completion_hook_runs_after_readable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(_episode(transcript_path))
            calls: list[tuple[str, Path]] = []

            def runner(_command: list[str], _cwd: Path, _log_path: Path) -> int:
                readable_path.write_text("# 阅读版\n", encoding="utf-8")
                return 0

            def completion_hook(episode: Episode, readable: Path, _log_path: Path) -> None:
                calls.append((episode.id, readable))

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                runner=runner,
                verify_runner=lambda _transcript, _readable, _log: 0,
                completion_hook=completion_hook,
                now=lambda: "now",
                run_async=False,
            )

            job = manager.start_readable_job("ep1")

            self.assertEqual(job.status, "succeeded")
            self.assertEqual(calls, [("ep1", readable_path)])

    def test_completion_hook_failure_does_not_fail_readable_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(_episode(transcript_path))
            log_paths: list[Path] = []

            def runner(_command: list[str], _cwd: Path, log_path: Path) -> int:
                log_paths.append(log_path)
                readable_path.write_text("# 阅读版\n", encoding="utf-8")
                return 0

            def completion_hook(_episode: Episode, _readable: Path, _log_path: Path) -> None:
                raise RuntimeError("feishu boom")

            with mock.patch("podcast_tracker.jobs.JOB_DIR", data_dir / "jobs"):
                manager = ReadableJobManager(
                    store,
                    codex_bin="/bin/codex",
                    runner=runner,
                    verify_runner=lambda _transcript, _readable, _log: 0,
                    completion_hook=completion_hook,
                    now=lambda: "now",
                    run_async=False,
                )

                job = manager.start_readable_job("ep1")

            self.assertEqual(job.status, "succeeded")
            self.assertIn("Feishu export failed", log_paths[0].read_text(encoding="utf-8"))

    def test_start_readable_job_runs_transcript_pipeline_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "generated.md"
            readable_path = data_dir / "generated-阅读版.md"
            store = _store(data_dir)
            store.upsert_episode(_pending_episode())
            stages: list[str] = []

            def transcript_runner(
                _episode: Episode,
                progress_callback,
            ) -> Path:
                for stage in [
                    "prepare_audio",
                    "download_audio",
                    "convert_audio",
                    "load_model",
                    "transcribe_audio",
                    "write_transcript",
                    "transcript_ready",
                ]:
                    stages.append(stage)
                    progress_callback(stage)
                transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
                return transcript_path

            def runner(_command: list[str], _cwd: Path, _log_path: Path) -> int:
                readable_path.write_text("# 阅读版\n", encoding="utf-8")
                return 0

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                runner=runner,
                transcript_runner=transcript_runner,
                verify_runner=lambda _transcript, _readable, _log: 0,
                now=lambda: "now",
                run_async=False,
            )

            job = manager.start_readable_job("ep1")

            self.assertEqual(job.status, "succeeded")
            self.assertEqual(stages[0], "prepare_audio")
            self.assertEqual(stages[-1], "transcript_ready")
            self.assertEqual(store.get_episode("ep1").transcript_path, str(transcript_path))

    def test_asr_transcript_stage_is_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = _store(data_dir)
            store.upsert_episode(_pending_episode_with_id("ep1"))
            store.upsert_episode(_pending_episode_with_id("ep2"))

            active_count = 0
            max_active_count = 0
            count_lock = threading.Lock()
            first_entered = threading.Event()
            release_first = threading.Event()

            def transcript_runner(
                episode: Episode,
                progress_callback,
            ) -> Path:
                nonlocal active_count, max_active_count
                with count_lock:
                    active_count += 1
                    max_active_count = max(max_active_count, active_count)
                try:
                    progress_callback("transcribe_audio")
                    if episode.id == "ep1":
                        first_entered.set()
                        self.assertTrue(release_first.wait(timeout=2))
                    transcript_path = data_dir / f"{episode.id}.md"
                    transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
                    progress_callback("transcript_ready")
                    return transcript_path
                finally:
                    with count_lock:
                        active_count -= 1

            def runner(command: list[str], _cwd: Path, _log_path: Path) -> int:
                command_text = " ".join(command)
                episode_id = "ep1" if "ep1" in command_text else "ep2"
                (data_dir / f"{episode_id}-阅读版.md").write_text("# 阅读版\n", encoding="utf-8")
                return 0

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                runner=runner,
                transcript_runner=transcript_runner,
                verify_runner=lambda _transcript, _readable, _log: 0,
                now=lambda: "now",
                run_async=True,
            )

            job1 = manager.start_readable_job("ep1")
            self.assertTrue(first_entered.wait(timeout=2))
            job2 = manager.start_readable_job("ep2")

            for _ in range(50):
                if manager.get_job(job2.id).stage == "asr_queued":
                    break
                time.sleep(0.02)
            self.assertEqual(manager.get_job(job2.id).stage, "asr_queued")

            release_first.set()
            _wait_until_done(manager, job1.id)
            _wait_until_done(manager, job2.id)

            self.assertEqual(max_active_count, 1)
            self.assertEqual(manager.get_job(job1.id).status, "succeeded")
            self.assertEqual(manager.get_job(job2.id).status, "succeeded")

    def test_start_readable_job_fails_when_codex_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(_episode(transcript_path))

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                runner=lambda _command, _cwd, _log_path: 0,
                now=lambda: "now",
                run_async=False,
            )

            job = manager.start_readable_job("ep1")

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.stage, "failed")
            self.assertEqual(job.progress, 75)
            self.assertEqual(job.stage_label, "失败")
            self.assertIn("no readable document", job.error or "")

    def test_codex_auth_prompt_is_reported_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(_episode(transcript_path))

            def runner(_command: list[str], _cwd: Path, log_path: Path) -> int:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    "Sign in with ChatGPT to generate an API key or paste one you already have.\n"
                    "ERROR Raw mode is not supported on the current process.stdin\n",
                    encoding="utf-8",
                )
                return 0

            with mock.patch("podcast_tracker.jobs.JOB_DIR", data_dir / "jobs"):
                manager = ReadableJobManager(
                    store,
                    codex_bin="/bin/codex",
                    runner=runner,
                    now=lambda: "now",
                    run_async=False,
                )

                job = manager.start_readable_job("ep1")

            self.assertEqual(job.status, "failed")
            self.assertIn("后台 Codex CLI 未登录", job.error or "")

    def test_resolve_codex_bin_prefers_exec_capable_candidate(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch(
                "podcast_tracker.jobs._candidate_codex_bins",
                return_value=[Path("/old/codex"), Path("/new/codex")],
            ):
                with mock.patch(
                    "podcast_tracker.jobs._codex_supports_exec",
                    side_effect=lambda candidate: str(candidate) == "/new/codex",
                ):
                    self.assertEqual(_resolve_codex_bin(), "/new/codex")

    def test_resolve_codex_bin_allows_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"PODCAST_TRACKER_CODEX_BIN": "/custom/codex"}):
            self.assertEqual(_resolve_codex_bin(), "/custom/codex")

    def test_job_state_is_persisted_when_state_path_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            state_path = data_dir / "jobs_state.json"
            transcript_path.write_text("# 内部逐字稿\n", encoding="utf-8")
            store = _store(data_dir)
            store.upsert_episode(_episode(transcript_path))

            def runner(_command: list[str], _cwd: Path, _log_path: Path) -> int:
                readable_path.write_text("# 阅读版\n", encoding="utf-8")
                return 0

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                runner=runner,
                verify_runner=lambda _transcript, _readable, _log: 0,
                now=lambda: "now",
                run_async=False,
                state_path=state_path,
            )

            job = manager.start_readable_job("ep1")
            rows = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(rows[-1]["id"], job.id)
            self.assertEqual(rows[-1]["status"], "succeeded")

    def test_read_progress_stage_from_subprocess_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_path = Path(tmpdir) / "progress.json"
            self.assertIsNone(_read_progress_stage(progress_path))
            progress_path.write_text(json.dumps({"stage": "load_model"}), encoding="utf-8")
            self.assertEqual(_read_progress_stage(progress_path), "load_model")
            progress_path.write_text("{bad json", encoding="utf-8")
            self.assertIsNone(_read_progress_stage(progress_path))

    def test_active_persisted_job_requeues_once_then_fails_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            state_path = data_dir / "jobs_state.json"
            store = _store(data_dir)
            state_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "job1",
                            "episode_id": "ep1",
                            "status": "running",
                            "created_at": "before",
                            "stage": "transcribe_audio",
                            "progress": 45,
                        }
                    ]
                ),
                encoding="utf-8",
            )

            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                now=lambda: "after",
                state_path=state_path,
                run_async=False,  # keep restore side-effect free in tests
            )
            job = manager.latest_by_episode()["ep1"]

            # First interruption: requeued automatically.
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.attempts, 1)
            self.assertIsNone(job.error)

            # Second interruption exceeds MAX_AUTO_ATTEMPTS: marked failed.
            state_path.write_text(
                json.dumps(
                    [{**job.to_dict(), "status": "running", "stage": "transcribe_audio"}]
                ),
                encoding="utf-8",
            )
            manager = ReadableJobManager(
                store,
                codex_bin="/bin/codex",
                now=lambda: "after",
                state_path=state_path,
                run_async=False,
            )
            job = manager.latest_by_episode()["ep1"]

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.stage, "failed")
            self.assertIn("服务进程重启", job.error or "")

    def test_verify_readable_uses_sidecar_speaker_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            speaker_map_path = data_dir / "episode-阅读版.speaker-map.json"
            log_path = data_dir / "verify.log"
            transcript_path.write_text(
                "# 标题\n\n## 逐字稿\n\n### 说话人 1\n\n[00:00:00 - 00:00:02]\n\n我是主持人A。\n",
                encoding="utf-8",
            )
            readable_path.write_text(
                "# 标题\n\n**主持人A**  `00:00`\n我是主持人A。\n",
                encoding="utf-8",
            )
            speaker_map_path.write_text(json.dumps({"说话人 1": "主持人A"}, ensure_ascii=False), encoding="utf-8")

            exit_code = _run_verify_readable(transcript_path, readable_path, log_path)

            self.assertEqual(exit_code, 0)
            self.assertIn("--speaker-map-file", log_path.read_text(encoding="utf-8"))

    def test_verify_readable_applies_glossary_before_strict_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            transcript_path = data_dir / "episode.md"
            readable_path = data_dir / "episode-阅读版.md"
            log_path = data_dir / "verify.log"
            transcript_path.write_text(
                "# 标题\n\n## 逐字稿\n\n### 主持人\n\n"
                "[00:00:00 - 00:00:02]\n\n我们在用 cloudcode。\n",
                encoding="utf-8",
            )
            readable_path.write_text(
                "# 标题\n\n**主持人**  `00:00`\n我们在用 cloudcode。\n",
                encoding="utf-8",
            )

            exit_code = _run_verify_readable(transcript_path, readable_path, log_path)

            log_text = log_path.read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0, log_text)
            self.assertIn("Claude Code", readable_path.read_text(encoding="utf-8"))
            self.assertIn("apply_glossary.py", log_text)
            self.assertIn("--strict", log_text)
            self.assertIn("--emit-glossary-candidates", log_text)


def _wait_until_done(manager: ReadableJobManager, job_id: str) -> None:
    for _ in range(100):
        if not manager.get_job(job_id).active:
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


if __name__ == "__main__":
    unittest.main()
