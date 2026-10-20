#!/usr/bin/env python3
"""Optional F2 adapter: enumerate folder names or videos in one exact folder."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from pipeline import DEFAULT_CONFIG, load_json, video_id, write_json


def parse_page(raw, mode):
    if not isinstance(raw, dict) or raw.get("status_code") != 0:
        raise ValueError("抖音未返回成功结果；需要检查登录态或 F2 接口版本。")
    key = "collects_list" if mode == "list" else "aweme_list"
    if not isinstance(raw.get(key), list) or "has_more" not in raw:
        raise ValueError("返回结构不完整；未将其当成空收藏夹。")
    rows = []
    for item in raw[key]:
        if mode == "list":
            cid = str(item.get("collects_id_str") or item.get("collects_id") or "")
            if not cid.isdigit():
                raise ValueError("收藏夹 ID 无效。")
            rows.append({"id": cid, "name": item.get("collects_name", ""),
                         "count": item.get("total_number")})
        else:
            vid = video_id(item["aweme_id"])
            is_image = bool(item.get("images") or item.get("image_post_info"))
            kind = "note" if is_image else "video"
            rows.append({"id": vid, "url": f"https://www.douyin.com/{kind}/{vid}",
                         "title": item.get("desc", ""),
                         "author": (item.get("author") or {}).get("nickname", ""),
                         "kind": "image" if is_image else "video"})
    return rows, bool(raw["has_more"]), raw.get("cursor", raw.get("max_cursor"))


async def collect(args, config):
    # F2 otherwise creates cwd/logs and may emit request diagnostics. Configure
    # its named loggers before import so auth-bearing requests never reach files.
    for name in ("f2", "f2-trace"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
    try:
        from f2.apps.douyin.crawler import DouyinCrawler
        from f2.apps.douyin.model import UserCollects, UserCollectsVideo
    except ImportError as exc:
        raise RuntimeError("尚未安装可选 F2 依赖，安装命令见 references/workflow.md。") from exc
    from yt_dlp import YoutubeDL
    browser = args.browser or config.get("browser")
    if not browser:
        raise ValueError("收藏夹需要配置已登录抖音的浏览器。")
    from douyin_access import cookie_source, QuietLogger
    profile = config.get("browser_profile") if browser == config.get("browser") else None
    with YoutubeDL({"cookiesfrombrowser": cookie_source(browser, profile), "quiet": True,
                    "no_warnings": True, "logger": QuietLogger()}) as ydl:
        cookies = [c for c in ydl.cookiejar if c.domain.lstrip(".") in ("douyin.com", "www.douyin.com")]
    if not any(c.name in ("sessionid", "sessionid_ss", "sid_guard") for c in cookies):
        raise ValueError("浏览器没有抖音账号登录态；公开视频可下载不代表能读取私人收藏夹。")
    kwargs = {
        "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                    "Referer": "https://www.douyin.com/"},
        "cookie": "; ".join(f"{c.name}={c.value}" for c in cookies),
        "proxies": {"http://": None, "https://": None}, "timeout": 20,
        "max_retries": 1,
    }
    rows, seen, cursors = [], set(), {args.cursor}
    cursor, complete, reason = args.cursor, False, "page_limit"
    try:
        async with DouyinCrawler(kwargs) as crawler:
            for _ in range(args.max_pages):
                if args.mode == "list":
                    raw = await crawler.fetch_user_collects(UserCollects(cursor=cursor, count=20))
                else:
                    raw = await crawler.fetch_user_collects_video(UserCollectsVideo(cursor=cursor, count=20, collects_id=args.id))
                page, has_more, next_cursor = parse_page(raw, args.mode)
                for row in page:
                    if row["id"] not in seen:
                        rows.append(row)
                        seen.add(row["id"])
                if not has_more:
                    complete, reason = True, None
                    break
                if next_cursor is None or next_cursor in cursors:
                    reason = "cursor_not_advancing"
                    break
                cursor = next_cursor
                cursors.add(cursor)
                await asyncio.sleep(2)
    except Exception as exc:
        # Retain already enumerated IDs while avoiding credentials in exception text.
        # The F2 dev branch raises typed APIErrors carrying status_code (e.g. 429).
        from douyin_access import error_code
        reason = f"request_failed:{error_code(exc)}"
    pagination_exhausted = complete
    if complete and args.expected_count is not None and len(rows) != args.expected_count:
        complete, reason = False, "reported_count_mismatch"
    result = {"mode": args.mode, "collection_id": args.id, "complete": complete,
              "pagination_exhausted": pagination_exhausted, "expected_count": args.expected_count,
              "reason": reason, "next_cursor": None if pagination_exhausted else cursor, "items": rows}
    write_json(args.output, result)
    print(json.dumps({"output": str(Path(args.output).resolve()), "count": len(rows),
                      "complete": complete, "expected_count": args.expected_count,
                      "reason": reason}, ensure_ascii=False))
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["list", "videos"])
    parser.add_argument("--id", help="指定收藏夹 ID；videos 模式必填")
    parser.add_argument("--browser", choices=["chrome", "edge", "firefox", "safari"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", required=True)
    parser.add_argument("--cursor", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--expected-count", type=int, help="收藏夹清单显示的作品数；数量不符时报告不完整")
    args = parser.parse_args()
    if args.mode == "videos" and (not args.id or not args.id.isdigit()):
        parser.error("videos 必须提供 --id，不能默认枚举全部收藏。")
    if not 1 <= args.max_pages <= 1000:
        parser.error("--max-pages 必须在 1–1000 之间。")
    if args.expected_count is not None and args.expected_count < 0:
        parser.error("--expected-count 不得为负数。")
    try:
        return asyncio.run(collect(args, load_json(args.config)))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
