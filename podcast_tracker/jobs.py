from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import os
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .config import DOCS_DIR, PROJECT_ROOT, READABLE_SKILL_DIR
from .models import Episode, utc_now_iso
from .private_runtime import private_runtime_dir
from .readable import readable_path_for_episode, speaker_map_path_for_readable
from .store import Store


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


JOB_DIR = private_runtime_dir() / "jobs"
JOB_STATE_FILE = private_runtime_dir() / "jobs_state.json"
ACTIVE_STATUSES = {"queued", "running"}
STAGE_META = {
    "queued": (5, "排队中"),
    "asr_queued": (8, "等待ASR"),
    "prepare_audio": (10, "准备音频"),
    "download_audio": (15, "下载音频"),
    "convert_audio": (20, "转码音频"),
    "load_model": (30, "加载模型"),
    "transcribe_audio": (45, "ASR处理中"),
    "write_transcript": (62, "写入逐字稿"),
    "transcript_ready": (65, "逐字稿就绪"),
    "generate_readable": (75, "生成阅读版"),
    "apply_glossary": (88, "修正术语"),
    "verify_readable": (94, "校验阅读版"),
    "send_lark": (97, "发送飞书"),
    "checking": (98, "收尾确认"),
    "succeeded": (100, "完成"),
    "failed": (0, "失败"),
}

# Watchdog: kill the ASR subprocess if a stage makes no progress for this long.
STAGE_TIMEOUT_SECONDS = {
    "prepare_audio": 600,
    "download_audio": 1800,
    "convert_audio": 1800,
    "load_model": _positive_int_env(
        "PODCAST_TRACKER_MODEL_LOAD_TIMEOUT_SECONDS", 2700
    ),  # first run may download models from ModelScope
    "transcribe_audio": 3 * 3600,
    "write_transcript": 600,
}
DEFAULT_STAGE_TIMEOUT_SECONDS = 900
# Hard cap for the codex readable-generation step.
CODEX_TIMEOUT_SECONDS = 3600
TIMEOUT_EXIT_CODE = 124
# How long a job may wait behind other episodes for the single ASR slot.
ASR_LOCK_WAIT_SECONDS = 6 * 3600
# Jobs interrupted by a restart are requeued automatically this many times.
MAX_AUTO_ATTEMPTS = 1
ASR_BACKEND_ENV = "PODCAST_TRACKER_ASR_BACKEND"
ASR_STARTUP_MAX_ATTEMPTS = 3
ASR_STARTUP_RETRY_DELAY_SECONDS = 2
ASR_RETRYABLE_WATCHDOG_STAGES = {"prepare_audio", "load_model"}

CommandRunner = Callable[[list[str], Path, Path], int]
TranscriptRunner = Callable[[Episode, Callable[[str], None]], Path]
VerifyRunner = Callable[[Path, Path, Path], int]
CompletionHook = Callable[[Episode, Path, Path], None]
Clock = Callable[[], str]


@dataclass(frozen=True)
class ReadableJob:
    id: str
    episode_id: str
    status: str
    created_at: str
    stage: str = "queued"
    progress: int = 5
    stage_started_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    log_path: str | None = None
    attempts: int = 0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def stage_label(self) -> str:
        return STAGE_META.get(self.stage, (self.progress, self.stage))[1]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "episode_id": self.episode_id,
            "status": self.status,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "progress": self.progress,
            "stage_started_at": self.stage_started_at,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "log_path": self.log_path,
            "attempts": self.attempts,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReadableJob":
        return cls(
            id=str(data["id"]),
            episode_id=str(data["episode_id"]),
            status=str(data.get("status") or "failed"),
            created_at=str(data.get("created_at") or ""),
            stage=str(data.get("stage") or "failed"),
            progress=int(data.get("progress") or 0),
            stage_started_at=data.get("stage_started_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
            log_path=data.get("log_path"),
            attempts=int(data.get("attempts") or 0),
        )


class ReadableJobManager:
    def __init__(
        self,
        store: Store,
        codex_bin: str | None = None,
        runner: CommandRunner | None = None,
        transcript_runner: TranscriptRunner | None = None,
        verify_runner: VerifyRunner | None = None,
        completion_hook: CompletionHook | None = None,
        now: Clock = utc_now_iso,
        run_async: bool = True,
        state_path: Path | None = None,
    ) -> None:
        self.store = store
        self.codex_bin = codex_bin or _resolve_codex_bin()
        self.runner = runner or _run_command
        self.transcript_runner = transcript_runner or self._run_funasr_transcript_subprocess
        self.verify_runner = verify_runner or _run_verify_readable
        self.completion_hook = completion_hook
        self.now = now
        self.run_async = run_async
        self.state_path = state_path
        self._lock = threading.Lock()
        self._asr_lock = threading.Lock()
        self._store_lock = threading.Lock()
        self._jobs: dict[str, ReadableJob] = self._load_persisted_jobs()
        self._active_by_episode: dict[str, str] = {}
        self._resume_restored_jobs()

    def start_readable_job(self, episode_id: str, force: bool = False) -> ReadableJob:
        episode = self._get_episode(episode_id)
        readable_path = readable_path_for_episode(episode)
        if force and readable_path is not None:
            from .document_patches import load_patch_events

            if load_patch_events(readable_path):
                raise ValueError(
                    "该单集已有人工修订（.patches.json 非空），禁止原地重新生成基线。"
                    "请先归档或迁移修订记录后再 force 重跑。"
                )
            readable_path = None  # proceed as if no readable exists
        with self._lock:
            active_job_id = self._active_by_episode.get(episode_id)
            if active_job_id:
                return self._jobs[active_job_id]

            created_at = self.now()
            stage = "succeeded" if readable_path else "queued"
            job = ReadableJob(
                id=uuid.uuid4().hex[:12],
                episode_id=episode_id,
                status="succeeded" if readable_path else "queued",
                stage=stage,
                progress=_stage_progress(stage),
                stage_started_at=created_at,
                created_at=created_at,
                finished_at=created_at if readable_path else None,
            )
            self._jobs[job.id] = job
            self._persist_jobs_locked()
            if readable_path:
                return job
            self._active_by_episode[episode_id] = job.id

        if self.run_async:
            thread = threading.Thread(target=self._run_job, args=(job.id,), daemon=True)
            thread.start()
        else:
            self._run_job(job.id)
        return self.get_job(job.id)

    def get_job(self, job_id: str) -> ReadableJob:
        with self._lock:
            return self._jobs[job_id]

    def latest_by_episode(self) -> dict[str, ReadableJob]:
        with self._lock:
            latest: dict[str, ReadableJob] = {}
            for job in self._jobs.values():
                latest[job.episode_id] = job
            return latest

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            log_path = JOB_DIR / f"{job.id}-{job.episode_id}.log"
            started_at = self.now()
            job = replace(
                job,
                status="running",
                stage="prepare_audio",
                progress=_stage_progress("prepare_audio"),
                stage_started_at=started_at,
                started_at=started_at,
                log_path=str(log_path),
            )
            self._jobs[job_id] = job
            self._persist_jobs_locked()

        try:
            episode = self._get_episode(job.episode_id)
            transcript_path = _existing_transcript_path(episode)
            if transcript_path is None:
                transcript_path = self._run_transcript_job(job_id, episode)
                episode = self._mark_transcribed(episode.id, transcript_path)
            else:
                self._set_stage(job_id, "transcript_ready")

            transcript_path = _existing_transcript_path(episode)
            if transcript_path is None:
                raise RuntimeError("Transcript generation finished but no transcript file was found.")

            self._set_stage(job_id, "generate_readable")
            command = self._codex_command(job.episode_id)
            exit_code = self.runner(command, PROJECT_ROOT, log_path)
            if exit_code == TIMEOUT_EXIT_CODE:
                raise RuntimeError(
                    f"阅读版生成超时（>{CODEX_TIMEOUT_SECONDS // 60} 分钟），已强制终止。"
                    f"Log: {log_path}"
                )
            if exit_code != 0:
                hint = _codex_failure_hint(log_path)
                if hint:
                    raise RuntimeError(f"{hint} Log: {log_path}")
                raise RuntimeError(f"Codex exited with status {exit_code}. Log: {log_path}")

            episode = self._get_episode(job.episode_id)
            readable_path = readable_path_for_episode(episode)
            if readable_path is None:
                hint = _codex_failure_hint(log_path)
                if hint:
                    raise RuntimeError(f"{hint} Log: {log_path}")
                raise RuntimeError(f"Codex finished but no readable document was found. Log: {log_path}")

            self._set_stage(job_id, "apply_glossary")
            verify_exit_code = self.verify_runner(transcript_path, readable_path, log_path)
            if verify_exit_code != 0:
                raise RuntimeError(
                    f"Readable verifier exited with status {verify_exit_code}. Log: {log_path}"
                )

            if self.completion_hook:
                self._set_stage(job_id, "send_lark")
                try:
                    self.completion_hook(self._get_episode(job.episode_id), readable_path, log_path)
                except Exception as exc:
                    _append_log_line(log_path, f"Feishu export failed after readable generation: {exc}")

            self._set_stage(job_id, "checking")
            self._finish(job_id, status="succeeded")
        except Exception as exc:
            self._finish(job_id, status="failed", error=str(exc))

    def _finish(self, job_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if status == "succeeded":
                stage = "succeeded"
                progress = _stage_progress(stage)
            elif status == "failed":
                stage = "failed"
                progress = job.progress
            else:
                stage = job.stage
                progress = job.progress
            self._jobs[job_id] = replace(
                job,
                status=status,
                stage=stage,
                progress=progress,
                stage_started_at=self.now() if stage != job.stage else job.stage_started_at,
                finished_at=self.now(),
                error=error,
            )
            self._active_by_episode.pop(job.episode_id, None)
            self._persist_jobs_locked()

    def _set_stage(self, job_id: str, stage: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._jobs[job_id] = replace(
                job,
                stage=stage,
                progress=_stage_progress(stage),
                stage_started_at=self.now(),
            )
            self._persist_jobs_locked()

    def _load_persisted_jobs(self) -> dict[str, ReadableJob]:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            rows = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        jobs: dict[str, ReadableJob] = {}
        changed = False
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                job = ReadableJob.from_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            if job.active:
                changed = True
                if job.attempts < MAX_AUTO_ATTEMPTS:
                    # Interrupted by a restart: requeue once automatically.
                    job = replace(
                        job,
                        status="queued",
                        stage="queued",
                        progress=_stage_progress("queued"),
                        stage_started_at=self.now(),
                        started_at=None,
                        finished_at=None,
                        error=None,
                        attempts=job.attempts + 1,
                    )
                else:
                    job = replace(
                        job,
                        status="failed",
                        stage="failed",
                        finished_at=self.now(),
                        error="服务进程重启，任务已中断，请点击重试。",
                    )
            jobs[job.id] = job
        if changed:
            self._jobs = jobs
            self._persist_jobs_locked()
        return jobs

    def _resume_restored_jobs(self) -> None:
        """Restart threads for jobs requeued during restore (async mode only)."""
        if not self.run_async:
            return
        with self._lock:
            pending = [
                job for job in self._jobs.values()
                if job.status == "queued" and job.episode_id not in self._active_by_episode
            ]
            for job in pending:
                self._active_by_episode[job.episode_id] = job.id
        for job in sorted(pending, key=lambda item: item.created_at):
            threading.Thread(target=self._run_job, args=(job.id,), daemon=True).start()

    def _persist_jobs_locked(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            rows = [job.to_dict() for job in self._jobs.values()]
            rows.sort(key=lambda item: item.get("created_at") or "")
            if len(rows) > 200:
                rows = rows[-200:]
            tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(self.state_path)
        except OSError:
            return

    def _codex_command(self, episode_id: str) -> list[str]:
        skill_path = READABLE_SKILL_DIR / "SKILL.md"
        prompt = (
            f"First read {skill_path} completely and use it as the controlling workflow. "
            f"Generate the final user-facing readable podcast transcript for episode_id {episode_id!r}. "
            "The internal verbatim transcript has already been created by the web job; do not run ASR again. "
            "Run `python -m podcast_tracker readable "
            f"{episode_id}` to get the source transcript path, target readable path, and verifier command. "
            "Create or fix only the same-name -阅读版.md file. "
            "When you apply high-confidence real speaker names, also write the same-name "
            "`-阅读版.speaker-map.json` JSON object mapping original labels to final names. "
            "Do not summarize, do not delete substantive content, and do not modify the original transcript. "
            "Run the repository verifier and fix the readable file until it passes. "
            "Return only the final readable Markdown path and verification result."
        )
        return [
            self.codex_bin,
            "exec",
            "-C",
            str(PROJECT_ROOT),
            "--add-dir",
            str(DOCS_DIR),
            "-s",
            "workspace-write",
            prompt,
        ]

    def _run_transcript_job(self, job_id: str, episode: Episode) -> Path:
        acquired = self._asr_lock.acquire(blocking=False)
        if not acquired:
            self._set_stage(job_id, "asr_queued")
            acquired = self._asr_lock.acquire(timeout=ASR_LOCK_WAIT_SECONDS)
            if not acquired:
                raise RuntimeError(
                    f"等待 ASR 队列超过 {ASR_LOCK_WAIT_SECONDS // 3600} 小时仍未轮到，"
                    "已放弃本次任务；请检查是否有前序任务卡死。"
                )
        try:
            self._set_stage(job_id, "prepare_audio")
            return self.transcript_runner(
                episode,
                lambda stage: self._set_stage(job_id, stage),
            )
        finally:
            self._asr_lock.release()

    def _run_funasr_transcript_subprocess(
        self,
        episode: Episode,
        progress_callback: Callable[[str], None],
    ) -> Path:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:8]
        progress_file = JOB_DIR / f"progress-{run_id}-{episode.id}.json"
        log_path = JOB_DIR / f"asr-{run_id}-{episode.id}.log"
        python = PROJECT_ROOT / ".venv" / "bin" / "python"
        if not python.exists():
            python = Path(sys.executable)
        backend = (os.getenv(ASR_BACKEND_ENV) or "funasr").strip() or "funasr"
        command = [
            str(python),
            "-m",
            "podcast_tracker",
            "transcribe-auto",
            episode.id,
            "--backend",
            backend,
            "--progress-file",
            str(progress_file),
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PODTRACK_DISABLE_CLUSTER_PATCH", "1")
        exit_code = 1
        for attempt in range(1, ASR_STARTUP_MAX_ATTEMPTS + 1):
            try:
                progress_file.unlink()
            except OSError:
                pass
            mode = "w" if attempt == 1 else "a"
            with log_path.open(mode, encoding="utf-8") as log:
                if attempt > 1:
                    log.write(f"\n\nRETRY: startup attempt {attempt}\n")
                log.write("$ " + " ".join(command) + "\n\n")
                log.flush()
                process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                )
                last_stage: str | None = None
                stage_changed_at = time.monotonic()
                while process.poll() is None:
                    stage = _read_progress_stage(progress_file)
                    if stage and stage != last_stage:
                        progress_callback(stage)
                        last_stage = stage
                        stage_changed_at = time.monotonic()
                    limit = STAGE_TIMEOUT_SECONDS.get(
                        last_stage or "prepare_audio", DEFAULT_STAGE_TIMEOUT_SECONDS
                    )
                    if time.monotonic() - stage_changed_at > limit:
                        process.kill()
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            pass
                        stuck_stage = last_stage or "prepare_audio"
                        log.write(
                            f"\n\nWATCHDOG: stage {stuck_stage} made no progress for "
                            f"{limit} seconds; subprocess killed.\n"
                        )
                        try:
                            progress_file.unlink()
                        except OSError:
                            pass
                        if (
                            stuck_stage in ASR_RETRYABLE_WATCHDOG_STAGES
                            and attempt < ASR_STARTUP_MAX_ATTEMPTS
                        ):
                            break
                        raise RuntimeError(
                            f"ASR 子进程在阶段 {stuck_stage} 超过 {limit // 60} 分钟无进展，"
                            f"已强制终止（常见原因：外接盘休眠、模型下载卡住）。Log: {log_path}"
                        )
                    time.sleep(1)
                stage = _read_progress_stage(progress_file)
                if stage and stage != last_stage:
                    progress_callback(stage)
                exit_code = process.returncode

            if exit_code == 0:
                break
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            if attempt >= ASR_STARTUP_MAX_ATTEMPTS or not _is_retryable_asr_startup_failure(
                detail
            ):
                try:
                    progress_file.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"FunASR subprocess exited with status {exit_code}. Log: {log_path}\n{detail}"
                )
            time.sleep(ASR_STARTUP_RETRY_DELAY_SECONDS * attempt)

        try:
            progress_file.unlink()
        except OSError:
            pass

        updated = self._get_episode(episode.id)
        transcript_path = _existing_transcript_path(updated)
        if transcript_path is None:
            raise RuntimeError(
                f"FunASR subprocess finished but no transcript was recorded. Log: {log_path}"
            )
        return transcript_path

    def _get_episode(self, episode_id: str) -> Episode:
        with self._store_lock:
            return self.store.get_episode(episode_id)

    def _mark_transcribed(self, episode_id: str, transcript_path: Path) -> Episode:
        with self._store_lock:
            return self.store.mark_transcribed(episode_id, transcript_path)


def _run_command(
    command: list[str],
    cwd: Path,
    log_path: Path,
    timeout_seconds: int = CODEX_TIMEOUT_SECONDS,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # own process group so we can kill children too
        )
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            log.write(f"\n\nTIMEOUT: command exceeded {timeout_seconds}s and was killed.\n")
            return TIMEOUT_EXIT_CODE


def _append_log_line(log_path: Path, text: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n" + text.rstrip() + "\n")
    except OSError:
        return


def _codex_failure_hint(log_path: Path) -> str | None:
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
    auth_markers = [
        "Sign in with ChatGPT",
        "Paste an API key",
        "OPENAI_API_KEY",
        "Raw mode is not supported",
        "Missing organization in id_token claims",
    ]
    if any(marker in text for marker in auth_markers):
        return (
            "后台 Codex CLI 未登录或未配置 OPENAI_API_KEY，"
            "Web 任务无法在非交互环境里生成阅读版。"
        )
    return None


def _resolve_codex_bin() -> str:
    override = os.getenv("PODCAST_TRACKER_CODEX_BIN")
    if override:
        return override

    candidates: list[Path] = []
    for candidate in _candidate_codex_bins():
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if _codex_supports_exec(candidate):
            return str(candidate)

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "codex"


def _candidate_codex_bins() -> list[Path]:
    candidates: list[Path] = []
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates.extend(sorted(nvm_root.glob("*/bin/codex"), reverse=True))

    candidates.extend([Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")])
    return candidates


def _codex_supports_exec(candidate: Path) -> bool:
    if not candidate.exists():
        return False
    try:
        completed = subprocess.run(
            [str(candidate), "exec", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and "Run Codex non-interactively" in completed.stdout


def _run_funasr_transcript(
    episode: Episode,
    progress_callback: Callable[[str], None],
) -> Path:
    from .asr_funasr import FunasrOptions, transcribe_episode_funasr

    result = transcribe_episode_funasr(
        episode,
        FunasrOptions(),
        progress_callback=progress_callback,
    )
    return result.transcript_path


def _run_verify_readable(transcript_path: Path, readable_path: Path, log_path: Path) -> int:
    applier = PROJECT_ROOT / "scripts" / "apply_glossary.py"
    verifier = PROJECT_ROOT / "scripts" / "verify_readable.py"
    python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    apply_command = [str(python), str(applier), str(readable_path)]
    candidate_path = readable_path.with_name(readable_path.stem + ".glossary-candidates.json")
    verify_command = [
        str(python),
        str(verifier),
        str(transcript_path),
        str(readable_path),
        "--strict",
        "--emit-glossary-candidates",
        str(candidate_path),
    ]
    speaker_map_path = speaker_map_path_for_readable(readable_path)
    if speaker_map_path.exists():
        verify_command.extend(["--speaker-map-file", str(speaker_map_path)])
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n\n$ " + " ".join(apply_command) + "\n\n")
        log.flush()
        completed = subprocess.run(
            apply_command,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
        log.write("\n\n$ " + " ".join(verify_command) + "\n\n")
        log.flush()
        completed = subprocess.run(
            verify_command,
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    return completed.returncode


def _read_progress_stage(progress_file: Path) -> str | None:
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stage = data.get("stage")
    return str(stage) if isinstance(stage, str) and stage else None


def _is_retryable_asr_startup_failure(detail: str) -> bool:
    normalized = detail.lower()
    return "resource deadlock avoided" in normalized or (
        "init_import_site" in normalized and "failed to import the site module" in normalized
    ) or (
        "watchdog: stage prepare_audio" in normalized
        or "watchdog: stage load_model" in normalized
    )


def _existing_transcript_path(episode: Episode) -> Path | None:
    if not episode.transcript_path:
        return None
    transcript_path = Path(episode.transcript_path)
    if transcript_path.exists() and transcript_path.is_file():
        return transcript_path
    return None


def _stage_progress(stage: str) -> int:
    return STAGE_META.get(stage, (0, stage))[0]
