"""Douyin image post extraction and local Apple Vision OCR; no music ASR."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


class StageError(RuntimeError):
    def __init__(self, stage, message, code=None):
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}: {message}")


async def post_detail(vid, browser, cache=None, *, profile=None):
    from douyin_access import recover_detail
    return await recover_detail(vid, browser, cache, profile=profile)


def image_rows(detail):
    rows = detail.get("images") or (detail.get("image_post_info") or {}).get("images") or []
    if not isinstance(rows, list):
        raise StageError("images", "图片列表格式异常。")
    return rows


def cached_valid(bundle, cache):
    images = bundle.get("images") or []
    if not images or len(images) != bundle.get("image_count"):
        return False
    for i, entry in enumerate(images, 1):
        path = cache / entry["file"]
        if entry.get("page") != i or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            return False
    return True


def download_images(detail, cache):
    from pipeline import write_json
    from PIL import Image
    rows = image_rows(detail)
    if not rows:
        raise StageError("images", "详情没有完整图片列表；不能把标题或配乐当作图文内容。")
    images = []
    directory = cache / "images"
    directory.mkdir(exist_ok=True)
    for page, row in enumerate(rows, 1):
        urls = row.get("url_list") or (row.get("display_image") or {}).get("url_list") or []
        if not urls:
            raise StageError("images", f"第 {page} 页缺少图片地址。")
        path = directory / f"{page:03d}.image"
        success = False
        for url in urls[:3]:
            if urlparse(url).scheme not in ("https", "http"):
                continue
            try:
                with urlopen(Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}), timeout=30) as response:
                    data = response.read(40 * 1024 * 1024 + 1)
                if len(data) > 40 * 1024 * 1024:
                    raise ValueError("image too large")
                path.write_bytes(data)
                with Image.open(path) as im:
                    width, height, fmt = im.width, im.height, im.format
                    im.verify()
                suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(fmt, ".image")
                final = path.with_suffix(suffix)
                path.replace(final)
                original = final
                # Apple Vision decodes the original supported JPEG/PNG. Convert
                # unsupported containers locally without modifying their content.
                if fmt not in ("JPEG", "PNG"):
                    final = path.with_suffix(".png")
                    with Image.open(path.with_suffix(suffix)) as im:
                        im.convert("RGB").save(final)
                images.append({"page": page, "file": final.relative_to(cache).as_posix(),
                               "sha256": hashlib.sha256(final.read_bytes()).hexdigest(), "width": width, "height": height,
                               "original_file": original.relative_to(cache).as_posix(),
                               "original_sha256": hashlib.sha256(original.read_bytes()).hexdigest()})
                success = True
                break
            except Exception:
                continue
        if not success:
            path.unlink(missing_ok=True)
            raise StageError("images", f"第 {page}/{len(rows)} 页下载或解码失败，保留缓存，未将其标记为完整。")
    vid = str(detail["aweme_id"])
    author = detail.get("author") or {}
    bundle = {"schema_version": 2, "aweme_id": vid, "source_url": f"https://www.douyin.com/note/{vid}",
              "media_kind": "image", "title": detail.get("desc") or "图文知识",
              "caption": detail.get("desc") or "", "author": author.get("nickname") or "未知作者",
              "author_id": author.get("sec_uid") if isinstance(author.get("sec_uid"), str) and author.get("sec_uid") else None,
              "published_at": datetime.fromtimestamp(detail["create_time"], timezone.utc).isoformat() if detail.get("create_time") else None,
              "downloaded_at": datetime.now(timezone.utc).isoformat(), "image_count": len(images), "images": images}
    write_json(cache / "bundle.json", bundle)
    return bundle


def prepare_images(vid, browser, cache, detail=None, *, profile=None):
    detail = detail or asyncio.run(post_detail(vid, browser, cache, profile=profile))
    return download_images(detail, cache)


def validate_evidence(raw, bundle, cache):
    if not cached_valid(bundle, cache) or str(raw.get("aweme_id")) != str(bundle["aweme_id"]):
        raise StageError("evidence", "图片证据与作品不匹配或缓存已被修改。")
    pages = raw.get("pages") or []
    if len(pages) != bundle["image_count"]:
        raise StageError("evidence", "图片识别页数不完整。")
    for page, image in zip(pages, bundle["images"]):
        if page.get("page") != image["page"] or page.get("sha256") != image["sha256"]:
            raise StageError("evidence", "图片识别顺序或内容指纹不匹配。")
    return raw


def extract(bundle_path):
    from pipeline import load_json, write_json
    bundle_path = Path(bundle_path)
    cache = bundle_path.parent
    bundle = load_json(bundle_path)
    output = cache / "transcript.json"
    if not cached_valid(bundle, cache):
        raise StageError("ocr", "图片缓存不完整，请重新 prepare。")
    if output.exists():
        raw = load_json(output)
        if raw.get("source_kind") == "image":
            try:
                validate_evidence(raw, bundle, cache)
                return {"status": "cached", "transcript": str(output), "pages": len(raw["pages"])}
            except StageError:
                pass
    script = Path(__file__).with_name("ocr_images.swift")
    paths = [str(cache / entry["file"]) for entry in bundle["images"]]
    try:
        process = subprocess.run(["xcrun", "swift", str(script), *paths], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageError("ocr", "无法运行本地 Apple Vision；需要 macOS 和 Xcode Command Line Tools。") from exc
    if process.returncode:
        raise StageError("ocr", f"Apple Vision 识别失败：{process.stderr[-800:]}")
    raw = json.loads(process.stdout)
    pages = raw["pages"]
    for page, image in zip(pages, bundle["images"]):
        page["sha256"] = image["sha256"]
    raw.update({"source_kind": "image", "aweme_id": bundle["aweme_id"], "caption": bundle.get("caption", ""),
                "text": "\n\n".join(f"第 {p['page']} 页\n{p['text']}" for p in pages),
                "extracted_at": datetime.now(timezone.utc).isoformat(), "segments": []})
    validate_evidence(raw, bundle, cache)
    write_json(output, raw)
    return {"status": "extracted", "transcript": str(output), "pages": len(pages),
            "characters": sum(len(p["text"]) for p in pages),
            "empty_pages": [p["page"] for p in pages if not p["text"].strip()]}
