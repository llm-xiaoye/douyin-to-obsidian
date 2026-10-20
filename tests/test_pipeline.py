import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import pipeline
import collect_folders


class PipelineTests(unittest.TestCase):
    def test_review_sections_normalize_quotes_but_reject_ambiguous_or_missing_headings(self):
        def analysis(section):
            return {'learning_review': {'learning_goal': '解释原理',
                'key_claims': [{'claim': '结论', 'evidence': '完整转写', 'section': section}],
                'application': '检验', 'uncertainties': []}}
        data = analysis('为什么“不能”')
        pipeline.validate_learning_review(data, '## 为什么"不能"\n内容')
        self.assertEqual(data['learning_review']['key_claims'][0]['section'], '为什么"不能"')
        data = analysis('最容易踩的坑')
        pipeline.validate_learning_review(data, '### 最容易踩的坑：不做隔离的后果\n内容')
        self.assertEqual(data['learning_review']['key_claims'][0]['section'], '最容易踩的坑：不做隔离的后果')
        for body in ('## 完全不同的主题\n内容', '## 最容易踩的坑：甲\n## 最容易踩的坑：乙\n'):
            with self.assertRaises(ValueError):
                pipeline.validate_learning_review(analysis('最容易踩的坑'), body)

    def test_url_identity_and_foreign_domain_rejection(self):
        expected = '7659111274168454446'
        for value in [expected, f'https://jingxuan.douyin.com/m/video/{expected}', f'https://www.douyin.com/note/{expected}',
                      f'复制打开抖音 https://www.douyin.com/?modal_id={expected} 看视频']:
            self.assertEqual(pipeline.normalize_url(value)[1], expected)
        with self.assertRaises(ValueError):
            pipeline.normalize_url(f'https://douyin.com.example.com/video/{expected}')

    def test_escape_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                pipeline.inside(Path(tmp), '../outside.md')

    def test_invalid_transcript_not_success(self):
        for raw in [{'text': ''}, {'text': 'test', 'segments': [{'start': 9, 'end': 2, 'text': 'test'}]}]:
            with self.assertRaises(ValueError):
                pipeline.validate_transcript(raw)

    def test_save_duplicate_and_moved_note_preserves_user_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / 'vault'
            vault.mkdir()
            cache = root / 'cache'
            cache.mkdir()
            vid = '7659111274168454446'
            audio = cache / 'audio.wav'
            with wave.open(str(audio), 'wb') as f:
                f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                f.writeframes(b'\0\0' * 160)
            digest = hashlib.sha256(audio.read_bytes()).hexdigest()
            pipeline.write_json(cache / 'bundle.json', {'aweme_id': vid, 'source_url': f'https://www.douyin.com/video/{vid}',
                                                       'title': 'Synthetic fixture', 'author': 'Test author'})
            pipeline.write_json(cache / 'transcript.json', {'aweme_id': vid, 'text': 'Synthetic speech.',
                'segments': [{'start': 0, 'end': 0.01, 'text': 'Synthetic speech.'}], 'audio_sha256': digest})
            summary = cache / 'summary.md'
            summary.write_text('> 一句话结论：测试结论。\n\n## 核心要点\n\n- 一\n- 二\n- 三\n\n## A synthetic note\n\nTest content.\n\n## 模型补充\n\n### 补充知识\n\n补充。\n\n### 纠错与边界\n\n边界。\n\n### 模型判断\n\n判断。\n\n## 自测\n\n题与答。\n')
            pipeline.write_json(cache/'analysis.json', {'title':'可读的知识主题', 'category':['领域','主题'], 'tags':['测试'], 'insight':'独立知识结论', 'learning_review': {'learning_goal': '用材料建立可检验的知识', 'key_claims': [{'claim': '可复用知识结论', 'evidence': '测试材料转写', 'section': 'A synthetic note'}, {'claim': '模型判断', 'evidence': '测试材料', 'section': '模型判断'}], 'application': '用独立输入检验结果', 'uncertainties': []}})
            args = argparse.Namespace(bundle=str(cache/'bundle.json'), summary=str(summary), vault=str(vault), analysis=str(cache/'analysis.json'),
                                      title=None, collection=None, tag=None, archive_source=True)
            config = {'notes_dir': 'notes', 'attachments_dir': 'attachments'}
            with contextlib.redirect_stdout(io.StringIO()):
                pipeline.save(args, config)
            note = vault / 'notes' / '领域' / '主题' / '可读的知识主题.md'
            self.assertTrue((vault/'attachments'/f'{vid}-audio.wav').exists())
            self.assertNotIn('## 来源', note.read_text())
            self.assertTrue((vault/'知识索引.md').exists())
            self.assertTrue((vault/'更新日志.md').exists())
            edited = note.read_text() + '\nPersonal edits must survive.\n'
            note.write_text(edited)
            moved = vault / 'renamed-by-user.md'
            note.rename(moved)
            with contextlib.redirect_stdout(io.StringIO()) as log:
                pipeline.save(args, config)
            self.assertEqual(json.loads(log.getvalue())['status'], 'exists')
            self.assertEqual(moved.read_text(), edited)
            self.assertFalse(note.exists())
            self.assertEqual(len(list(vault.rglob('*.md'))), 3)
            with self.assertRaises(ValueError):
                audio.write_bytes(b'changed')
                pipeline.save(args, config)

    def test_collection_schema_and_large_ids(self):
        raw = {'status_code': 0, 'has_more': True, 'cursor': 20,
               'collects_list': [{'collects_id': 7659111274168454446, 'collects_name': '知识', 'total_number': 2}]}
        rows, has_more, cursor = collect_folders.parse_page(raw, 'list')
        self.assertEqual(rows[0]['id'], '7659111274168454446')
        self.assertTrue(has_more)
        self.assertEqual(cursor, 20)
        with self.assertRaises(ValueError):
            collect_folders.parse_page({'status_code': 0}, 'videos')
        with self.assertRaises(ValueError):
            collect_folders.parse_page({'status_code': 8, 'aweme_list': [], 'has_more': False}, 'videos')


class LocalAsrModelTests(unittest.TestCase):
    def test_existing_directory_is_used_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pipeline.local_asr_model(tmp), tmp)

    def test_cached_snapshot_is_preferred_without_network(self):
        from unittest import mock
        with mock.patch('huggingface_hub.snapshot_download', return_value='/cache/snap') as fake:
            self.assertEqual(pipeline.local_asr_model('org/model'), '/cache/snap')
        fake.assert_called_once_with(repo_id='org/model', local_files_only=True)

    def test_missing_snapshot_falls_back_to_repo_id(self):
        from unittest import mock
        with mock.patch('huggingface_hub.snapshot_download', side_effect=OSError('not cached')):
            self.assertEqual(pipeline.local_asr_model('org/model'), 'org/model')


if __name__ == '__main__':
    unittest.main()
