from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "douyin-to-obsidian" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import topic_taxonomy as taxonomy


class TopicTaxonomyTest(unittest.TestCase):
    def test_examples_use_subject_not_source(self):
        examples = [
            ("douyinknowledge/抖音/AI/Agent记忆/a.md", "Agent 长期记忆的写入前验证", "只读探测与异步策展",
             ["AI", "Agent", "记忆管理", "写入与更新"]),
            ("douyinknowledge/抖音/AI/Agent控制/a.md", "Claude Code 上下文管理：分级压缩", "外部存储与按需恢复",
             ["AI", "Agent", "上下文管理", "压缩与摘要"]),
            ("douyinknowledge/抖音/AI/RAG/a.md", "混合检索的召回分配与融合", "权衡成本与精度",
             ["AI", "RAG", "检索与重排", "向量与混合检索"]),
            ("douyinknowledge/抖音/AI/面试/a.md", "什么是 ReAct？和 CoT 有什么区别", "用真实观测纠正推理方向",
             ["AI", "Agent", "基础与架构", "概念与组成"]),
        ]
        for old, title, insight, expected in examples:
            with self.subTest(title=title):
                category, reason = taxonomy.classify_note(old, title, insight)
                self.assertEqual(category, expected)
                self.assertTrue(reason)
                self.assertEqual(taxonomy.validate_category(category), category)

    def test_source_and_interview_are_not_folders(self):
        for category in (["AI", "Agent", "面试"], ["抖音", "AI"], ["AI", "Agent", "记忆管理", "视频"]):
            with self.subTest(category=category):
                with self.assertRaises(ValueError):
                    taxonomy.validate_category(category)


if __name__ == "__main__":
    unittest.main()
