import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from diagnose_archive_text import inspect_archive


class ArchiveTextDiagnosticTests(unittest.TestCase):
    def test_aggregate_format_clues_exclude_private_content_and_do_not_modify_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_files = {}
            contexts = ["<p>합성 한글 PRIVATE_CONTENT_NEVER_REPORT</p>",
                        "&lt;table&gt;합성 한글&lt;/table&gt;",
                        "%3Cp%3E%EA%B2%B0%EC%9E%AC%3C%2Fp%3E",
                        "\\uacb0\\uc7ac", None]
            for number, context in enumerate(contexts):
                folder = root / "documents" / str(number)
                folder.mkdir(parents=True)
                raw = folder / "database.json"
                raw.write_text(json.dumps({"document": {"context": context, "title": "PRIVATE_TITLE_NEVER_REPORT"}}, ensure_ascii=False), encoding="utf-8")
                html = folder / "document.html"
                html.write_text('<meta charset="utf-8">저장된 결재선 기록 · 브라우저 인쇄', encoding="utf-8")
                original_files[raw] = raw.read_bytes()
                original_files[html] = html.read_bytes()
            result = inspect_archive(str(root))
            counts = result["counts"]
            self.assertEqual(counts["html_valid_utf8"], 5)
            self.assertEqual(counts["html_known_korean_headers"], 5)
            self.assertEqual(counts["context_has_html_tags"], 1)
            self.assertEqual(counts["context_has_escaped_html"], 1)
            self.assertEqual(counts["context_has_percent_escapes"], 1)
            self.assertEqual(counts["context_has_literal_unicode_escapes"], 1)
            self.assertEqual(counts["context_empty_or_null"], 1)
            serialized = json.dumps(result)
            self.assertNotIn("PRIVATE_", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertTrue(all(p.read_bytes() == original for p, original in original_files.items()))


if __name__ == "__main__":
    unittest.main()
