"""Read only Douyin's exact-work responses already received by Chrome.

No browser automation, cookie export or request replay. Chromium Simple Cache v5
layout: https://chromium.googlesource.com/chromium/src/+/master/net/disk_cache/simple/simple_entry_format.h
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import parse_qs, urlsplit
import zlib

INITIAL = 0xfcfb6d1ba7725c30
FINAL = 0xf4fa6f45970d41d8
MAX_FILE = 8 * 1024 * 1024
MAX_BODY = 8 * 1024 * 1024
MAX_KEY = 16384
MAX_AGE = 12 * 3600
_index = {}


def default_cache(browser):
    if browser == "chrome" and sys.platform == "darwin":
        return Path.home() / "Library/Caches/Google/Chrome/Default/Cache/Cache_Data"
    return None


def request_id(key):
    # Chrome prefixes the URL with its network-isolation key.
    parts = key.split()
    if not parts:
        return None
    url = parts[-1]
    url = re.sub(rb'^\d+/\d+/', b'', url)
    parsed = urlsplit(url.decode("ascii"))
    if (parsed.scheme != 'https' or parsed.netloc not in ('www.douyin.com', 'www-hj.douyin.com')
            or parsed.path != '/aweme/v1/web/aweme/detail/'):
        return None
    ids = parse_qs(parsed.query).get("aweme_id", [])
    return ids[0] if len(ids) == 1 and re.fullmatch(r"[0-9]{16,22}", ids[0]) else None


def decode_body(body, encoding):
    if encoding in ("gzip", "deflate"):
        decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
        decoded = decoder.decompress(body, MAX_BODY + 1)
        if len(decoded) > MAX_BODY or not decoder.eof:
            raise ValueError("Incomplete or oversized compressed cache response")
        return decoded
    if encoding == "br":
        import brotli
        decoder, chunks, size = brotli.Decompressor(), [], 0
        for offset in range(0, len(body), 4096):
            try:
                chunk = decoder.process(body[offset:offset + 4096])
            except brotli.error:
                raise ValueError("Invalid compressed cache response") from None
            size += len(chunk)
            if size > MAX_BODY:
                raise ValueError("Oversized cached response")
            chunks.append(chunk)
        if not decoder.is_finished():
            raise ValueError("Incomplete cached response")
        return b"".join(chunks)
    if encoding not in ("", "identity"):
        raise ValueError("Unsupported cache encoding")
    return body


def parse_entry(data, vid):
    if not 72 <= len(data) <= MAX_FILE:
        return None
    magic, version, key_size, _, _ = struct.unpack_from("<QIIII", data)
    if magic != INITIAL or version != 5 or not 1 <= key_size <= MAX_KEY:
        return None
    key_end = 24 + key_size
    key = data[24:key_end]
    if request_id(key) != vid:
        return None
    magic0, flags0, crc0, size0, _ = struct.unpack_from("<QIIII", data, len(data) - 24)
    if magic0 != FINAL or flags0 & ~3 or not 0 < size0 <= 65536:
        return None
    hash_size = 32 if flags0 & 2 else 0
    headers_end = len(data) - 24 - hash_size
    headers_start = headers_end - size0
    body_end = headers_start - 24
    if body_end <= key_end:
        return None
    if hash_size and hashlib.sha256(key).digest() != data[headers_end:headers_end + 32]:
        return None
    magic1, flags1, crc1, _, _ = struct.unpack_from("<QIIII", data, body_end)
    if magic1 != FINAL or flags1 & ~1:
        return None
    headers, body = data[headers_start:headers_end], data[key_end:body_end]
    if flags0 & 1 and zlib.crc32(headers) != crc0:
        return None
    if flags1 & 1 and zlib.crc32(body) != crc1:
        return None
    encoding = ""
    for header in headers.split(b"\0"):
        if header.lower().startswith(b"content-encoding:"):
            encoding = header.split(b":", 1)[1].strip().decode("ascii").lower()
    raw = json.loads(decode_body(body, encoding))
    if not isinstance(raw, dict) or raw.get("status_code") != 0:
        return None
    detail = raw.get("aweme_detail")
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != vid:
        return None
    return detail


def indexed_candidates(root, diagnostics=None):
    """Use Chromium's CRC-checked Simple Index as a locator, never as media evidence.

    Layout: simple_index_file.cc IndexMetadata and simple_index.cc EntryMetadata.
    Reading one index avoids stat() on every entry in a large browser cache.
    Every located response still passes parse_entry's exact-ID/CRC checks.
    """
    probe = diagnostics if diagnostics is not None else {}
    limit = 24 * 1000000 + 48
    try:
        with (Path(root) / 'index-dir' / 'the-real-index').open('rb') as stream:
            data = stream.read(limit + 1)
        if not 48 <= len(data) <= limit:
            raise ValueError('index size')
        payload_size, crc = struct.unpack_from('<II', data)
        magic, version, count, _, reason = struct.unpack_from('<QIQQI', data, 8)
        if (payload_size != len(data) - 8 or zlib.crc32(data[8:]) != crc
                or magic != 0x656e74657220796f or version not in (8, 9)
                or count > 1000000 or reason >= 4 or len(data) != 48 + count * 24):
            raise ValueError('index format or CRC')
        rows = list(struct.iter_unpack('<QqQ', memoryview(data)[40:-8]))
        if len({row[0] for row in rows}) != count or any(row[2] >> 38 for row in rows):
            raise ValueError('index entries')
        rows.sort(key=lambda row: row[1], reverse=True)
        probe.update(chrome_index_status='valid', chrome_index_entries=count,
                     chrome_index_candidates=min(count, 12000))
        return [str(Path(root) / f'{key:016x}_0') for key, _, _ in rows[:12000]]
    except (OSError, ValueError, struct.error):
        probe['chrome_index_status'] = 'unavailable_or_invalid'
        return []


def candidates(root, deadline=None, diagnostics=None):
    """Bound discovery once per process; batch items reuse the same index."""
    now = time.time()
    previous = _index.get(str(root))
    if previous and now - previous[0] < 5:
        if diagnostics is not None:
            diagnostics.update(previous[2], index_reused=True)
        return previous[1]
    found = []
    scan = {"scanned_entries": 0, "scan_incomplete": False, "index_reused": False}
    deadline = min(deadline, time.monotonic() + 8) if deadline is not None else time.monotonic() + 8
    with os.scandir(root) as entries:
        for ordinal, entry in enumerate(entries):
            if ordinal >= 200000 or time.monotonic() > deadline:
                scan["scan_incomplete"] = True
                break
            scan["scanned_entries"] += 1
            if not re.fullmatch(r"[0-9a-f]{16}_0", entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if 72 <= stat.st_size <= MAX_FILE and 0 <= now - stat.st_mtime <= MAX_AGE:
                found.append((stat.st_mtime, entry.path))
    found.sort(reverse=True)
    result = [path for _, path in found[:12000]]
    _index[str(root)] = (now, result, scan)
    if diagnostics is not None:
        diagnostics.update(scan)
    return result


def snapshot_entries(browser="chrome", root=None, *, timeout=2):
    """Cheap filename-only baseline before opening a work; never read cookies."""
    root = Path(root) if root is not None else default_cache(browser)
    if root is None or not root.is_dir():
        return None
    names = set()
    deadline = time.monotonic() + max(0, timeout)
    try:
        with os.scandir(root) as entries:
            for ordinal, entry in enumerate(entries):
                if ordinal >= 200000 or time.monotonic() > deadline:
                    return None  # A partial baseline must not masquerade as complete.
                if re.fullmatch(r"[0-9a-f]{16}_0", entry.name):
                    names.add(entry.name)
    except OSError:
        return None
    return names


def find_detail(vid, browser="chrome", root=None, *, timeout=12, refresh=False,
                baseline=None, diagnostics=None):
    """Return matching official detail only; never log cache keys or headers."""
    if not re.fullmatch(r"[0-9]{16,22}", str(vid)):
        return None
    root = Path(root) if root is not None else default_cache(browser)
    if root is None or not root.is_dir():
        return None
    deadline = time.monotonic() + timeout
    if refresh:
        _index.pop(str(root), None)
    probe = diagnostics if diagnostics is not None else {}
    probe.update(matched=False, new_candidates=0, checked=0, read_errors=0,
                 deadline_reached=False)

    def paths_to_read():
        # Read new responses BEFORE stat/sorting the whole browser cache. A
        # large old cache must not consume the budget before fresh media is read.
        fresh = set()
        if baseline is not None:
            current = snapshot_entries(browser, root, timeout=min(2, max(0, deadline - time.monotonic())))
            if current is not None:
                fresh = current - baseline
                probe["new_candidates"] = len(fresh)
                for name in sorted(fresh):
                    yield str(root / name)
        # A filename snapshot can time out under load. The on-disk index still
        # locates recently used entries without restarting a slow directory scan.
        indexed = indexed_candidates(root, probe)
        for path in indexed:
            if Path(path).name not in fresh:
                yield path
        seen = set(indexed)
        try:
            for path in candidates(root, deadline, probe):
                if path not in seen and Path(path).name not in fresh:
                    yield path
        except OSError:
            probe["read_errors"] += 1

    for path in paths_to_read():
        if time.monotonic() > deadline:
            probe["deadline_reached"] = True
            break
        probe["checked"] += 1
        try:
            with open(path, "rb") as stream:
                before = os.fstat(stream.fileno())
                header = stream.read(24)
                if len(header) != 24:
                    continue
                magic, version, size, _, _ = struct.unpack("<QIIII", header)
                if magic != INITIAL or version != 5 or not 1 <= size <= MAX_KEY:
                    continue
                key = stream.read(size)
                if request_id(key) != vid:
                    continue
                stream.seek(0)
                data = stream.read(MAX_FILE + 1)
                after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                continue
            detail = parse_entry(data, vid)
            if detail:
                probe["matched"] = True
                probe["matched_new_entry"] = baseline is not None and Path(path).name not in baseline
                return detail
        except (OSError, ValueError, UnicodeError, struct.error, ImportError):
            probe["read_errors"] += 1
            continue
    probe["deadline_reached"] = probe["deadline_reached"] or time.monotonic() > deadline
    return None


def wait_for_detail(vid, browser="chrome", root=None, timeout=40, *, baseline=None, diagnostics=None):
    """Wait only for local responses after one browser open, with a shared budget."""
    if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 40:
        raise ValueError("浏览器媒体响应等待上限须为 0–40 秒。")
    root = Path(root) if root is not None else default_cache(browser)
    if root is None or not root.is_dir() or not re.fullmatch(r"[0-9]{16,22}", str(vid)):
        return None
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        options = {"baseline": baseline, "diagnostics": diagnostics} if baseline is not None or diagnostics is not None else {}
        detail = find_detail(vid, browser, root, timeout=min(12, remaining), refresh=True, **options)
        if detail:
            return detail
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    return None
