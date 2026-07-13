#!/usr/bin/env python3
"""ASR A/B benchmark harness（方案 B 章的落地）。

用真实播客片段对比多个引擎的：中文错字率 CER、英文专名命中率、耗时 RTF。
说话人归属/标点/幻觉仍需人工评，本脚本把每个引擎的原始输出落盘供盲评。

用法：
  1. 准备 manifest.json（示例见文件底部 EXAMPLE_MANIFEST），wav 需 16kHz 单声道。
  2. .venv/bin/python scripts/benchmark_asr.py manifest.json --out bench_out
  3. 查看 bench_out/results.csv 与 bench_out/<engine>/<clip>.txt

内置引擎：
  - funasr            paraformer-zh + fsmn-vad + ct-punc（走本仓库默认模型）
  - whisper[:size]    faster-whisper，如 whisper:large-v3-turbo / whisper:large-v3
外置引擎（qwen3-mlx、sensevoice 等）用 cmd 形式接入：
  {"name": "qwen3-mlx", "cmd": "mlx_qwen3_asr --audio {wav} --quiet"}
  约定：命令把纯文本转写结果打到 stdout。

参照文本：优先用已人工精校的阅读版（去掉说话人行/时间戳后的正文）。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PUNCT_RE = re.compile(r"[\s　-〿＀-￯!-/:-@\[-`{-~·…—“”‘’]+")


# ---------- 文本规范化与指标 ----------

def normalize_text(text: str) -> str:
    """去标点/空白、全角转半角、拉丁小写、繁转简（若装了 opencc）。"""
    text = unicodedata.normalize("NFKC", text)
    text = PUNCT_RE.sub("", text)
    text = text.lower()
    try:
        from opencc import OpenCC  # type: ignore

        text = OpenCC("t2s").convert(text)
    except Exception:
        pass
    return text


def cer(reference: str, hypothesis: str) -> float:
    """字符错误率 = 编辑距离 / 参照长度（规范化后按字符）。"""
    ref = list(normalize_text(reference))
    hyp = list(normalize_text(hypothesis))
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, ref_char in enumerate(ref, start=1):
        current = [i] + [0] * len(hyp)
        for j, hyp_char in enumerate(hyp, start=1):
            cost = 0 if ref_char == hyp_char else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1] / len(ref)


def load_entities(path: Path) -> list[list[str]]:
    """实体表：每行一个实体，竖线分隔可接受变体，如 `Claude Code|ClaudeCode`。"""
    groups: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        groups.append([variant.strip() for variant in line.split("|") if variant.strip()])
    return groups


def entity_recall(reference: str, hypothesis: str, groups: list[list[str]]) -> tuple[int, int, list[str]]:
    """返回 (命中数, 应命中数, 未命中实体)。应命中 = 实体确实出现在参照里。"""
    ref_lower = reference.lower()
    hyp_lower = hypothesis.lower()
    hits, total, missed = 0, 0, []
    for group in groups:
        if not any(variant.lower() in ref_lower for variant in group):
            continue  # 这期没提到，不计
        total += 1
        if any(variant.lower() in hyp_lower for variant in group):
            hits += 1
        else:
            missed.append(group[0])
    return hits, total, missed


def strip_readable_markup(text: str) -> str:
    """把阅读版 md 变成纯正文参照：去说话人行、时间戳、头部元信息。"""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        if re.match(r"^\*\*[^*]+\*\*", stripped):  # 说话人行
            continue
        stripped = re.sub(r"`?\[?\d{2}:\d{2}(:\d{2})?( - \d{2}:\d{2}(:\d{2})?)?\]?`?", "", stripped)
        lines.append(stripped)
    return "\n".join(lines)


def audio_duration_seconds(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def format_secs(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


class Heartbeat:
    """引擎运行期间每隔 interval 秒打一行心跳：能区分「慢」和「卡死」。"""

    def __init__(self, label: str, audio_seconds: float, interval: float = 15.0):
        self.label = label
        self.audio_seconds = audio_seconds
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            elapsed = time.monotonic() - self._started
            ratio = f"，已运行/音频 = {elapsed / self.audio_seconds:.2f}" if self.audio_seconds else ""
            print(
                f"[bench]   {self.label} 已运行 {format_secs(elapsed)}"
                f"（音频 {format_secs(self.audio_seconds)}{ratio}）",
                flush=True,
            )

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


# ---------- 引擎适配 ----------

def run_whisper(wav_path: Path, model_size: str, audio_seconds: float = 0.0) -> str:
    from faster_whisper import WhisperModel  # type: ignore

    from podcast_tracker.asr_whisper import _glossary_prompt

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(wav_path),
        language="zh",
        vad_filter=True,
        initial_prompt=_glossary_prompt(),
        condition_on_previous_text=False,
    )
    pieces: list[str] = []
    last_report = 0.0
    for piece in segments:
        pieces.append(piece.text or "")
        if audio_seconds and piece.end - last_report >= 60:
            last_report = piece.end
            print(
                f"[bench]   whisper 已转写到 {format_secs(piece.end)}/{format_secs(audio_seconds)}"
                f"（{piece.end / audio_seconds:.0%}）",
                flush=True,
            )
    return "".join(pieces)


def run_funasr(wav_path: Path) -> str:
    from funasr import AutoModel  # type: ignore

    from podcast_tracker.glossary import default_glossary_paths, hotword_terms, load_glossary

    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        disable_update=True,
    )
    hotword = " ".join(hotword_terms(load_glossary(default_glossary_paths())))
    result = model.generate(input=str(wav_path), hotword=hotword or None)
    return "".join(item.get("text", "") for item in result)


def run_cmd_engine(wav_path: Path, command_template: str) -> str:
    """跑外置引擎命令；stderr 实时透传（多数引擎把进度打在 stderr）。"""
    command = command_template.format(wav=str(wav_path))
    process = subprocess.Popen(
        command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    stderr_tail: list[str] = []

    def _pump_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            line = line.rstrip()
            if line:
                stderr_tail.append(line)
                del stderr_tail[:-20]
                print(f"[engine] {line}", flush=True)

    pump = threading.Thread(target=_pump_stderr, daemon=True)
    pump.start()
    assert process.stdout is not None
    stdout = process.stdout.read()
    returncode = process.wait(timeout=4 * 3600)
    pump.join(timeout=5)
    if returncode != 0:
        raise RuntimeError(f"engine command failed ({returncode}): {' | '.join(stderr_tail)[-500:]}")
    return stdout.strip()


def run_engine(engine: dict, wav_path: Path, audio_seconds: float = 0.0) -> str:
    kind = engine["name"]
    if engine.get("cmd"):
        return run_cmd_engine(wav_path, engine["cmd"])
    if kind == "funasr":
        return run_funasr(wav_path)
    if kind.startswith("whisper"):
        model_size = kind.split(":", 1)[1] if ":" in kind else "large-v3-turbo"
        return run_whisper(wav_path, model_size, audio_seconds)
    raise ValueError(f"Unknown engine: {kind}")


# ---------- 主流程 ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path, default=Path("bench_out"))
    parser.add_argument("--engines", nargs="*", help="只跑这些引擎（按 name 过滤）")
    parser.add_argument("--clips", nargs="*", help="只跑这些片段（按 id 过滤）")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    base = args.manifest.resolve().parent
    engines = [
        engine if isinstance(engine, dict) else {"name": engine}
        for engine in manifest.get("engines", ["funasr", "whisper:large-v3-turbo"])
    ]
    if args.engines:
        engines = [engine for engine in engines if engine["name"] in set(args.engines)]

    args.out.mkdir(parents=True, exist_ok=True)
    clips = manifest["clips"]
    if args.clips:
        clips = [clip for clip in clips if clip["id"] in set(args.clips)]
    rows = []
    for clip in clips:
        wav_path = (base / clip["wav"]).resolve()
        duration = audio_duration_seconds(wav_path)
        reference_path = (base / clip["reference"]).resolve() if clip.get("reference") else None
        reference_text = ""
        if reference_path:
            reference_text = reference_path.read_text(encoding="utf-8")
            if reference_path.suffix == ".md":
                reference_text = strip_readable_markup(reference_text)
        entity_groups = (
            load_entities((base / clip["entities"]).resolve()) if clip.get("entities") else []
        )

        for engine in engines:
            name = engine["name"]
            print(f"[bench] {clip['id']} × {name} ...（音频 {format_secs(duration)}）", flush=True)
            started = time.monotonic()
            try:
                with Heartbeat(f"{clip['id']} × {name}", duration):
                    hypothesis = run_engine(engine, wav_path, duration)
                error = ""
            except Exception as exc:  # 单引擎失败不拖垮整场
                hypothesis, error = "", str(exc)
            elapsed = time.monotonic() - started
            status = "失败" if error else "完成"
            print(f"[bench]   {status}，用时 {format_secs(elapsed)}", flush=True)

            engine_dir = args.out / re.sub(r"[^\w.-]", "_", name)
            engine_dir.mkdir(parents=True, exist_ok=True)
            (engine_dir / f"{clip['id']}.txt").write_text(hypothesis, encoding="utf-8")

            row = {
                "clip": clip["id"],
                "engine": name,
                "seconds": round(elapsed, 1),
                "rtf": round(elapsed / duration, 3) if duration else "",
                "cer": "",
                "entity_hits": "",
                "entity_total": "",
                "entity_recall": "",
                "missed_entities": "",
                "error": error,
            }
            if reference_text and hypothesis:
                row["cer"] = round(cer(reference_text, hypothesis), 4)
                if entity_groups:
                    hits, total, missed = entity_recall(reference_text, hypothesis, entity_groups)
                    row.update(
                        entity_hits=hits,
                        entity_total=total,
                        entity_recall=round(hits / total, 3) if total else "",
                        missed_entities=" / ".join(missed),
                    )
            rows.append(row)

    results_path = args.out / "results.csv"
    fieldnames = list(rows[0].keys())
    # 与已有结果按 (clip, engine) 合并：单独重跑某个引擎不会冲掉其他引擎的行
    merged: dict[tuple[str, str], dict] = {}
    if results_path.exists():
        with results_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for old in csv.DictReader(handle):
                merged[(old.get("clip", ""), old.get("engine", ""))] = {
                    key: old.get(key, "") for key in fieldnames
                }
    for row in rows:
        merged[(row["clip"], row["engine"])] = row
    with results_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged.values())
    print(f"[bench] done → {results_path}")
    return 0


EXAMPLE_MANIFEST = """
{
  "engines": [
    "funasr",
    "whisper:large-v3-turbo",
    "whisper:large-v3",
    {"name": "qwen3-mlx", "cmd": "mlx_qwen3_asr --audio {wav} --quiet"}
  ],
  "clips": [
    {
      "id": "crossing-agent500",
      "wav": "clips/crossing-agent500-10min.wav",
      "reference": "refs/crossing-agent500.ref.md",
      "entities": "refs/crossing-agent500.entities.txt"
    }
  ]
}
"""


if __name__ == "__main__":
    raise SystemExit(main())
