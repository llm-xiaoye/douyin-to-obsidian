#!/usr/bin/env python3
"""Install /dy into an existing cc-connect Claude Code workspace."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "skills" / "douyin-to-obsidian"
MARKER = ".dy-install.json"


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def directory(value, label):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label}必须是已存在的目录：{path}")
    return path


def vault_catalogs(vault, *, initialize=False):
    """Validate existing catalogs; bootstrap only when both documents are absent."""
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(SOURCE / "scripts"))
        import unified_store
    finally:
        sys.path[:] = original_path
    if Path(unified_store.__file__).resolve() != (SOURCE / "scripts/unified_store.py").resolve():
        raise ValueError("总览工具必须来自本次下载的 skill")
    paths = [vault / unified_store.INDEX_NAME, vault / unified_store.LOG_NAME]

    def inspect_or_create():
        if any(path.is_symlink() for path in paths):
            raise ValueError("总览不能是符号链接；未覆盖现有文件")
        present = [path.exists() for path in paths]
        if all(present):
            unified_store.load(vault)
            return "preserved"
        if any(present):
            raise ValueError("两份总览必须同时存在；检测到部分文件，先备份检查，未覆盖")
        index = unified_store.render_index([])
        log = unified_store.render_log([])
        unified_store.validate_rendered(index, log, 0, 0)
        if not initialize:
            return "initialization_needed"
        for path, content in zip(paths, (index, log)):
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        unified_store.load(vault)
        return "initialized"

    if not initialize:
        return inspect_or_create()
    import fcntl
    if (vault / ".knowledge-migration-in-progress.json").exists() or (
            vault.parent / ".knowledge-migration-in-progress.json").exists():
        raise ValueError("知识库正在迁移，未初始化总览")
    descriptor = os.open(vault / ".unified-catalog.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            return inspect_or_create()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def merged_settings(settings, command, previous_command=None):
    result = copy.deepcopy(settings)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json 的 hooks 必须是对象")
    groups = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(groups, list):
        raise ValueError("UserPromptSubmit 必须是数组")
    found = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError("UserPromptSubmit 的每组必须包含 hooks 数组")
        for hook in group["hooks"]:
            if not isinstance(hook, dict):
                raise ValueError("hook 必须是对象")
            if hook.get("type") == "command" and not isinstance(hook.get("command"), str):
                raise ValueError("command hook 必须包含 command 字符串")
            if hook.get("type") == "command" and (hook.get("command") == command or
                    (previous_command is not None and hook.get("command") == previous_command)):
                if found:
                    raise ValueError("发现重复 /dy hook，请先检查已有设置")
                hook["command"] = command
                found = True
    if not found:
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    return result


def install(args):
    if sys.version_info[:2] != (3, 12):
        raise ValueError("请使用 Python 3.12 的绝对路径启动安装器")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("完整功能要求 Apple Silicon macOS；其他平台尚未适配")
    workspace = directory(args.workspace, "cc-connect 工作区")
    vault = directory(args.vault, "Obsidian Vault")
    profile = directory(args.browser_profile, "Chrome Profile")
    if args.browser_cache_dir:
        browser_cache = directory(args.browser_cache_dir, "Chrome Cache_Data")
    else:
        chrome_root = Path.home() / "Library/Application Support/Google/Chrome"
        try:
            relative = profile.relative_to(chrome_root.resolve())
        except ValueError as exc:
            raise ValueError("自定义 Chrome 用户数据目录须显式传 --browser-cache-dir") from exc
        if len(relative.parts) != 1:
            raise ValueError("请指定 chrome://version 显示的完整 Profile 目录")
        browser_cache = directory(
            Path.home() / "Library/Caches/Google/Chrome" / relative / "Cache/Cache_Data",
            "Chrome Cache_Data")
    cache = Path(args.cache_dir or Path.home() / "Library/Caches/douyin-to-obsidian").expanduser().resolve()
    if cache == vault or vault in cache.parents or cache == profile or profile in cache.parents:
        raise ValueError("运行缓存请放在 Vault 和 Chrome Profile 之外")
    destination = workspace / ".claude/skills/douyin-to-obsidian"
    if destination.is_symlink():
        raise ValueError("目标 skill 是软链接；请用独立工作区安装，避免改动已有源码")
    metadata_path = destination / MARKER
    previous = read_object(metadata_path) if metadata_path.is_file() else None
    if destination.exists() and (not previous or previous.get("installation_complete") is not True):
        raise ValueError("目标目录已存在且不是完整的受管安装；请保留现场并选择另一目录")
    python = destination / ".venv/bin/python"
    config = read_object(SOURCE / "config.example.json")
    config.update(vault=str(vault), unified_root_vault=str(vault), python=str(python),
                  cache_dir=str(cache), browser_profile=str(profile),
                  browser_cache_dir=str(browser_cache))
    if previous:
        old_config = read_object(destination / "config.json")
        for key in ("vault", "unified_root_vault", "python", "cache_dir",
                    "browser_profile", "browser_cache_dir"):
            if old_config.get(key) != config[key]:
                raise ValueError(f"已有配置的 {key} 与本次参数不同；请显式编辑本地 config.json")
        config = old_config
    settings_path = workspace / ".claude/settings.json"
    settings = read_object(settings_path) if settings_path.exists() else {}
    hook_command = shlex.join(["/usr/bin/env", "PYTHONDONTWRITEBYTECODE=1",
                              str(python), "-B", str(destination / "scripts/auto_route.py")])
    updated_settings = merged_settings(settings, hook_command,
                                      previous.get("hook_command") if previous else None)
    template = (SOURCE / "integrations/cc-connect/dy.md").read_text(encoding="utf-8")
    command_text = template.replace("{{SKILL_FILE_JSON}}",
                                    json.dumps(str(destination / "SKILL.md"), ensure_ascii=False))
    command_path = workspace / ".claude/commands/dy.md"
    if command_path.exists():
        digest = hashlib.sha256(command_path.read_bytes()).hexdigest()
        if not previous or digest != previous.get("command_sha256"):
            raise ValueError("已有 /dy 命令不是本安装器上次生成的内容；请先备份并处理冲突")
    vault_catalogs(vault)
    if not previous:
        destination.mkdir(parents=True, exist_ok=False)
        write_json(metadata_path, {"installation_complete": False})
    cache.mkdir(parents=True, exist_ok=True)
    for path in sorted(SOURCE.rglob("*")):
        relative = path.relative_to(SOURCE)
        if path.is_symlink():
            raise ValueError(f"发布技能中不应包含软链接：{relative}")
        if path.is_file() and path.name != "config.json" and "__pycache__" not in path.parts:
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
    if not previous:
        subprocess.run([sys.executable, "-B", "-m", "venv", str(destination / ".venv")], check=True)
    if not args.skip_dependencies:
        subprocess.run([str(python), "-B", "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(REPOSITORY / "requirements.lock.txt")], check=True, stdout=sys.stderr)
        subprocess.run([str(python), "-B", "-m", "pip", "check"], check=True, stdout=sys.stderr)
    catalog_status = vault_catalogs(vault, initialize=True)
    write_json(destination / "config.json", config)
    if updated_settings != settings and settings_path.exists():
        backup_dir = workspace / ".claude/dy-settings-backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copyfile(settings_path, backup_dir / f"settings-{stamp}-{uuid.uuid4().hex[:8]}.json")
    if updated_settings != settings:
        write_json(settings_path, updated_settings)
    command_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.write_text(command_text, encoding="utf-8")
    write_json(metadata_path, {
        "installation_complete": True, "version": "0.1.0",
        "dependencies_installed": not args.skip_dependencies,
        "hook_command": hook_command,
        "command_sha256": hashlib.sha256(command_text.encode("utf-8")).hexdigest(),
    })
    return {
        "status": "installed", "skill": str(destination), "config": str(destination / "config.json"),
        "vault_catalogs": catalog_status,
        "dependencies_installed": not args.skip_dependencies,
        "doctor_command": shlex.join([str(python), "-B", str(destination / "scripts/doctor.py")]),
        "next": "执行 doctor_command；通过后在使用该工作区的新 cc-connect 会话发送 /dy 链接",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--browser-profile", required=True)
    parser.add_argument("--browser-cache-dir")
    parser.add_argument("--cache-dir")
    parser.add_argument("--skip-dependencies", action="store_true",
                        help="仅供开发测试；不安装依赖，不代表可运行完整功能")
    try:
        result = install(parser.parse_args())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
