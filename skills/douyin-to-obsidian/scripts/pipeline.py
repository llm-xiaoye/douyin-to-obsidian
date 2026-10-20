#!/usr/bin/env python3
"""Download one Douyin item, transcribe its audio locally, and save a knowledge note.

The calling agent writes the summary from transcript.json; this script never invents
a summary and never sends audio to a cloud transcription service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import wave

import library
import image_posts
import douyin_access
import browser_cache
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "config.json"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        temp = Path(f.name)
    os.replace(temp, path)


def video_id(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{16,22}", value):
        raise ValueError("视频 ID 必须是 16–22 位数字字符串。")
    return value


def normalize_url(text):
    if re.fullmatch(r"[0-9]{16,22}", text.strip()):
        return f"https://www.douyin.com/video/{text.strip()}", text.strip()
    match = re.search(r"https?://[^\s<>\"，。()\[\]]+", text)
    if not match:
        raise ValueError("请提供抖音视频链接、分享文本或视频 ID。")
    url = match.group(0).rstrip(".,;!！）)")
    parsed = urlparse(url)
    hosts = {"douyin.com", "www.douyin.com", "v.douyin.com", "jingxuan.douyin.com",
             "iesdouyin.com", "www.iesdouyin.com"}
    if parsed.hostname not in hosts:
        raise ValueError("仅接受抖音官方域名的视频链接。")
    if parsed.hostname == "v.douyin.com":
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=25) as response:
            url = response.url
        parsed = urlparse(url)
        if parsed.hostname not in hosts:
            raise ValueError("短链跳转到了非抖音域名。")
    match = re.search(r"/(?:share/)?(?:video|note)/(\d+)", parsed.path)
    query = parse_qs(parsed.query)
    found = match.group(1) if match else (query.get("modal_id") or query.get("aweme_id") or [""])[0]
    vid = video_id(found)
    kind = "note" if "/note/" in parsed.path else "video"
    return f"https://www.douyin.com/{kind}/{vid}", vid


def ffmpeg_path():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("需要 ffmpeg，或在当前 Python 环境安装 imageio-ffmpeg。") from exc


def extract_audio(source, output):
    """Decode only the first real audio stream; ignore video frames and metadata."""
    subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", str(source), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)],
                   check=True, timeout=600)
    if not Path(output).is_file() or Path(output).stat().st_size <= 44:
        raise RuntimeError("没有提取到可转写的音轨。")


def validate_audio(path, expected_seconds=None):
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise image_posts.StageError("extract", "音轨格式不完整。", "INVALID_AUDIO")
        seconds = audio.getnframes() / audio.getframerate()
        if seconds <= 0:
            raise image_posts.StageError("extract", "没有取得音轨。", "EMPTY_AUDIO")
    if (isinstance(expected_seconds, (int, float)) and expected_seconds > 0
            and abs(seconds - expected_seconds) > max(3, expected_seconds * .02)):
        raise image_posts.StageError("extract", "下载音轨时长与作品时长不符，停止分析。", "INCOMPLETE_AUDIO")
    return seconds


def prepare(args, config):
    local = Path(args.source).expanduser()
    try:
        is_local = local.is_file()
    except OSError:
        is_local = False
    url, vid = normalize_url(args.url if is_local and args.url else args.source)
    cache = Path(config["cache_dir"]).expanduser().resolve() / vid
    cache.mkdir(parents=True, exist_ok=True)
    bundle_path = cache / "bundle.json"
    if bundle_path.exists() and not is_local:
        bundle = load_json(bundle_path)
        cached_audio = cache / "audio.wav"
        if bundle.get("media_kind") == "image" and bundle.get("aweme_id") == vid and image_posts.cached_valid(bundle, cache):
            print(json.dumps({"status": "cached", "bundle": str(bundle_path), "media_kind": "image"}, ensure_ascii=False))
            return
        if (bundle.get("aweme_id") == vid and cached_audio.is_file()
                and bundle.get("audio_sha256") == hashlib.sha256(cached_audio.read_bytes()).hexdigest()):
            bundle["audio_duration_seconds"] = validate_audio(cached_audio, bundle.get("duration_seconds"))
            if args.title:
                bundle["title"] = args.title
            if args.author:
                bundle["author"] = args.author
            if args.collection:
                bundle["collections"] = sorted(set(bundle.get("collections", []) + args.collection))
            write_json(bundle_path, bundle)
            print(json.dumps({"status": "cached", "bundle": str(bundle_path)}, ensure_ascii=False))
            return
    browser = args.browser if args.browser is not None else config.get("browser")
    profile = config.get("browser_profile") if browser == config.get("browser") else None
    cached_detail = None
    background_detail = False
    if not is_local:
        cache_options = {}
        if "_browser_cache_baseline" in config or "_browser_cache_diagnostics" in config:
            cache_options = {"baseline": config.get("_browser_cache_baseline"),
                             "diagnostics": config.get("_browser_cache_diagnostics")}
        detail = browser_cache.find_detail(vid, browser, config.get("browser_cache_dir"), **cache_options)
        if detail is None and config.get("_browser_media_wait_seconds", 0):
            detail = browser_cache.wait_for_detail(
                vid, browser, config.get("browser_cache_dir"), config["_browser_media_wait_seconds"],
                **cache_options)
        cached_detail = detail if detail and douyin_access.matched_detail(detail, vid) else None
        if not cached_detail and config.get("_browser_media_only"):
            raise image_posts.StageError("detail", "Chrome 尚未提供此作品的匹配媒体响应，需要打开作品后读取。",
                                         "DETAIL_UNAVAILABLE")
        if not cached_detail and config.get("_background_detail_only"):
            import asyncio
            try:
                # recover_detail returns only an ID-matched detail (F2 with the pinned profile's
                # cookies, in-memory session refresh, public share page).
                cached_detail = asyncio.run(image_posts.post_detail(vid, browser, cache, profile=profile))
            except image_posts.StageError as exc:
                if exc.code != "DETAIL_UNAVAILABLE":
                    raise
                raise image_posts.StageError("detail", "后台请求未取得匹配详情，需要打开作品后读取。",
                                             "DETAIL_UNAVAILABLE") from None
            background_detail = True
    if not is_local and ("/note/" in url or (cached_detail and image_posts.image_rows(cached_detail))):
        bundle = image_posts.prepare_images(vid, browser, cache, cached_detail, profile=profile)
        bundle["collections"] = args.collection or []
        write_json(bundle_path, bundle)
        print(json.dumps({"status": "prepared", "bundle": str(bundle_path), "media_kind": "image", "pages": bundle["image_count"]}, ensure_ascii=False))
        return
    metadata = {}
    if is_local:
        source = local.resolve()
    else:
        from yt_dlp import YoutubeDL
        options = {
            "format": "bestaudio/best", "outtmpl": str(cache / "source.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "retries": 1, "extractor_retries": 1, "socket_timeout": 25,
            "overwrites": False, "restrictfilenames": True,
            "logger": douyin_access.QuietLogger(),
        }
        browser = args.browser if args.browser is not None else config.get("browser")
        if browser:
            options["cookiesfrombrowser"] = douyin_access.cookie_source(browser, profile)
        try:
            detail = cached_detail
            if detail and not image_posts.image_rows(detail):
                source = douyin_access.download_video(detail, vid, cache)
                metadata = detail_metadata(detail)
                metadata["media_route"] = "background_detail" if background_detail else "chrome_received_detail"
            else:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info or str(info.get("id")) != vid:
                        raise ValueError("下载器返回的视频 ID 与指定视频不一致。")
                    info = ydl.process_ie_result(info, download=True)
                    source = Path(ydl.prepare_filename(info))
                    metadata = {"title": info.get("title"), "author": info.get("creator") or info.get("uploader"),
                                "duration_seconds": info.get("duration"), "media_route": "yt_dlp",
                                "published_at": (datetime.fromtimestamp(info["timestamp"], timezone.utc).isoformat()
                                                 if info.get("timestamp") else None)}
        except image_posts.StageError:
            # A matched video's download failure is not a missing-detail failure.
            raise
        except Exception:
            # A numeric ID or modal URL can also identify an image post.
            import asyncio
            detail = asyncio.run(image_posts.post_detail(vid, browser, cache, profile=profile))
            if image_posts.image_rows(detail):
                bundle = image_posts.prepare_images(vid, browser, cache, detail)
                bundle["collections"] = args.collection or []
                write_json(bundle_path, bundle)
                print(json.dumps({"status": "prepared", "bundle": str(bundle_path), "media_kind": "image", "pages": bundle["image_count"]}, ensure_ascii=False))
                return
            source = douyin_access.download_video(detail, vid, cache)
            metadata = detail_metadata(detail)
            metadata["media_route"] = "detail_recovery"
    audio = cache / "audio.wav"
    extract_audio(source, audio)
    audio_seconds = validate_audio(audio, metadata.get("duration_seconds"))
    bundle = {"schema_version": 2, "media_kind": "video", "aweme_id": vid, "source_url": url,
              "title": args.title or metadata.get("title") or f"抖音视频 {vid}",
              "author": args.author or metadata.get("author") or "未知作者",
              # Only a verified detail sec_uid; the yt-dlp route leaves it unknown.
              "author_id": metadata.get("author_id"),
              "published_at": metadata.get("published_at"),
              "duration_seconds": metadata.get("duration_seconds"),
              "audio_duration_seconds": audio_seconds,
              "media_route": metadata.get("media_route", "local_file"),
              "collections": args.collection or [], "audio_file": "audio.wav",
              "downloaded_at": datetime.now(timezone.utc).isoformat(),
              "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}
    write_json(bundle_path, bundle)
    print(json.dumps({"status": "prepared", "bundle": str(bundle_path),
                      "title": bundle["title"], "audio_bytes": audio.stat().st_size}, ensure_ascii=False))


def detail_metadata(detail):
    duration_ms = (detail.get("video") or {}).get("duration")
    author = detail.get("author") or {}
    sec_uid = author.get("sec_uid")
    return {"title": detail.get("desc"), "author": author.get("nickname"),
            "author_id": sec_uid if isinstance(sec_uid, str) and sec_uid else None,
            "duration_seconds": duration_ms / 1000 if isinstance(duration_ms, (int, float)) else None,
            "published_at": (datetime.fromtimestamp(detail["create_time"], timezone.utc).isoformat()
                             if detail.get("create_time") else None)}


def validate_transcript(raw):
    text = str(raw.get("text", "")).strip()
    if not text:
        raise ValueError("转写为空，不创建成功笔记。")
    segments = []
    for segment in raw.get("segments", []):
        start, end = float(segment["start"]), float(segment["end"])
        if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end < start:
            raise ValueError("转写时间戳无效。")
        content = str(segment.get("text", "")).strip()
        if content:
            segments.append({"start": start, "end": end, "text": content})
    return {"text": text, "segments": segments, "language": raw.get("language", "zh")}


def local_asr_model(model):
    """Prefer an already-downloaded Hugging Face snapshot so ASR works offline.

    mlx_whisper otherwise calls snapshot_download online on every run, which fails
    (e.g. SSLError / timeout) whenever huggingface.co is unreachable even though the
    model is cached. Falls back to the repo id so a first-time download still works.
    """
    if Path(model).expanduser().exists():
        return str(Path(model).expanduser())
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(repo_id=model, local_files_only=True)
    except Exception:
        return model


def transcribe(args, config):
    bundle_path = Path(args.bundle).expanduser().resolve()
    bundle = load_json(bundle_path)
    if bundle.get("media_kind") == "image":
        print(json.dumps(image_posts.extract(bundle_path), ensure_ascii=False))
        return
    audio = bundle_path.parent / "audio.wav"
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    model = args.model or config["asr_model"]
    output = bundle_path.parent / "transcript.json"
    if output.exists():
        old = load_json(output)
        if old.get("audio_sha256") == digest and old.get("asr_model") == model:
            validate_transcript(old)
            print(json.dumps({"status": "cached", "transcript": str(output)}, ensure_ascii=False))
            return
    import mlx_whisper
    import numpy as np
    import wave
    with wave.open(str(audio), "rb") as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError("请先 prepare，转为 16kHz 单声道 PCM。")
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    raw = mlx_whisper.transcribe(samples, path_or_hf_repo=local_asr_model(model), language="zh",
                                 task="transcribe", verbose=False, condition_on_previous_text=False)
    result = validate_transcript(raw)
    result.update({"aweme_id": video_id(bundle["aweme_id"]), "asr_model": model,
                   "audio_sha256": digest, "transcribed_at": datetime.now(timezone.utc).isoformat()})
    write_json(output, result)
    print(json.dumps({"status": "transcribed", "transcript": str(output),
                      "characters": len(result["text"]), "segments": len(result["segments"])}, ensure_ascii=False))


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("输出路径必须位于指定知识库内。")
    return path


def timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def find_existing_note(vault, vid, notes_dir="抖音"):
    return library.find_existing(vault, video_id(vid))


def save(args, config):
    bundle_path = Path(args.bundle).expanduser().resolve()
    bundle = load_json(bundle_path)
    vid = video_id(bundle["aweme_id"])
    _, source_vid = normalize_url(bundle["source_url"])
    if source_vid != vid:
        raise ValueError("来源链接与作品 ID 不一致。")
    raw = load_json(bundle_path.parent / "transcript.json")
    analysis_file = getattr(args, "analysis", None)
    if not analysis_file:
        raise ValueError("需要 --analysis <analysis.json> 提供文章标题、分类、标签和核心洞察。")
    analysis_data = load_json(analysis_file)
    analysis = library.validate_analysis(analysis_data)
    images = []
    if bundle.get("media_kind") == "image":
        image_posts.validate_evidence(raw, bundle, bundle_path.parent)
        reviewed = load_json(analysis_file).get("reviewed_pages")
        expected = list(range(1, bundle["image_count"] + 1))
        if reviewed != expected:
            raise ValueError("图文需逐页读取图片并核对 OCR，在 reviewed_pages 中记录完整页码后才能保存。")
        from note_assets import evidence_images
        images = evidence_images(bundle, bundle_path.parent)
    else:
        validate_transcript(raw)
        audio = bundle_path.parent / "audio.wav"
        if raw.get("aweme_id") != vid or raw.get("audio_sha256") != hashlib.sha256(audio.read_bytes()).hexdigest():
            raise ValueError("转写与作品或音频不匹配。")
    body = Path(args.summary).read_text(encoding="utf-8").strip()
    if not body or body.startswith("---"):
        raise ValueError("knowledge.md 需要知识正文，不含 YAML。")
    validate_learning_review(analysis_data, body)
    effective = dict(config, vault=str(Path(args.vault or config["vault"]).expanduser().resolve()))
    if not Path(effective["vault"]).is_dir():
        raise ValueError("知识库目录不存在。")
    if getattr(args, "command", None) == "revise":
        digest = hashlib.sha256((bundle_path.parent / "transcript.json").read_bytes()).hexdigest()
        if digest != args.expected_evidence_sha256:
            raise ValueError("原材料在审阅后发生变化，请重新核对，未修订。")
        result = library.revise_note(effective, vid, analysis, body, args.expected_note_sha256,
                                     images=images, source_url=bundle["source_url"])
    else:
        # New notes use the shared skeleton (explicit revisions of old notes keep their layout).
        validate_note_skeleton(body, analysis["title"], analysis_data)
        author = {"nickname": bundle.get("author"), "sec_uid": bundle.get("author_id")}
        result = library.save_note(effective, vid, analysis, body, args.collection or bundle.get("collections", []),
                                   images=images, source_url=bundle["source_url"], author=author)
    # Explicit opt-in archive stays separate from the knowledge prose.
    if getattr(args, "archive_source", False) and result["written"]:
        attachment_dir = library.inside(Path(effective["vault"]), config.get("attachments_dir", "附件"))
        attachment_dir.mkdir(parents=True, exist_ok=True)
        originals = [bundle_path.parent / "transcript.json"]
        if bundle.get("media_kind") == "image":
            originals += [bundle_path.parent / entry["file"] for entry in bundle["images"]]
        else:
            originals += [bundle_path.parent / "audio.wav"]
        for original in originals:
            dest = attachment_dir / (vid + "-" + original.name)
            if not dest.exists():
                with dest.open("xb") as target:
                    target.write(original.read_bytes())
            elif dest.read_bytes() != original.read_bytes():
                raise ValueError("附件内容冲突，未覆盖；知识笔记已保存。")
        result["source_archived"] = True
    print(json.dumps(result, ensure_ascii=False))


def review(args, config):
    """Persist the model's follow-up audit after checking work/evidence identity."""
    bundle_path = Path(args.bundle).expanduser().resolve()
    bundle = load_json(bundle_path)
    vid = video_id(bundle["aweme_id"])
    _, source_vid = normalize_url(bundle["source_url"])
    if source_vid != vid:
        raise ValueError("来源链接与作品 ID 不一致。")
    transcript = bundle_path.parent / "transcript.json"
    transcript_bytes = transcript.read_bytes()
    digest = hashlib.sha256(transcript_bytes).hexdigest()
    if digest != args.expected_evidence_sha256:
        raise ValueError("原材料在复审后发生变化，未登记结论。")
    raw = json.loads(transcript_bytes)
    report = load_json(args.review_file)
    is_image = bundle.get("media_kind") == "image"
    from review_log import validate_report
    validate_report(report, is_image)
    if is_image:
        image_posts.validate_evidence(raw, bundle, bundle_path.parent)
        if report.get("reviewed_pages") != list(range(1, bundle["image_count"] + 1)):
            raise ValueError("图文复审必须实际查看全部原图，并登记完整 reviewed_pages。")
        from note_assets import evidence_images
        evidence_images(bundle, bundle_path.parent)
    else:
        validate_transcript(raw)
        audio = bundle_path.parent / "audio.wav"
        if raw.get("aweme_id") != vid or raw.get("audio_sha256") != hashlib.sha256(audio.read_bytes()).hexdigest():
            raise ValueError("转写与作品或音频不匹配。")
    effective = dict(config, vault=str(Path(args.vault or config["vault"]).expanduser().resolve()))
    result = library.record_review(effective, vid, report, args.expected_note_sha256,
                                   digest, bundle["source_url"], is_image=is_image)
    print(json.dumps(result, ensure_ascii=False))


def markdown_blocks(body):
    """Headings outside fenced code: [(level, title, [content lines])]; preamble level 0."""
    blocks, fence = [(0, "", [])], None
    for line in body.splitlines():
        mark = re.match(r"^\s*(`{3,}|~{3,})", line)
        if mark:
            if fence is None:
                fence = mark[1][0]
            elif mark[1][0] == fence:
                fence = None
            blocks[-1][2].append(line)
            continue
        heading = None if fence else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            blocks.append((len(heading[1]), heading[2].strip(), []))
        else:
            blocks[-1][2].append(line)
    return blocks


SKELETON_SUBSECTIONS = ("补充知识", "纠错与边界", "模型判断")


def _skeleton_key(value):
    import unicodedata
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value.translate(str.maketrans("“”‘’", '\"\"\'\''))).strip()


def validate_note_skeleton(body, title, analysis_data=None):
    """New knowledge-note layout shared with dy-author (see references/knowledge-notes.md)."""
    text = re.sub(r"\A# [^\n]+\n*", "", body.strip()).strip()
    blocks = markdown_blocks(text)
    if any(level == 1 for level, _, _ in blocks):
        raise ValueError("正文不要再写一级标题，脚本会按 title 生成。")
    first = next((line for line in blocks[0][2] if line.strip()), "")
    if not first.startswith("> ") or not first[2:].strip():
        raise ValueError("正文第一行需要一句话结论，格式为“> 结论”。")
    h2 = [(i, name) for i, (level, name, _) in enumerate(blocks) if level == 2]
    names = [_skeleton_key(name) for _, name in h2]
    if _skeleton_key(title) in names:
        raise ValueError("不要写与文章标题同名的二级标题。")
    if _skeleton_key("我的补充") in names:
        raise ValueError("正文不要包含“我的补充”，脚本会添加并保留。")
    for required in ("核心要点", "模型补充", "自测"):
        if names.count(required) != 1:
            raise ValueError(f"正文需要且只能有一个“## {required}”小节。")
    core, extra, test = (names.index(k) for k in ("核心要点", "模型补充", "自测"))
    if not core < extra < test:
        raise ValueError("小节顺序应为：核心要点 → 正文小节 → 模型补充 → 自测。")
    if extra - core < 2:
        raise ValueError("核心要点与模型补充之间至少需要一个按机制组织的正文小节。")
    if test != len(names) - 1:
        raise ValueError("“自测”应是最后一个二级小节。")

    def section(position):
        start = h2[position][0]
        end = h2[position + 1][0] if position + 1 < len(h2) else len(blocks)
        return blocks[start:end]

    items = [line for line in section(core)[0][2] if re.match(r"^(?:[-*+]|\d+[.)])\s+\S", line)]
    if not 3 <= len(items) <= 5 or any(level == 3 for level, _, _ in section(core)[1:]):
        raise ValueError("“核心要点”需要 3–5 条顶层列表项。")
    subs = {_skeleton_key(name): lines for level, name, lines in section(extra)[1:] if level == 3}
    for name in SKELETON_SUBSECTIONS:
        if name not in subs:
            raise ValueError(f"“模型补充”下需要“### {name}”。")
        if not any(line.strip() for line in subs[name]):
            raise ValueError(f"“### {name}”不能为空。")
    if not any(line.strip() for _, _, lines in section(test) for line in lines):
        raise ValueError("“自测”需要题目和可核对的答案。")
    if analysis_data is not None:
        claims = (analysis_data.get("learning_review") or {}).get("key_claims") or []
        if not any(isinstance(c, dict) and _skeleton_key(str(c.get("section", ""))) == "模型判断" for c in claims):
            raise ValueError("learning_review.key_claims 至少一条需对应“模型判断”小节。")


def validate_learning_review(data, body):
    """Require an inspectable review, not an automatic score of semantic quality."""
    review = data.get("learning_review")
    if not isinstance(review, dict) or not isinstance(review.get("learning_goal"), str) or not review["learning_goal"].strip():
        raise ValueError("缺少 learning_review.learning_goal，请先按知识笔记规范完成学习质量审阅。")
    claims = review.get("key_claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("learning_review.key_claims 需记录核心结论、证据和对应正文小节。")
    headings = [m[1].strip() for m in re.finditer(r"^#{2,6}\s+(.+)$", body, re.M)]
    def section_key(value):
        import unicodedata
        value = unicodedata.normalize("NFKC", value)
        return re.sub(r"\s+", " ", value.translate(str.maketrans("“”‘’", '\"\"\'\''))).strip()
    for claim in claims:
        if not isinstance(claim, dict) or any(not isinstance(claim.get(k), str) or not claim[k].strip() for k in ("claim", "evidence", "section")):
            raise ValueError("每条 key_claims 需要非空 claim、evidence、section。")
        if claim["section"] not in headings:
            key = section_key(claim["section"])
            matches = [h for h in headings if section_key(h) == key]
            if not matches:
                matches = [h for h in headings if section_key(h).split(":", 1)[0] == key]
            if len(matches) != 1:
                raise ValueError("核心结论的 section 未唯一匹配正文标题，请使用：" + json.dumps(headings[:30], ensure_ascii=False))
            claim["section"] = matches[0]
    if not isinstance(review.get("application"), str) or not review["application"].strip() or not isinstance(review.get("uncertainties"), list):
        raise ValueError("learning_review 需记录 application 和 uncertainties 数组。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="下载指定视频或处理本地媒体，提取真实音轨")
    p.add_argument("source", help="抖音 URL/分享文本/ID，或本地视频路径")
    p.add_argument("--url", help="本地媒体对应的抖音原链接")
    p.add_argument("--browser", choices=["chrome", "edge", "firefox", "safari", ""], default=None)
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--collection", action="append")
    p = commands.add_parser("transcribe", aliases=["extract"], help="视频本地 ASR；图文本地 OCR")
    p.add_argument("bundle")
    p.add_argument("--model")
    p = commands.add_parser("save", help="将经整理的知识正文与原图保存到 Obsidian")
    p.add_argument("bundle")
    p.add_argument("--knowledge-file", "--summary", dest="summary", required=True,
                   help="根据 transcript.json 提炼的独立知识正文，解释概念、原理和方法")
    p.add_argument("--analysis", required=True, help="包含 title、category、tags、insight；图文还需 reviewed_pages")
    p.add_argument("--vault")
    p.add_argument("--title")
    p.add_argument("--collection", action="append")
    p.add_argument("--tag", action="append")
    p.add_argument("--archive-source", action="store_true", help="同时归档音频和完整转写，适用于用户提供的媒体")
    p = commands.add_parser("revise", help="用户明确要求时修订已有正文，备份并保留个人补充；不用于普通增量导入")
    p.add_argument("bundle")
    p.add_argument("--knowledge-file", dest="summary", required=True)
    p.add_argument("--analysis", required=True)
    p.add_argument("--expected-note-sha256", required=True)
    p.add_argument("--expected-evidence-sha256", required=True)
    p.add_argument("--vault")
    p.set_defaults(collection=None, archive_source=False)
    p = commands.add_parser("review", help="完成内容复审后登记结果、时间和文章版本；不改正文")
    p.add_argument("bundle")
    p.add_argument("--review-file", required=True)
    p.add_argument("--expected-note-sha256", required=True)
    p.add_argument("--expected-evidence-sha256", required=True)
    p.add_argument("--vault")
    args = parser.parse_args()
    try:
        config = load_json(args.config)
        {"prepare": prepare, "transcribe": transcribe, "extract": transcribe, "save": save,
         "revise": save, "review": review}[args.command](args, config)
    except (ValueError, OSError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
