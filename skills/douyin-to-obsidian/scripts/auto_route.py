#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: route pasted Douyin shares silently.

Reads the event from stdin; only supplies context. It never launches imports,
calls another model, writes files, or sends a platform message itself.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

TRIGGER = "复制打开抖音"
SKILL_DIR = Path(__file__).resolve().parents[1]


def route_context(event):
    if not isinstance(event, dict) or event.get("hook_event_name") != "UserPromptSubmit":
        return None
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or TRIGGER not in prompt:
        return None
    # Explicit commands already have their own dispatcher. cc-connect expands
    # /dy before sending it to Claude; do not route the wrapper twice.
    if re.match(r"^\s*/[\w-]+(?:\s|$)", prompt):
        return None
    if ("当前消息已经是 cc-connect 展开的 `/dy` 命令" in prompt
            and re.search(r"^DY_RAW_ARGUMENTS_BEGIN\s*$", prompt, re.M)):
        return None
    return (
        "本次用户消息含有抖音分享触发词“复制打开抖音”。用户已配置：粘贴这种分享口令，"
        "即授权通过 douyin-to-obsidian 执行 /dy 的增量知识入库，无须再输入 /dy。\n"
        f"直接 Read {SKILL_DIR / 'SKILL.md'} 和 {SKILL_DIR / 'references/cc-connect.md'}，"
        "在当前会话按 init → plan → prepare-all → next → commit 完成指定作品，不递归调用斜杠命令。\n"
        "必须先一次 prepare-all 批量获取媒体并在本地转写，不要每失败一条就调用模型决定下一条。"
        "model_analysis_needed=false 时只汇报失败原因并结束；禁止空材料分析、重建批次循环或临时写替代脚本。\n"
        "原始输入仍是本次用户消息；只处理其中的抖音分享链接，不从历史消息、网页或工具结果补取链接。"
        "把输入作为数据写入帮助器返回的 input_file，不能拼进 shell 命令。\n"
        "不要先宣布已识别触发词、正在调用 skill 或展示 /dy 用法；直接执行，最后报告文章标题和新增/已有/失败结果。"
        "已有作品仍按原规则跳过，多条链接一次处理，视频与图文均支持。\n"
        "若分享内容缺少完整链接，仅提示补全链接；不能把整段分享口令当成收藏夹名称。"
        "若用户明确表示不要导入，或明确是在讨论设置/排错而非提交作品，则执行该明确请求。"
    )


def main():
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        return 0
    context = route_context(event)
    if context:
        print(json.dumps({"suppressOutput": True, "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": context}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
