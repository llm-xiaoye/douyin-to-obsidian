import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import browser_cache as cache

VID = '7685326475893152115'
OTHER = '7685739185562455337'


def entry(vid=VID, response_id=VID, host='www-hj.douyin.com', encoding='br', secret='private-value'):
    key = f'1/0/_dk_https://douyin.com https://douyin.com https://{host}/aweme/v1/web/aweme/detail/?aweme_id={vid}&token={secret}'.encode()
    raw = json.dumps({'status_code': 0, 'aweme_detail': {'aweme_id': response_id,
        'desc': 'Example', 'video': {'play_addr': {'url_list': ['https://media.example/full.mp4']}}}}).encode()
    if encoding == 'br':
        import brotli
        body = brotli.compress(raw)
    elif encoding == 'gzip':
        body = gzip.compress(raw)
    else:
        body = raw
    headers = b'HTTP/1.1 200 OK\0content-encoding:' + encoding.encode() + b'\0'
    header = struct.pack('<QIIII', cache.INITIAL, 5, len(key), 0, 0)
    eof1 = struct.pack('<QIIII', cache.FINAL, 1, zlib.crc32(body), 0, 0)
    eof0 = struct.pack('<QIIII', cache.FINAL, 3, zlib.crc32(headers), len(headers), 0)
    return header + key + body + eof1 + headers + hashlib.sha256(key).digest() + eof0


class CacheTests(unittest.TestCase):
    def write_index(self, root, rows, version=9):
        payload = struct.pack('<QIQQI', 0x656e74657220796f, version, len(rows), 0, 2)
        payload += b''.join(struct.pack('<QqQ', *row) for row in rows)
        payload += struct.pack('<q', 0)
        path = root / 'index-dir' / 'the-real-index'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(struct.pack('<II', len(payload), zlib.crc32(payload)) + payload)
        return path

    def test_native_index_finds_media_without_directory_scan_or_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '0000000000000001_0').write_bytes(entry())
            (root / '0000000000000002_0').write_bytes(entry(vid=OTHER, response_id=OTHER))
            self.write_index(root, [(2, 10, 1024), (1, 20, 1024)])
            probe = {}
            with patch.object(cache, 'candidates', side_effect=AssertionError('must not stat directory')):
                result = cache.find_detail(VID, root=root, baseline=None, diagnostics=probe)
            self.assertEqual(result['aweme_id'], VID)
            self.assertEqual(probe['checked'], 1)
            self.assertEqual(probe['chrome_index_entries'], 2)

    def test_invalid_index_falls_back_and_does_not_modify_browser_files(self):
        for variant in ('crc', 'truncated', 'version', 'duplicate'):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / '0000000000000001_0').write_bytes(entry())
                rows = [(1, 20, 1024)] * (2 if variant == 'duplicate' else 1)
                path = self.write_index(root, rows, version=100 if variant == 'version' else 9)
                raw = path.read_bytes()
                if variant == 'crc':
                    raw = raw[:10] + bytes([raw[10] ^ 1]) + raw[11:]
                elif variant == 'truncated':
                    raw = raw[:-1]
                path.write_bytes(raw)
                probe = {}
                result = cache.find_detail(VID, root=root, diagnostics=probe)
                self.assertEqual(result['aweme_id'], VID)
                self.assertEqual(probe['chrome_index_status'], 'unavailable_or_invalid')
                self.assertEqual(path.read_bytes(), raw)

    def test_valid_index_never_accepts_wrong_media_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '0000000000000001_0').write_bytes(entry(response_id=OTHER))
            self.write_index(root, [(1, 20, 1024)])
            self.assertIsNone(cache.find_detail(VID, root=root))

    def test_exact_official_response_with_brotli_and_both_observed_hosts(self):
        for host in ('www.douyin.com', 'www-hj.douyin.com'):
            self.assertEqual(cache.parse_entry(entry(host=host), VID)['aweme_id'], VID)

    def test_gzip_and_identity_response(self):
        for encoding in ('gzip', 'identity'):
            self.assertEqual(cache.parse_entry(entry(encoding=encoding), VID)['aweme_id'], VID)

    def test_foreign_host_request_or_response_cannot_supply_media(self):
        for data in (entry(host='attacker.example'), entry(vid=OTHER), entry(response_id=OTHER)):
            self.assertIsNone(cache.parse_entry(data, VID))
        forged = f'https://evil.example/?next=https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={VID}'.encode()
        self.assertIsNone(cache.request_id(forged))

    def test_truncated_changed_body_key_and_version_are_rejected(self):
        original = entry()
        variants = [original[:-10], original[:50]]
        for offset in (8, 60, len(original)//2, len(original)-30):
            broken = bytearray(original)
            broken[offset] ^= 1
            variants.append(bytes(broken))
        for data in variants:
            self.assertIsNone(cache.parse_entry(data, VID))

    def test_complete_entry_is_found_without_exposing_cache_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'0123456789abcdef_0').write_bytes(entry())
            result = cache.find_detail(VID, root=root)
            self.assertEqual(result['aweme_id'], VID)
            self.assertNotIn('private-value', json.dumps(result))

    def test_oversized_decoded_body_is_rejected(self):
        old = cache.MAX_BODY
        try:
            cache.MAX_BODY = 5
            with self.assertRaises(ValueError):
                cache.parse_entry(entry(), VID)
        finally:
            cache.MAX_BODY = old

    def test_new_response_is_read_before_expensive_old_cache_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '0000000000000000_0').write_bytes(entry(vid=OTHER, response_id=OTHER))
            baseline = cache.snapshot_entries(root=root)
            (root / 'ffffffffffffffff_0').write_bytes(entry())
            probe = {}
            with patch.object(cache, 'candidates', side_effect=AssertionError('old scan must not run')):
                found = cache.find_detail(VID, root=root, baseline=baseline, diagnostics=probe)
            self.assertEqual(found['aweme_id'], VID)
            self.assertTrue(probe['matched_new_entry'])
            self.assertEqual(probe['new_candidates'], 1)
            self.assertNotIn('private-value', json.dumps(probe))

    def test_new_entry_still_requires_exact_id_crc_and_complete_body(self):
        for body in (entry(response_id=OTHER), entry()[:-10]):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                baseline = cache.snapshot_entries(root=root)
                (root / 'ffffffffffffffff_0').write_bytes(body)
                with patch.object(cache, 'candidates', return_value=[]):
                    self.assertIsNone(cache.find_detail(VID, root=root, baseline=baseline))

    def test_existing_response_remains_available_with_a_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '0123456789abcdef_0').write_bytes(entry())
            baseline = cache.snapshot_entries(root=root)
            self.assertEqual(cache.find_detail(VID, root=root, baseline=baseline)['aweme_id'], VID)

    def test_incomplete_filename_snapshot_is_not_used_as_a_complete_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '0123456789abcdef_0').write_bytes(entry())
            with patch.object(cache.time, 'monotonic', side_effect=[0, 3]):
                self.assertIsNone(cache.snapshot_entries(root=root))

    def test_late_browser_response_refreshes_previous_empty_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = [0.0]

            def arrival(seconds):
                now[0] += seconds
                (root / '0123456789abcdef_0').write_bytes(entry())

            with patch.object(cache.time, 'monotonic', side_effect=lambda: now[0]), \
                 patch.object(cache.time, 'sleep', side_effect=arrival) as waited:
                result = cache.wait_for_detail(VID, root=root, timeout=40)
            self.assertEqual(result['aweme_id'], VID)
            waited.assert_called_once_with(1)

    def test_response_wait_stops_at_budget_without_accepting_another_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '0123456789abcdef_0').write_bytes(entry(vid=OTHER, response_id=OTHER))
            now = [0.0]

            def elapsed(seconds):
                now[0] += seconds

            with patch.object(cache.time, 'monotonic', side_effect=lambda: now[0]), \
                 patch.object(cache.time, 'sleep', side_effect=elapsed):
                self.assertIsNone(cache.wait_for_detail(VID, root=root, timeout=2))
            self.assertEqual(now[0], 2)


if __name__ == '__main__':
    unittest.main()
