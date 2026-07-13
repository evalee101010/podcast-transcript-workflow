"""FunASR local backend — the default ASR engine for the MVP.

Pipeline (all local, no API key):

    audio (.m4a) -> ffmpeg 16k mono wav
                 -> FunASR AutoModel(paraformer-zh + fsmn-vad + ct-punc + cam++)
                 -> sentence_info (text / start / end / spk)
                 -> verbatim speaker-segmented Markdown

Design rules:
  * VERBATIM ONLY. We take FunASR's sentence_info straight to Markdown. The only
    transformations are (a) punctuation restoration, which FunASR's ct-punc model
    does as part of recognition (it adds punctuation, it does not rewrite words),
    and (b) merging consecutive turns by the same speaker. No summarisation, no
    deletion, no LLM cleanup. Ever.
  * Heavy deps (funasr, torch) are imported lazily inside the function so the
    tracker CLI stays importable without them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .asr import download_audio, make_run_dir
from .audio_utils import ms_to_timestamp, to_wav_16k_mono
from .clustering_patch import apply_clustering_speedup
from .config import DATA_DIR
from .glossary import default_glossary_paths, hotword_terms, load_glossary, write_hotword_file
from .models import Episode, TranscriptSegment
from .private_runtime import private_runtime_dir
from .transcript import write_transcript_markdown

# FunASR model aliases. paraformer-zh is the battle-tested Chinese path with
# clean sentence-level timestamps; SenseVoiceSmall is offered as an option for
# mixed zh/en/ja/ko/yue audio (its native diarization is newer, May 2026).
DEFAULT_ASR_MODEL = "paraformer-zh"
DEFAULT_VAD_MODEL = "fsmn-vad"
DEFAULT_PUNC_MODEL = "ct-punc"
DEFAULT_SPK_MODEL = "cam++"
DEFAULT_DEVICE = "cpu"  # Apple Silicon runs FunASR on CPU; MPS support is limited
DEFAULT_BATCH_SIZE_S = 300
DEFAULT_WORK_DIR = DATA_DIR / "audio"
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class FunasrOptions:
    asr_model: str = DEFAULT_ASR_MODEL
    vad_model: str = DEFAULT_VAD_MODEL
    punc_model: str = DEFAULT_PUNC_MODEL
    spk_model: str = DEFAULT_SPK_MODEL
    device: str = DEFAULT_DEVICE
    batch_size_s: int = DEFAULT_BATCH_SIZE_S
    work_dir: Path = DEFAULT_WORK_DIR
    # Optional spk index -> display name mapping, e.g. ["主持人", "嘉宾"].
    speaker_names: tuple[str, ...] = field(default_factory=tuple)
    model_hub: str = "ms"  # "ms" = ModelScope (China-friendly), "hf" = HuggingFace
    reuse_existing: bool = True
    use_hotwords: bool = True
    glossary_paths: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FunasrResult:
    transcript_path: Path
    work_dir: Path
    audio_path: Path
    wav_path: Path
    raw_output: Path
    segment_count: int
    speaker_count: int


def transcribe_episode_funasr(
    episode: Episode,
    options: FunasrOptions | None = None,
    progress_callback: ProgressCallback | None = None,
) -> FunasrResult:
    options = options or FunasrOptions()
    if not episode.audio_url:
        raise RuntimeError(f"Episode has no audio_url: {episode.id}")

    run_dir = _latest_reusable_run_dir(options.work_dir, episode.id) if options.reuse_existing else None
    if run_dir is None:
        _report(progress_callback, "prepare_audio")
        run_dir = make_run_dir(options.work_dir, episode.id)

    audio_path = _find_source_audio(run_dir)
    if audio_path is None:
        _report(progress_callback, "download_audio")
        audio_path = download_audio(episode, run_dir)

    wav_path = run_dir / "audio_16k_mono.wav"
    if not wav_path.exists() or wav_path.stat().st_size == 0:
        _report(progress_callback, "convert_audio")
        wav_path = to_wav_16k_mono(audio_path, wav_path)

    apply_clustering_speedup()  # best effort; safe no-op if it can't patch

    raw_output = run_dir / "raw_funasr.json"
    if raw_output.exists() and raw_output.stat().st_size > 0:
        payload = json.loads(raw_output.read_text(encoding="utf-8"))
    else:
        _report(progress_callback, "load_model")
        _prepare_jieba_runtime()
        try:
            from funasr import AutoModel  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on local install
            raise RuntimeError(
                "FunASR is not installed. Install the local ASR extra first:\n"
                "  pip install -r requirements-funasr.txt\n"
                f"(import error: {exc})"
            ) from exc

        model = AutoModel(
            model=options.asr_model,
            vad_model=options.vad_model,
            punc_model=options.punc_model,
            spk_model=options.spk_model,
            device=options.device,
            hub=options.model_hub,
            disable_update=True,
        )

        _report(progress_callback, "transcribe_audio")
        hotword_path = _prepare_hotword_file(run_dir, options)
        raw = _generate_with_optional_hotword(
            model,
            input_path=wav_path,
            batch_size_s=options.batch_size_s,
            hotword_path=hotword_path,
        )
        payload = raw[0] if isinstance(raw, list) and raw else raw
        raw_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    sentence_info = _extract_sentence_info(payload)
    segments = segments_from_sentence_info(sentence_info, options.speaker_names)
    if not segments:
        raise RuntimeError(
            "FunASR returned no usable sentence_info. Check that spk_model is set "
            "and the audio contains speech."
        )

    _report(progress_callback, "write_transcript")
    from .asr import merge_adjacent_segments  # reuse shared merge logic

    merged = merge_adjacent_segments(segments)
    transcript_path = write_transcript_markdown(episode, merged)
    _report(progress_callback, "transcript_ready")
    return FunasrResult(
        transcript_path=transcript_path,
        work_dir=run_dir,
        audio_path=audio_path,
        wav_path=wav_path,
        raw_output=raw_output,
        segment_count=len(segments),
        speaker_count=len({seg.speaker for seg in segments}),
    )


def _extract_sentence_info(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        info = payload.get("sentence_info")
        if isinstance(info, list):
            return [row for row in info if isinstance(row, dict)]
    return []


def segments_from_sentence_info(
    sentence_info: list[dict],
    speaker_names: tuple[str, ...] = (),
) -> list[TranscriptSegment]:
    """Convert FunASR sentence_info rows into verbatim transcript segments."""
    segments: list[TranscriptSegment] = []
    for row in sentence_info:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                speaker=_speaker_label(row.get("spk"), speaker_names),
                text=text,
                start=ms_to_timestamp(row.get("start")),
                end=ms_to_timestamp(row.get("end")),
            )
        )
    return segments


def _speaker_label(spk: object, speaker_names: tuple[str, ...]) -> str:
    if isinstance(spk, bool):
        spk = None
    if isinstance(spk, (int, float)):
        index = int(spk)
        if 0 <= index < len(speaker_names):
            return speaker_names[index]
        return f"说话人 {index + 1}"
    if isinstance(spk, str) and spk.strip():
        return spk.strip()
    return "说话人"


def _report(progress_callback: ProgressCallback | None, stage: str) -> None:
    if progress_callback:
        progress_callback(stage)


def _prepare_hotword_file(run_dir: Path, options: FunasrOptions) -> Path | None:
    if not options.use_hotwords:
        return None
    glossary = load_glossary(default_glossary_paths() + list(options.glossary_paths))
    terms = hotword_terms(glossary)
    return write_hotword_file(run_dir / "hotwords.txt", terms)


def _prepare_jieba_runtime(cache_dir: Path | None = None) -> Path:
    """Prebuild jieba's prefix dict cache in a stable runtime directory.

    FunASR's punctuation model calls `jieba.load_userdict()` while building
    AutoModel. On macOS, letting jieba lazily build its cache under the system
    temp directory can intermittently fail with `Errno 11 Resource deadlock
    avoided`, especially when jobs are retried from a long-running service.
    """
    cache_dir = cache_dir or (private_runtime_dir() / "cache" / "jieba")
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        import jieba  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency install issue
        raise RuntimeError(f"jieba is required by FunASR punctuation model: {exc}") from exc

    jieba.dt.tmp_dir = str(cache_dir)
    try:
        jieba.initialize()
    except OSError:
        for cache_file in cache_dir.glob("jieba*.cache"):
            try:
                cache_file.unlink()
            except OSError:
                pass
        try:
            jieba.dt.initialized = False
            jieba.initialize()
        except OSError as exc:
            raise RuntimeError(
                "jieba failed to initialize its cache for FunASR. "
                f"Cache directory: {cache_dir}. Original error: {exc}"
            ) from exc
    return cache_dir


def _generate_with_optional_hotword(
    model: object,
    input_path: Path,
    batch_size_s: int,
    hotword_path: Path | None,
) -> object:
    kwargs = {
        "input": str(input_path),
        "batch_size_s": batch_size_s,
        "return_spk_res": True,
    }
    if hotword_path is not None:
        kwargs["hotword"] = str(hotword_path)
    try:
        return model.generate(**kwargs)
    except TypeError:
        if "hotword" not in kwargs:
            raise
        kwargs.pop("hotword")
        return model.generate(**kwargs)


def _latest_reusable_run_dir(work_dir: Path, episode_id: str) -> Path | None:
    root = work_dir / episode_id
    if not root.exists():
        return None
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir()
        and (
            (path / "raw_funasr.json").exists()
            or (path / "audio_16k_mono.wav").exists()
            or _find_source_audio(path) is not None
        )
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _find_source_audio(run_dir: Path) -> Path | None:
    for path in sorted(run_dir.glob("source.*")):
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def dry_run_report(episode: Episode, options: FunasrOptions | None = None) -> list[str]:
    import shutil

    options = options or FunasrOptions()
    try:
        import funasr  # type: ignore  # noqa: F401

        funasr_state = "installed"
    except Exception:
        funasr_state = "missing (pip install -r requirements-funasr.txt)"

    lines = [
        "Backend: funasr (local, no API key)",
        f"Episode: {episode.id}",
        f"Title: {episode.title}",
        f"Audio URL: {episode.audio_url or '(missing)'}",
        f"Work dir: {options.work_dir / episode.id}",
        f"ASR model: {options.asr_model}",
        f"VAD/PUNC/SPK: {options.vad_model} / {options.punc_model} / {options.spk_model}",
        f"Device: {options.device}",
        f"Model hub: {options.model_hub}",
        f"Hotwords: {'enabled' if options.use_hotwords else 'disabled'}",
        f"ffmpeg: {shutil.which('ffmpeg') or '(missing)'}",
        f"FunASR: {funasr_state}",
    ]
    if not episode.audio_url:
        lines.append("Status: cannot run; episode has no audio_url.")
    elif not shutil.which("ffmpeg"):
        lines.append("Status: install ffmpeg before running.")
    elif funasr_state != "installed":
        lines.append("Status: install FunASR before running.")
    else:
        lines.append("Status: ready to download, convert, and transcribe locally.")
    return lines
