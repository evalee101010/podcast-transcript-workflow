from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_readable.py"
SPEC = importlib.util.spec_from_file_location("verify_readable", SCRIPT_PATH)
verify_readable = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = verify_readable
SPEC.loader.exec_module(verify_readable)


class VerifyReadableTests(unittest.TestCase):
    def test_passes_with_filler_removed(self) -> None:
        original = [
            verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "嗯，我们今天聊 AI。")
        ]
        readable = [verify_readable.Block(1, "主持人", "00:00", "我们今天聊 AI。")]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertTrue(ok, errors)

    def test_ignores_deleted_pure_backchannel_block(self) -> None:
        original = [
            verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "开始介绍。"),
            verify_readable.Block(2, "嘉宾", "[00:00:02 - 00:00:03]", "嗯，"),
            verify_readable.Block(3, "主持人", "[00:00:03 - 00:00:04]", "继续。"),
        ]
        readable = [
            verify_readable.Block(1, "主持人", "00:00", "开始介绍。"),
            verify_readable.Block(2, "主持人", "00:03", "继续。"),
        ]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertTrue(ok, errors)

    def test_fails_on_speaker_order_mismatch(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "我们开始。")]
        readable = [verify_readable.Block(1, "嘉宾", "00:00", "我们开始。")]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertFalse(ok)
        self.assertIn("Speaker mismatch", "\n".join(errors))

    def test_fails_when_retention_is_too_low(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "今天讨论 AI 投资策略。")]
        readable = [verify_readable.Block(1, "主持人", "00:00", "今天讨论 AI。")]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertFalse(ok)
        self.assertIn("retention", "\n".join(errors))

    def test_english_term_corrections_do_not_lower_chinese_retention(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "你最近用 clob code 多吗？")]
        readable = [verify_readable.Block(1, "主持人", "00:00", "你最近用 Claude Code 多吗？")]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertTrue(ok, errors)

    def test_chinese_number_version_normalization_does_not_lower_retention(self) -> None:
        original = [verify_readable.Block(1, "嘉宾", "[00:00:00 - 00:00:02]", "我是在四点六之前用这个最多。")]
        readable = [verify_readable.Block(1, "嘉宾", "00:00", "我是在 4.6 之前用这个最多。")]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertTrue(ok, errors)

    def test_fails_on_suspicious_english_mixed_capitalization(self) -> None:
        original = [
            verify_readable.Block(
                1,
                "主持人",
                "[00:00:00 - 00:00:02]",
                "OpenAI 在 customer experience 和 one person company 上有变化。",
            )
        ]
        readable = [
            verify_readable.Block(
                1,
                "主持人",
                "00:00",
                "OPEnAI 在 customer exPErience 和 one PErson company 上有变化。",
            )
        ]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertFalse(ok)
        error_text = "\n".join(errors)
        self.assertIn("OPEnAI", error_text)
        self.assertIn("exPErience", error_text)
        self.assertIn("PErson", error_text)

    def test_allows_acronyms_and_known_mixed_case_terms(self) -> None:
        original = [
            verify_readable.Block(
                1,
                "嘉宾",
                "[00:00:00 - 00:00:02]",
                "FDE、FDPM、PE、API、LLMs、SaaS 和 OpenAI 都是这里的术语。",
            )
        ]
        readable = [
            verify_readable.Block(
                1,
                "嘉宾",
                "00:00",
                "FDE、FDPM、PE、API、LLMs、SaaS 和 OpenAI 都是这里的术语。",
            )
        ]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertTrue(ok, errors)

    def test_qna_split_log_tolerates_logged_split(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:05]", "请问您的年龄三十七岁。")]
        readable = [
            verify_readable.Block(1, "主持人", "00:00", "请问您的年龄。"),
            verify_readable.Block(2, "课代表", "00:03", "三十七岁。"),
        ]

        ok, errors, _ = verify_readable.verify(original, readable, qna_splits={1: [1, 2]})

        self.assertTrue(ok, errors)

    def test_qna_split_log_tolerates_grouped_original_blocks(self) -> None:
        original = [
            verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:03]", "毕业院校是康奈尔的经济学博士。"),
            verify_readable.Block(2, "课代表", "[00:00:03 - 00:00:06]", "本科是一个叫 Lake Forest College 的不知名文理学院。"),
            verify_readable.Block(3, "主持人", "[00:00:06 - 00:00:08]", "你的 MBTI 和星座呢？"),
        ]
        readable = [
            verify_readable.Block(1, "主持人", "00:00", "毕业院校是？"),
            verify_readable.Block(2, "课代表", "00:01", "康奈尔的经济学博士。本科是一个叫 Lake Forest College 的不知名文理学院。"),
            verify_readable.Block(3, "主持人", "00:06", "你的 MBTI 和星座呢？"),
        ]

        ok, errors, _ = verify_readable.verify(
            original,
            readable,
            qna_splits={1: ([1, 2], [1, 2])},
        )

        self.assertTrue(ok, errors)

    def _results_for(self, original, readable, **kwargs):
        ok, errors, results = verify_readable.verify(original, readable, **kwargs)
        return results

    def test_residual_filler_left_in_readable_warns(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "嗯，服务器崩了。")]
        readable = [verify_readable.Block(1, "主持人", "00:00", "服务器就崩了嗯，会员注册。")]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertTrue(any("residual filler" in w for w in warnings), warnings)

    def test_single_particle_is_not_flagged_as_filler(self) -> None:
        # Single 啊/哦 are legitimate particles and must not be flagged.
        self.assertEqual(verify_readable.residual_filler_hits("这样很好啊，对哦。"), [])
        self.assertTrue(verify_readable.residual_filler_hits("啊啊也很厉害啊。"))

    def test_editor_note_in_body_warns(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "这里说的是某个产品。")]
        readable = [verify_readable.Block(1, "主持人", "00:00", "这里说的是某个产品（听不清）。")]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertTrue(any("editor note" in w for w in warnings), warnings)

    def test_uncertainty_density_warns(self) -> None:
        text = "这是一个" + "词[?]" * 5 + "。"
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "这是一个词词词词词。")]
        readable = [verify_readable.Block(1, "主持人", "00:00", text)]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertTrue(any("[?] density" in w for w in warnings), warnings)

    def test_gibberish_signals_detect_garbled_text(self) -> None:
        self.assertTrue(verify_readable.gibberish_signals("但社交网是人联联联合创始。"))  # char×3
        self.assertTrue(verify_readable.gibberish_signals("阿里巴 a 是 bo 国 c 集团 d。"))  # stray latin
        self.assertEqual(verify_readable.gibberish_signals("这是一句正常的话，没有任何问题。"), [])

    def test_orphan_speaker_warns(self) -> None:
        original = [
            verify_readable.Block(1, "刘飞", "[00:00:00 - 00:00:02]", "我们开始吧。"),
            verify_readable.Block(2, "说话人 9", "[00:00:02 - 00:00:05]", "青春如同奔流的江河。"),
            verify_readable.Block(3, "刘飞", "[00:00:05 - 00:00:08]", "好，继续讲。"),
        ]
        readable = [
            verify_readable.Block(1, "刘飞", "00:00", "我们开始吧。"),
            verify_readable.Block(2, "说话人 9", "00:02", "青春如同奔流的江河。"),
            verify_readable.Block(3, "刘飞", "00:05", "好，继续讲。"),
        ]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertTrue(any("说话人 9" in w and "orphan" in w for w in warnings), warnings)

    def test_reverse_retention_flags_added_content(self) -> None:
        original = [verify_readable.Block(1, "嘉宾", "[00:00:00 - 00:00:05]", "我们今天聊视频网站。")]
        readable = [
            verify_readable.Block(
                1,
                "嘉宾",
                "00:00",
                "我们今天聊视频网站，顺便补充一段原文里完全没有出现过的全新内容凑字数。",
            )
        ]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertTrue(any("reverse retention" in w for w in warnings), warnings)

    def test_missing_and_bad_timestamp_warn(self) -> None:
        original = [
            verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "第一段内容在这里。"),
            verify_readable.Block(2, "嘉宾", "[00:00:02 - 00:00:05]", "第二段内容也在这里。"),
        ]
        readable = [
            verify_readable.Block(1, "主持人", "", "第一段内容在这里。"),
            verify_readable.Block(2, "嘉宾", "0:0", "第二段内容也在这里。"),
        ]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertTrue(any("missing timestamp" in w for w in warnings), warnings)
        self.assertTrue(any("bad timestamp" in w for w in warnings), warnings)

    def test_clean_readable_produces_no_warnings(self) -> None:
        original = [
            verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:03]", "嗯，我们今天聊视频网站的历史。"),
            verify_readable.Block(2, "嘉宾", "[00:00:03 - 00:00:06]", "好的，这个话题很有意思。"),
        ]
        readable = [
            verify_readable.Block(1, "主持人", "00:00", "我们今天聊视频网站的历史。"),
            verify_readable.Block(2, "嘉宾", "00:03", "好的，这个话题很有意思。"),
        ]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)

        self.assertEqual(warnings, [])

    def test_expanded_backchannel_block_is_ignored(self) -> None:
        original = [
            verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:02]", "开始介绍。"),
            verify_readable.Block(2, "嘉宾", "[00:00:02 - 00:00:03]", "啊啊，"),
            verify_readable.Block(3, "主持人", "[00:00:03 - 00:00:04]", "继续。"),
        ]
        readable = [
            verify_readable.Block(1, "主持人", "00:00", "开始介绍。"),
            verify_readable.Block(2, "主持人", "00:03", "继续。"),
        ]

        ok, errors, _ = verify_readable.verify(original, readable)

        self.assertTrue(ok, errors)

    def test_strict_mode_fails_on_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = Path(tmpdir) / "original.md"
            readable_path = Path(tmpdir) / "readable.md"
            original_path.write_text(
                "# 标题\n\n## 逐字稿\n\n### 主持人\n\n[00:00:00 - 00:00:02]\n\n嗯，欢迎大家。\n",
                encoding="utf-8",
            )
            # Keeps a residual 呃 => warning, but fidelity still passes.
            readable_path.write_text(
                "# 标题\n\n**主持人**  `00:00`\n呃欢迎大家。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                lenient = verify_readable.main([str(original_path), str(readable_path)])
                strict = verify_readable.main([str(original_path), str(readable_path), "--strict"])

        self.assertEqual(lenient, 0)
        self.assertEqual(strict, 1)

    def test_known_glossary_error_warns_and_blocks(self) -> None:
        original = [verify_readable.Block(1, "主持人", "[00:00:00 - 00:00:03]", "我们在用 Claude Code。")]
        readable = [verify_readable.Block(1, "主持人", "00:00", "我们在用 cloudcode。")]
        glossary = ({"Claude Code"}, {"cloudcode": "Claude Code"})

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results, glossary=glossary)

        known = [w for w in warnings if "known ASR error" in w]
        self.assertTrue(known, warnings)
        self.assertIn("cloudcode", known[0])
        self.assertIn("Claude Code", known[0])
        # blocking under --strict (not an advisory prefix)
        self.assertFalse(known[0].startswith(verify_readable.ADVISORY_PREFIXES))

    def test_unknown_english_token_is_advisory(self) -> None:
        original = [verify_readable.Block(1, "嘉宾", "[00:00:00 - 00:00:03]", "他们发布了新模型。")]
        readable = [verify_readable.Block(1, "嘉宾", "00:00", "他们发布了 mistholes 这个新模型。")]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results, glossary=(set(), {}))

        unknown = [w for w in warnings if w.startswith("unknown English tokens")]
        self.assertTrue(unknown, warnings)
        self.assertIn("mistholes", unknown[0])
        self.assertTrue(unknown[0].startswith(verify_readable.ADVISORY_PREFIXES))

    def test_near_duplicate_variants_detected(self) -> None:
        original = [verify_readable.Block(1, "嘉宾", "[00:00:00 - 00:00:05]", "关于额度的三种说法。")]
        readable = [
            verify_readable.Block(
                1, "嘉宾", "00:00", "他说 tokengrant，又说 tokengrand，还有 tokenground。"
            )
        ]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results, glossary=(set(), {}))

        variants = [w for w in warnings if w.startswith("inconsistent spelling variants")]
        self.assertTrue(variants, warnings)

    def test_canonical_terms_not_flagged_as_unknown(self) -> None:
        original = [verify_readable.Block(1, "嘉宾", "[00:00:00 - 00:00:03]", "我们看 SWE-bench 和 agentic 能力。")]
        readable = [verify_readable.Block(1, "嘉宾", "00:00", "我们看 SWE-bench 和 agentic 能力。")]
        glossary = ({"SWE-bench", "agentic"}, {})

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results, glossary=glossary)

        self.assertFalse([w for w in warnings if w.startswith("unknown English tokens")], warnings)

    def test_glossary_none_skips_glossary_checks(self) -> None:
        original = [verify_readable.Block(1, "嘉宾", "[00:00:00 - 00:00:03]", "他们发布了 mistholes。")]
        readable = [verify_readable.Block(1, "嘉宾", "00:00", "他们发布了 mistholes。")]

        results = self._results_for(original, readable)
        warnings = verify_readable.collect_warnings(original, readable, results)  # no glossary

        self.assertFalse([w for w in warnings if "English token" in w], warnings)

    def test_load_glossary_merges_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            g1 = Path(tmpdir) / "a.json"
            g2 = Path(tmpdir) / "b.json"
            g1.write_text(json.dumps({"canonical": ["Claude Code"], "corrections": {"cloudcode": "Claude Code"}}), encoding="utf-8")
            g2.write_text(json.dumps({"canonical": ["UI/UX"], "corrections": {"URUX": "UI/UX"}}), encoding="utf-8")
            canonical, corrections = verify_readable.load_glossary([g1, g2, None])
        self.assertEqual(canonical, {"Claude Code", "UI/UX"})
        self.assertEqual(corrections, {"cloudcode": "Claude Code", "URUX": "UI/UX"})

    def test_cli_emits_glossary_candidates_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = Path(tmpdir) / "original.md"
            readable_path = Path(tmpdir) / "readable.md"
            candidates_path = Path(tmpdir) / "candidates.json"
            original_path.write_text(
                "# 标题\n\n## 逐字稿\n\n### 主持人\n\n[00:00:00 - 00:00:02]\n\n"
                "我们聊 zzqterm，还有 tokengrant、tokengrand 和 tokenground。\n",
                encoding="utf-8",
            )
            readable_path.write_text(
                "# 标题\n\n**主持人**  `00:00`\n"
                "我们聊 zzqterm，还有 tokengrant、tokengrand 和 tokenground。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                code = verify_readable.main(
                    [
                        str(original_path),
                        str(readable_path),
                        "--emit-glossary-candidates",
                        str(candidates_path),
                    ]
                )

            data = json.loads(candidates_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertIn("zzqterm", {row["token"] for row in data["unknown_tokens"]})
            self.assertTrue(
                any({"tokengrant", "tokengrand"} <= set(cluster) for cluster in data["variant_clusters"]),
                data["variant_clusters"],
            )

    def test_cli_parses_markdown_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = Path(tmpdir) / "original.md"
            readable_path = Path(tmpdir) / "readable.md"
            original_path.write_text(
                "# 标题\n\n## 逐字稿\n\n### 主持人\n\n[00:00:00 - 00:00:02]\n\n嗯，欢迎大家。\n",
                encoding="utf-8",
            )
            readable_path.write_text(
                "# 标题\n\n**主持人**  `00:00`\n欢迎大家。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                code = verify_readable.main([str(original_path), str(readable_path)])

        self.assertEqual(code, 0)

    def test_cli_accepts_speaker_map_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = Path(tmpdir) / "original.md"
            readable_path = Path(tmpdir) / "readable.md"
            speaker_map_path = Path(tmpdir) / "readable.speaker-map.json"
            original_path.write_text(
                "# 标题\n\n## 逐字稿\n\n### 说话人 1\n\n[00:00:00 - 00:00:02]\n\n我是主持人A。\n",
                encoding="utf-8",
            )
            readable_path.write_text(
                "# 标题\n\n**主持人A**  `00:00`\n我是主持人A。\n",
                encoding="utf-8",
            )
            speaker_map_path.write_text(json.dumps({"说话人 1": "主持人A"}, ensure_ascii=False), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                code = verify_readable.main(
                    [
                        str(original_path),
                        str(readable_path),
                        "--speaker-map-file",
                        str(speaker_map_path),
                    ]
                )

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
