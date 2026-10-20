#!/usr/bin/env python3
"""Read-only installation checks: no Cookie extraction, media download or Vault write."""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

SKILL = Path(__file__).resolve().parents[1]


def inspect(config_path, workspace=None):
    checks = []

    def record(name, passed, detail, severity="error"):
        checks.append({"name": name, "passed": bool(passed), "severity": severity, "detail": detail})

    record("platform", platform.system() == "Darwin" and platform.machine() == "arm64",
           f"{platform.system()} {platform.machine()}; full support requires Apple Silicon macOS")
    record("python", sys.version_info[:2] == (3, 12), sys.version.split()[0])
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("配置根节点必须是对象")
    except (OSError, ValueError) as exc:
        record("config", False, str(exc))
        return checks
    required = ["vault", "unified_root_vault", "cache_dir", "python", "browser",
                "browser_profile", "browser_cache_dir", "notes_dir", "attachments_dir"]
    missing = [key for key in required if not isinstance(config.get(key), str) or not config[key]]
    record("required_configuration", not missing, {"missing": missing})
    if missing:
        return checks
    for key in ("vault", "unified_root_vault", "cache_dir", "browser_profile", "browser_cache_dir"):
        path = Path(config[key]).expanduser()
        record(key, path.is_absolute() and path.is_dir(), str(path))
    record("vault_binding", Path(config["vault"]).expanduser().resolve() ==
           Path(config["unified_root_vault"]).expanduser().resolve(), "two Vault fields must match")
    record("vault_permissions", os.access(Path(config["vault"]).expanduser(), os.R_OK | os.W_OK | os.X_OK),
           "checks access flags without writing notes")
    for key in ("notes_dir", "attachments_dir"):
        relative = Path(config[key])
        record(key, not relative.is_absolute() and ".." not in relative.parts,
               "must be a relative directory inside the Vault")
    configured_python = Path(config["python"]).expanduser().absolute()
    record("configured_python", configured_python == Path(sys.executable).absolute(),
           {"configured": str(configured_python), "running": sys.executable})
    record("browser", config["browser"] == "chrome", "complete browser recovery supports Chrome")
    chrome = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    record("chrome_application", any(path.is_file() for path in chrome), "Google Chrome application")
    for name in ("f2", "f2-trace"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
    for module in ("yaml", "PIL", "brotli", "yt_dlp", "imageio_ffmpeg", "mlx_whisper",
                   "f2.apps.douyin.crawler", "f2.apps.douyin.model"):
        try:
            loaded = importlib.import_module(module)
            record("import:" + module, True, str(getattr(loaded, "__file__", "")))
        except Exception as exc:
            record("import:" + module, False, f"{type(exc).__name__}: {exc}")
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=15)
        record("ffmpeg", result.returncode == 0, result.stdout.splitlines()[:1])
    except Exception as exc:
        record("ffmpeg", False, f"{type(exc).__name__}: {exc}")
    node = shutil.which("node")
    record("node", node is not None, node or "F2 signatures need a Node.js runtime")
    xcrun = shutil.which("xcrun")
    if xcrun:
        result = subprocess.run([xcrun, "--find", "swift"], capture_output=True, text=True, timeout=15)
        record("swift", result.returncode == 0, result.stdout.strip() or result.stderr.strip())
    else:
        record("swift", False, "Install Xcode Command Line Tools for Apple Vision OCR")
    if workspace is None and SKILL.parent.name == "skills" and SKILL.parent.parent.name == ".claude":
        workspace = SKILL.parents[2]
    if workspace is not None:
        try:
            metadata = json.loads((SKILL / ".dy-install.json").read_text(encoding="utf-8"))
            settings = json.loads((workspace / ".claude/settings.json").read_text(encoding="utf-8"))
            groups = settings.get("hooks", {}).get("UserPromptSubmit", [])
            matches = [hook for group in groups for hook in group["hooks"]
                       if hook.get("command") == metadata["hook_command"]]
            record("hook", len(matches) == 1, "exactly one installed UserPromptSubmit hook")
            command = workspace / ".claude/commands/dy.md"
            text = command.read_text(encoding="utf-8")
            record("slash_command", str(SKILL / "SKILL.md") in text and
                   "DY_RAW_ARGUMENTS_BEGIN" in text, "installed /dy command")
            record("dependencies_installed", metadata.get("dependencies_installed") is True,
                   "skip-dependencies is a development option only")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            record("integration", False, f"{type(exc).__name__}: {exc}")
    else:
        record("integration", False, "Run the installed copy or pass --workspace")
    record("online_access", True,
           "NOT_TESTED: no Cookie read, login check, model download or Douyin request", "info")
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SKILL / "config.json")
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    checks = inspect(args.config, args.workspace)
    passed = all(item["passed"] for item in checks if item["severity"] == "error")
    print(json.dumps({"ok": passed, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
