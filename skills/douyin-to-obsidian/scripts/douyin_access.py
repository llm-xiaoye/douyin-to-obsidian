"""Bounded Douyin detail recovery. Credentials stay in memory."""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
import uuid

from image_posts import StageError, USER_AGENT

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
PAGE_LIMIT = 4 * 1024 * 1024
MEDIA_LIMIT = 512 * 1024 * 1024


def cookie_source(browser, profile=None):
    if profile is None:
        return (browser,)
    path = Path(profile).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise StageError("browser_profile", "配置的浏览器用户资料目录不存在。", "PROFILE_UNAVAILABLE")
    return (browser, str(path.resolve()))


def refresh_browser_session(vid, browser, wait_seconds=8, *, source_url=None, profile=None):
    """Open the exact work in the existing browser; never claim page inspection."""
    if not re.fullmatch(r"[0-9]{16,22}", str(vid)):
        raise StageError("browser_recovery", "作品 ID 格式错误。", "INVALID_ID")
    applications = {"chrome": "com.google.Chrome", "edge": "com.microsoft.edgemac",
                    "safari": "com.apple.Safari", "firefox": "org.mozilla.firefox"}
    if sys.platform != "darwin" or browser not in applications:
        raise StageError("browser_recovery", "当前宿主不能自动打开配置浏览器。", "BROWSER_UNAVAILABLE")
    if not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 20:
        raise StageError("browser_recovery", "浏览器等待时间须为 0–20 秒。", "INVALID_WAIT")
    url = f"https://www.douyin.com/video/{vid}"
    if source_url is not None:
        parsed = urlparse(source_url)
        if (parsed.scheme != "https" or parsed.netloc != "www.douyin.com"
                or parsed.path not in (f"/video/{vid}", f"/note/{vid}") or parsed.query or parsed.fragment):
            raise StageError("browser_recovery", "作品链接与 ID 不匹配。", "INVALID_URL")
        url = source_url
    profile_path = None
    if profile is not None:
        if browser != "chrome":
            raise StageError("browser_profile", "自动打开指定用户资料目前仅支持 Chrome。", "PROFILE_UNSUPPORTED")
        profile_path = Path(cookie_source(browser, profile)[1])
    process = None
    try:
        if profile_path is not None:
            # Call the binary so flags reach Chrome even when it is already running.
            # Chrome's process singleton hands the URL to the selected existing profile.
            process = subprocess.Popen([
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "--user-data-dir=" + str(profile_path.parent),
                "--profile-directory=" + profile_path.name, url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            subprocess.run(["/usr/bin/open", "-g", "-b", applications[browser], url],
                           check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StageError("browser_recovery", "自动打开浏览器失败：" + type(exc).__name__,
                         "BROWSER_OPEN_FAILED") from None
    time.sleep(wait_seconds)
    if process is not None and process.poll() not in (None, 0):
        raise StageError("browser_recovery", "Chrome 未能打开指定用户资料。", "BROWSER_OPEN_FAILED")
    return {"action": "opened_existing_browser", "browser": browser, "url": url,
            "profile": str(profile_path) if profile_path else None,
            "wait_seconds": wait_seconds, "page_state": "not_observed"}


# Chrome tab bookkeeping for browser recovery. The page is only a trigger for Chrome to
# receive the work's detail response; once the script has read it, the tab it opened is
# closed. Tabs that already showed the work before opening are never touched. The work ID
# and tab IDs are passed as argv to the run handler (no string interpolation), Chrome is
# never launched by these scripts, and every failure (no Automation permission, timeout,
# Chrome not running) is swallowed so it cannot change an item's result.
_TAB_LIST_SCRIPT = (
    "on run argv",
    "set needle to item 1 of argv",
    "set found to \"\"",
    "if application \"Google Chrome\" is not running then return \"\"",
    "tell application \"Google Chrome\"",
    "repeat with w in windows",
    "repeat with t in tabs of w",
    "set u to URL of t",
    "if u contains \"douyin.com\" and u contains needle then set found to found & (id of t as text) & linefeed",
    "end repeat",
    "end repeat",
    "end tell",
    "return found",
    "end run",
)
_TAB_CLOSE_SCRIPT = (
    "on run argv",
    "set needle to item 1 of argv",
    "set keep to rest of argv",
    "set closed to 0",
    "if application \"Google Chrome\" is not running then return \"0\"",
    "tell application \"Google Chrome\"",
    "repeat with w in windows",
    "set ts to tabs of w",
    "repeat with i from (count of ts) to 1 by -1",
    "set t to item i of ts",
    "set u to URL of t",
    "if u contains \"douyin.com\" and u contains needle and keep does not contain (id of t as text) then",
    "close t",
    "set closed to closed + 1",
    "end if",
    "end repeat",
    "end repeat",
    "end tell",
    "return closed as text",
    "end run",
)


def _chrome_tab_script(lines, args, timeout):
    argv = ["/usr/bin/osascript"]
    for line in lines:
        argv += ["-e", line]
    try:
        done = subprocess.run(argv + list(args), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not isinstance(done.stdout, str):
        return None
    return done.stdout


def douyin_tab_ids(vid, browser="chrome", *, timeout=10):
    """IDs of Chrome tabs already showing this work, or None when that cannot be known."""
    if sys.platform != "darwin" or browser != "chrome" or not re.fullmatch(r"[0-9]{16,22}", str(vid)):
        return None
    out = _chrome_tab_script(_TAB_LIST_SCRIPT, [str(vid)], timeout)
    if out is None:
        return None
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    return ids if all(re.fullmatch(r"[0-9]+", value) for value in ids) else None


def close_new_tabs(vid, before, browser="chrome", *, timeout=10):
    """Close tabs showing this work that were not open before; returns the count or None."""
    if before is None or sys.platform != "darwin" or browser != "chrome":
        return None
    if not re.fullmatch(r"[0-9]{16,22}", str(vid)) or not all(re.fullmatch(r"[0-9]+", str(v)) for v in before):
        return None
    out = _chrome_tab_script(_TAB_CLOSE_SCRIPT, [str(vid), *[str(v) for v in before]], timeout)
    try:
        return int(out.strip()) if out is not None else None
    except ValueError:
        return None


class QuietLogger:
    # Third-party exception/log strings may contain cookies or signed URLs.
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def browser_cookies(browser, profile=None):
    if not browser:
        return CookieJar()
    from yt_dlp import YoutubeDL
    with YoutubeDL({"cookiesfrombrowser": cookie_source(browser, profile), "quiet": True,
                    "no_warnings": True, "logger": QuietLogger()}) as ydl:
        return ydl.cookiejar


def error_code(exc):
    """Credential-free failure category for urllib errors and the F2 dev branch's typed APIErrors."""
    status = exc.code if isinstance(exc, HTTPError) else getattr(exc, "status_code", None)
    if type(exc).__name__ == "APIRateLimitError":
        status = status or 429
    return f"http_{status}" if isinstance(status, int) else type(exc).__name__


async def api_detail(vid, jar):
    for name in ("f2", "f2-trace"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail
    request = Request("https://www.douyin.com/aweme/v1/web/aweme/detail/")
    jar.add_cookie_header(request)
    options = {"headers": {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"},
               "cookie": request.get_header("Cookie", ""),
               "proxies": {"http://": None, "https://": None}, "timeout": 20, "max_retries": 1}
    async with DouyinCrawler(options) as crawler:
        raw = await crawler.fetch_post_detail(PostDetail(aweme_id=vid))
    if isinstance(raw, dict) and raw.get("status_code") == 0:
        return raw.get("aweme_detail")
    return None


def page_request(url, jar, user_agent):
    opener = build_opener(HTTPCookieProcessor(jar))
    with opener.open(Request(url, headers={"User-Agent": user_agent,
                                         "Referer": "https://www.douyin.com/"}), timeout=20) as response:
        if urlparse(response.url).hostname not in ("www.douyin.com", "www.iesdouyin.com"):
            raise ValueError("unexpected official-page redirect")
        body = response.read(PAGE_LIMIT + 1)
    if len(body) > PAGE_LIMIT:
        raise ValueError("page limit exceeded")
    return body.decode("utf-8")


class ScriptText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def public_share_detail(html, vid):
    """Read JSON only; never execute site JavaScript or select a recommendation."""
    parser = ScriptText()
    parser.feed(html)
    for script in parser.parts:
        marker = re.search(r"(?:window\.)?_ROUTER_DATA\s*=\s*", script)
        if not marker:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(script[marker.end():])
        except (ValueError, RecursionError):
            continue
        loaders = data.get("loaderData") if isinstance(data, dict) else None
        if not isinstance(loaders, dict):
            continue
        for page in loaders.values():
            if not isinstance(page, dict):
                continue
            info = page.get("videoInfoRes")
            items = info.get("item_list") if isinstance(info, dict) and info.get("status_code") == 0 else None
            if not isinstance(items, list):
                continue
            for item in items:
                if matched_detail(item, vid):
                    return item
    return None


def matched_detail(detail, vid):
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != vid:
        return False
    video = detail.get("video")
    if isinstance(video, dict):
        for key in ("play_addr_h264", "play_addr", "download_addr"):
            addr = video.get(key)
            urls = addr.get("url_list") if isinstance(addr, dict) else None
            if isinstance(urls, list) and any(isinstance(url, str) and url.startswith("https://") for url in urls):
                return True
    post = detail.get("image_post_info")
    rows = detail.get("images") or (post.get("images") if isinstance(post, dict) else None)
    return isinstance(rows, list) and bool(rows)


async def recover_detail(vid, browser, cache=None, *, profile=None):
    if not re.fullmatch(r"[0-9]{16,22}", str(vid)):
        raise StageError("detail", "作品 ID 格式错误。")
    vid = str(vid)
    attempts = []
    route = None

    def failure(name, exc):
        # Preserve codes, not credential-bearing exception text.
        code = error_code(exc)
        attempts.append({"route": name, "result": code})
        if code == "http_429":
            raise StageError("detail", "抖音返回 HTTP 429，已停止本条重试；稍后再处理。", "RATE_LIMITED") from None

    try:
        try:
            jar = browser_cookies(browser, profile) if profile is not None else browser_cookies(browser)
            attempts.append({"route": "browser_cookie", "result": "loaded" if browser else "anonymous"})
        except Exception as exc:
            failure("browser_cookie", exc)
            jar = CookieJar()
        for name in ("f2", "f2_after_session_refresh"):
            if name == "f2_after_session_refresh":
                try:
                    # Refresh Set-Cookie in this process; do not change Chrome's cookie DB.
                    page_request(f"https://www.douyin.com/video/{vid}", jar, USER_AGENT)
                    attempts.append({"route": "official_page_refresh", "result": "completed"})
                except Exception as exc:
                    failure("official_page_refresh", exc)
            try:
                detail = await api_detail(vid, jar)
                if matched_detail(detail, vid):
                    route = name
                    attempts.append({"route": name, "result": "matched"})
                    return detail
                attempts.append({"route": name, "result": "empty_or_mismatched"})
            except Exception as exc:
                failure(name, exc)
        try:
            html = page_request(f"https://www.iesdouyin.com/share/video/{vid}/", CookieJar(), MOBILE_UA)
            detail = public_share_detail(html, vid)
            if detail:
                route = "public_share"
                attempts.append({"route": route, "result": "matched"})
                return detail
            attempts.append({"route": "public_share", "result": "no_matching_media"})
        except Exception as exc:
            failure("public_share", exc)
        raise StageError("detail", "抖音详情接口和公开分享页均未取得匹配媒体；已重读 Cookie 并刷新请求会话。"
                         "不能仅凭空详情认定 Cookie 过期。", "DETAIL_UNAVAILABLE") from None
    finally:
        if cache is not None:
            directory = Path(cache) / "recovery"
            directory.mkdir(parents=True, exist_ok=True)
            report = {"schema_version": 1, "aweme_id": vid, "source_url": f"https://www.douyin.com/video/{vid}",
                      "at": datetime.now(timezone.utc).isoformat(), "route": route, "attempts": attempts}
            with (directory / f"{uuid.uuid4().hex}.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")


def download_video(detail, vid, cache):
    """Use only this matched work's video addresses, never its music/cover."""
    if not matched_detail(detail, vid):
        raise StageError("download", "媒体详情与本作品 ID 不匹配。")
    video = detail.get("video")
    if not isinstance(video, dict):
        raise StageError("download", "匹配的详情没有视频媒体。")
    urls = []
    for key in ("play_addr_h264", "play_addr", "download_addr"):
        addr = video.get(key)
        for url in addr.get("url_list", []) if isinstance(addr, dict) else []:
            if isinstance(url, str) and urlparse(url).scheme == "https" and url not in urls:
                urls.append(url)
    target = Path(cache) / "source-detail.mp4"
    part = target.with_suffix(".mp4.part")
    for url in urls[:3]:
        try:
            deadline = time.monotonic() + 90
            total = 0
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}), timeout=25) as response:
                length = response.headers.get("Content-Length")
                expected = int(length) if length is not None else None
                if expected is not None and not 0 < expected <= MEDIA_LIMIT:
                    raise ValueError("media length")
                if response.status != 200:
                    raise ValueError("not a full response")
                with part.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MEDIA_LIMIT or time.monotonic() > deadline:
                            raise ValueError("media budget")
                        stream.write(chunk)
            if total <= 44 or (expected is not None and total != expected):
                raise ValueError("incomplete media")
            part.replace(target)
            return target
        except Exception:
            part.unlink(missing_ok=True)
    raise StageError("download", "已取得匹配的视频详情，但其媒体地址下载失败或不完整；未生成笔记。") from None
