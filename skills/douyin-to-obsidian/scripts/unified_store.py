#!/usr/bin/env python3
"""Read and update the skill's knowledge index and import history."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, unquote

import review_log

INDEX_NAME = "知识总索引.md"
LOG_NAME = "导入总日志.md"
INDEX_HEADERS = ["笔记", "类型", "关键词", "核心洞察", "日期"]
LEGACY_LOG_HEADERS = ["时间", "来源", "操作", "笔记", "分类", "来源 URL", "内容复审"]
LOG_HEADERS = [*LEGACY_LOG_HEADERS, "作者名称"]


def log_row(row: list[str]) -> list[str]:
    """Normalize historical events without backfilling their author."""
    if len(row) == len(LEGACY_LOG_HEADERS):
        return [*row, ""]
    if len(row) != len(LOG_HEADERS):
        raise ValueError("导入总日志行列数无效")
    return list(row)


def verified_author_name(front: dict) -> str:
    sec_uid, name = front.get("douyin_author_id"), front.get("douyin_author")
    if not isinstance(sec_uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,120}", sec_uid):
        return ""
    if not isinstance(name, str) or not name.strip() or name.strip() == sec_uid[:12]:
        return ""
    return name.strip()


def cells(line: str, expected: int) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError("总览表格行边界无效")
    parts = [part.strip() for part in re.split(r"(?<!\\)\|", line)[1:-1]]
    if len(parts) != expected:
        raise ValueError("总览表格列数无效")
    return parts


def plain(value: object) -> str:
    return str(value or "").replace("\\|", "|")


def escape(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def note_link(title: object, relative: str) -> str:
    label = escape(title).replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({quote(relative, safe='/-_.~')})"


def link_path(value: str) -> str | None:
    match = re.fullmatch(r"\[(?:\\.|[^\]])*\]\(([^)]+)\)", value)
    if not match:
        return None
    relative = unquote(match[1])
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] != "知识库" or ".." in parts or not relative.endswith(".md"):
        raise ValueError("总览笔记链接路径无效")
    return relative


def link_title(value: str) -> str:
    match = re.match(r"^\[(.*)\]\([^)]+\)$", value)
    return re.sub(r"\\([\\\[\]|])", r"\1", match[1]) if match else value


def topic(relative: str) -> str:
    parts = PurePosixPath(relative).parts
    if len(parts) < 3 or parts[0] != "知识库":
        raise ValueError("总览笔记必须位于知识库主题目录")
    return " / ".join(parts[1:-1])


def _read_index(raw: str) -> list[dict]:
    lines = raw.splitlines()
    if not lines or lines[0] != "# 知识总索引":
        raise ValueError("知识总索引标题无效")
    declared = re.search(r"^共 (\d+) 篇笔记。", raw, re.M)
    if not declared:
        raise ValueError("知识总索引计数缺失")
    group, header, rows = None, False, []
    for line in lines[1:]:
        if line.startswith("## "):
            group, header = line[3:], False
        elif line.startswith("|"):
            row = cells(line, 5)
            if row == INDEX_HEADERS:
                if not group:
                    raise ValueError("知识总索引表头没有主题")
                header = True
            elif all(re.fullmatch(r":?-{3,}:?", part) for part in row):
                if not header:
                    raise ValueError("知识总索引分隔行位置无效")
            else:
                if not header:
                    raise ValueError("知识总索引条目缺少表头")
                relative = link_path(row[0])
                if not relative or topic(relative) != group:
                    raise ValueError("知识总索引条目路径与主题不符")
                rows.append({"path": relative, "cells": row})
        elif line and not (line.startswith("共 ") or line.startswith("原始记录：") or line.startswith("其中 ")):
            raise ValueError("知识总索引包含未识别的正文，未覆盖")
    paths = [row["path"] for row in rows]
    if len(paths) != len(set(paths)) or len(rows) != int(declared[1]):
        raise ValueError("知识总索引存在重复路径或计数不符")
    return rows


def _read_log(raw: str) -> list[list[str]]:
    lines = raw.splitlines()
    if not lines or lines[0] != "# 导入总日志":
        raise ValueError("导入总日志标题无效")
    declared = re.search(r"^共 (\d+) 条(?:[：。].*)?$", raw, re.M)
    if not declared:
        raise ValueError("导入总日志计数缺失")
    header, rows = None, []
    for line in lines[1:]:
        if line.startswith("|"):
            count = len(re.split(r"(?<!\\)\|", line)) - 2
            row = cells(line, count)
            if count not in (len(LEGACY_LOG_HEADERS), len(LOG_HEADERS)):
                raise ValueError("导入总日志列数无效")
            if row in (LEGACY_LOG_HEADERS, LOG_HEADERS):
                header = row
            elif all(re.fullmatch(r":?-{3,}:?", part) for part in row):
                if not header or len(row) != len(header):
                    raise ValueError("导入总日志分隔行位置无效")
            else:
                if not header or len(row) != len(header) or not row[1] or not re.match(r"^\d{4}-\d{2}-\d{2}", row[0]):
                    raise ValueError("导入总日志事件结构无效")
                # Two historical rows have no live note link. Keep their cells verbatim.
                if row[3].startswith("["):
                    link_path(row[3])
                rows.append(log_row(row))
        elif line and not (line.startswith("共 ") or line.startswith("原始记录：") or line.startswith("其中 ")):
            raise ValueError("导入总日志包含未识别的正文，未覆盖")
    if len(rows) != int(declared.group(1)):
        raise ValueError("导入总日志事件计数不符")
    return rows


def load(vault: Path) -> tuple[list[dict], list[list[str]]]:
    index_path, log_path = vault / INDEX_NAME, vault / LOG_NAME
    if any(path.is_symlink() or not path.is_file() for path in (index_path, log_path)):
        raise ValueError("两份总览缺失或是符号链接")
    return _read_index(index_path.read_text(encoding="utf-8")), _read_log(log_path.read_text(encoding="utf-8"))


def render_index(rows: list[dict]) -> bytes:
    groups: dict[str, list[list[str]]] = {}
    for row in rows:
        groups.setdefault(topic(row["path"]), []).append(row["cells"])
    lines = ["# 知识总索引", "", f"共 {len(rows)} 篇笔记。按主题汇总；相近笔记各自保留。", ""]
    for group in sorted(groups, key=str.casefold):
        lines.extend([f"## {group}", "", "| " + " | ".join(INDEX_HEADERS) + " |",
                      "| --- | --- | --- | --- | --- |"])
        for row in sorted(groups[group], key=lambda item: link_title(item[0]).casefold()):
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def render_log(rows: list[list[str]]) -> bytes:
    rows = [log_row(row) for row in rows]
    lines = ["# 导入总日志", "",
             f"共 {len(rows)} 条。按时间由近到远排列。", "",
             "| " + " | ".join(LOG_HEADERS) + " |", "| " + " | ".join(["---"] * len(LOG_HEADERS)) + " |"]
    for row in sorted(rows, key=lambda item: item[0], reverse=True):
        lines.append("| " + " | ".join(row) + " |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def validate_rendered(index: bytes, log: bytes, index_count: int, log_count: int) -> None:
    if len(_read_index(index.decode("utf-8"))) != index_count or len(_read_log(log.decode("utf-8"))) != log_count:
        raise ValueError("总览写入前回读计数不符")


def dy_changes(vault: Path, config: dict, changes: dict, events: list[dict],
               dy_rows: list[dict], moves: dict[str, str] | None = None) -> tuple[Path, Path]:
    index, logs = load(vault)
    moves = moves or {}
    if moves:
        old_paths = {old + ".md" for old in moves}
        index = [row for row in index if row["path"] not in old_paths]
        for row in logs:
            old = link_path(row[3])
            if row[1] == "DY" and old and old.removesuffix(".md") in moves:
                new = moves[old.removesuffix(".md")] + ".md"
                row[3] = note_link(link_title(row[3]), new)
                row[4] = escape(topic(new))
    by_path = {row["path"]: row for row in dy_rows}
    index = [row for row in index if row["path"] not in by_path]
    for row in dy_rows:
        relative = row["path"]
        fields = [note_link(row.get("title", Path(relative).stem), relative),
                  escape(row.get("type", "知识笔记")),
                  escape("、".join(row.get("tags", []))),
                  escape(row.get("insight", "")),
                  escape(review_log.display_time(row.get("created")))]
        index.append({"path": relative, "cells": fields})

    state_path = vault / config.get("review_state_file", review_log.STATE_FILE)
    state = review_log.load_state(state_path, changes)
    for row in dy_rows:
        state["items"].setdefault(row["aweme_id"], {"initial_status": "未登记"})
    for event in events:
        row = by_path[event["path"]]
        item = state["items"][row["aweme_id"]]
        if event["action"] == "新增":
            item["initial_status"] = "未复审"
        if event.get("source_url"):
            item["source_url"] = review_log.source_url(event["source_url"], row["aweme_id"])
    current = {}
    for row in dy_rows:
        item = state["items"][row["aweme_id"]]
        if not item.get("source_url"):
            item["source_url"] = review_log.cached_source(config, row["aweme_id"])
        review = item.get("review")
        status, reviewed_at = item["initial_status"], "—"
        if review:
            path = vault / row["path"]
            raw = changes[path] if path in changes else path.read_bytes()
            status = review["outcome"] if hashlib.sha256(raw).hexdigest() == review["note_sha256"] else "修改后待复审"
            reviewed_at = review_log.display_time(review["reviewed_at"])
        current[row["path"]] = (item.get("source_url"),
                                status + (f"（{reviewed_at}）" if reviewed_at != "—" else ""))
    encoded_state = review_log.encode_state(state)
    if not state_path.exists() or state_path.read_bytes() != encoded_state:
        changes[state_path] = encoded_state
    for row in logs:
        relative = link_path(row[3]) if row[1] == "DY" else None
        if relative in current:
            url, review = current[relative]
            row[5] = f"[原作品]({url})" if url else "—"
            row[6] = review
    for event in events:
        relative = event["path"]
        url, review = current[relative]
        logs.append([review_log.display_time(event["time"]), "DY", escape(event["action"]),
                     note_link(event["title"], relative), escape(topic(relative)),
                     f"[原作品]({url})" if url else "—", review,
                     escape(verified_author_name(by_path[relative]))])

    index_raw, log_raw = render_index(index), render_log(logs)
    validate_rendered(index_raw, log_raw, len(index), len(logs))
    index_path, log_path = vault / INDEX_NAME, vault / LOG_NAME
    changes[index_path], changes[log_path] = index_raw, log_raw
    return index_path, log_path


def check(vault: Path) -> dict:
    index, logs = load(vault)
    missing = sum(not (vault / row["path"]).is_file() for row in index)
    return {"status": "ok", "changed": False, "index_count": len(index),
            "log_count": len(logs),
            "missing_index_notes": missing, "index": str(vault / INDEX_NAME),
            "log": str(vault / LOG_NAME)}
