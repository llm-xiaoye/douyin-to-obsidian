import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dy_installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SettingsTests(unittest.TestCase):
    def test_preserves_settings_and_does_not_duplicate_hook(self):
        settings = {"permissions": {"defaultMode": "default"},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]}],
                              "UserPromptSubmit": [{"hooks": [
                                  {"type": "command", "command": "other"}]}]}}
        first = installer.merged_settings(settings, "new-dy-hook")
        second = installer.merged_settings(first, "new-dy-hook")
        self.assertEqual(first, second)
        self.assertEqual(first["permissions"], settings["permissions"])
        self.assertEqual(first["hooks"]["Stop"], settings["hooks"]["Stop"])
        self.assertEqual(settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "other")
        self.assertEqual(len(first["hooks"]["UserPromptSubmit"]), 2)

    def test_malformed_settings_are_rejected(self):
        for settings in ({"hooks": []}, {"hooks": {"UserPromptSubmit": {}}},
                         {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command"}]}]}}):
            with self.assertRaises(ValueError):
                installer.merged_settings(settings, "new-dy-hook")


@unittest.skipUnless(platform.system() == "Darwin" and platform.machine() == "arm64"
                     and sys.version_info[:2] == (3, 12), "full installer requires Apple Silicon Python 3.12")
class InstallationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dy-install-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "工作 区"
        self.vault = self.root / "知识 库"
        self.profile = self.root / "Chrome Profile"
        self.browser_cache = self.root / "Chrome Cache"
        for path in (self.workspace, self.vault, self.profile, self.browser_cache):
            path.mkdir()
        self.skill = self.workspace / ".claude/skills/douyin-to-obsidian"
        self.command = [sys.executable, "-B", str(ROOT / "scripts/install.py"),
                        "--workspace", str(self.workspace), "--vault", str(self.vault),
                        "--browser-profile", str(self.profile),
                        "--browser-cache-dir", str(self.browser_cache),
                        "--cache-dir", str(self.root / "运行 缓存"), "--skip-dependencies"]

    def invoke(self, command=None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        result = subprocess.run(command or self.command, capture_output=True, text=True, env=env)
        return result, json.loads(result.stdout if result.returncode == 0 else result.stderr)

    def test_real_install_repeat_hook_and_local_module_origin(self):
        settings_file = self.workspace / ".claude/settings.json"
        settings_file.parent.mkdir()
        settings_file.write_text(json.dumps({"permissions": {"defaultMode": "default"},
                                            "hooks": {"Stop": [{"hooks": [
                                                {"type": "command", "command": "keep-me"}]}]}}))
        first, report = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(report["status"], "installed")
        config_path = self.skill / "config.json"
        config = json.loads(config_path.read_text())
        self.assertEqual(config["vault"], str(self.vault))
        self.assertEqual(config["browser_profile"], str(self.profile))
        config["prepare_workers"] = 3
        config_path.write_text(json.dumps(config))
        second, _ = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(config_path.read_text())["prepare_workers"], 3)
        settings = json.loads(settings_file.read_text())
        self.assertEqual(settings["permissions"], {"defaultMode": "default"})
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "keep-me")
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
        python = self.skill / ".venv/bin/python"
        event = json.dumps({"hook_event_name": "UserPromptSubmit",
                            "prompt": "复制打开抖音 https://www.douyin.com/video/7659111274168454446"})
        hook = subprocess.run([str(python), "-B", str(self.skill / "scripts/auto_route.py")],
                              input=event, capture_output=True, text=True)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertIn(str(self.skill / "SKILL.md"), json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"])
        probe = "import sys,json;sys.path.insert(0,sys.argv[1]);import auto_route;print(json.dumps({'file':auto_route.__file__,'prefix':sys.prefix}))"
        origin = subprocess.run([str(python), "-B", "-c", probe, str(self.skill / "scripts")],
                                capture_output=True, text=True, check=True)
        observed = json.loads(origin.stdout)
        self.assertEqual(Path(observed["file"]), self.skill / "scripts/auto_route.py")
        self.assertEqual(Path(observed["prefix"]), self.skill / ".venv")
        self.assertNotIn("{{SKILL_FILE_JSON}}", (self.workspace / ".claude/commands/dy.md").read_text())

    def test_rejects_missing_profile_before_writing_install(self):
        missing = self.root / "does-not-exist"
        command = list(self.command)
        command[command.index("--browser-profile") + 1] = str(missing)
        result, report = self.invoke(command)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "error")
        self.assertFalse(self.skill.exists())

    def test_existing_unmanaged_install_is_preserved(self):
        self.skill.mkdir(parents=True)
        sentinel = self.skill / "keep.txt"
        sentinel.write_text("personal installation")
        result, _ = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sentinel.read_text(), "personal installation")
        self.assertFalse((self.skill / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
