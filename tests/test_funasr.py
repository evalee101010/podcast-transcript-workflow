import unittest
from pathlib import Path
import sys
import tempfile
import types
from unittest import mock

from podcast_tracker.asr_funasr import (
    _extract_sentence_info,
    _find_source_audio,
    _generate_with_optional_hotword,
    _latest_reusable_run_dir,
    _prepare_hotword_file,
    _prepare_jieba_runtime,
    FunasrOptions,
    segments_from_sentence_info,
)
from podcast_tracker.audio_utils import ms_to_timestamp, seconds_to_timestamp


class FunasrSegmentTests(unittest.TestCase):
    def test_sentence_info_maps_speaker_text_and_timestamps(self) -> None:
        sentence_info = [
            {"spk": 0, "start": 1250, "end": 3500, "text": "嗯，我们先说第一点。"},
            {"spk": 1, "start": 4000, "end": 8000, "text": "好的，我补充一个细节。"},
        ]
        segments = segments_from_sentence_info(sentence_info)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].speaker, "说话人 1")
        self.assertEqual(segments[0].start, "00:00:01.250")
        self.assertEqual(segments[0].end, "00:00:03.500")
        self.assertEqual(segments[0].text, "嗯，我们先说第一点。")
        self.assertEqual(segments[1].speaker, "说话人 2")

    def test_speaker_names_override_labels(self) -> None:
        sentence_info = [
            {"spk": 0, "start": 0, "end": 1000, "text": "你好。"},
            {"spk": 1, "start": 1000, "end": 2000, "text": "你好你好。"},
        ]
        segments = segments_from_sentence_info(sentence_info, ("主持人", "嘉宾"))
        self.assertEqual(segments[0].speaker, "主持人")
        self.assertEqual(segments[1].speaker, "嘉宾")

    def test_empty_text_rows_are_dropped(self) -> None:
        sentence_info = [
            {"spk": 0, "start": 0, "end": 10, "text": "   "},
            {"spk": 0, "start": 10, "end": 20, "text": "有内容"},
        ]
        segments = segments_from_sentence_info(sentence_info)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "有内容")

    def test_extract_sentence_info_from_payload(self) -> None:
        payload = {"text": "全文", "sentence_info": [{"spk": 0, "text": "a"}]}
        self.assertEqual(_extract_sentence_info(payload), [{"spk": 0, "text": "a"}])
        self.assertEqual(_extract_sentence_info({"text": "x"}), [])

    def test_timestamp_helpers(self) -> None:
        self.assertEqual(ms_to_timestamp(61250), "00:01:01.250")
        self.assertEqual(seconds_to_timestamp(61.25), "00:01:01.250")
        self.assertIsNone(ms_to_timestamp(None))

    def test_reusable_run_dir_detects_partial_audio_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            run_dir = work_dir / "ep1" / "20260623T000000Z"
            run_dir.mkdir(parents=True)
            source = run_dir / "source.m4a"
            wav = run_dir / "audio_16k_mono.wav"
            source.write_bytes(b"audio")
            wav.write_bytes(b"wav")

            self.assertEqual(_find_source_audio(run_dir), source)
            self.assertEqual(_latest_reusable_run_dir(work_dir, "ep1"), run_dir)

    def test_hotword_file_is_built_from_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            glossary = run_dir / "glossary.json"
            glossary.write_text(
                '{"canonical": ["Claude Code", "Anthropic", "纯中文"], "corrections": {"majourney": "Midjourney"}}',
                encoding="utf-8",
            )
            options = FunasrOptions(glossary_paths=(glossary,))

            hotword_path = _prepare_hotword_file(run_dir, options)

            self.assertIsNotNone(hotword_path)
            text = hotword_path.read_text(encoding="utf-8")
            self.assertIn("Claude Code", text)
            self.assertIn("Anthropic", text)
            self.assertIn("Midjourney", text)
            self.assertNotIn("纯中文", text)

    def test_hotword_file_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(_prepare_hotword_file(Path(tmpdir), FunasrOptions(use_hotwords=False)))

    def test_prepare_jieba_runtime_sets_stable_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            fake_jieba = types.SimpleNamespace(
                dt=types.SimpleNamespace(tmp_dir=None, initialized=False),
                initialize=lambda: calls.append("initialize"),
            )

            with mock.patch.dict(sys.modules, {"jieba": fake_jieba}):
                cache_dir = _prepare_jieba_runtime(Path(tmpdir) / "jieba-cache")

            self.assertEqual(cache_dir, Path(tmpdir) / "jieba-cache")
            self.assertEqual(fake_jieba.dt.tmp_dir, str(cache_dir))
            self.assertEqual(calls, ["initialize"])

    def test_generate_falls_back_when_hotword_arg_is_unsupported(self) -> None:
        class FakeModel:
            def __init__(self) -> None:
                self.calls = []

            def generate(self, **kwargs):
                self.calls.append(kwargs)
                if "hotword" in kwargs:
                    raise TypeError("unexpected keyword argument 'hotword'")
                return [{"text": "ok"}]

        model = FakeModel()
        result = _generate_with_optional_hotword(model, Path("audio.wav"), 300, Path("hotwords.txt"))

        self.assertEqual(result, [{"text": "ok"}])
        self.assertIn("hotword", model.calls[0])
        self.assertNotIn("hotword", model.calls[1])


if __name__ == "__main__":
    unittest.main()
