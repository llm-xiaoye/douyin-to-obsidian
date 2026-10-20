#!/usr/bin/env python3
"""Own Douyin library: topic paths, index/log transactions, and explicit migration."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import yaml

REMOVED_FIELDS = {"source", "source_url", "author", "published_at", "duration_seconds",
                  "analysis_mode", "asr_model", "status"}
REMOVED_SECTIONS = {"回听定位", "来源", "原音频", "带时间戳的语音转写"}
START, END = "%% douyin:managed:start %%", "%% douyin:managed:end %%"
LEGACY_START, LEGACY_END = "<!-- douyin:managed:start -->", "<!-- douyin:managed:end -->"


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def inside(root, relative):
    root = Path(root).expanduser().resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("输出路径必须位于指定知识库内，且不能是符号链接。")
    return path


def read_note(path):
    text = Path(path).read_text(encoding="utf-8")
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            front = yaml.safe_load(parts[1])
            if isinstance(front, dict):
                return front, parts[2].lstrip("\n")
    return {}, text


def render_note(front, body):
    return "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body.rstrip() + "\n"


def note_source_url(config, vault, vid, supplied=None, front=None):
    """Resolve a verified work URL; never guess video versus image from an ID."""
    import review_log
    current = review_log.source_url(supplied, vid) if supplied else None
    previous = front.get("source_url") if isinstance(front, dict) else None
    if previous:
        try:
            previous = review_log.source_url(previous, vid)
        except ValueError:
            if not current:
                raise
            previous = None
    if previous:
        if current and current != previous:
            raise ValueError("本次作品链接与笔记原链接不一致，未覆盖。")
        return previous
    if current:
        return current
    state_path = inside(vault, config.get("review_state_file", review_log.STATE_FILE))
    state = review_log.load_state(state_path, {})
    recorded = state["items"].get(str(vid), {}).get("source_url")
    if recorded:
        return review_log.source_url(recorded, vid)
    cached = review_log.cached_source(config, vid)
    if cached:
        return cached
    raise ValueError("缺少可核对的抖音原作品链接，未保存笔记。")


def scan(vault, replacements=None):
    """Vault notes, including user moves, are the deduplication source of truth."""
    vault = Path(vault).resolve()
    replacements = replacements or {}
    paths = set(vault.rglob("*.md")) | {p for p in replacements if p.suffix == ".md"}
    rows, ids = [], set()
    for path in sorted(paths):
        if path.is_symlink() or any(p.startswith(".") for p in path.relative_to(vault).parts):
            continue
        content = replacements.get(path, ...)
        if content is None:
            continue
        try:
            if content is ...:
                front, _ = read_note(path)
            else:
                front = yaml.safe_load(content.decode().split("---", 2)[1])
        except (ValueError, IndexError, yaml.YAMLError):
            continue
        if not isinstance(front, dict) or not re.fullmatch(r"\d{16,22}", str(front.get("aweme_id", ""))):
            continue
        vid = str(front["aweme_id"])
        if vid in ids:
            raise ValueError(f"同一作品存在多篇笔记，请先消除重复：{path.name}")
        ids.add(vid)
        rows.append(dict(front, aweme_id=vid, path=path.relative_to(vault).as_posix()))
    return rows


def find_existing(vault, vid):
    return next((Path(vault).resolve() / r["path"] for r in scan(vault) if r["aweme_id"] == str(vid)), None)


def slug(title):
    value = re.sub(r'[\\/:*?"<>|\[\]#^\x00-\x1f：]+', "-", title)
    value = re.sub(r"\s+", "-", value).strip(" .-")
    value = re.sub("-+", "-", value)[:100].rstrip(" .-")
    if not value or value.isdigit():
        raise ValueError("标题必须是可读的知识主题，不能只有数字。")
    return value


def validate_analysis(data, *, strict_topics=False):
    if not isinstance(data, dict):
        raise ValueError("analysis.json 必须是对象。")
    title, tags, category, insight = (data.get(k) for k in ("title", "tags", "category", "insight"))
    if not isinstance(title, str) or not title.strip():
        raise ValueError("analysis.json 缺少文章标题 title。")
    title = " ".join(title.split())
    slug(title)
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
        raise ValueError("tags 需要非空字符串数组。")
    if not isinstance(category, list) or not 2 <= len(category) <= 4:
        raise ValueError("category 需要 2–4 级语义目录，例如 [\"AI\",\"Agent记忆\"]。")
    for part in category:
        if not isinstance(part, str) or not part.strip() or part != part.strip() or len(part) > 24 or re.search(r'[\\/:*?"<>|\[\]#^\x00-\x1f]', part) or part.startswith("."):
            raise ValueError("分类目录名必须简短，不得含路径符号或隐藏目录。")
    if strict_topics:
        from topic_taxonomy import validate_category
        validate_category(category)
    if not isinstance(insight, str) or not 1 <= len(insight.strip()) <= 60:
        raise ValueError("insight 需要一句简短的知识结论，最多 60 字。")
    return {"title": title, "tags": list(dict.fromkeys(t.strip() for t in tags)),
            "category": category, "insight": insight.strip(), "type": str(data.get("type") or "知识笔记")}


def clean_body(body):
    # Remove entire requested appendices, preserving later sections/personal notes.
    personal = re.search(r"^## 我的补充\s*$", body, re.M)
    tail = body[personal.start():] if personal else ""
    main = body[:personal.start()] if personal else body
    lines, result, dropping = main.splitlines(), [], False
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            level, heading = len(match[1]), match[2]
            if dropping and level <= dropping:
                dropping = False
            if not dropping and heading in REMOVED_SECTIONS:
                dropping = level
        if not dropping:
            result.append(line)
    return "\n".join(result).strip() + ("\n\n" + tail if tail else "")


def body_for_title(body, title):
    body = clean_body(body)
    body = re.sub(r"\A# [^\n]+\n*", "", body).strip()
    if not body:
        raise ValueError("知识正文不能为空。")
    if not re.search(r"^## 我的补充\s*$", body, re.M):
        body += "\n\n## 我的补充\n"
    return f"# {title}\n\n{body}\n"


def new_path(vault, config, analysis, occupied=()):
    directory = inside(vault, config.get("notes_dir", "抖音"))
    directory = inside(vault, directory.relative_to(vault) / Path(*analysis["category"]))
    stem = slug(analysis["title"])
    target, suffix = directory / f"{stem}.md", 2
    while target.exists() or target.is_symlink() or target in occupied:
        target = directory / f"{stem}-{suffix}.md"
        suffix += 1
    return target


def cell(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def managed_parts(old, heading):
    pairs = [(start, end) for start, end in ((START, END), (LEGACY_START, LEGACY_END))
             if start in old or end in old]
    if len(pairs) != 1:
        raise ValueError(f"{heading} 已存在且没有唯一的管理区域，未覆盖。请换用空闲的索引/日志文件名。")
    start, end = pairs[0]
    if old.count(start) != 1 or old.count(end) != 1 or old.index(start) > old.index(end):
        raise ValueError(f"{heading} 的管理区域不完整或重复，未覆盖。")
    prefix, content, suffix = old.split(start, 1)[0], old.split(start, 1)[1].split(end, 1)[0], old.split(end, 1)[1]
    if start == LEGACY_START and prefix.startswith(f"# {heading}\n"):
        # Obsidian already shows the filename as the inline title. Only remove
        # the exact heading generated by the legacy writer; preserve custom text.
        prefix = prefix[len(f"# {heading}\n"):].lstrip("\n")
    return prefix, content, suffix


def managed(old, heading, content):
    # Native Obsidian comments and separated blocks also work in Live Preview.
    region = f"{START}\n\n{content.rstrip()}\n\n{END}"
    if old is None:
        return region + "\n"
    prefix, _, suffix = managed_parts(old, heading)
    return prefix + region + suffix


def metadata_changes(vault, config, changes, events, moves=None):
    import review_log
    rows = scan(vault, changes)
    if config.get("unified_only"):
        import unified_store
        return unified_store.dy_changes(vault, config, changes, events, rows, moves)
    index = inside(vault, config.get("index_file", "知识索引.md"))
    log = inside(vault, config.get("log_file", "更新日志.md"))
    if index == log or any(vault / r["path"] in (index, log) for r in rows):
        raise ValueError("索引、日志和知识笔记路径不得重合。")
    groups = {}
    for row in rows:
        groups.setdefault(str(Path(row["path"]).parent), []).append(row)
    lines = [f"共 {len(rows)} 篇知识笔记。按知识主题归类；重复导入保留已有笔记。", ""]
    for group, entries in sorted(groups.items()):
        display_group = group.removeprefix("知识库/") if config.get("notes_dir") == "知识库" else group
        lines += [f"## {display_group.replace('/', ' / ')}", "", "| 文章 | 类型 | 关键词 | 核心洞察 | 入库时间 |",
                  "| --- | --- | --- | --- | --- |"]
        for row in sorted(entries, key=lambda r: r.get("title", "")):
            link = f"[[{row['path'][:-3]}|{row.get('title', Path(row['path']).stem)}]]"
            lines.append(f"| {link} | {cell(row.get('type', '知识笔记'))} | {cell('、'.join(row.get('tags', [])))} | {cell(row.get('insight'))} | {cell(review_log.display_time(row.get('created')))} |")
        lines.append("")
    # Obsidian wikilink aliases inside tables must escape the pipe.
    content = "\n".join(lines)
    content = re.sub(r"\[\[([^\]\n]*?)\|([^\]\n]*)\]\]", r"[[\1\\|\2]]", content)
    changes[index] = managed(index.read_text() if index.exists() else None, "抖音知识索引", content).encode()
    old = changes[log].decode() if log in changes and changes[log] is not None else (log.read_text() if log.exists() else None)
    previous = managed_parts(old, "抖音更新日志")[1] if old is not None else ""
    history = review_log.render(vault, config, changes, rows, events, previous,
                                inside(vault, config.get("review_state_file", review_log.STATE_FILE)), cell)
    changes[log] = managed(old, "抖音更新日志", history).encode()
    return index, log


def atomic_write(path, content, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".dy-", delete=False) as f:
        temp = Path(f.name)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    try:
        if exclusive:
            os.link(temp, path)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(vault):
    # KI obtains this lock before its own lock too. Catalog writes are serialized.
    with inside(vault, ".unified-catalog.lock").open("a") as shared:
        fcntl.flock(shared, fcntl.LOCK_EX)
        try:
            with inside(vault, ".dy-library.lock").open("a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    marker = Path(vault).resolve() / ".knowledge-migration-in-progress.json"
                    parent_marker = Path(vault).resolve().parent / ".knowledge-migration-in-progress.json"
                    if marker.exists() or parent_marker.exists():
                        raise ValueError("知识库正在迁移，请等待迁移完成后重新导入。")
                    recover(vault)
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            fcntl.flock(shared, fcntl.LOCK_UN)


def recover(vault):
    journal = inside(vault, ".dy-transaction.json")
    if not journal.exists():
        return
    data = json.loads(journal.read_text())
    # Never undo an unrelated edit made after an interrupted transaction.
    for row in data["files"]:
        path = inside(vault, row["path"])
        current = path.read_bytes() if path.exists() else None
        before = base64.b64decode(row["before"]) if row["before"] is not None else None
        after = base64.b64decode(row["after"]) if row["after"] is not None else None
        if current not in (before, after):
            raise ValueError(f"中断后文件已被修改，保留现场供恢复：{path}")
    for row in reversed(data["files"]):
        path = inside(vault, row["path"])
        if row["before"] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(row["before"]))
    journal.unlink()


def transact(vault, changes):
    journal = inside(vault, ".dy-transaction.json")
    records = []
    for path, after in changes.items():
        path = inside(vault, path.relative_to(vault))
        before = path.read_bytes() if path.exists() else None
        records.append({"path": path.relative_to(vault).as_posix(),
                        "before": base64.b64encode(before).decode() if before is not None else None,
                        "after": base64.b64encode(after).decode() if after is not None else None})
    atomic_write(journal, json.dumps({"files": records}, ensure_ascii=False).encode(), exclusive=True)
    applied = []
    try:
        for row in records:
            path = inside(vault, row["path"])
            expected = base64.b64decode(row["before"]) if row["before"] is not None else None
            if (path.read_bytes() if path.exists() else None) != expected:
                raise ValueError(f"文件在提交前发生变化，未覆盖：{path.name}")
            if row["after"] is None:
                path.unlink()
            else:
                atomic_write(path, base64.b64decode(row["after"]), exclusive=row["before"] is None)
            applied.append(row)
    except BaseException:
        # Only roll back files this invocation actually wrote; preserve a racing writer.
        for row in reversed(applied):
            path = inside(vault, row["path"])
            if row["before"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, base64.b64decode(row["before"]))
        journal.unlink(missing_ok=True)
        raise
    journal.unlink()


def sync_unified_after_write(function):
    """Refresh the cross-skill view only after the DY vault lock is released."""
    @wraps(function)
    def wrapped(config, *args, **kwargs):
        result = function(config, *args, **kwargs)
        if config.get("unified_only"):
            return result
        root = config.get("unified_root_vault")
        changed = result.get("written") or result.get("status") in ("migrated", "reviewed") or bool(result.get("changed_files"))
        if root and changed and Path(config["vault"]).expanduser().resolve() in (
                Path(root).expanduser().resolve(), Path(root).expanduser().resolve() / "douyinknowledge"):
            try:
                from unified_catalog import rebuild
                rebuild(root)
            except (OSError, ValueError, UnicodeError, ImportError, KeyError, json.JSONDecodeError) as exc:
                result["unified_sync_warning"] = str(exc)[:300]
        return result
    return wrapped


def author_front(author):
    """Verified author identity only (same keys/order as dy-author); unknown authors are omitted."""
    if not author:
        return {}
    nickname, sec_uid = author.get("nickname"), author.get("sec_uid")
    if not isinstance(sec_uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,120}", sec_uid):
        return {}
    name = nickname.strip() if isinstance(nickname, str) and nickname.strip() and nickname != "未知作者" else sec_uid[:12]
    return {"douyin_author": name, "douyin_author_id": sec_uid}


@sync_unified_after_write
def save_note(config, vid, analysis, body, collections=(), images=(), source_url=None, author=None):
    vault = Path(config["vault"]).expanduser().resolve()
    analysis = validate_analysis(analysis, strict_topics=config.get("notes_dir") == "知识库")
    with locked(vault):
        existing = find_existing(vault, vid)
        if existing:
            return {"status": "exists", "note": str(existing), "written": False}
        source_url = note_source_url(config, vault, vid, source_url)
        path = new_path(vault, config, analysis)
        front = {"title": analysis["title"], "source_url": source_url, "aweme_id": str(vid)}
        front.update(author_front(author))
        front.update({"type": analysis["type"], "tags": analysis["tags"],
                      "insight": analysis["insight"], "created": now()})
        if collections:
            front["collections"] = list(collections)
        from note_assets import stage
        body, changes, attachments = stage(vault, config, path, clean_body(body), images)
        changes[path] = render_note(front, body_for_title(body, analysis["title"])).encode()
        events = [{"time": now(), "action": "新增", "path": path.relative_to(vault).as_posix(),
                   "title": analysis["title"], "source_url": source_url}]
        index, log = metadata_changes(vault, config, changes, events)
        transact(vault, changes)
        return {"status": "saved", "note": str(path), "title": analysis["title"],
                "index": str(index), "log": str(log), "written": True, "attachments": attachments}


@sync_unified_after_write
def revise_note(config, vid, analysis, body, expected_sha256, images=(), source_url=None):
    """Explicit content revision only; ordinary import still skips existing notes."""
    from note_assets import stage
    vault = Path(config["vault"]).expanduser().resolve()
    analysis = validate_analysis(analysis, strict_topics=config.get("notes_dir") == "知识库")
    with locked(vault):
        path = find_existing(vault, vid)
        if not path:
            raise ValueError("待修订文章不存在。请用普通导入流程。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError("文章在审阅后被修改，请重新阅读并合并个人修改，未覆盖。")
        front, previous = read_note(path)
        source_url = note_source_url(config, vault, vid, source_url, front)
        if analysis["title"] != front.get("title"):
            raise ValueError("内容修订保留原文章标题；改名请单独使用 migrate。")
        if re.search(r"^## 我的补充\s*$", body, re.M):
            raise ValueError("修订正文不要包含‘我的补充’，脚本会保留原文。")
        personal = re.search(r"^## 我的补充\s*$", previous, re.M)
        tail = previous[personal.start():] if personal else "## 我的补充\n"
        body, changes, attachments = stage(vault, config, path, clean_body(body), images)
        body = body.rstrip() + "\n\n" + tail
        front = {k: v for k, v in front.items() if k not in REMOVED_FIELDS}
        front.update({k: analysis[k] for k in ("title", "tags", "type", "insight")})
        front["source_url"] = source_url
        front["updated"] = now()
        changes[path] = render_note(front, body_for_title(body, analysis["title"])).encode()
        events = [{"time": now(), "action": "深化知识与配图", "path": path.relative_to(vault).as_posix(),
                   "title": analysis["title"], "source_url": source_url}]
        index, log = metadata_changes(vault, config, changes, events)
        backup = Path(config["cache_dir"]) / "revisions" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        snapshot = {p.relative_to(vault).as_posix(): base64.b64encode(p.read_bytes()).decode() if p.exists() else None for p in changes}
        (backup / "before.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        transact(vault, changes)
        return {"status": "revised", "note": str(path), "title": analysis["title"], "backup": str(backup),
                "index": str(index), "log": str(log), "written": True, "attachments": attachments}


@sync_unified_after_write
def record_review(config, vid, report, expected_sha256, evidence_sha256, source_url, is_image=False):
    """Register a real follow-up review; never infer one from import or formatting."""
    import review_log
    checked = review_log.validate_report(report, is_image)
    url = review_log.source_url(source_url, vid)
    if not re.fullmatch(r"[0-9a-f]{64}", str(evidence_sha256)):
        raise ValueError("复审需要实际核对的原材料指纹。")
    vault = Path(config["vault"]).expanduser().resolve()
    with locked(vault):
        path = find_existing(vault, vid)
        if not path or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError("文章不存在或已在复审后修改，未登记结果；请重新核对当前全文。")
        front, _ = read_note(path)
        state_path = inside(vault, config.get("review_state_file", review_log.STATE_FILE))
        state = review_log.load_state(state_path, {})
        item = state["items"].setdefault(str(vid), {"initial_status": "未登记"})
        reviewed_at = now()
        item.update(source_url=url, review=dict(checked, note_sha256=expected_sha256,
                    evidence_sha256=evidence_sha256, reviewed_at=reviewed_at,
                    reviewed_pages=report.get("reviewed_pages", [])))
        changes = {state_path: review_log.encode_state(state)}
        events = [{"time": reviewed_at, "action": "内容复审", "path": path.relative_to(vault).as_posix(),
                   "title": front["title"], "source_url": url}]
        index, log = metadata_changes(vault, config, changes, events)
        backup = Path(config["cache_dir"]) / "reviews" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        snapshot = {p.relative_to(vault).as_posix(): base64.b64encode(p.read_bytes()).decode() if p.exists() else None for p in changes}
        (backup / "before.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        transact(vault, changes)
        return {"status": "reviewed", "outcome": checked["outcome"], "title": front["title"],
                "note": str(path), "reviewed_at": review_log.display_time(reviewed_at),
                "index": str(index), "log": str(log), "backup": str(backup), "written": True}


@sync_unified_after_write
def migrate(config, mapping):
    """Explicit user-authorized reformatting, never part of an ordinary /dy update."""
    vault = Path(config["vault"]).resolve()
    with locked(vault):
        changes, events, moves = {}, [], {}
        for vid, data in mapping.items():
            old = find_existing(vault, vid)
            if not old:
                raise ValueError(f"待迁移笔记不存在：{vid}")
            analysis = validate_analysis(data, strict_topics=config.get("notes_dir") == "知识库")
            target_base = inside(vault, Path(config.get("notes_dir", "抖音")) / Path(*analysis["category"]) / (slug(analysis["title"]) + ".md"))
            target = old if old == target_base else new_path(vault, config, analysis, changes)
            front, body = read_note(old)
            source_url = note_source_url(config, vault, vid, data.get("source_url"), front)
            front = {k: v for k, v in front.items() if k not in REMOVED_FIELDS}
            front.update({k: analysis[k] for k in ("title", "tags", "type", "insight")})
            front["source_url"] = source_url
            front["updated"] = now()
            if target != old:
                from note_assets import rebase_embeds
                body = rebase_embeds(body, old, target)
                changes[old] = None
                moves[old.relative_to(vault).with_suffix("").as_posix()] = target.relative_to(vault).with_suffix("").as_posix()
            changes[target] = render_note(front, body_for_title(body, analysis["title"])).encode()
            events.append({"time": now(), "action": "整理目录与格式", "path": target.relative_to(vault).as_posix(),
                           "title": analysis["title"], "source_url": source_url})
        def update_links(text):
            def replace(m):
                destination, suffix = m[1], m[2] or ""
                # Tables escape the alias separator as \|; it is not part
                # of the destination filename.
                if destination.endswith("\\"):
                    destination, suffix = destination[:-1], "\\" + suffix
                for old, new in moves.items():
                    if destination in (old, old + ".md", Path(old).name):
                        return "[[" + new + suffix + "]]"
                return m[0]
            return re.sub(r"\[\[([^\]|#]+)((?:[#|][^\]]*)?)\]\]", replace, text)
        for path in set(vault.rglob("*.md")) | set(changes):
            if path.is_symlink() or any(p.startswith(".") for p in path.relative_to(vault).parts) or changes.get(path, ...) is None:
                continue
            before = changes[path].decode() if path in changes else path.read_text()
            after = update_links(before)
            if after != before:
                changes[path] = after.encode()
        index, log = metadata_changes(vault, config, changes, events, moves)
        backup = Path(config["cache_dir"]) / "migrations" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        snapshot = {p.relative_to(vault).as_posix(): base64.b64encode(p.read_bytes()).decode() if p.exists() else None for p in changes}
        (backup / "before.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        transact(vault, changes)
        for old in moves:
            parent = inside(vault, Path(old).parent)
            while parent != vault:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return {"status": "migrated", "count": len(events), "notes": [e["path"] for e in events],
                "backup": str(backup), "index": str(index), "log": str(log)}


def catalog(config):
    vault = Path(config["vault"]).resolve()
    rows = scan(vault)
    return {"notes_root": config.get("notes_dir", "抖音"),
            "categories": sorted({str(Path(r["path"]).parent) for r in rows}),
            "notes": [{k: r.get(k) for k in ("path", "title", "tags", "insight")} for r in rows]}


@sync_unified_after_write
def refresh_metadata(config):
    """Repair index/log rendering without importing items or touching articles."""
    vault = Path(config["vault"]).expanduser().resolve()
    with locked(vault):
        changes = {}
        index, log = metadata_changes(vault, config, changes, [])
        changes = {p: content for p, content in changes.items() if not p.exists() or p.read_bytes() != content}
        backup = None
        if changes:
            backup = Path(config["cache_dir"]) / "metadata-repairs" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
            backup.mkdir(parents=True)
            snapshot = {p.relative_to(vault).as_posix(): base64.b64encode(p.read_bytes()).decode() if p.exists() else None for p in changes}
            (backup / "before.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
            transact(vault, changes)
        return {"status": "refreshed", "changed_files": [str(p) for p in changes],
                "backup": str(backup) if backup else None, "index": str(index), "log": str(log)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    sub.add_parser("refresh", help="备份并修复索引/日志格式，不修改文章或新增导入记录")
    p = sub.add_parser("migrate")
    p.add_argument("--analysis-map", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        if args.command == "catalog":
            result = catalog(config)
        elif args.command == "refresh":
            result = refresh_metadata(config)
        else:
            result = migrate(config, json.loads(Path(args.analysis_map).read_text()))
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
