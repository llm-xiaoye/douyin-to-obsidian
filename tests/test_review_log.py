import argparse
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from markdown_it import MarkdownIt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import library
import pipeline
import review_log

VID = '7652616197779915496'
URL = 'https://www.douyin.com/note/' + VID
ANALYSIS = {'title': '记忆的检验', 'category': ['AI', '记忆'], 'tags': ['memory'], 'insight': '先核验再记忆'}


def report():
    return {'outcome': '复审通过', 'summary': '与完整材料逐项核对后的复审记录。',
            'checks': {key: {'result': '不适用' if key == 'images' else '通过',
                             'details': key + ' 的具体核对依据与正文对应位置。'} for key in review_log.CHECKS},
            'issues': []}


class ReviewLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.config = {'vault': str(self.vault), 'cache_dir': str(self.cache)}

    def save(self):
        return Path(library.save_note(self.config, VID, ANALYSIS, '## 机制\n知识正文。', source_url=URL)['note'])

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}

    def review(self, note, data=None):
        return library.record_review(self.config, VID, data or report(),
                                     hashlib.sha256(note.read_bytes()).hexdigest(), '1' * 64, URL)

    def test_legacy_upgrade_preserves_history_and_local_clock(self):
        note = library.new_path(self.vault, self.config, ANALYSIS)
        note.parent.mkdir(parents=True)
        note.write_text(library.render_note(dict(ANALYSIS, aweme_id=VID, created='2026-09-13T11:33:08-05:00'), '知识正文'))
        before = note.read_bytes(), note.stat().st_mtime_ns
        raw_cache = self.cache / VID
        raw_cache.mkdir()
        (raw_cache / 'bundle.json').write_text(json.dumps({'aweme_id': VID, 'source_url': URL}))
        link = '[[' + note.relative_to(self.vault).with_suffix('').as_posix() + '\\|记忆的检验]]'
        old = '| 时间 | 操作 | 文章 | 分类 |\n| --- | --- | --- | --- |\n'
        old += '| 2026-09-13T11:33:08-05:00 | 深化知识与配图 | ' + link + ' | 抖音 / AI / 记忆 |\n'
        log = self.vault / '更新日志.md'
        log.write_text(library.managed(None, '抖音更新日志', old) + '\n个人记录保留。\n')
        result = library.refresh_metadata(self.config)
        text = log.read_text()
        self.assertIn('2026-09-13 11:33:08', text)
        self.assertIn('2026-09-13 11:33:08', (self.vault / '知识索引.md').read_text())
        self.assertIn('[原作品](' + URL + ') | 未登记 | —', text)
        self.assertEqual(text.count('| 深化知识与配图 |'), 1)
        self.assertTrue(text.endswith('个人记录保留。\n'))
        tokens = MarkdownIt('commonmark').enable('table').parse(text)
        self.assertEqual(sum(t.type == 'th_open' for t in tokens), 7)
        self.assertEqual(sum(t.type == 'td_open' for t in tokens), 7)
        self.assertEqual((note.read_bytes(), note.stat().st_mtime_ns), before)
        self.assertTrue((Path(result['backup']) / 'before.json').is_file())
        (raw_cache / 'bundle.json').unlink()
        self.assertEqual(library.refresh_metadata(self.config)['changed_files'], [])
        self.assertEqual(log.read_text(), text)

    def test_import_stays_unreviewed_and_duplicate_is_noop(self):
        self.save()
        text = (self.vault / '更新日志.md').read_text()
        self.assertIn('[原作品](' + URL + ') | 未复审 | —', text)
        before = self.snapshot()
        result = library.save_note(self.config, VID, ANALYSIS, '替换', source_url=URL)
        self.assertEqual(result['status'], 'exists')
        self.assertEqual(before, self.snapshot())

    def test_pending_then_passed_review_and_subsequent_edit(self):
        note = self.save()
        before = note.read_bytes(), note.stat().st_mtime_ns
        data = report()
        data.update(outcome='复审待完善', issues=['机制示例未解释边界。'])
        data['checks']['depth'] = {'result': '待完善', 'details': '缺少边界条件。'}
        self.review(note, data)
        self.assertEqual((note.read_bytes(), note.stat().st_mtime_ns), before)
        self.assertIn('复审待完善', (self.vault / '更新日志.md').read_text())
        result = self.review(note)
        text = (self.vault / '更新日志.md').read_text()
        self.assertEqual(text.count('| 复审通过 |'), 3)
        self.assertEqual(text.count('| 内容复审 | [['), 2)
        self.assertTrue((Path(result['backup']) / 'before.json').is_file())
        digest = hashlib.sha256(note.read_bytes()).hexdigest()
        note.write_text(note.read_text() + '\n新的内容。\n')
        library.refresh_metadata(self.config)
        self.assertNotIn('| 复审通过 |', (self.vault / '更新日志.md').read_text())
        self.assertIn('修改后待复审', (self.vault / '更新日志.md').read_text())
        before = self.snapshot()
        with self.assertRaises(ValueError):
            library.record_review(self.config, VID, report(), digest, '1' * 64, URL)
        self.assertEqual(self.snapshot(), before)

    def test_false_pass_missing_checks_and_mismatched_url_rejected(self):
        note = self.save()
        original = self.snapshot()
        bad = report()
        bad['checks']['depth']['result'] = '待完善'
        missing = report()
        del missing['checks']['model_judgment']
        open_issues = report()
        open_issues['issues'] = ['事实还未核验']
        for data in (bad, missing, open_issues):
            with self.assertRaises(ValueError):
                self.review(note, data)
        with self.assertRaises(ValueError):
            library.record_review(self.config, VID, report(), hashlib.sha256(note.read_bytes()).hexdigest(),
                                  '1' * 64, 'https://www.douyin.com/note/7652616197779915497')
        self.assertEqual(original, self.snapshot())

    def test_review_transaction_failure_restores_state_and_log(self):
        note = self.save()
        original = self.snapshot()
        real_write = library.atomic_write
        def fail_log(path, content, exclusive=False):
            if path.name == '更新日志.md':
                raise OSError('simulated failure before replacing log')
            return real_write(path, content, exclusive)
        with patch.object(library, 'atomic_write', side_effect=fail_log):
            with self.assertRaises(OSError):
                self.review(note)
        self.assertEqual(original, self.snapshot())

    def test_image_review_requires_all_pages_and_matching_evidence(self):
        from PIL import Image
        note = self.save()
        image = self.cache / '1.png'
        Image.new('RGB', (4, 4), 'white').save(image)
        entry = {'page': 1, 'file': '1.png', 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}
        bundle = {'aweme_id': VID, 'source_url': URL, 'media_kind': 'image', 'image_count': 1, 'images': [entry]}
        raw = {'aweme_id': VID, 'pages': [dict(entry, text='完整知识材料')]}
        pipeline.write_json(self.cache / 'bundle.json', bundle)
        pipeline.write_json(self.cache / 'transcript.json', raw)
        data = report()
        data['checks']['images']['result'] = '通过'
        args = argparse.Namespace(bundle=str(self.cache / 'bundle.json'), review_file=str(self.cache / 'review.json'),
                                  expected_note_sha256=hashlib.sha256(note.read_bytes()).hexdigest(),
                                  expected_evidence_sha256=hashlib.sha256((self.cache / 'transcript.json').read_bytes()).hexdigest(), vault=None)
        pipeline.write_json(self.cache / 'review.json', data)
        original = self.snapshot()
        with self.assertRaises(ValueError):
            pipeline.review(args, self.config)
        self.assertEqual(original, self.snapshot())
        data['reviewed_pages'] = [1]
        pipeline.write_json(self.cache / 'review.json', data)
        wrong = copy.copy(args)
        wrong.expected_evidence_sha256 = '0' * 64
        with self.assertRaises(ValueError):
            pipeline.review(wrong, self.config)
        self.assertEqual(original, self.snapshot())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            pipeline.review(args, self.config)
        self.assertEqual(json.loads(output.getvalue())['outcome'], '复审通过')


if __name__ == '__main__':
    unittest.main()
