#!/usr/bin/env python3
"""Incremental /dy queue; the current agent supplies each knowledge note.

No timers or model subprocesses are created. Existing vault notes are the
source of truth for deduplication, including notes renamed by the user.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import re
import shlex
import shutil
import sys
import uuid

import collect_folders
import pipeline
from auto_route import TRIGGER

TERMINAL = {"saved", "exists", "skipped", "failed"}


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def initialize(config):
    vault = Path(config["vault"]).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"知识库目录不存在：{vault}")
    if (vault / ".knowledge-migration-in-progress.json").exists():
        raise ValueError("知识库正在迁移，请迁移完成后重新 init。")
    base = Path(config["cache_dir"]).expanduser().resolve() / "dy-runs"
    run = base / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, mode=0o700)
    snapshot = dict(config, vault=str(vault), cache_dir=str(base.parent))
    pipeline.write_json(run / "config.json", snapshot)
    pipeline.write_json(run / "state.json", {"schema_version": 1, "run_dir": str(run),
                        "planned": False, "items": [], "warnings": [], "collections": []})
    return {"run_dir": str(run), "input_file": str(run / "input.txt"),
            "state_file": str(run / "state.json")}


def load_run(value, config):
    run = Path(value).expanduser().resolve()
    base = Path(config["cache_dir"]).expanduser().resolve() / "dy-runs"
    if run.parent != base or not (run / "state.json").is_file():
        raise ValueError("请使用本次 init 返回的运行目录。")
    state = pipeline.load_json(run / "state.json")
    if state.get("run_dir") != str(run):
        raise ValueError("运行目录与状态不匹配。")
    return run, state, pipeline.load_json(run / "config.json")


def write_state(run, state):
    pipeline.write_json(run / "state.json", state)


def existing(vid, config):
    return pipeline.find_existing_note(config["vault"], vid, config.get("notes_dir", "抖音/视频"))


def parse_input(raw):
    text = re.sub(r"^/dy(?:\s+|$)", "", raw.strip(), count=1).strip()
    if not text:
        raise ValueError("用法：/dy 抖音链接，或 /dy 收藏夹名称；多个收藏夹用逗号分隔。")
    if text == "--list":
        return "list", []
    urls = re.findall(r"https?://[^\s<>\"，。、]+", text)
    if urls:
        return "videos", list(dict.fromkeys(urls))
    if TRIGGER in text:
        raise ValueError("识别到抖音分享口令，但没有找到完整链接。请重新复制完整分享内容。")
    tokens = re.split(r"[\s,，、;；]+", text)
    if all(re.fullmatch(r"\d{16,22}", token) for token in tokens):
        return "videos", list(dict.fromkeys(tokens))
    return "collections", text


def resolve_collections(text, folders):
    def matches(value):
        if value.startswith("收藏夹ID:") or value.startswith("收藏夹ID："):
            cid = re.split(r"[:：]", value, maxsplit=1)[1].strip()
            return [folder for folder in folders if folder["id"] == cid]
        return [folder for folder in folders if folder["name"] == value]

    # A whole-name match wins, so spaces/commas in a real folder name are safe.
    whole = matches(text)
    if whole:
        selectors = [text]
    elif re.search(r"[,，、;；\n]", text):
        selectors = [p.strip() for p in re.split(r"[,，、;；\n]+", text) if p.strip()]
    else:
        selectors = shlex.split(text)
        if not selectors:
            selectors = [text]
    selected, seen = [], set()
    for selector in selectors:
        found = matches(selector)
        if not found:
            raise ValueError(f"未找到收藏夹：{selector}。使用 /dy --list 查看名称；不使用模糊匹配。")
        if len(found) > 1:
            ids = "、".join(f["id"] for f in found)
            raise ValueError(f"收藏夹重名：{selector}（ID：{ids}）。请用 /dy 收藏夹ID:具体ID。")
        if found[0]["id"] not in seen:
            selected.append(found[0])
            seen.add(found[0]["id"])
    return selected


def read_collection(mode, config, output, cid=None, expected_count=None):
    args = argparse.Namespace(mode=mode, browser=None, id=cid, cursor=0, max_pages=100,
                              expected_count=expected_count, output=str(output))
    with redirect_stdout(io.StringIO()):
        asyncio.run(collect_folders.collect(args, config))
    return pipeline.load_json(output)


def report(state):
    counts = Counter(item["status"] for item in state["items"])
    counts = {key: counts[key] for key in ("pending", "ready", "saved", "exists", "skipped", "failed")}
    finished = state.get("planned", False) and not counts["pending"] and not counts["ready"]
    failures = [{"id": i["id"], "title": i.get("title", ""),
                 "url": i.get("url") or f"https://www.douyin.com/{'note' if i.get('kind') == 'image' else 'video'}/{i['id']}",
                 "source_url": i.get("source_url", i.get("url", "")),
                 "stage": i.get("stage"), "reason": i.get("reason", "")}
                for i in state["items"] if i["status"] == "failed"]
    unresolved = [w["source"] for w in state["warnings"]
                  if w.get("source") and w.get("reason", "").startswith("invalid_or_unresolved_link:")]
    retry_links = list(dict.fromkeys([i["url"] for i in failures] + unresolved))
    return {"run_dir": state["run_dir"], "state_file": str(Path(state["run_dir"]) / "state.json"),
            "mode": state.get("mode"), "finished": finished,
            "source_complete": state.get("source_complete", False),
            "complete": finished and state.get("source_complete", False) and not counts["failed"],
            "counts": counts, "warnings": state["warnings"], "collections": state["collections"],
            "saved_notes": [{"title": i.get("knowledge_title", i.get("title")), "note": i["note"]} for i in state["items"] if i["status"] == "saved"],
            "failures": failures, "retry_links": retry_links,
            "retry_command": "/dy " + " ".join(retry_links) if retry_links else ""}


def plan(run, state, config):
    if state["planned"]:
        return report(state)
    raw = (run / "input.txt").read_text(encoding="utf-8")
    mode, requested = parse_input(raw)
    state.update(mode=mode, source_complete=True, input_sha256=hashlib.sha256(raw.encode()).hexdigest())
    by_id = {}

    def add(row, collection=None):
        vid = pipeline.video_id(row["id"])
        if vid not in by_id:
            kind = "note" if row.get("kind") == "image" else "video"
            by_id[vid] = {"id": vid, "url": row.get("url") or f"https://www.douyin.com/{kind}/{vid}",
                          "source_url": row.get("source_url", row.get("url", "")),
                          "title": row.get("title", ""), "author": row.get("author", ""),
                          "kind": row.get("kind", "video"), "collections": [], "status": "pending"}
        if collection and collection not in by_id[vid]["collections"]:
            by_id[vid]["collections"].append(collection)

    if mode == "videos":
        for source in requested:
            try:
                url, vid = pipeline.normalize_url(source)
                add({"id": vid, "url": url, "source_url": source,
                     "kind": "image" if "/note/" in url else "video"})
            except Exception as exc:
                state["source_complete"] = False
                state["warnings"].append({"source": source, "reason": f"invalid_or_unresolved_link:{type(exc).__name__}"})
    else:
        listing = read_collection("list", config, run / "collections.json")
        if not listing.get("complete"):
            state["source_complete"] = False
            state["warnings"].append({"scope": "收藏夹列表", "reason": listing.get("reason", "incomplete")})
        folders = listing["items"]
        if mode == "list":
            state["collections"] = folders
        else:
            selected = resolve_collections(requested, folders)
            for folder in selected:
                expected = folder.get("count")
                expected = int(expected) if str(expected).isdigit() else None
                try:
                    result = read_collection("videos", config, run / f"collection-{folder['id']}.json",
                                             cid=folder["id"], expected_count=expected)
                except Exception as exc:
                    result = {"items": [], "complete": False, "reason": f"request_failed:{type(exc).__name__}"}
                state["collections"].append(dict(folder, enumerated=len(result["items"]), complete=result.get("complete", False)))
                if not result.get("complete"):
                    state["source_complete"] = False
                    state["warnings"].append({"collection": folder["name"], "id": folder["id"],
                                              "expected": expected, "actual": len(result["items"]),
                                              "reason": result.get("reason", "incomplete")})
                for row in result["items"]:
                    add(row, folder["name"])
    for item in by_id.values():
        note = existing(item["id"], config)
        if note:
            item.update(status="exists", note=str(note))
    state["items"] = list(by_id.values())
    state["planned"] = True
    write_state(run, state)
    return report(state)


def call_pipeline(function, args, config):
    with redirect_stdout(io.StringIO()) as output:
        function(args, config)
    return json.loads(output.getvalue().strip().splitlines()[-1])


def ready_result(item):
    return {"status": "ready", "id": item["id"], "source_url": item["url"],
            "source_title": item["title"], "media_kind": item.get("kind", "video"),
            "collections": item["collections"], **item["artifacts"]}


def model_material(transcript, bundle, target):
    """Lossless video text view; full timestamps/hashes stay in transcript.json."""
    if bundle.get("media_kind", "video") == "image":
        return transcript
    raw = pipeline.load_json(transcript)
    pipeline.validate_transcript(raw)
    path = target / "material.txt"
    path.write_text(raw["text"], encoding="utf-8")
    return path


def prepare_worker(run, state, item, config):
    # Each process has its own stdout and durable checkpoint; only the parent
    # writes the batch state. Shared browser/GPU resources have separate locks.
    run = Path(run)
    checkpoint = run / ".workers" / item["id"]
    previous = checkpoint / "state.json"
    if previous.is_file():
        old = pipeline.load_json(previous)
        if len(old.get("items", [])) == 1 and old["items"][0]["id"] == item["id"]:
            item = old["items"][0]
    child = dict(state, items=[item])
    next_item(checkpoint, child, dict(config, _artifacts_run=str(run)))
    return child["items"][0]


def prepare_parallel(run, state, config, workers):
    pending = [item for item in state["items"] if item["status"] not in TERMINAL | {"ready"}]
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(prepare_worker, str(run), state, item, config): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            # Worker failures propagate; checkpoints preserve recovery budgets
            # and can be resumed without declaring a missing product result.
            item.update(future.result())
            write_state(run, state)
    ready = [ready_result(item) for item in state["items"] if item["status"] == "ready"]
    return {"status": "prepared" if ready else "done", "ready": ready,
            "model_analysis_needed": bool(ready), **report(state)}


def prepare_item(run, state, item, config):
    args = argparse.Namespace(source=item["url"], url=None, browser=None,
                              title=item["title"] or None, author=item["author"] or None,
                              collection=item["collections"] or None)
    try:
        initial_config = config
        attempted = item.get("browser_recovery", {}).get("attempted")
        browser_flow = (config.get("browser") == "chrome" and pipeline.sys.platform == "darwin"
                        and config.get("auto_browser_recovery", True) and not attempted)
        if browser_flow and config.get("prefer_browser_media", False):
            initial_config = dict(config, _browser_media_only=True)
        elif browser_flow and config.get("background_detail_first", False):
            # Same matched detail JSON as the page would deliver, fetched in the background;
            # only when it is unavailable does recovery open (and later close) a tab.
            initial_config = dict(config, _background_detail_only=True)
        return call_pipeline(pipeline.prepare, args, initial_config)
    except pipeline.image_posts.StageError as exc:
        if exc.code != "DETAIL_UNAVAILABLE" or config.get("auto_browser_recovery", True) is not True:
            raise
        if item.get("browser_recovery", {}).get("attempted"):
            raise pipeline.image_posts.StageError("detail", "浏览器恢复机会已使用；本条停止重试。",
                                                  "RECOVERY_EXHAUSTED") from None
        recovery = {"attempted": True, "status": "started", "first_failure": str(exc),
                    "started_at": datetime.now(timezone.utc).isoformat()}
        item.update(stage="browser_recovery", browser_recovery=recovery)
        # Persist before opening: interrupted/resumed runs cannot loop browser refresh.
        write_state(run, state)
    with locked(Path(config["cache_dir"]) / ".locks" / "browser-recovery.lock"):
        return recover_item(run, state, item, config, args, recovery)


def recover_item(run, state, item, config, args, recovery):
    # Close only the tab this recovery opens, after its media has been read (or it failed).
    track = bool(config.get("close_opened_tabs")) and config.get("browser") == "chrome"
    before = pipeline.douyin_access.douyin_tab_ids(item["id"], "chrome") if track else None
    try:
        return _recover_item(run, state, item, config, args, recovery)
    finally:
        if track:
            closed = pipeline.douyin_access.close_new_tabs(item["id"], before, "chrome")
            recovery["opened_tab_closed"] = closed if closed is not None else "unavailable"
            write_state(run, state)


def _recover_item(run, state, item, config, args, recovery):
    baseline = pipeline.browser_cache.snapshot_entries(config.get("browser"), config.get("browser_cache_dir"))
    probe = {}
    recovery["cache_baseline_entries"] = len(baseline) if baseline is not None else None
    recovery["cache_probe"] = probe
    cache_config = dict(config, _browser_cache_baseline=baseline, _browser_cache_diagnostics=probe)
    try:
        recovery.update(pipeline.douyin_access.refresh_browser_session(
            item["id"], config.get("browser"), config.get("browser_recovery_wait_seconds", 8),
            source_url=item["url"], profile=config.get("browser_profile")))
        recovery["status"] = "retrying"
        retry_config = dict(cache_config, _browser_media_wait_seconds=config.get("browser_media_timeout_seconds", 40))
        recovery["media_wait_timeout_seconds"] = retry_config["_browser_media_wait_seconds"]
        write_state(run, state)
        try:
            result = call_pipeline(pipeline.prepare, args, retry_config)
        except pipeline.image_posts.StageError as exc:
            if exc.code != "DETAIL_UNAVAILABLE":
                raise
            # HTTP fallbacks take time too. Check once for a response that
            # arrived during them, without opening again or repeating HTTP.
            result = call_pipeline(pipeline.prepare, args,
                                   dict(cache_config, _browser_media_only=True, _browser_media_wait_seconds=0))
    except Exception as exc:
        recovery.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(),
                        failure_code=getattr(exc, "code", None) or type(exc).__name__)
        write_state(run, state)
        if isinstance(exc, pipeline.image_posts.StageError) and exc.code == "DETAIL_UNAVAILABLE":
            if probe.get("scan_incomplete") or probe.get("deadline_reached"):
                raise pipeline.image_posts.StageError("detail",
                    "缓存查找未完成或超时，尚不能判断作品媒体是否可提取。"
                    "已停止本条、未写入笔记；应检查缓存读取性能，不能据此要求重新登录或等待几天。",
                    "CACHE_LOOKUP_INCOMPLETE") from None
            raise pipeline.image_posts.StageError("detail", "媒体提取失败：本次未取得与作品 ID 匹配的完整媒体。"
                "已停止本条，不再循环；未写入笔记。浏览器登录/验证码状态未观察，不能据此建议重新登录。"
                "页面能正常播放时应检查提取适配；扫描统计见 browser_recovery.cache_probe。",
                "RECOVERY_EXHAUSTED") from None
        raise
    recovery.update(status="recovered", finished_at=datetime.now(timezone.utc).isoformat())
    write_state(run, state)
    return result


def next_item(run, state, config, prepare_all=False):
    if not state["planned"]:
        raise ValueError("请先执行 plan。")
    workers = config.get("prepare_workers", 1)
    if type(workers) is not int or not 1 <= workers <= 3:
        raise ValueError("prepare_workers 必须是 1–3 的整数。")
    if prepare_all and workers > 1:
        return prepare_parallel(run, state, config, workers)
    for item in state["items"]:
        if item["status"] in TERMINAL:
            continue
        vid = item["id"]
        note = existing(vid, config)
        if note:
            item.update(status="exists", note=str(note))
            write_state(run, state)
            continue
        if item["status"] == "ready":
            if prepare_all:
                continue
            return ready_result(item)
        stage = "prepare"
        try:
            item.update(stage=stage)
            write_state(run, state)
            with locked(Path(config["cache_dir"]) / ".locks" / f"{vid}.lock"):
                result = prepare_item(run, state, item, config)
                bundle_path = Path(result["bundle"])
                stage = "extract"
                item.update(stage=stage)
                write_state(run, state)
                with locked(Path(config["cache_dir"]) / ".locks" / "local-recognition.lock"):
                    call_pipeline(pipeline.transcribe, argparse.Namespace(bundle=str(bundle_path), model=None), config)
                target = Path(config.get("_artifacts_run", run)) / "items" / vid
                target.mkdir(parents=True, exist_ok=True)
                transcript = target / "transcript.json"
                shutil.copyfile(bundle_path.parent / "transcript.json", transcript)
                bundle = pipeline.load_json(bundle_path)
                material = model_material(transcript, bundle, target)
                catalog_path = target / "catalog.json"
                pipeline.write_json(catalog_path, pipeline.library.catalog(config))
                item.update(status="ready", stage="analysis", kind=bundle.get("media_kind", "video"),
                            title=bundle["title"], bundle=str(bundle_path),
                            transcript_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest(),
                            material_sha256=hashlib.sha256(material.read_bytes()).hexdigest(),
                            artifacts={"transcript_file": str(transcript), "material_file": str(material),
                                       "knowledge_file": str(target / "knowledge.md"),
                                       "analysis_file": str(target / "analysis.json"), "reason_file": str(target / "reason.txt"),
                                       "catalog_file": str(catalog_path),
                                       "image_files": [str(bundle_path.parent / p["file"]) for p in bundle.get("images", [])]})
        except Exception as exc:
            safe_reason = str(exc) if isinstance(exc, (ValueError, pipeline.image_posts.StageError)) else type(exc).__name__
            item.update(status="failed", stage=getattr(exc, "stage", stage),
                        error_code=getattr(exc, "code", type(exc).__name__), reason=f"{stage} 失败：{safe_reason}"[:1200])
        write_state(run, state)
        if prepare_all:
            continue
        if item["status"] == "ready":
            return ready_result(item)
        return {"status": "failed", "id": vid, "reason": item["reason"],
                "browser_recovery": item.get("browser_recovery")}
    if prepare_all:
        ready = [ready_result(item) for item in state["items"] if item["status"] == "ready"]
        return {"status": "prepared" if ready else "done", "ready": ready,
                "model_analysis_needed": bool(ready), **report(state)}
    return {"status": "done", **report(state)}


def select_item(state, vid):
    vid = pipeline.video_id(vid)
    found = next((i for i in state["items"] if i["id"] == vid), None)
    if found is None:
        raise ValueError("该视频不在本次输入清单中。")
    return found


def commit(run, state, config, vid):
    item = select_item(state, vid)
    # Lock only the write step; speech recognition and model reasoning don't
    # hold the vault lock. A second job may have imported this video meanwhile.
    with locked(Path(config["vault"]) / ".douyin-import.lock"):
        note = existing(vid, config)
        if note:
            prior = item["status"]
            item.update(status="saved" if prior == "saved" else "exists", note=str(note))
            write_state(run, state)
            return {"status": "exists", "id": vid, "note": str(note), "written": False}
        if item["status"] != "ready":
            raise ValueError("此视频尚未准备好知识正文，不能提交。")
        target = run / "items" / vid
        analysis = pipeline.load_json(target / "analysis.json")
        title, tags = analysis.get("title"), analysis.get("tags", [])
        if not isinstance(title, str) or not title.strip() or not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("analysis.json 需要非空 title 和字符串数组 tags。")
        with locked(Path(config["cache_dir"]) / ".locks" / f"{vid}.lock"):
            current_transcript = Path(item["bundle"]).parent / "transcript.json"
            if hashlib.sha256(current_transcript.read_bytes()).hexdigest() != item["transcript_sha256"]:
                raise ValueError("音频转写在本次提炼后发生变化；请重新获取该视频并提炼，未写入笔记。")
            if item.get("material_sha256") and hashlib.sha256(Path(item["artifacts"]["material_file"]).read_bytes()).hexdigest() != item["material_sha256"]:
                raise ValueError("本次模型材料已变化，未写入笔记。")
            result = call_pipeline(pipeline.save, argparse.Namespace(bundle=item["bundle"], analysis=str(target / "analysis.json"),
                                   summary=str(target / "knowledge.md"), vault=config["vault"],
                                   title=title, collection=item["collections"] or None, tag=tags,
                                   archive_source=False), config)
        item.update(status=result["status"], stage="done", note=result["note"], knowledge_title=result.get("title", title))
        if result.get("unified_sync_warning"):
            state["warnings"].append({"scope": "知识总索引/导入总日志", "reason": result["unified_sync_warning"]})
        write_state(run, state)
        return {"id": vid, **result}


def mark_item(run, state, vid, status):
    item = select_item(state, vid)
    if item["status"] in TERMINAL:
        raise ValueError("该视频已有最终结果，不修改它。")
    reason_file = run / "items" / vid / "reason.txt"
    reason = reason_file.read_text(encoding="utf-8").strip()
    if not reason:
        raise ValueError("请将具体原因写入该视频的 reason_file。")
    item.update(status=status, reason=reason[:1000])
    write_state(run, state)
    return {"id": vid, "status": status, "reason": item["reason"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(pipeline.DEFAULT_CONFIG))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建隔离的本次运行目录")
    for command in ("plan", "next", "prepare-all", "commit", "skip", "fail", "status"):
        p = commands.add_parser(command)
        p.add_argument("--run", required=True)
        if command in ("commit", "skip", "fail"):
            p.add_argument("--id", required=True)
    args = parser.parse_args()
    try:
        config = pipeline.load_json(args.config)
        if args.command == "init":
            result = initialize(config)
        else:
            run, _, _ = load_run(args.run, config)
            with locked(run / ".lock"):
                run, state, frozen = load_run(args.run, config)
                if args.command == "plan":
                    result = plan(run, state, frozen)
                elif args.command == "next":
                    result = next_item(run, state, frozen)
                elif args.command == "prepare-all":
                    result = next_item(run, state, frozen, prepare_all=True)
                elif args.command == "commit":
                    if frozen.get("vault") != config.get("vault") or frozen.get("notes_dir") != config.get("notes_dir"):
                        raise ValueError("批次创建于旧知识目录，请重新 init；禁止写回迁移前位置。")
                    result = commit(run, state, frozen, args.id)
                elif args.command in ("skip", "fail"):
                    result = mark_item(run, state, args.id, "skipped" if args.command == "skip" else "failed")
                else:
                    result = report(state)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
