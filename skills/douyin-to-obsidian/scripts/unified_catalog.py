#!/usr/bin/env python3
"""Check the two canonical catalogs; support legacy four-source migration fixtures."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import quote


INDEX_NAME = "知识总索引.md"
LOG_NAME = "导入总日志.md"
STATE_NAME = ".unified-catalog-state.json"
JOURNAL_NAME = ".unified-catalog-journal.json"
DY_START = "%% douyin:managed:start %%"
DY_END = "%% douyin:managed:end %%"
VERSION = 1


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def row_cells(line: str) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError("表格行边界无效")
    return [part.strip() for part in re.split(r"(?<!\\)\|", line)[1:-1]]


def canonical_generated_tables(raw: bytes) -> bytes:
    """Accept spacing added by Obsidian's table formatter without accepting edited cells."""
    lines = raw.decode("utf-8").split("\n")
    for i, line in enumerate(lines):
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = row_cells(line)
        if all(re.fullmatch(r"-{3,}", cell) for cell in cells):
            cells = ["---"] * len(cells)
        lines[i] = "| " + " | ".join(cells) + " |"
    return "\n".join(lines).encode("utf-8")


def managed(text: str, name: str) -> str:
    if text.count(DY_START) != 1 or text.count(DY_END) != 1:
        raise ValueError(f"{name} 缺少唯一管理区域")
    start = text.index(DY_START) + len(DY_START)
    end = text.index(DY_END)
    if start >= end:
        raise ValueError(f"{name} 管理区域顺序无效")
    return text[start:end]


def source_table(text: str, headers: list[str], name: str, *, grouped: bool = False):
    group = ""
    saw_header = False
    rows = []
    in_table = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            group = line[3:].strip()
            in_table = False
            continue
        if not line.startswith("|"):
            if line.strip():
                in_table = False
            continue
        cells = row_cells(line)
        if cells == headers:
            saw_header = True
            in_table = True
            continue
        if all(re.fullmatch(r":?-{3,}:?", item) for item in cells):
            continue
        if not in_table or len(cells) != len(headers) or (grouped and not group):
            raise ValueError(f"{name} 第 {number} 行表格结构无效")
        rows.append((group, dict(zip(headers, cells))))
    if not saw_header:
        raise ValueError(f"{name} 缺少表格表头")
    return rows


def note_target(cell: str, source: str) -> tuple[str, str]:
    if source == "KI":
        match = re.fullmatch(r"\[(.*)\]\((.+\.md)\)", cell)
        if not match:
            raise ValueError(f"KI 笔记链接无效：{cell[:90]}")
        return match.group(2), match.group(1)
    match = re.fullmatch(r"\[\[(.+?)\\\|(.+)\]\]", cell)
    if not match:
        raise ValueError(f"DY 笔记链接无效：{cell[:90]}")
    target = match.group(1) + ".md"
    return (target if target.startswith("知识库/") else "douyinknowledge/" + target), match.group(2)


def safe_note(root: Path, relative: str, *, require_exists: bool) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or path.suffix != ".md" or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"笔记路径无效：{relative}")
    target = root / relative
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise ValueError(f"笔记路径越界或为符号链接：{relative}")
    exists = target.is_file()
    if require_exists and not exists:
        raise ValueError(f"索引笔记不存在：{relative}")
    return exists


def clean_cell(value: str) -> str:
    return str(value).replace("\\|", "|").replace("\n", " ").replace("|", "\\|")


def link(title: str, relative: str) -> str:
    label = title.replace("\\|", "|").replace("[", "\\[").replace("]", "\\]")
    return f"[{clean_cell(label)}]({quote(relative, safe='/-_.~')})"


def parse_sources(root: Path, dy_vault: Path):
    dy_at_root = dy_vault == root
    names = {
        "ki_index": root / "_knowledge-index.md",
        "ki_log": root / "_import-log.md",
        "dy_index": dy_vault / ("DY-知识索引.md" if dy_at_root else "知识索引.md"),
        "dy_log": dy_vault / ("DY-更新日志.md" if dy_at_root else "更新日志.md"),
    }
    sources = {}
    for key, path in names.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"来源文件不存在或是符号链接：{path}")
        raw = path.read_bytes()
        sources[key] = {"sha256": digest(raw), "text": raw.decode("utf-8")}
    index_rows = []
    ki_headers = ["文档", "type", "关键词", "核心洞察", "日期"]
    dy_headers = ["文章", "类型", "关键词", "核心洞察", "入库时间"]
    for group, row in source_table(sources["ki_index"]["text"], ki_headers, "KI 索引", grouped=True):
        relative, title = note_target(row["文档"], "KI")
        exists = safe_note(root, relative, require_exists=False)
        index_rows.append((group, "KI", relative, title, row["type"], row["关键词"], row["核心洞察"], row["日期"], exists))
    for group, row in source_table(managed(sources["dy_index"]["text"], "DY 索引"), dy_headers, "DY 索引", grouped=True):
        relative, title = note_target(row["文章"], "DY")
        exists = safe_note(root, relative, require_exists=False)
        topic = group.removeprefix("抖音 / ").removeprefix("知识库 / ")
        index_rows.append((topic, "DY", relative, title, row["类型"], row["关键词"], row["核心洞察"], row["入库时间"], exists))
    log_rows = []
    titles_by_path = {row[2]: row[3] for row in index_rows}
    ki_log_headers = ["时间", "来源 URL", "写入路径", "操作"]
    dy_log_headers = ["时间", "操作", "文章", "分类", "来源 URL", "内容复审", "复审时间"]
    for _, row in source_table(sources["ki_log"]["text"], ki_log_headers, "KI 日志"):
        relative, source_title = note_target(row["写入路径"], "KI")
        title = titles_by_path.get(relative, source_title)
        exists = safe_note(root, relative, require_exists=False)
        category = str(PurePosixPath(relative).parent).removeprefix("知识库/").replace("/", " / ")
        log_rows.append((row["时间"], "KI", row["操作"], relative, title, category, row["来源 URL"], "—", exists))
    for _, row in source_table(managed(sources["dy_log"]["text"], "DY 日志"), dy_log_headers, "DY 日志"):
        relative, source_title = note_target(row["文章"], "DY")
        title = titles_by_path.get(relative, source_title)
        exists = safe_note(root, relative, require_exists=False)
        review = row["内容复审"]
        if row["复审时间"] != "—":
            review += "（" + row["复审时间"] + "）"
        category = row["分类"].removeprefix("抖音 / ").removeprefix("知识库 / ")
        log_rows.append((row["时间"], "DY", row["操作"], relative, title, category, row["来源 URL"], review, exists))
    hashes = {key: value["sha256"] for key, value in sources.items()}
    return index_rows, log_rows, hashes


def render(index_rows, log_rows, *, dy_at_root: bool = True):
    ki_count = sum(row[1] == "KI" for row in index_rows)
    dy_count = len(index_rows) - ki_count
    groups = {}
    for row in index_rows:
        group = str(PurePosixPath(row[2]).parent).removeprefix("知识库/").replace("/", " / ") if row[2].startswith("知识库/") else row[0]
        groups.setdefault(group, []).append(row)
    missing = sum(not row[-1] for row in index_rows)
    dy_index_link = "DY-知识索引.md" if dy_at_root else "douyinknowledge/知识索引.md"
    dy_log_link = "DY-更新日志.md" if dy_at_root else "douyinknowledge/更新日志.md"
    index = ["# 知识总索引", "", f"共 {len(index_rows)} 篇笔记。按主题汇总；相近笔记各自保留。", "",
             f"原始记录：[KI 索引](_knowledge-index.md) · [DY 索引]({dy_index_link})", ""]
    if missing:
        index += [f"其中 {missing} 条来源索引指向已不存在的笔记，已标注为「原笔记缺失」。", ""]
    for group in sorted(groups, key=str.casefold):
        index += [f"## {group}", "", "| 笔记 | 类型 | 关键词 | 核心洞察 | 日期 |", "| --- | --- | --- | --- | --- |"]
        for _, source, relative, title, kind, tags, insight, date, exists in sorted(groups[group], key=lambda row: (row[3].casefold(), row[1], row[2])):
            note = link(title, relative) if exists else clean_cell(title) + "（原笔记缺失）"
            index.append(f"| {note} | {clean_cell(kind)} | {clean_cell(tags)} | {clean_cell(insight)} | {clean_cell(date)} |")
        index.append("")
    old_paths = sum(not row[-1] for row in log_rows)
    log = ["# 导入总日志", "", f"共 {len(log_rows)} 条：KI {sum(row[1] == 'KI' for row in log_rows)} 条，DY {sum(row[1] == 'DY' for row in log_rows)} 条。按时间由近到远排列。", "",
           f"原始记录：[KI 日志](_import-log.md) · [DY 日志]({dy_log_link})", "",
           *( [f"其中 {old_paths} 条历史记录的原笔记路径已失效。", ""] if old_paths else [] ),
           "| 时间 | 来源 | 操作 | 笔记 | 分类 | 来源 URL | 内容复审 |",
           "| --- | --- | --- | --- | --- | --- | --- |"]
    for time, source, action, relative, title, category, url, review, exists in sorted(log_rows, key=lambda row: row[0], reverse=True):
        note = link(title, relative) if exists else clean_cell(title) + "（原路径失效）"
        log.append(f"| {clean_cell(time)} | {source} | {clean_cell(action)} | {note} | {clean_cell(category)} | {clean_cell(url)} | {clean_cell(review)} |")
    return ("\n".join(index).rstrip() + "\n").encode("utf-8"), ("\n".join(log) + "\n").encode("utf-8")


def atomic_write(path: Path, content: bytes, *, exclusive: bool = False):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".unified-", delete=False) as handle:
        temp = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if exclusive:
            os.link(temp, path)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def existing_bytes(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"总览目标类型无效：{path}")
    return path.read_bytes() if path.exists() else None


def recover(root: Path):
    path = root / JOURNAL_NAME
    raw = existing_bytes(path)
    if raw is None:
        return
    journal = json.loads(raw)
    if journal.get("version") != VERSION or set(journal.get("files", {})) != {INDEX_NAME, LOG_NAME, STATE_NAME}:
        raise ValueError("总览事务日志格式无效，已保留现场")
    for name, values in journal["files"].items():
        current = existing_bytes(root / name)
        before = base64.b64decode(values["before"]) if values["before"] is not None else None
        after = base64.b64decode(values["after"])
        if current not in (before, after):
            raise ValueError(f"总览事务中断后文件被修改，已保留现场：{name}")
    for name, values in journal["files"].items():
        target = root / name
        before = base64.b64decode(values["before"]) if values["before"] is not None else None
        if before is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, before)
    path.unlink()


def write_transaction(root: Path, updates: dict[str, bytes]):
    before = {name: existing_bytes(root / name) for name in updates}
    journal = {"version": VERSION, "files": {
        name: {"before": base64.b64encode(before[name]).decode("ascii") if before[name] is not None else None,
               "after": base64.b64encode(content).decode("ascii")}
        for name, content in updates.items()}}
    journal_path = root / JOURNAL_NAME
    atomic_write(journal_path, (json.dumps(journal, ensure_ascii=False) + "\n").encode("utf-8"), exclusive=True)
    try:
        for name, content in updates.items():
            if existing_bytes(root / name) != before[name]:
                raise ValueError(f"总览提交前被修改：{name}")
            atomic_write(root / name, content, exclusive=before[name] is None)
    except BaseException:
        recover(root)
        raise
    journal_path.unlink()


@contextmanager
def file_lock(path: Path):
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ki_lock_path(root: Path) -> Path:
    key = digest(str(root).encode("utf-8"))[:20]
    return Path(tempfile.gettempdir()) / f"ki-import-{key}.lock"


def rebuild(vault: str | Path, dy_vault: str | Path | None = None, *, check: bool = False):
    root = Path(vault).expanduser().resolve()
    dy = Path(dy_vault).expanduser().resolve() if dy_vault else root
    if not root.is_dir() or not dy.is_dir() or dy not in (root, root / "douyinknowledge"):
        raise ValueError("总览 Vault 或 DY 目录无效")
    with ExitStack() as stack:
        stack.enter_context(file_lock(root / ".unified-catalog.lock"))
        stack.enter_context(file_lock(ki_lock_path(root)))
        stack.enter_context(file_lock((root if (root / "知识库").is_dir() else dy) / ".dy-library.lock"))
        if not check:
            recover(root)
        elif (root / JOURNAL_NAME).exists():
            raise ValueError("存在未完成的总览事务，请先运行 rebuild 恢复")
        if (dy / ".dy-transaction.json").exists() or (root / ".dy-transaction.json").exists():
            raise ValueError("DY 来源事务尚未完成，请先恢复 DY 事务")
        legacy = [root / "_knowledge-index.md", root / "_import-log.md",
                  dy / ("DY-知识索引.md" if dy == root else "知识索引.md"),
                  dy / ("DY-更新日志.md" if dy == root else "更新日志.md")]
        if dy == root and not any(path.exists() for path in legacy):
            import unified_store
            return unified_store.check(root)
        index_rows, log_rows, source_hashes = parse_sources(root, dy)
        index, log = render(index_rows, log_rows, dy_at_root=dy == root)
        outputs = {INDEX_NAME: index, LOG_NAME: log}
        state_path = root / STATE_NAME
        state_raw = existing_bytes(state_path)
        state = None
        if state_raw is None:
            if any(existing_bytes(root / name) is not None for name in outputs):
                raise ValueError("总览文件已存在但没有生成状态；已停止覆盖")
        else:
            state = json.loads(state_raw)
            if state.get("version") != VERSION or set(state.get("outputs", {})) != set(outputs):
                raise ValueError("总览生成状态格式无效")
            for name in outputs:
                current = existing_bytes(root / name)
                if current is None or (digest(current) != state["outputs"][name]
                                       and digest(canonical_generated_tables(current)) != state["outputs"][name]):
                    raise ValueError(f"总览文件有手工改动，已停止覆盖：{name}")
        changed = any(existing_bytes(root / name) != content for name, content in outputs.items())
        summary = {"status": "needs_rebuild" if check and changed else "ok", "changed": changed,
                   "index_count": len(index_rows), "ki_index_count": sum(row[1] == "KI" for row in index_rows),
                   "dy_index_count": sum(row[1] == "DY" for row in index_rows),
                   "missing_index_notes": sum(not row[-1] for row in index_rows),
                   "log_count": len(log_rows), "old_log_paths": sum(not row[-1] for row in log_rows),
                   "index": str(root / INDEX_NAME), "log": str(root / LOG_NAME)}
        if not check and (changed or state is None or state.get("sources") != source_hashes):
            state = {"version": VERSION, "sources": source_hashes,
                     "outputs": {name: digest(content) for name, content in outputs.items()},
                     "index_count": len(index_rows), "log_count": len(log_rows)}
            updates = {**outputs, STATE_NAME: (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
            write_transaction(root, updates)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "rebuild"))
    parser.add_argument("--vault", required=True)
    parser.add_argument("--dy-vault")
    args = parser.parse_args()
    try:
        result = rebuild(args.vault, args.dy_vault, check=args.command == "check")
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
