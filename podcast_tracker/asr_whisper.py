"""faster-whisper fallback backend — ASR only, NO speaker diarization.

Use this only when FunASR is unavailable or you don't need speaker separation.
Whisper produces strong verbatim text but cannot tell speakers apart, so the
whole transcript is attributed to a single speaker label. For the core
"按说话人整理逐字稿" requirement, prefer the FunASR backend.

Heavy deps are imported lazily so the CLI stays importable without them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .asr import download_audio, make_run_dir
from .audio_utils import seconds_to_timestamp, to_wav_16k_mono
from .config import DATA_DIR
from .models import Episode, TranscriptSegment
from .transcript import write_transcript_markdown

DEFAULT_MODEL_SIZE = "large-v3-turbo"  # ~5x faster than large-v3 on CPU, minor quality cost
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"  # CPU-friendly; use "float16" on GPU
DEFAULT_SPEAKER = "说话人 1"
DEFAULT_WORK_DIR = DATA_DIR / "audio"
PROMPT_TERM_LIMIT = 120  # initial_prompt is capped at ~224 tokens by whisper


@dataclass(frozen=True)
class WhisperOptions:
    model_size: str = DEFAULT_MODEL_SIZE
    device: str = DEFAULT_DEVICE
    compute_type: str = DEFAULT_COMPUTE_TYPE
    language: str | None = "zh"
    speaker: str = DEFAULT_SPEAKER
    work_dir: Path = DEFAULT_WORK_DIR
    # Bias recognition toward glossary terms (Claude Code, Anthropic, ...).
    use_glossary_prompt: bool = True
    initial_prompt: str | None = None  # overrides the glossary prompt when set
    # False prevents hallucinations from snowballing across long audio.
    condition_on_previous_text: bool = False
    word_timestamps: bool = True  # needed for future speaker alignment (hybrid engine)
    to_simplified: bool = True  # whisper occasionally emits traditional characters


@dataclass(frozen=True)
class WhisperResult:
    transcript_path: Path
    work_dir: Path
    audio_path: Path
    wav_path: Path
    raw_output: Path
    segment_count: int


def transcribe_episode_whisper(
    episode: Episode,
    options: WhisperOptions | None = None,
) -> WhisperResult:
    options = options or WhisperOptions()
    if not episode.audio_url:
        raise RuntimeError(f"Episode has no audio_url: {episode.id}")

    run_dir = make_run_dir(options.work_dir, episode.id)
    audio_path = download_audio(episode, run_dir)
    wav_path = to_wav_16k_mono(audio_path, run_dir / "audio_16k_mono.wav")

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local install
        raise RuntimeError(
            "faster-whisper is not installed. Install it first:\n"
            "  pip install faster-whisper\n"
            f"(import error: {exc})"
        ) from exc

    model = WhisperModel(
        options.model_size,
        device=options.device,
        compute_type=options.compute_type,
    )
    initial_prompt = options.initial_prompt
    if initial_prompt is None and options.use_glossary_prompt:
        initial_prompt = _glossary_prompt()
    iterator, info = model.transcribe(
        str(wav_path),
        language=options.language,
        vad_filter=True,
        initial_prompt=initial_prompt,
        condition_on_previous_text=options.condition_on_previous_text,
        word_timestamps=options.word_timestamps,
    )

    to_simplified = _simplified_converter() if options.to_simplified else None
    rows: list[dict] = []
    segments: list[TranscriptSegment] = []
    for piece in iterator:
        text = (piece.text or "").strip()
        if not text:
            continue
        if to_simplified is not None:
            text = to_simplified(text)
        row: dict = {"start": piece.start, "end": piece.end, "text": text}
        words = getattr(piece, "words", None)
        if words:
            row["words"] = [
                {"start": word.start, "end": word.end, "word": word.word}
                for word in words
            ]
        rows.append(row)
        segments.append(
            TranscriptSegment(
                speaker=options.speaker,
                text=text,
                start=seconds_to_timestamp(piece.start),
                end=seconds_to_timestamp(piece.end),
            )
        )

    raw_output = run_dir / "raw_whisper.json"
    raw_output.write_text(
        json.dumps(
            {"language": getattr(info, "language", options.language), "segments": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not segments:
        raise RuntimeError("Whisper returned no text segments.")

    transcript_path = write_transcript_markdown(episode, segments)
    return WhisperResult(
        transcript_path=transcript_path,
        work_dir=run_dir,
        audio_path=audio_path,
        wav_path=wav_path,
        raw_output=raw_output,
        segment_count=len(segments),
    )


def _glossary_prompt() -> str | None:
    """Build an initial_prompt from the shared glossary (best effort)."""
    try:
        from .glossary import default_glossary_paths, hotword_terms, load_glossary

        terms = hotword_terms(load_glossary(default_glossary_paths()), limit=PROMPT_TERM_LIMIT)
    except Exception:
        return None
    if not terms:
        return None
    return "以下是普通话科技播客的逐字稿，可能出现这些专有名词：" + "、".join(terms) + "。"


_SIMPLIFIED_CONVERTER = None


def _simplified_converter():
    """Return a traditional→simplified converter, or None when opencc is missing."""
    global _SIMPLIFIED_CONVERTER
    if _SIMPLIFIED_CONVERTER is not None:
        return _SIMPLIFIED_CONVERTER or None
    try:
        from opencc import OpenCC  # type: ignore

        converter = OpenCC("t2s")
        _SIMPLIFIED_CONVERTER = converter.convert
    except Exception:
        _SIMPLIFIED_CONVERTER = False
        return None
    return _SIMPLIFIED_CONVERTER


def dry_run_report(episode: Episode, options: WhisperOptions | None = None) -> list[str]:
    import shutil

    options = options or WhisperOptions()
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        state = "installed"
    except Exception:
        state = "missing (pip install faster-whisper)"
    opencc_state = "installed" if _simplified_converter() else "missing (pip install opencc-python-reimplemented)"
    prompt = _glossary_prompt()

    return [
        "Backend: whisper (faster-whisper, local, NO diarization)",
        f"Episode: {episode.id}",
        f"Audio URL: {episode.audio_url or '(missing)'}",
        f"Model size: {options.model_size} ({options.compute_type} on {options.device})",
        f"ffmpeg: {shutil.which('ffmpeg') or '(missing)'}",
        f"faster-whisper: {state}",
        f"opencc (繁→简): {opencc_state}",
        f"Glossary prompt terms: {0 if not prompt else prompt.count('、') + 1}",
        "Note: all text is attributed to one speaker; use funasr for diarization.",
    ]
