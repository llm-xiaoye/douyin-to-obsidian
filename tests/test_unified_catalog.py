"""Regressions for the packaged read-only catalog checker."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/douyin-to-obsidian/scripts"
sys.path.insert(0, str(SCRIPTS))
import unified_catalog as catalog
import unified_store as store


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dy-catalog-")
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name).resolve()
        (self.vault / store.INDEX_NAME).write_bytes(store.render_index([]))
        (self.vault / store.LOG_NAME).write_bytes(store.render_log([]))

    def test_empty_catalogs_are_checked_without_creating_files(self):
        before = {p.name: p.read_bytes() for p in self.vault.iterdir()}
        result = catalog.check(self.vault)
        self.assertEqual((result["status"], result["index_count"], result["log_count"]), ("ok", 0, 0))
        self.assertFalse(result["changed"])
        self.assertEqual({p.name: p.read_bytes() for p in self.vault.iterdir()}, before)

    def test_malformed_log_is_rejected_without_repairing_it(self):
        path = self.vault / store.LOG_NAME
        invalid = path.read_bytes().replace("共 0 条".encode(), "共 1 条".encode())
        path.write_bytes(invalid)
        with self.assertRaises(ValueError):
            catalog.check(self.vault)
        self.assertEqual(path.read_bytes(), invalid)

    def test_missing_note_is_reported(self):
        relative = "知识库/AI/示例.md"
        row = {"path": relative, "cells": [store.note_link("示例", relative), "教程", "示例", "解释", "2026-10-21"]}
        (self.vault / store.INDEX_NAME).write_bytes(store.render_index([row]))
        result = catalog.check(self.vault)
        self.assertEqual((result["index_count"], result["missing_index_notes"]), (1, 1))

    def test_command_uses_the_packaged_catalog_modules(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "unified_catalog.py"),
                                 "check", "--vault", str(self.vault)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")

    def test_log_summary_preserves_history_without_product_breakdowns(self):
        row = ["2026-10-21 10:00:00", "历史来源", "新建",
               store.note_link("旧笔记", "知识库/AI/旧笔记.md"), "AI",
               "https://example.com/article", "—", ""]
        raw = store.render_log([row])
        self.assertIn("共 1 条。按时间由近到远排列。", raw.decode("utf-8"))
        self.assertEqual(store._read_log(raw.decode("utf-8")), [row])

    def test_invalid_catalog_returns_structured_error(self):
        (self.vault / store.LOG_NAME).write_text("invalid", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = catalog.main(["check", "--vault", str(self.vault)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
