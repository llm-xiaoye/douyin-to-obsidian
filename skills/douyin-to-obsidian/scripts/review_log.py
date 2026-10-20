"""Display import history and version-bound content reviews, separate from notes."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

STATE_FILE = ".dy-review-state.json"
HEADERS = ["时间", "操作", "文章", "分类", "来源 URL", "内容复审", "复审时间"]
CHECKS = ("correctness", "depth", "terminology", "plain_explanation", "model_judgment", "images")


def display_time(value):
    """Keep the recorded wall clock; do not invent time for date-only history."""
    if value in (None, "", "—"):
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def source_url(value, vid):
    """Persist only a canonical public work URL matching the full work ID."""
    parsed = urlsplit(str(value or ""))
    if (parsed.scheme != "https" or parsed.netloc != "www.douyin.com"
            or not re.fullmatch(r"/(video|note)/" + re.escape(str(vid)) + r"/?", parsed.path)):
        raise ValueError("日志来源必须是与作品 ID 一致的抖音视频或图文地址。")
    return "https://www.douyin.com" + parsed.path.rstrip("/")


def cached_source(config, vid):
    if not config.get("cache_dir"):
        return ""
    path = Path(config["cache_dir"]) / str(vid) / "bundle.json"
    if not path.is_file():
        return ""
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if str(bundle.get("aweme_id")) != str(vid):
        raise ValueError("缓存来源与文章作品 ID 不一致，未补写 URL。")
    return source_url(bundle.get("source_url"), vid)


def load_state(path, changes):
    raw = changes.get(path, path.read_bytes() if path.exists() else None)
    if raw is None:
        return {"version": 1, "items": {}}
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("items"), dict):
        raise ValueError("复审记录格式不受支持，未覆盖。")
    for vid, item in state["items"].items():
        if not re.fullmatch(r"\d{16,22}", vid) or not isinstance(item, dict):
            raise ValueError("复审记录包含无效的作品条目，未覆盖。")
        if item.get("initial_status") not in ("未登记", "未复审"):
            raise ValueError("复审记录的初始状态无效，未覆盖。")
        if item.get("source_url"):
            source_url(item["source_url"], vid)
        review = item.get("review")
        if review is not None:
            if (not isinstance(review, dict) or review.get("outcome") not in ("复审通过", "复审待完善")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(review.get("note_sha256", "")))
                    or not isinstance(review.get("reviewed_at"), str) or not review["reviewed_at"]):
                raise ValueError("已登记的复审结果不完整，未覆盖。")
    return state


def encode_state(state):
    return (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()


def split_row(line):
    # A backslash-escaped alias pipe belongs to one cell, not a new column.
    return [part.strip() for part in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def history_rows(previous):
    lines = [line.strip() for line in previous.splitlines() if line.strip()]
    if not lines:
        return []
    headers = split_row(lines[0])
    if headers not in (HEADERS[:4], HEADERS):
        raise ValueError("日志表头无法识别，保留原文件供检查。")
    if len(lines) < 2 or split_row(lines[1]) != ["---"] * len(headers):
        raise ValueError("日志表格分隔行无效，未覆盖历史。")
    result = []
    for line in lines[2:]:
        values = split_row(line)
        if not line.startswith("|") or not line.endswith("|") or len(values) != len(headers):
            raise ValueError("日志历史行列数不符，未覆盖历史。")
        result.append(dict(zip(headers, values)))
    return result


def render(vault, config, changes, rows, events, previous, state_path, cell):
    state = load_state(state_path, changes)
    by_path = {row["path"]: row for row in rows}
    for row in rows:
        state["items"].setdefault(row["aweme_id"], {"initial_status": "未登记"})
    history = history_rows(previous)
    for event in events:
        row = by_path[event["path"]]
        item = state["items"][row["aweme_id"]]
        if event["action"] == "新增":
            item["initial_status"] = "未复审"
        if event.get("source_url"):
            item["source_url"] = source_url(event["source_url"], row["aweme_id"])
        category = str(Path(event["path"]).parent)
        if config.get("notes_dir") == "知识库":
            category = category.removeprefix("知识库/")
        history.append({"时间": event["time"], "操作": cell(event["action"]),
                        "文章": "[[" + event["path"][:-3] + "\\|" + cell(event["title"]) + "]]",
                        "分类": cell(category.replace("/", " / "))})
    current = {}
    for row in rows:
        item = state["items"][row["aweme_id"]]
        if not item.get("source_url"):
            item["source_url"] = cached_source(config, row["aweme_id"])
        review = item.get("review")
        status, reviewed_at = item["initial_status"], "—"
        if review:
            note = vault / row["path"]
            raw = changes[note] if note in changes else note.read_bytes()
            status = review["outcome"] if hashlib.sha256(raw).hexdigest() == review["note_sha256"] else "修改后待复审"
            reviewed_at = display_time(review["reviewed_at"])
        url = item.get("source_url")
        current[row["path"][:-3]] = (f"[原作品]({url})" if url else "—", status, reviewed_at)
    lines = ["| " + " | ".join(HEADERS) + " |", "| " + " | ".join(["---"] * len(HEADERS)) + " |"]
    for row in history:
        row["时间"] = cell(display_time(row["时间"]))
        link = re.fullmatch(r"\[\[(.*?)\\\|.*\]\]", row["文章"])
        target = link[1].removesuffix(".md") if link else None
        if target in current:
            row["来源 URL"], row["内容复审"], row["复审时间"] = current[target]
        else:
            # A moved/deleted historical link must not silently remain approved.
            row.setdefault("来源 URL", "—")
            row["内容复审"] = "文章未定位"
            row["复审时间"] = display_time(row.get("复审时间"))
        lines.append("| " + " | ".join(row[key] for key in HEADERS) + " |")
    encoded = encode_state(state)
    if not state_path.exists() or state_path.read_bytes() != encoded:
        changes[state_path] = encoded
    return "\n".join(lines) + "\n"


def validate_report(report, is_image):
    if not isinstance(report, dict) or report.get("outcome") not in ("复审通过", "复审待完善"):
        raise ValueError("内容复审 outcome 必须是‘复审通过’或‘复审待完善’。")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise ValueError("内容复审需要说明检查结论 summary。")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise ValueError("复审必须检查正确性、深度、术语、通俗解释、模型判断和配图。")
    has_issue = False
    for key in CHECKS:
        check = checks[key]
        if (not isinstance(check, dict) or check.get("result") not in ("通过", "待完善", "不适用")
                or not isinstance(check.get("details"), str) or not check["details"].strip()):
            raise ValueError(f"复审项 {key} 需要明确结果与具体依据。")
        if check["result"] == "不适用" and (key != "images" or is_image):
            raise ValueError("只有语音视频的配图检查可以标为不适用。")
        has_issue |= check["result"] == "待完善"
    issues = report.get("issues")
    if not isinstance(issues, list) or not all(isinstance(i, str) and i.strip() for i in issues):
        raise ValueError("issues 必须是尚待解决问题的字符串数组。")
    if report["outcome"] == "复审通过" and (has_issue or issues):
        raise ValueError("存在待完善项或未解决问题，不能登记复审通过。")
    if report["outcome"] == "复审待完善" and not (has_issue and issues):
        raise ValueError("复审待完善需要具体未通过项和待办问题。")
    return {key: report[key] for key in ("outcome", "summary", "checks", "issues")}
