"""Resolve reviewed page images; render local embeds without touching the vault."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit

TOKEN = re.compile(r"\{\{DY_IMAGE:(\d+)\}\}")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def evidence_images(bundle, cache):
    """Prefer downloaded originals, including pixel-verified legacy WebP caches."""
    from PIL import Image
    cache = Path(cache).resolve()
    result = []
    for row in bundle["images"]:
        display = (cache / row["file"]).resolve()
        if not display.is_relative_to(cache) or digest(display.read_bytes()) != row["sha256"]:
            raise ValueError("图片证据发生变化，不能归档。")
        original = display
        if row.get("original_file"):
            original = (cache / row["original_file"]).resolve()
            if not original.is_relative_to(cache) or digest(original.read_bytes()) != row.get("original_sha256"):
                raise ValueError("下载原图的指纹不匹配。")
        elif display.suffix == ".png" and display.with_suffix(".webp").is_file():
            # Older bundles only named the PNG generated for Apple Vision.
            candidate = display.with_suffix(".webp").resolve()
            if not candidate.is_relative_to(cache):
                raise ValueError("原图路径超出缓存。")
            with Image.open(display) as a, Image.open(candidate) as b:
                if a.size == b.size and a.convert("RGB").tobytes() == b.convert("RGB").tobytes():
                    original = candidate
        # Unknown image containers retain a renderable, full-resolution PNG.
        if original.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            original = display
        result.append({"page": row["page"], "path": original,
                       "sha256": digest(original.read_bytes())})
    return result


def stage(vault, config, note, body, images):
    """All page markers must be standalone, outside fences, and used once."""
    from library import inside, slug
    pages = [r["page"] for r in images]
    found, fence = [], None
    for line in body.splitlines():
        mark = re.match(r"^\s*(`{3,}|~{3,})", line)
        if mark:
            if fence is None:
                fence = mark[1][0]
            elif mark[1][0] == fence:
                fence = None
        matches = TOKEN.findall(line)
        if matches:
            if fence or not TOKEN.fullmatch(line.strip()):
                raise ValueError("原图占位符须单独成段，不能位于代码块或行内。")
            found.extend(int(p) for p in matches)
    if sorted(found) != pages or len(set(pages)) != len(pages):
        raise ValueError("图文正文必须为每页原图各放置一个 {{DY_IMAGE:页码}}，不得遗漏或重复。")
    if "{{DY_IMAGE" in TOKEN.sub("", body):
        raise ValueError("原图占位符格式错误。")
    changes, manifest = {}, []
    for row in images:
        data = Path(row["path"]).read_bytes()
        if digest(data) != row["sha256"]:
            raise ValueError("图片在提交前被修改，未写入。")
        rel = Path(config.get("attachments_dir", "附件")) / slug(note.stem)
        rel /= f"原图-{row['page']:02d}-{row['sha256'][:12]}{Path(row['path']).suffix.lower()}"
        dest = inside(vault, rel)
        if dest.exists():
            if dest.read_bytes() != data:
                raise ValueError("原图附件已存在但内容不同，未覆盖。")
        else:
            changes[dest] = data
        url = quote(Path(os.path.relpath(dest, note.parent)).as_posix(), safe="/-._~")
        body = body.replace("{{DY_IMAGE:" + str(row["page"]) + "}}", f"![原图 {row['page']}]({url})")
        manifest.append({"page": row["page"], "path": str(dest), "sha256": row["sha256"]})
    return body, changes, manifest


def rebase_embeds(body, old, target):
    """Preserve local Markdown images when explicit classification moves a note."""
    def replace(match):
        url = match[2]
        parts = urlsplit(url)
        if parts.scheme or parts.netloc or url.startswith("/"):
            return match[0]
        path = (old.parent / unquote(parts.path)).resolve()
        relative = quote(Path(os.path.relpath(path, target.parent)).as_posix(), safe="/-._~")
        if parts.fragment:
            relative += "#" + parts.fragment
        return match[1] + relative + match[3]
    return re.sub(r"(!\[[^\]\n]*\]\()([^\s)]+)(\))", replace, body)
