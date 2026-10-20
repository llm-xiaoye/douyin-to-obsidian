import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import dy
import pipeline

OLD = '7659111274168454446'
FIRST = '7659111274168454447'
SECOND = '7659111274168454448'
BAD = '7659111274168454449'
IMAGE = '7659111274168454450'


class DyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.config = {'vault': str(self.vault), 'cache_dir': str(self.root / 'cache'),
                       'notes_dir': 'notes', 'attachments_dir': 'attachments', 'asr_model': 'fixture'}

    def create_run(self, raw):
        created = dy.initialize(self.config)
        Path(created['input_file']).write_text(raw)
        return dy.load_run(created['run_dir'], self.config)

    def existing_note(self, vid, name='renamed-by-user.md'):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\naweme_id: "{vid}"\ntitle: 用户标题\ntags: [用户标签]\n---\n\n## 我的补充\n请完整保留。\n')
        return path

    def fake_prepare(self, args, config):
        _, vid = pipeline.normalize_url(args.source)
        target = Path(config['cache_dir']) / vid
        target.mkdir(parents=True, exist_ok=True)
        audio = target / 'audio.wav'
        with wave.open(str(audio), 'wb') as f:
            f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            f.writeframes(b'\0\0' * 160)
        pipeline.write_json(target / 'bundle.json', {'aweme_id': vid, 'source_url': args.source,
            'title': 'Synthetic source', 'author': 'Test', 'collections': args.collection or []})
        print(json.dumps({'bundle': str(target / 'bundle.json')}))

    def fake_transcribe(self, args, config):
        target = Path(args.bundle).parent
        if target.name == BAD:
            raise RuntimeError('Synthetic recognition failure')
        pipeline.write_json(target / 'transcript.json', {'aweme_id': target.name,
            'text': 'Synthetic knowledge evidence.', 'segments': [], 'asr_model': 'fixture',
            'audio_sha256': hashlib.sha256((target / 'audio.wav').read_bytes()).hexdigest()})
        print(json.dumps({'status': 'transcribed'}))

    def write_knowledge(self, ready):
        Path(ready['knowledge_file']).write_text('> 一句话结论：测试结论。\n\n## 核心要点\n\n- 一\n- 二\n- 三\n\n## 原理\n\nSynthetic standalone knowledge.\n\n## 模型补充\n\n### 补充知识\n\n补充。\n\n### 纠错与边界\n\n边界。\n\n### 模型判断\n\n判断。\n\n## 自测\n\n题与答。\n')
        pipeline.write_json(ready['analysis_file'], {'title': 'Synthetic knowledge', 'tags': ['测试'], 'category': ['领域','主题'], 'insight': '测试知识结论', 'learning_review': {'learning_goal': '用材料建立可检验的知识', 'key_claims': [{'claim': '可复用知识结论', 'evidence': '测试材料转写', 'section': '原理'}, {'claim': '模型判断', 'evidence': '测试材料', 'section': '模型判断'}], 'application': '用独立输入检验结果', 'uncertainties': []}})

    def test_plain_share_plans_only_one_image_work_even_with_author_records(self):
        short = 'https://v.douyin.com/cgZocvcgTeE/'
        share = '1.23 复制打开抖音，看看【赢泟老周（企业AI落地）的图文作品】我们落地制造业企业agent构架全景。 ' + short
        self.assertEqual(dy.parse_input(share), ('videos', [short]))
        authors = self.vault / '抖音作者'
        authors.mkdir()
        (authors / '旧作者.md').write_text('---\ntype: 抖音作者\ndouyin_author_id: cached-author\n---\n历史全量清单。\n')
        run, state, config = self.create_run(share)
        with patch.object(pipeline, 'normalize_url', return_value=(f'https://www.douyin.com/note/{IMAGE}', IMAGE)) as resolve, \
             patch.object(dy, 'read_collection') as enumerate_folder:
            result = dy.plan(run, state, config)
            resolve.assert_called_once_with(short)
            enumerate_folder.assert_not_called()
        self.assertEqual(state['mode'], 'videos')
        self.assertEqual([item['id'] for item in state['items']], [IMAGE])
        self.assertEqual(state['items'][0]['kind'], 'image')
        self.assertEqual(result['counts']['pending'], 1)

    def test_input_routing_and_exact_folder_selection(self):
        mode, urls = dy.parse_input(f'/dy 复制打开 https://www.douyin.com/video/{FIRST} 看视频 https://www.douyin.com/video/{SECOND}')
        self.assertEqual(mode, 'videos')
        self.assertEqual(len(urls), 2)
        self.assertEqual(dy.parse_input('skills,api'), ('collections', 'skills,api'))
        folders = [{'id': '1', 'name': 'skills'}, {'id': '2', 'name': 'api'},
                   {'id': '3', 'name': 'AI 学习资料'}, {'id': '4', 'name': 'skills,api'}]
        self.assertEqual(dy.resolve_collections('AI 学习资料', folders)[0]['id'], '3')
        self.assertEqual(dy.resolve_collections('skills,api', folders)[0]['id'], '4')
        self.assertEqual([f['id'] for f in dy.resolve_collections('skills，api', folders)], ['1', '2'])
        self.assertEqual([f['id'] for f in dy.resolve_collections('skills api', folders)], ['1', '2'])
        self.assertEqual(dy.resolve_collections('收藏夹ID:2', folders)[0]['name'], 'api')
        with self.assertRaises(ValueError):
            dy.resolve_collections('学习资料', folders)
        with self.assertRaises(ValueError):
            dy.resolve_collections('skills', folders + [{'id': '5', 'name': 'skills'}])
        with self.assertRaises(ValueError):
            dy.parse_input('')

    def test_report_retry_links_cover_failures_and_unresolved_inputs_only(self):
        short = 'https://v.douyin.com/example/'
        unresolved = 'https://v.douyin.com/unresolved/'
        run, state, config = self.create_run(short)
        canonical = f'https://www.douyin.com/video/{FIRST}'
        with patch.object(pipeline, 'normalize_url', return_value=(canonical, FIRST)):
            dy.plan(run, state, config)
        state['items'][0].update(status='failed', stage='detail', reason='no media')
        state['items'].append({'id': IMAGE, 'kind': 'image', 'status': 'failed'})
        state['items'].append({'id': SECOND, 'status': 'exists'})
        state['warnings'].extend([{'source': unresolved, 'reason': 'invalid_or_unresolved_link:TimeoutError'}] * 2)
        result = dy.report(state)
        expected = [canonical, f'https://www.douyin.com/note/{IMAGE}', unresolved]
        self.assertEqual(result['retry_links'], expected)
        self.assertEqual(result['failures'][0]['source_url'], short)
        self.assertEqual(result['retry_command'], '/dy ' + ' '.join(expected))
        self.assertEqual(dy.parse_input(result['retry_command']), ('videos', expected))
        state['items'] = [{'id': SECOND, 'status': 'exists'}]
        state['warnings'] = []
        self.assertEqual(dy.report(state)['retry_command'], '')

    def test_parallel_preparation_keeps_all_items_and_lossless_views_without_network(self):
        run, state, config = self.create_run(f'{FIRST} {SECOND}')
        dy.plan(run, state, config)
        for vid in (FIRST, SECOND):
            with contextlib.redirect_stdout(io.StringIO()):
                self.fake_prepare(argparse.Namespace(source=f'https://www.douyin.com/video/{vid}', collection=[]), config)
                self.fake_transcribe(argparse.Namespace(bundle=str(Path(config['cache_dir']) / vid / 'bundle.json')), config)
            bundle_path = Path(config['cache_dir']) / vid / 'bundle.json'
            bundle = pipeline.load_json(bundle_path)
            bundle['audio_sha256'] = hashlib.sha256((bundle_path.parent / 'audio.wav').read_bytes()).hexdigest()
            pipeline.write_json(bundle_path, bundle)
        result = dy.next_item(run, state, dict(config, prepare_workers=2), prepare_all=True)
        self.assertEqual([item['id'] for item in result['ready']], [FIRST, SECOND])
        self.assertEqual([item['status'] for item in pipeline.load_json(run / 'state.json')['items']], ['ready', 'ready'])
        for ready in result['ready']:
            transcript = pipeline.load_json(ready['transcript_file'])
            self.assertEqual(Path(ready['material_file']).read_text(), transcript['text'])
            self.assertEqual(Path(ready['knowledge_file']).parent, run / 'items' / ready['id'])
            self.write_knowledge(ready)
            self.assertEqual(dy.commit(run, state, config, ready['id'])['status'], 'saved')
        self.assertEqual(dy.next_item(run, state, config)['status'], 'done')

    def test_lossless_video_view_keeps_every_character_and_image_evidence_stays_json(self):
        target = self.root / 'view'
        target.mkdir()
        transcript = target / 'transcript.json'
        content = '  开头\n完整正文，包括标点。\n最后一句  '
        pipeline.write_json(transcript, {'text': content, 'segments': [{'start': 0, 'end': 1, 'text': content}]})
        view = dy.model_material(transcript, {'media_kind': 'video'}, target)
        self.assertEqual(view.read_text(), content)
        self.assertEqual(dy.model_material(transcript, {'media_kind': 'image'}, target), transcript)

    def test_last_local_check_recovers_response_arriving_during_http_without_reopening(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        observed = []
        def prepare(args, current):
            observed.append(dict(current))
            if len(observed) < 3:
                raise pipeline.image_posts.StageError('detail', 'not yet received', 'DETAIL_UNAVAILABLE')
            self.fake_prepare(args, current)
        with patch.object(pipeline, 'prepare', side_effect=prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session', return_value={}) as browser:
            self.assertEqual(dy.next_item(run, state, config)['status'], 'ready')
        browser.assert_called_once()
        self.assertTrue(observed[-1]['_browser_media_only'])
        self.assertEqual(observed[-1]['_browser_media_wait_seconds'], 0)

    def test_worker_checkpoint_resume_does_not_spend_browser_budget_again(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        checkpoint = run / '.workers' / FIRST
        item = dict(state['items'][0], browser_recovery={'attempted': True, 'status': 'started'})
        dy.write_state(checkpoint, dict(state, items=[item]))
        missing = pipeline.image_posts.StageError('detail', 'no media', 'DETAIL_UNAVAILABLE')
        with patch.object(pipeline, 'prepare', side_effect=missing), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session') as browser:
            result = dy.prepare_worker(str(run), state, state['items'][0], config)
        self.assertEqual(result['status'], 'failed')
        browser.assert_not_called()

    def test_background_first_then_browser_closes_only_the_opened_tab(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        config = dict(config, browser='chrome', browser_cache_dir=str(self.root / 'no-chrome-cache'),
                      background_detail_first=True, close_opened_tabs=True, prefer_browser_media=False)
        missing = pipeline.image_posts.StageError('detail', 'no media', 'DETAIL_UNAVAILABLE')
        seen, order = [], []
        def prepare(args, current):
            seen.append(current.get('_background_detail_only'))
            if len(seen) == 1:
                raise missing
            self.fake_prepare(args, current)
        with patch.object(dy.pipeline.sys, 'platform', 'darwin'), \
             patch.object(pipeline, 'prepare', side_effect=prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe), \
             patch.object(pipeline.douyin_access, 'douyin_tab_ids',
                          side_effect=lambda *a, **k: order.append('snapshot') or []), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session',
                          side_effect=lambda *a, **k: order.append('open') or {}), \
             patch.object(pipeline.douyin_access, 'close_new_tabs',
                          side_effect=lambda *a, **k: order.append('close') or 1) as closer:
            self.assertEqual(dy.next_item(run, state, config)['status'], 'ready')
        self.assertEqual(seen[0], True)
        self.assertIsNone(seen[1])
        self.assertEqual(order, ['snapshot', 'open', 'close'])
        closer.assert_called_once_with(FIRST, [], 'chrome')

    def test_background_success_opens_no_tab(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        config = dict(config, browser='chrome', background_detail_first=True, close_opened_tabs=True)
        with patch.object(dy.pipeline.sys, 'platform', 'darwin'), \
             patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe), \
             patch.object(pipeline.douyin_access, 'douyin_tab_ids') as snapshot, \
             patch.object(pipeline.douyin_access, 'refresh_browser_session') as browser:
            self.assertEqual(dy.next_item(run, state, config)['status'], 'ready')
        snapshot.assert_not_called()
        browser.assert_not_called()

    def test_changed_model_view_rejected_before_commit(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            ready = dy.next_item(run, state, config)
        self.write_knowledge(ready)
        Path(ready['material_file']).write_text('changed')
        with self.assertRaisesRegex(ValueError, '模型材料'):
            dy.commit(run, state, config, FIRST)
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_shared_folder_videos_deduplicate_before_download_and_partial_list_stays_partial(self):
        old = self.existing_note(OLD)
        before, mtime = old.read_bytes(), old.stat().st_mtime_ns
        run, state, config = self.create_run('skills，api')
        folders = {'complete': True, 'items': [{'id': '1', 'name': 'skills', 'count': 3},
                                             {'id': '2', 'name': 'api', 'count': 4}]}
        videos = [
            {'complete': True, 'items': [{'id': OLD}, {'id': FIRST}, {'id': IMAGE, 'kind': 'image'}]},
            {'complete': False, 'reason': 'reported_count_mismatch', 'items': [{'id': FIRST}, {'id': SECOND}]},
        ]
        with patch.object(dy, 'read_collection', side_effect=[folders, *videos]) as reader:
            result = dy.plan(run, state, config)
        self.assertFalse(result['source_complete'])
        self.assertEqual(result['counts']['pending'], 3)
        self.assertEqual(result['counts']['exists'], 1)
        self.assertEqual(result['counts']['skipped'], 0)
        self.assertEqual(dy.select_item(state, FIRST)['collections'], ['skills', 'api'])
        self.assertEqual(reader.call_args_list[-1].kwargs['expected_count'], 4)
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns), (before, mtime))

    def test_multiple_new_videos_resume_and_repeat_without_rewriting(self):
        old = self.existing_note(OLD)
        before, mtime = old.read_bytes(), old.stat().st_mtime_ns
        raw = ' '.join(f'https://www.douyin.com/video/{vid}' for vid in (OLD, FIRST, SECOND, FIRST))
        run, state, config = self.create_run(raw)
        self.assertEqual(dy.plan(run, state, config)['counts']['pending'], 2)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare) as prepare, \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            for vid in (FIRST, SECOND):
                ready = dy.next_item(run, state, config)
                self.assertEqual(ready['id'], vid)
                # Loading the persistent state after interruption returns the
                # same prepared item, without another download/transcription.
                run, state, config = dy.load_run(str(run), self.config)
                self.assertEqual(dy.next_item(run, state, config), ready)
                self.write_knowledge(ready)
                self.assertEqual(dy.commit(run, state, config, vid)['status'], 'saved')
            result = dy.next_item(run, state, config)
        self.assertTrue(result['complete'])
        self.assertEqual(result['counts']['saved'], 2)
        self.assertEqual(result['counts']['exists'], 1)
        self.assertEqual(prepare.call_count, 2)
        snapshot = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob('*.md')}
        next_run, next_state, next_config = self.create_run(raw)
        self.assertNotEqual(run, next_run)
        result = dy.plan(next_run, next_state, next_config)
        with patch.object(pipeline, 'prepare', side_effect=AssertionError('existing notes must not download')):
            self.assertEqual(dy.next_item(next_run, next_state, next_config)['status'], 'done')
        self.assertEqual(result['counts']['exists'], 3)
        self.assertEqual(snapshot, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob('*.md')})
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns), (before, mtime))

    def test_failed_item_does_not_stop_remaining_items(self):
        run, state, config = self.create_run(f'{BAD} {FIRST}')
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            self.assertEqual(dy.next_item(run, state, config)['status'], 'failed')
            ready = dy.next_item(run, state, config)
            self.assertEqual(ready['id'], FIRST)
            self.write_knowledge(ready)
            dy.commit(run, state, config, FIRST)
            result = dy.next_item(run, state, config)
        self.assertTrue(result['finished'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['saved'], 1)
        self.assertEqual(result['failures'][0]['id'], BAD)

    def test_prepare_all_handles_failures_and_all_sources_in_one_call_without_notes(self):
        run, state, config = self.create_run(f'{BAD} {FIRST} {SECOND}')
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare) as prepare, \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            result = dy.next_item(run, state, config, prepare_all=True)
            self.assertEqual(result['status'], 'prepared')
            self.assertEqual([item['id'] for item in result['ready']], [FIRST, SECOND])
            self.assertEqual(result['counts']['failed'], 1)
            self.assertTrue(result['model_analysis_needed'])
            self.assertEqual(prepare.call_count, 3)
            dy.next_item(run, state, config, prepare_all=True)
            self.assertEqual(prepare.call_count, 3)
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_prepare_all_empty_evidence_explicitly_stops_model_analysis(self):
        run, state, config = self.create_run(BAD)
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            result = dy.next_item(run, state, config, prepare_all=True)
        self.assertEqual(result['status'], 'done')
        self.assertFalse(result['model_analysis_needed'])
        self.assertEqual(result['ready'], [])
        self.assertEqual(result['counts']['failed'], 1)

    def test_detail_failure_opens_browser_once_then_recovers_without_committing(self):
        run, state, config = self.create_run(FIRST)
        config['browser'] = 'chrome'
        dy.plan(run, state, config)
        missing = pipeline.image_posts.StageError('detail', 'No matching media', 'DETAIL_UNAVAILABLE')
        calls = []

        def prepare(args, actual_config):
            calls.append(args.source)
            if len(calls) == 1:
                raise missing
            self.fake_prepare(args, actual_config)

        def refresh(*args, **kwargs):
            persisted = pipeline.load_json(run / 'state.json')['items'][0]
            self.assertTrue(persisted['browser_recovery']['attempted'])
            self.assertEqual(persisted['stage'], 'browser_recovery')
            return {'page_state': 'not_observed'}

        with patch.object(pipeline, 'prepare', side_effect=prepare), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session', side_effect=refresh) as browser, \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            self.assertEqual(dy.next_item(run, state, config)['status'], 'ready')
        browser.assert_called_once_with(FIRST, 'chrome', 8, source_url=f'https://www.douyin.com/video/{FIRST}', profile=None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(state['items'][0]['browser_recovery']['status'], 'recovered')
        self.assertEqual(state['items'][0]['browser_recovery']['media_wait_timeout_seconds'], 40)
        self.assertIn('No matching media', state['items'][0]['browser_recovery']['first_failure'])
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_browser_first_pass_and_recovery_have_distinct_bounded_routes(self):
        run, state, config = self.create_run(FIRST)
        config.update(browser='chrome', prefer_browser_media=True)
        dy.plan(run, state, config)
        observed = []

        def prepare(args, actual_config):
            observed.append((actual_config.get('_browser_media_only', False),
                             actual_config.get('_browser_media_wait_seconds', 0)))
            if len(observed) == 1:
                raise pipeline.image_posts.StageError('detail', 'cache miss', 'DETAIL_UNAVAILABLE')
            self.fake_prepare(args, actual_config)

        with patch.object(pipeline.sys, 'platform', 'darwin'), \
             patch.object(pipeline, 'prepare', side_effect=prepare), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session', return_value={}) as browser, \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            self.assertEqual(dy.next_item(run, state, config, prepare_all=True)['status'], 'prepared')
        self.assertEqual(observed, [(True, 0), (False, 40)])
        browser.assert_called_once()

    def test_exhausted_browser_recovery_is_terminal_and_next_item_still_runs(self):
        run, state, config = self.create_run(f'{BAD} {FIRST}')
        dy.plan(run, state, config)
        missing = pipeline.image_posts.StageError('detail', 'No matching media', 'DETAIL_UNAVAILABLE')

        def prepare(args, actual_config):
            if BAD in args.source:
                raise missing
            self.fake_prepare(args, actual_config)

        with patch.object(pipeline, 'prepare', side_effect=prepare), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session', return_value={}) as browser, \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            failed = dy.next_item(run, state, config)
            self.assertEqual(state['items'][0]['error_code'], 'RECOVERY_EXHAUSTED')
            self.assertIn('cache_probe', state['items'][0]['browser_recovery'])
            self.assertEqual(dy.next_item(run, state, config)['id'], FIRST)
        browser.assert_called_once()
        self.assertEqual(state['items'][0]['browser_recovery']['status'], 'failed')
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_incomplete_cache_search_is_not_classified_as_unavailable_media(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        def missing(args, actual):
            if '_browser_cache_diagnostics' in actual:
                actual['_browser_cache_diagnostics']['scan_incomplete'] = True
            raise pipeline.image_posts.StageError('detail', 'cache miss', 'DETAIL_UNAVAILABLE')
        with patch.object(pipeline, 'prepare', side_effect=missing), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session', return_value={}):
            result = dy.next_item(run, state, config)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(state['items'][0]['error_code'], 'CACHE_LOOKUP_INCOMPLETE')
        self.assertEqual(dy.report(state)['retry_links'], [f'https://www.douyin.com/video/{FIRST}'])
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_resume_after_browser_attempt_does_not_open_again(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        state['items'][0]['browser_recovery'] = {'attempted': True, 'status': 'started'}
        dy.write_state(run, state)
        run, state, config = dy.load_run(str(run), config)
        missing = pipeline.image_posts.StageError('detail', 'No matching media', 'DETAIL_UNAVAILABLE')
        with patch.object(pipeline, 'prepare', side_effect=missing), \
             patch.object(pipeline.douyin_access, 'refresh_browser_session') as browser:
            self.assertEqual(dy.next_item(run, state, config)['status'], 'failed')
        browser.assert_not_called()

    def test_rate_limit_does_not_open_browser_or_retry(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        limited = pipeline.image_posts.StageError('detail', 'HTTP 429', 'RATE_LIMITED')
        with patch.object(pipeline, 'prepare', side_effect=limited) as prepare, \
             patch.object(pipeline.douyin_access, 'refresh_browser_session') as browser:
            self.assertEqual(dy.next_item(run, state, config)['status'], 'failed')
        prepare.assert_called_once()
        browser.assert_not_called()

    def test_commit_rechecks_note_created_after_plan(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            ready = dy.next_item(run, state, config)
        self.write_knowledge(ready)
        concurrent = self.existing_note(FIRST, 'a-new-user-name.md')
        before, mtime = concurrent.read_bytes(), concurrent.stat().st_mtime_ns
        self.assertEqual(dy.commit(run, state, config, FIRST)['status'], 'exists')
        self.assertEqual((concurrent.read_bytes(), concurrent.stat().st_mtime_ns), (before, mtime))
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)

    def test_changed_transcript_is_not_paired_with_old_knowledge(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            ready = dy.next_item(run, state, config)
        self.write_knowledge(ready)
        cached = Path(config['cache_dir']) / FIRST / 'transcript.json'
        data = pipeline.load_json(cached)
        data['text'] = 'Replacement source evidence.'
        pipeline.write_json(cached, data)
        with self.assertRaises(ValueError):
            dy.commit(run, state, config, FIRST)
        self.assertFalse(list(self.vault.rglob('*.md')))

    def test_atomic_publication_does_not_replace_competing_note(self):
        run, state, config = self.create_run(FIRST)
        dy.plan(run, state, config)
        with patch.object(pipeline, 'prepare', side_effect=self.fake_prepare), \
             patch.object(pipeline, 'transcribe', side_effect=self.fake_transcribe):
            ready = dy.next_item(run, state, config)
        self.write_knowledge(ready)

        original = pipeline.library.atomic_write
        raced = []
        def competing_writer(destination, content, exclusive=False):
            if destination.suffix == '.md' and destination.name.startswith('Synthetic'):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text('Existing user knowledge\n')
                raced.append(destination)
                raise FileExistsError(str(destination))
            return original(destination, content, exclusive)

        with patch.object(pipeline.library, 'atomic_write', side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                dy.commit(run, state, config, FIRST)
        self.assertEqual(raced[0].read_text(), 'Existing user knowledge\n')
        self.assertFalse((self.vault / '知识索引.md').exists())
        self.assertFalse((self.vault / '.dy-transaction.json').exists())

    def test_invalid_links_are_not_imported_and_unknown_run_is_rejected(self):
        run, state, config = self.create_run(f'https://evil.example/video/{FIRST}')
        result = dy.plan(run, state, config)
        self.assertTrue(result['finished'])
        self.assertFalse(result['source_complete'])
        self.assertEqual(state['items'], [])
        with self.assertRaises(ValueError):
            dy.load_run(str(self.root), config)
        with self.assertRaises(ValueError):
            dy.select_item(state, FIRST)


if __name__ == '__main__':
    unittest.main()
