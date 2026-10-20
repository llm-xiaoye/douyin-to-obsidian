import argparse
import asyncio
import contextlib
from http.cookiejar import Cookie, CookieJar
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/douyin-to-obsidian/scripts"))
import douyin_access as access
import image_posts
import pipeline

VID = "7685046652700409314"
OTHER = "7685046652700409315"
VIDEO = {"aweme_id": VID, "desc": "长期记忆的适用边界", "author": {"nickname": "作者"},
         "video": {"duration": 145000, "play_addr": {"url_list": ["https://media.example/video.mp4"]}}}
IMAGE = {"aweme_id": VID, "images": [{"url_list": ["https://media.example/page.webp"]}]}


def share_page(items):
    payload = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"status_code": 0, "item_list": items}}}}
    return "<script>window._ROUTER_DATA = " + json.dumps(payload) + ";</script>"


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        cache_reader = patch.object(pipeline.browser_cache, 'find_detail', return_value=None)
        cache_reader.start()
        self.addCleanup(cache_reader.stop)

    def report(self):
        reports = list((self.cache / "recovery").glob("*.json"))
        self.assertEqual(len(reports), 1)
        return json.loads(reports[0].read_text())

    def test_browser_recovery_uses_launchservices_and_exact_work_without_shell(self):
        with patch.object(access.sys, 'platform', 'darwin'), \
             patch.object(access.subprocess, 'run') as launched, \
             patch.object(access.time, 'sleep') as waited:
            result = access.refresh_browser_session(VID, 'chrome', 8)
        self.assertEqual(launched.call_args.args[0], ['/usr/bin/open', '-g', '-b',
                         'com.google.Chrome', f'https://www.douyin.com/video/{VID}'])
        self.assertNotIn('shell', launched.call_args.kwargs)
        waited.assert_called_once_with(8)
        self.assertEqual(result['page_state'], 'not_observed')

    def test_pinned_profile_launch_and_cookies_use_same_directory(self):
        profile = self.cache / 'User Data' / 'Profile 2'
        profile.mkdir(parents=True)
        profile = profile.resolve()
        with patch.object(access.sys, 'platform', 'darwin'), \
             patch.object(access.subprocess, 'Popen') as launched, \
             patch.object(access.subprocess, 'run') as generic, \
             patch.object(access.time, 'sleep'):
            launched.return_value.poll.return_value = 0
            result = access.refresh_browser_session(VID, 'chrome', 0, profile=str(profile))
        argv = launched.call_args.args[0]
        self.assertEqual(argv, ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                               '--user-data-dir=' + str(profile.parent),
                               '--profile-directory=Profile 2', f'https://www.douyin.com/video/{VID}'])
        generic.assert_not_called()
        self.assertEqual(result['profile'], str(profile))
        self.assertEqual(result['page_state'], 'not_observed')
        with patch('yt_dlp.YoutubeDL') as ydl:
            access.browser_cookies('chrome', str(profile))
        self.assertEqual(ydl.call_args.args[0]['cookiesfrombrowser'], ('chrome', str(profile)))

    def test_missing_pinned_profile_does_not_open_another_profile(self):
        with patch.object(access.sys, 'platform', 'darwin'), \
             patch.object(access.subprocess, 'Popen') as pinned, \
             patch.object(access.subprocess, 'run') as generic:
            with self.assertRaises(image_posts.StageError) as error:
                access.refresh_browser_session(VID, 'chrome', 0, profile=str(self.cache / 'missing'))
        self.assertEqual(error.exception.code, 'PROFILE_UNAVAILABLE')
        pinned.assert_not_called()
        generic.assert_not_called()

    def test_recovery_cookie_profile_is_forwarded(self):
        with patch.object(access, 'browser_cookies', return_value=CookieJar()) as cookies, \
             patch.object(access, 'api_detail', new=AsyncMock(return_value=VIDEO)):
            asyncio.run(access.recover_detail(VID, 'chrome', self.cache, profile=str(self.cache)))
        cookies.assert_called_once_with('chrome', str(self.cache))

    def test_invalid_browser_recovery_never_launches_a_command(self):
        with patch.object(access.subprocess, 'run') as launched:
            with self.assertRaises(image_posts.StageError):
                access.refresh_browser_session(VID + ';open', 'chrome')
            with self.assertRaises(image_posts.StageError):
                access.refresh_browser_session(VID, 'unknown-browser')
        launched.assert_not_called()

    def test_refresh_updates_same_memory_session_and_succeeds_without_handoff(self):
        jar = CookieJar()
        secret = "private-cookie-must-never-be-logged"

        def warm(url, actual_jar, ua):
            self.assertIs(actual_jar, jar)
            actual_jar.set_cookie(Cookie(0, "sessionid", secret, None, False, ".douyin.com",
                True, True, "/", True, True, None, True, None, None, {}))
            return "<html></html>"

        async def detail(vid, actual_jar):
            return VIDEO if list(actual_jar) else None

        with patch.object(access, "browser_cookies", return_value=jar) as cookies, \
             patch.object(access, "api_detail", side_effect=detail) as api, \
             patch.object(access, "page_request", side_effect=warm) as page:
            self.assertEqual(asyncio.run(access.recover_detail(VID, "chrome", self.cache)), VIDEO)
        self.assertEqual(cookies.call_count, 1)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(page.call_count, 1)
        report = self.report()
        self.assertEqual(report["route"], "f2_after_session_refresh")
        self.assertNotIn(secret, json.dumps(report))

    def test_cookie_read_failure_can_recover_public_images_without_browser(self):
        with patch.object(access, "browser_cookies", side_effect=RuntimeError("secret-cookie-value")), \
             patch.object(access, "api_detail", new=AsyncMock(return_value=None)), \
             patch.object(access, "page_request", side_effect=["", share_page([IMAGE])]):
            self.assertEqual(asyncio.run(access.recover_detail(VID, "chrome", self.cache)), IMAGE)
        report = self.report()
        self.assertEqual(report["route"], "public_share")
        self.assertNotIn("secret-cookie-value", json.dumps(report))

    def test_recommendations_and_title_only_data_are_not_target_evidence(self):
        wrong = dict(VIDEO, aweme_id=OTHER)
        self.assertIsNone(access.public_share_detail(share_page([wrong, {"aweme_id": VID, "desc": "标题"}]), VID))
        self.assertEqual(access.public_share_detail(share_page([wrong, VIDEO]), VID), VIDEO)
        self.assertIsNone(access.public_share_detail("<script>window._ROUTER_DATA = runCode()</script>", VID))
        self.assertIsNone(access.public_share_detail("<script>window._ROUTER_DATA = null</script>", VID))
        self.assertIsNone(access.public_share_detail(share_page([{"aweme_id": VID, "video": {}, "images": []}]), VID))

    def test_total_failure_is_bounded_and_does_not_claim_cookie_expiry(self):
        with patch.object(access, "browser_cookies", return_value=CookieJar()), \
             patch.object(access, "api_detail", new=AsyncMock(return_value={"aweme_id": OTHER})) as api, \
             patch.object(access, "page_request", return_value="请在抖音内观看") as page:
            with self.assertRaises(image_posts.StageError) as caught:
                asyncio.run(access.recover_detail(VID, "chrome", self.cache))
        self.assertEqual((api.call_count, page.call_count), (2, 2))
        self.assertIn("不能仅凭空详情认定 Cookie 过期", str(caught.exception))
        self.assertIsNone(self.report()["route"])
        self.assertFalse((self.cache / "bundle.json").exists())

    def test_rate_limit_stops_before_another_detail_request(self):
        limited = HTTPError("https://www.douyin.com/", 429, "limited", {}, None)
        with patch.object(access, "browser_cookies", return_value=CookieJar()), \
             patch.object(access, "api_detail", new=AsyncMock(return_value=None)) as api, \
             patch.object(access, "page_request", side_effect=limited) as page:
            with self.assertRaisesRegex(image_posts.StageError, "429"):
                asyncio.run(access.recover_detail(VID, "chrome", self.cache))
        self.assertEqual((api.call_count, page.call_count), (1, 1))

    def test_api_rate_limit_does_not_trigger_page_or_api_retry(self):
        limited = HTTPError("https://www.douyin.com/", 429, "limited", {}, None)
        with patch.object(access, "browser_cookies", return_value=CookieJar()), \
             patch.object(access, "api_detail", new=AsyncMock(side_effect=limited)) as api, \
             patch.object(access, "page_request") as page:
            with self.assertRaisesRegex(image_posts.StageError, "429"):
                asyncio.run(access.recover_detail(VID, "chrome", self.cache))
        self.assertEqual(api.call_count, 1)
        page.assert_not_called()
        self.assertEqual(self.report()["attempts"][-1]["result"], "http_429")

    def test_partial_media_and_foreign_work_cannot_replace_existing_file(self):
        target = self.cache / "source-detail.mp4"
        target.write_bytes(b"existing media")
        with patch.object(access, "urlopen", return_value=Response(b"x" * 100, length=200)):
            with self.assertRaises(image_posts.StageError):
                access.download_video(VIDEO, VID, self.cache)
        self.assertEqual(target.read_bytes(), b"existing media")
        self.assertFalse(target.with_suffix(".mp4.part").exists())
        with patch.object(access, "urlopen") as request:
            with self.assertRaises(image_posts.StageError):
                access.download_video(dict(VIDEO, aweme_id=OTHER), VID, self.cache)
        request.assert_not_called()

    def test_media_download_uses_video_not_music_and_never_sends_browser_cookie(self):
        detail = dict(VIDEO, music={"play_url": {"url_list": ["https://music.example/bgm.mp3"]}})
        data = b"video fixture" * 100
        with patch.object(access, "urlopen", return_value=Response(data)) as request:
            self.assertEqual(access.download_video(detail, VID, self.cache).read_bytes(), data)
        self.assertEqual(request.call_args.args[0].full_url, "https://media.example/video.mp4")
        self.assertIsNone(request.call_args.args[0].get_header("Cookie"))

    def test_pipeline_recovers_video_instead_of_discarding_f2_video_detail(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        config = {"cache_dir": str(self.cache), "browser": "chrome"}

        def audio(source, output):
            with wave.open(str(output), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * 16000 * 145)

        with patch("yt_dlp.YoutubeDL") as downloader, \
             patch.object(image_posts, "post_detail", new=AsyncMock(return_value=VIDEO)), \
             patch.object(access, "urlopen", return_value=Response(b"media" * 100)), \
             patch.object(pipeline, "extract_audio", side_effect=audio), \
             contextlib.redirect_stdout(io.StringIO()):
            downloader.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("Fresh cookies needed")
            pipeline.prepare(args, config)
        bundle = json.loads((self.cache / VID / "bundle.json").read_text())
        self.assertEqual(bundle["aweme_id"], VID)
        self.assertEqual(bundle["title"], VIDEO["desc"])
        self.assertEqual(bundle["duration_seconds"], 145)
        self.assertEqual(bundle["media_kind"], "video")
        self.assertTrue(bundle["audio_sha256"])

    def test_incomplete_audio_never_reaches_transcription(self):
        target = self.cache / 'partial.wav'
        with wave.open(str(target), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 16000)
        with self.assertRaisesRegex(image_posts.StageError, '时长'):
            pipeline.validate_audio(target, 145)

    def test_matched_video_download_error_is_not_masked_as_detail_failure(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        config = {'cache_dir': str(self.cache), 'browser': 'chrome'}
        error = image_posts.StageError('download', 'matched video download failed')
        with patch.object(pipeline.browser_cache, 'find_detail', return_value=VIDEO), \
             patch.object(access, 'download_video', side_effect=error), \
             patch.object(image_posts, 'post_detail', new=AsyncMock()) as api:
            with self.assertRaises(image_posts.StageError) as caught:
                pipeline.prepare(args, config)
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.stage, 'download')
        api.assert_not_called()

    def test_late_browser_response_is_used_before_network_fallback(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        config = {'cache_dir': str(self.cache), 'browser': 'chrome', '_browser_media_wait_seconds': 40}

        def audio(source, output):
            with wave.open(str(output), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 16000 * 145)

        with patch.object(pipeline.browser_cache, 'wait_for_detail', return_value=VIDEO) as waited, \
             patch('yt_dlp.YoutubeDL') as downloader, \
             patch.object(image_posts, 'post_detail', new=AsyncMock()) as api, \
             patch.object(access, 'urlopen', return_value=Response(b'media' * 100)), \
             patch.object(pipeline, 'extract_audio', side_effect=audio), \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.prepare(args, config)
        waited.assert_called_once_with(VID, 'chrome', None, 40)
        downloader.assert_not_called()
        api.assert_not_called()
        self.assertEqual(pipeline.load_json(self.cache / VID / 'bundle.json')['media_route'], 'chrome_received_detail')

    def test_image_post_uses_browser_detail_instead_of_repeating_broken_api(self):
        args = argparse.Namespace(source=f'https://www.douyin.com/note/{VID}', url=None,
                                  browser=None, title=None, author=None, collection=[])
        detail = {'aweme_id': VID, 'images': [{'url_list': ['https://example.com/page.jpg']}]}
        with patch.object(pipeline.browser_cache, 'find_detail', return_value=detail), \
             patch.object(image_posts, 'prepare_images', return_value={'image_count': 1}) as images, \
             patch.object(image_posts, 'post_detail', new=AsyncMock()) as api, \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.prepare(args, {'cache_dir': str(self.cache), 'browser': 'chrome', '_browser_media_only': True})
        self.assertEqual(images.call_args.args[3], detail)
        api.assert_not_called()

    def test_browser_recovery_preserves_note_url_and_rejects_mismatched_work(self):
        url = f'https://www.douyin.com/note/{VID}'
        with patch.object(access.sys, 'platform', 'darwin'), \
             patch.object(access.subprocess, 'run') as opened, patch.object(access.time, 'sleep'):
            self.assertEqual(access.refresh_browser_session(VID, 'chrome', 0, source_url=url)['url'], url)
            self.assertEqual(opened.call_args.args[0][-1], url)
            with self.assertRaises(image_posts.StageError):
                access.refresh_browser_session(VID, 'chrome', 0, source_url=f'https://www.douyin.com/note/{OTHER}')
        opened.assert_called_once()

    def test_browser_first_cache_miss_does_not_spend_time_retrying_http(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        config = {'cache_dir': str(self.cache), 'browser': 'chrome', '_browser_media_only': True}
        with patch('yt_dlp.YoutubeDL') as downloader, \
             patch.object(image_posts, 'post_detail', new=AsyncMock()) as api:
            with self.assertRaises(image_posts.StageError) as caught:
                pipeline.prepare(args, config)
        self.assertEqual(caught.exception.code, 'DETAIL_UNAVAILABLE')
        downloader.assert_not_called()
        api.assert_not_called()

    def test_browser_received_response_bypasses_broken_api_and_downloader(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        config = {'cache_dir': str(self.cache), 'browser': 'chrome'}

        def audio(source, output):
            with wave.open(str(output), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 16000 * 145)

        with patch.object(pipeline.browser_cache, 'find_detail', return_value=VIDEO), \
             patch('yt_dlp.YoutubeDL') as downloader, \
             patch.object(image_posts, 'post_detail', new=AsyncMock()) as api, \
             patch.object(access, 'urlopen', return_value=Response(b'media' * 100)), \
             patch.object(pipeline, 'extract_audio', side_effect=audio), \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.prepare(args, config)
        downloader.assert_not_called()
        api.assert_not_called()
        bundle = pipeline.load_json(self.cache / VID / 'bundle.json')
        self.assertEqual(bundle['media_route'], 'chrome_received_detail')
        self.assertEqual(bundle['audio_duration_seconds'], 145)



    def test_background_detail_uses_same_matched_detail_and_download_without_browser(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        profile = self.cache / 'Chrome' / 'Default'
        profile.mkdir(parents=True)
        config = {'cache_dir': str(self.cache), 'browser': 'chrome', 'browser_profile': str(profile),
                  '_background_detail_only': True}
        detail = dict(VIDEO, author={'nickname': '作者', 'sec_uid': 'MS4wLjABAAAA_author'})

        def audio(source, output):
            with wave.open(str(output), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 16000 * 145)

        with patch('yt_dlp.YoutubeDL') as downloader, \
             patch.object(image_posts, 'post_detail', new=AsyncMock(return_value=detail)) as api, \
             patch.object(access, 'urlopen', return_value=Response(b'media' * 100)), \
             patch.object(access, 'refresh_browser_session') as browser, \
             patch.object(pipeline, 'extract_audio', side_effect=audio), \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.prepare(args, config)
        api.assert_awaited_once_with(VID, 'chrome', (self.cache / VID).resolve(), profile=str(profile))
        downloader.assert_not_called()
        browser.assert_not_called()
        bundle = pipeline.load_json(self.cache / VID / 'bundle.json')
        self.assertEqual(bundle['media_route'], 'background_detail')
        self.assertEqual(bundle['author_id'], 'MS4wLjABAAAA_author')
        self.assertEqual(bundle['audio_duration_seconds'], 145)

    def test_background_detail_miss_hands_over_to_browser_without_ytdlp(self):
        args = argparse.Namespace(source=VID, url=None, browser=None, title=None, author=None, collection=[])
        config = {'cache_dir': str(self.cache), 'browser': 'chrome', '_background_detail_only': True}
        missing = image_posts.StageError('detail', 'none', 'DETAIL_UNAVAILABLE')
        with patch('yt_dlp.YoutubeDL') as downloader, \
             patch.object(image_posts, 'post_detail', new=AsyncMock(side_effect=missing)):
            with self.assertRaises(image_posts.StageError) as caught:
                pipeline.prepare(args, config)
        self.assertEqual(caught.exception.code, 'DETAIL_UNAVAILABLE')
        downloader.assert_not_called()
        limited = image_posts.StageError('detail', '429', 'RATE_LIMITED')
        with patch.object(image_posts, 'post_detail', new=AsyncMock(side_effect=limited)):
            with self.assertRaises(image_posts.StageError) as caught:
                pipeline.prepare(args, config)
        self.assertEqual(caught.exception.code, 'RATE_LIMITED')


class TabTests(unittest.TestCase):
    def ran(self, stdout, returncode=0):
        return patch.object(access.subprocess, 'run', return_value=type(
            'Done', (), {'returncode': returncode, 'stdout': stdout})())

    def test_tab_ids_pass_work_id_as_argv_not_script_text(self):
        with patch.object(access.sys, 'platform', 'darwin'), self.ran('12\n34\n') as run:
            self.assertEqual(access.douyin_tab_ids(VID), ['12', '34'])
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], '/usr/bin/osascript')
        self.assertEqual(argv[-1], VID)
        self.assertFalse(any(VID in part for part in argv[:-1]))
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertIn('timeout', run.call_args.kwargs)

    def test_close_keeps_preexisting_tabs_and_reports_count(self):
        with patch.object(access.sys, 'platform', 'darwin'), self.ran('1\n') as run:
            self.assertEqual(access.close_new_tabs(VID, ['12', '34']), 1)
        self.assertEqual(run.call_args.args[0][-3:], [VID, '12', '34'])
        script = ' '.join(run.call_args.args[0])
        self.assertIn('keep does not contain', script)
        self.assertIn('is not running then return', script)

    def test_unknown_state_never_closes_or_raises(self):
        with patch.object(access.sys, 'platform', 'darwin'), self.ran('') as run:
            self.assertIsNone(access.close_new_tabs(VID, None))
            run.assert_not_called()
            self.assertIsNone(access.close_new_tabs(VID + ';x', []))
            self.assertIsNone(access.douyin_tab_ids('../x'))
            run.assert_not_called()
        with patch.object(access.sys, 'platform', 'darwin'), self.ran('', returncode=1):
            self.assertIsNone(access.douyin_tab_ids(VID))
        with patch.object(access.sys, 'platform', 'darwin'), \
             patch.object(access.subprocess, 'run', side_effect=access.subprocess.TimeoutExpired('osascript', 10)):
            self.assertIsNone(access.douyin_tab_ids(VID))
            self.assertIsNone(access.close_new_tabs(VID, []))
        with patch.object(access.sys, 'platform', 'linux'), self.ran('1') as run:
            self.assertIsNone(access.douyin_tab_ids(VID))
            self.assertIsNone(access.close_new_tabs(VID, []))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
