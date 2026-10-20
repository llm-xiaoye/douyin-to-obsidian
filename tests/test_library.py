import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import library
import image_posts
import unified_store

A = '7659111274168454401'
B = '7659111274168454402'
URL_A = f'https://www.douyin.com/video/{A}'
ANALYSIS = {'title': '记忆的状态与历史', 'category': ['AI', 'Agent记忆'], 'tags': ['agent', 'memory'], 'insight': '分开状态与历史证据'}

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'dy'
        self.vault.mkdir()
        self.config = {'vault': str(self.vault), 'cache_dir': str(self.root/'cache'), 'notes_dir': '抖音'}
        self.ki = self.root / '_knowledge-index.md'
        self.ki.write_text('Unrelated KI index')

    def save(self, config, vid, analysis, body):
        return library.save_note(config, vid, analysis, body,
                                 source_url=f'https://www.douyin.com/video/{vid}')

    def test_titles_categories_metadata_index_and_dedup(self):
        r = self.save(self.config, A, ANALYSIS, '## 原理\n具体知识。\n## 回听定位\n00:00\n## 来源\nhttps://example.com\n')
        note = Path(r['note'])
        self.assertEqual(note.relative_to(self.vault).as_posix(), '抖音/AI/Agent记忆/记忆的状态与历史.md')
        front, body = library.read_note(note)
        self.assertEqual(front['title'], ANALYSIS['title'])
        self.assertFalse((library.REMOVED_FIELDS - {'source_url'}).intersection(front))
        self.assertEqual(front['source_url'], URL_A)
        self.assertNotIn('## 来源', body)
        self.assertNotIn('00:00', body)
        self.assertIn('记忆的状态与历史', (self.vault/'知识索引.md').read_text())
        self.assertIn(f'[原作品]({URL_A})', (self.vault/'更新日志.md').read_text())
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob('*.md')}
        self.assertEqual(self.save(self.config, A, ANALYSIS, 'Replacement')['status'], 'exists')
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob('*.md')})
        self.assertEqual(self.ki.read_text(), 'Unrelated KI index')
        second = self.save(self.config, B, ANALYSIS, 'Another work with the same topic.')
        self.assertTrue(second['note'].endswith('记忆的状态与历史-2.md'))

    def test_shared_topic_tree_keeps_dy_metadata_at_vault_root(self):
        config = dict(self.config, notes_dir='知识库',
                      index_file='DY-知识索引.md',
                      log_file='DY-更新日志.md',
                      review_state_file='.dy-review-state.json',
                      attachments_dir='附件')
        analysis = dict(ANALYSIS, category=['AI', 'Agent', '记忆管理', '体系与分层'])
        result = self.save(config, A, analysis, '状态与历史属于不同的记忆层。')
        note = Path(result['note'])
        self.assertEqual(note.relative_to(self.vault).as_posix(),
                         '知识库/AI/Agent/记忆管理/体系与分层/记忆的状态与历史.md')
        self.assertTrue((self.vault/'DY-知识索引.md').is_file())
        self.assertTrue((self.vault/'DY-更新日志.md').is_file())
        self.assertTrue((self.vault/'.dy-review-state.json').is_file())
        self.assertIn('## AI / Agent / 记忆管理 / 体系与分层',
                      (self.vault/'DY-知识索引.md').read_text())
        self.assertIn('| AI / Agent / 记忆管理 / 体系与分层 |',
                      (self.vault/'DY-更新日志.md').read_text())
        self.assertEqual(self.save(config, A, analysis, '不同正文')['status'], 'exists')
        self.assertEqual((self.vault/'DY-更新日志.md').read_text().count('| 新增 |'), 1)

    def test_unified_only_updates_totals_without_legacy_catalogs(self):
        (self.vault / unified_store.INDEX_NAME).write_bytes(unified_store.render_index([]))
        (self.vault / unified_store.LOG_NAME).write_bytes(unified_store.render_log([]))
        config = dict(self.config, notes_dir='知识库', unified_only=True,
                      review_state_file='.dy-review-state.json')
        analysis = dict(ANALYSIS, category=['AI', 'Agent', '记忆管理', '体系与分层'])
        result = self.save(config, A, analysis, '状态与历史属于不同的记忆层。')
        self.assertEqual(Path(result['index']).name, unified_store.INDEX_NAME)
        self.assertEqual(Path(result['log']).name, unified_store.LOG_NAME)
        index, log = unified_store.load(self.vault)
        self.assertEqual((len(index), len(log)), (1, 1))
        self.assertIn(URL_A, log[0][5])
        self.assertEqual(self.save(config, A, analysis, '重复')['status'], 'exists')
        self.assertEqual(len(unified_store.load(self.vault)[1]), 1)
        note = Path(result['note'])
        library.revise_note(config, A, analysis, '更深入的正文。',
                            hashlib.sha256(note.read_bytes()).hexdigest(), source_url=URL_A)
        self.assertEqual(len(unified_store.load(self.vault)[1]), 2)
        moved = library.migrate(config, {A: dict(analysis, title='记忆体系新标题')})
        new_relative = moved['notes'][0]
        index, log = unified_store.load(self.vault)
        self.assertEqual((len(index), len(log), index[0]['path']), (1, 3, new_relative))
        self.assertTrue(all(unified_store.link_path(row[3]) == new_relative for row in log))
        from review_log import CHECKS
        review = {'outcome': '复审通过', 'summary': '已逐项核对正文与原材料。',
                  'checks': {key: {'result': '不适用' if key == 'images' else '通过',
                                   'details': '逐项核对的具体依据。'} for key in CHECKS}, 'issues': []}
        new_note = self.vault / new_relative
        library.record_review(config, A, review, hashlib.sha256(new_note.read_bytes()).hexdigest(),
                              '1' * 64, URL_A)
        _, log = unified_store.load(self.vault)
        self.assertEqual(len(log), 4)
        self.assertTrue(all('复审通过' in row[6] for row in log))
        self.assertTrue(all(not (self.vault / name).exists() for name in
                            ('DY-知识索引.md', 'DY-更新日志.md')))

    def test_unified_only_failed_index_write_rolls_back(self):
        index = self.vault / unified_store.INDEX_NAME
        log = self.vault / unified_store.LOG_NAME
        index.write_bytes(unified_store.render_index([]))
        log.write_bytes(unified_store.render_log([]))
        before = (index.read_bytes(), log.read_bytes())
        config = dict(self.config, notes_dir='知识库', unified_only=True)
        analysis = dict(ANALYSIS, category=['AI', 'Agent', '记忆管理'])
        original = library.atomic_write
        def fail_index(path, content, exclusive=False):
            if path == index:
                raise OSError('synthetic index failure')
            return original(path, content, exclusive)
        with patch.object(library, 'atomic_write', side_effect=fail_index):
            with self.assertRaises(OSError):
                self.save(config, A, analysis, '正文')
        self.assertEqual((index.read_bytes(), log.read_bytes()), before)
        self.assertFalse(library.find_existing(self.vault, A))
        self.assertFalse((self.vault / '.dy-transaction.json').exists())

    def test_source_url_is_required_and_must_match_work_id(self):
        with self.assertRaisesRegex(ValueError, '原作品链接'):
            library.save_note(self.config, A, ANALYSIS, '内容')
        with self.assertRaises(ValueError):
            library.save_note(self.config, A, ANALYSIS, '内容',
                              source_url=f'https://www.douyin.com/video/{B}')
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_revision_preserves_original_source_url(self):
        note = Path(self.save(self.config, A, ANALYSIS, '原知识正文。')['note'])
        before = hashlib.sha256(note.read_bytes()).hexdigest()
        library.revise_note(self.config, A, ANALYSIS, '深化后的知识正文。', before,
                            source_url=URL_A)
        front, body = library.read_note(note)
        self.assertEqual(front['source_url'], URL_A)
        self.assertIn('深化后的知识正文', body)

    def test_migration_preserves_personal_content_and_repairs_links(self):
        old = self.vault/'抖音'/'视频'/f'{A}.md'
        old.parent.mkdir(parents=True)
        front = dict(ANALYSIS, aweme_id=A, created='2020-01-01', **{k:'old' for k in library.REMOVED_FIELDS})
        front['source_url'] = URL_A
        old.write_text(library.render_note(front, '# 旧标题\n\n## 知识\n不能丢失。\n## 回听定位\n00:00\n### 旧子节\n片段\n## 来源\n链接\n## 我的补充\n个人判断。\n### 来源\n我自己的补充依据。\n'))
        relation = self.vault/'我的关联.md'
        relation.write_text(f'相关 [[{A}|旧名字]]。')
        result = library.migrate(self.config, {A: ANALYSIS})
        new = self.vault/result['notes'][0]
        self.assertFalse(old.exists())
        f, b = library.read_note(new)
        self.assertEqual(f['created'], '2020-01-01')
        self.assertFalse((library.REMOVED_FIELDS - {'source_url'}).intersection(f))
        self.assertEqual(f['source_url'], URL_A)
        self.assertIn('个人判断。\n### 来源\n我自己的补充依据。', b)
        self.assertNotIn('00:00', b)
        self.assertIn(new.relative_to(self.vault).with_suffix('').as_posix(), relation.read_text())
        saved = json.loads((Path(result['backup'])/'before.json').read_text())
        self.assertIn('00:00', base64.b64decode(saved['抖音/视频/'+A+'.md']).decode())
        self.assertEqual(self.ki.read_text(), 'Unrelated KI index')

    def test_failed_index_write_rolls_back_note_and_metadata(self):
        self.save(self.config, A, ANALYSIS, 'Existing content')
        snapshot = {p: p.read_bytes() for p in self.vault.rglob('*.md')}
        original = library.atomic_write
        failed = False
        def write(path, content, exclusive=False):
            nonlocal failed
            if path.name == '知识索引.md' and not failed:
                failed = True
                raise OSError('Synthetic disk write failure')
            return original(path, content, exclusive)
        with patch.object(library, 'atomic_write', side_effect=write):
            with self.assertRaises(OSError):
                self.save(self.config, B, ANALYSIS, 'New content')
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.vault.rglob('*.md')})
        self.assertFalse((self.vault/'.dy-transaction.json').exists())

    def test_personal_index_text_survives_new_import(self):
        self.save(self.config, A, ANALYSIS, 'Content')
        index = self.vault/'知识索引.md'
        index.write_text(index.read_text() + '\n我自己的导航。\n')
        self.save(self.config, B, ANALYSIS, 'Content')
        self.assertTrue(index.read_text().endswith('我自己的导航。\n'))

    def test_later_migration_repairs_historical_table_links(self):
        self.save(self.config, A, ANALYSIS, 'Content')
        result = library.migrate(self.config, {A: dict(ANALYSIS, title='记忆的分层设计')})
        new_path = result['notes'][0][:-3]
        log = (self.vault/'更新日志.md').read_text()
        self.assertEqual(log.count('[[' + new_path + '\\|'), 2)
        self.assertNotIn('[[抖音/AI/Agent记忆/记忆的状态与历史', log)

    def test_invalid_categories_are_rejected_before_writing(self):
        for category in [['AI'], ['AI','../escape'], ['AI','/tmp'], ['AI','.private']]:
            with self.assertRaises(ValueError):
                self.save(self.config, A, dict(ANALYSIS, category=category), 'Content')
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_refresh_legacy_layout_preserves_history_and_articles(self):
        from markdown_it import MarkdownIt
        result = self.save(self.config, A, ANALYSIS, '个人知识内容。')
        note = Path(result['note'])
        note_before = note.read_bytes(), note.stat().st_mtime_ns
        # Reproduce the original metadata format, with no gap before the table.
        log = self.vault/'更新日志.md'
        _, history, _ = library.managed_parts(log.read_text(), '抖音更新日志')
        log.write_text('# 抖音更新日志\n\n' + library.LEGACY_START + '\n' + history.strip() + '\n' + library.LEGACY_END + '\n\n我的私人补充。\n')
        result = library.refresh_metadata(self.config)
        text = log.read_text()
        self.assertEqual(library.managed_parts(text, '抖音更新日志')[1].strip(), history.strip())
        self.assertTrue(text.endswith('我的私人补充。\n'))
        tokens = MarkdownIt('commonmark').enable('table').parse(text)
        self.assertEqual(sum(t.type == 'table_open' for t in tokens), 1)
        self.assertEqual(sum(t.type == 'th_open' for t in tokens), 7)
        self.assertEqual(sum(t.type == 'td_open' for t in tokens), 7)
        self.assertFalse(any(t.type == 'heading_open' and t.tag == 'h1' for t in tokens))
        self.assertEqual((note.read_bytes(), note.stat().st_mtime_ns), note_before)
        self.assertTrue((Path(result['backup'])/'before.json').is_file())
        snapshot = log.read_bytes(), log.stat().st_mtime_ns
        self.assertEqual(library.refresh_metadata(self.config)['changed_files'], [])
        self.assertEqual((log.read_bytes(), log.stat().st_mtime_ns), snapshot)
        self.save(self.config, B, ANALYSIS, '新增知识。')
        new_tokens = MarkdownIt('commonmark').enable('table').parse(log.read_text())
        self.assertEqual(sum(t.type == 'table_open' for t in new_tokens), 1)
        self.assertEqual(sum(t.type == 'td_open' for t in new_tokens), 14)
        self.assertTrue(log.read_text().endswith('我的私人补充。\n'))

class ImageEvidenceTests(unittest.TestCase):
    def test_partial_or_changed_pages_are_rejected(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            f = cache/'1.png'
            f.write_bytes(b'fixture image')
            entry = {'page':1,'file':'1.png','sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
            bundle = {'aweme_id':A,'image_count':1,'images':[entry]}
            valid = {'aweme_id':A,'pages':[dict(entry, text='知识证据')]}
            image_posts.validate_evidence(valid,bundle,cache)
            with self.assertRaises(image_posts.StageError):
                image_posts.validate_evidence(dict(valid,pages=[]),bundle,cache)
            f.write_bytes(b'changed')
            with self.assertRaises(image_posts.StageError):
                image_posts.validate_evidence(valid,bundle,cache)

if __name__ == '__main__':
    unittest.main()
