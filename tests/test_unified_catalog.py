"""The shared view must preserve both sources and survive ordinary rebuilds."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "douyin-to-obsidian" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import unified_catalog as catalog
import unified_store
import library


class UnifiedCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dy = self.root
        (self.root / "知识库" / "AI" / "Agent" / "记忆管理").mkdir(parents=True)
        (self.root / "知识库" / "AI" / "Agent" / "记忆管理" / "existing.md").write_text("# Existing note\n")
        (self.root / "知识库" / "AI" / "Agent" / "记忆管理" / "dy.md").write_text("# DY\n")
        self.sources = {
            self.root / "_knowledge-index.md": """# 知识索引

## AI / Agent / 记忆管理

| 文档 | type | 关键词 | 核心洞察 | 日期 |
| --- | --- | --- | --- | --- |
| [同题](知识库/AI/Agent/记忆管理/existing.md) | 教程 | agent | 已有观点 | 2026-09-25 |
""",
            self.root / "_import-log.md": """# 导入日志

| 时间 | 来源 URL | 写入路径 | 操作 |
| --- | --- | --- | --- |
| 2026-09-25 09:00:00 | https://example.com/existing | [知识库/AI/Agent/记忆管理/existing.md](知识库/AI/Agent/记忆管理/existing.md) | 新建 |
""",
            self.dy / "DY-知识索引.md": """%% douyin:managed:start %%

共 1 篇知识笔记。

## AI / Agent / 记忆管理

| 文章 | 类型 | 关键词 | 核心洞察 | 入库时间 |
| --- | --- | --- | --- | --- |
| [[知识库/AI/Agent/记忆管理/dy\\|同题]] | 知识笔记 | agent | DY 观点 | 2026-09-26 10:00:00 |

%% douyin:managed:end %%
""",
            self.dy / "DY-更新日志.md": """%% douyin:managed:start %%

| 时间 | 操作 | 文章 | 分类 | 来源 URL | 内容复审 | 复审时间 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-26 10:00:00 | 新增 | [[知识库/AI/Agent/记忆管理/dy\\|同题]] | AI / Agent / 记忆管理 | [原作品](https://www.douyin.com/video/1234567890123456) | 未复审 | — |

%% douyin:managed:end %%
""",
        }
        for path, content in self.sources.items():
            path.write_text(content, encoding="utf-8")

    def test_merges_by_topic_and_time_without_deduplicating_similar_notes(self):
        result = catalog.rebuild(self.root)
        self.assertEqual((result["ki_index_count"], result["dy_index_count"], result["log_count"]), (1, 1, 2))
        index = (self.root / catalog.INDEX_NAME).read_text()
        log = (self.root / catalog.LOG_NAME).read_text()
        self.assertNotIn("| 来源 |", index)
        self.assertIn("## AI / Agent / 记忆管理", index)
        self.assertIn("%E7%9F%A5%E8%AF%86%E5%BA%93/AI/Agent/%E8%AE%B0%E5%BF%86%E7%AE%A1%E7%90%86/dy.md", index)
        self.assertIn("| KI |", log)
        self.assertIn("| DY |", log)
        self.assertIn("[DY 索引](DY-知识索引.md)", index)
        self.assertIn("[DY 日志](DY-更新日志.md)", log)
        self.assertLess(log.index("2026-09-26"), log.index("2026-09-25"))
        for path, content in self.sources.items():
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertFalse(catalog.rebuild(self.root)["changed"])

    def test_missing_historical_note_is_kept_without_broken_link(self):
        (self.root / "知识库" / "AI" / "Agent" / "记忆管理" / "existing.md").unlink()
        result = catalog.rebuild(self.root)
        self.assertEqual(result["missing_index_notes"], 1)
        self.assertIn("同题（原笔记缺失）", (self.root / catalog.INDEX_NAME).read_text())

    def test_manual_edit_blocks_overwrite_and_preserves_sources(self):
        catalog.rebuild(self.root)
        target = self.root / catalog.INDEX_NAME
        target.write_text(target.read_text() + "我的修改\n")
        with self.assertRaisesRegex(ValueError, "手工改动"):
            catalog.rebuild(self.root)
        self.assertIn("我的修改", target.read_text())
        for path, content in self.sources.items():
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_obsidian_table_padding_can_be_rebuilt_without_changing_cells(self):
        catalog.rebuild(self.root)
        target = self.root / catalog.LOG_NAME
        formatted = target.read_text().replace("| 时间 | 来源 | 操作 |", "| 时间      | 来源  | 操作 |")
        formatted = formatted.replace("| --- | --- | --- |", "| ---------- | ----- | --- |", 1)
        target.write_text(formatted)
        self.assertTrue(catalog.rebuild(self.root)["changed"])
        self.assertFalse(catalog.rebuild(self.root, check=True)["changed"])

    def test_malformed_source_does_not_replace_existing_view(self):
        catalog.rebuild(self.root)
        index = (self.root / catalog.INDEX_NAME).read_bytes()
        (self.dy / "DY-更新日志.md").write_text("broken\n")
        with self.assertRaisesRegex(ValueError, "管理区域"):
            catalog.rebuild(self.root)
        self.assertEqual((self.root / catalog.INDEX_NAME).read_bytes(), index)

    def test_dy_hook_reports_view_failure_without_losing_save_result(self):
        config = {"vault": str(self.root), "unified_root_vault": str(self.root)}
        save = library.sync_unified_after_write(lambda _config: {"status": "saved", "written": True})
        with patch.object(catalog, "rebuild", side_effect=ValueError("synthetic view failure")):
            result = save(config)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["unified_sync_warning"], "synthetic view failure")

    def test_canonical_mode_checks_totals_without_recreating_legacy_sources(self):
        for path in self.sources:
            path.unlink()
        (self.root / unified_store.INDEX_NAME).write_bytes(unified_store.render_index([]))
        (self.root / unified_store.LOG_NAME).write_bytes(unified_store.render_log([]))
        self.assertEqual(catalog.rebuild(self.root)["status"], "ok")
        self.assertEqual(catalog.rebuild(self.root, check=True)["index_count"], 0)
        self.assertFalse((self.root / catalog.STATE_NAME).exists())
        self.assertTrue(all(not path.exists() for path in self.sources))


class AuthorLogFieldsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        (self.vault / unified_store.INDEX_NAME).write_bytes(unified_store.render_index([]))
        self.old_cells = ["2026-09-25 09:00:00", "KI", "新建", "历史记录", "AI / Agent",
                          "https://example.com/old", "—"]
        legacy = ("# 导入总日志\n\n共 1 条：KI 1 条，DY 0 条。按时间由近到远排列。\n\n"
                  "| " + " | ".join(unified_store.LEGACY_LOG_HEADERS) + " |\n"
                  "| --- | --- | --- | --- | --- | --- | --- |\n"
                  "| " + " | ".join(self.old_cells) + " |\n")
        (self.vault / unified_store.LOG_NAME).write_text(legacy, encoding="utf-8")

    def test_legacy_rows_gain_an_empty_author_without_changing_existing_cells(self):
        _, rows = unified_store.load(self.vault)
        self.assertEqual(rows, [[*self.old_cells, ""]])
        rendered = unified_store.render_log(rows)
        self.assertIn("作者名称".encode(), rendered)
        self.assertEqual(unified_store._read_log(rendered.decode()), rows)

    def test_new_ki_author_is_escaped_and_preserved_while_unknown_stays_blank(self):
        meta = {"title": "新知识", "type": "教程", "tags": ["agent"], "insight": "分层",
                "article_date": "2026-10-04", "author_name": "公众号|甲"}
        relative = "知识库/AI/Agent/新知识.md"
        changes = unified_store.ki_changes(self.vault, meta, relative, "2026-10-04 10:00:00",
                                           "https://mp.weixin.qq.com/s/new", "新建")
        for path, raw in changes.items():
            path.write_bytes(raw)
        meta.pop("author_name")
        changes = unified_store.ki_changes(self.vault, meta, relative, "2026-10-04 11:00:00",
                                           "https://mp.weixin.qq.com/s/unknown", "追加")
        rows = unified_store._read_log(changes[self.vault / unified_store.LOG_NAME].decode())
        authors = {row[5]: row[-1] for row in rows}
        self.assertEqual(authors["https://example.com/old"], "")
        self.assertEqual(authors["https://mp.weixin.qq.com/s/new"], "公众号\\|甲")
        self.assertEqual(authors["https://mp.weixin.qq.com/s/unknown"], "")

    def test_author_id_fallback_and_unverified_names_are_not_log_authors(self):
        sec_uid = "MS4wLjABAAAAVerifiedIdentity12345"
        self.assertEqual(unified_store.verified_author_name({"douyin_author": "猜测作者"}), "")
        self.assertEqual(unified_store.verified_author_name({"douyin_author_id": sec_uid,
                                                            "douyin_author": sec_uid[:12]}), "")
        self.assertEqual(unified_store.verified_author_name({"douyin_author_id": sec_uid,
                                                            "douyin_author": " 核实作者 "}), "核实作者")


if __name__ == "__main__":
    unittest.main()
