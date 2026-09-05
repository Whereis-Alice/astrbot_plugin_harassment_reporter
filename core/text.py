from __future__ import annotations

import ast
import random
import re
import string
import time
from datetime import datetime
from typing import Any

# 插件的中文展示名，命令回执、卡片页脚都用它，改这一处即可全局生效。
PLUGIN_DISPLAY_NAME = "我会打小报告"

SEVERITY_TEXT = {"low": "低", "medium": "中", "high": "高"}


def now_text() -> str:
    """当前时间的可读文本。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_ts(timestamp: float | int | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把时间戳格式化为可读文本，失败时返回占位符。"""
    if not timestamp:
        return "-"
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime(fmt)
    except Exception:
        return "-"


def relative_time(timestamp: float | int | None) -> str:
    """把时间戳说成「刚刚」「3 分钟前」这种人话，用于卡片副标题。"""
    try:
        moment = float(timestamp or 0)
    except Exception:
        return ""
    if moment <= 0:
        return ""
    delta = time.time() - moment
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} 天前"
    return format_ts(moment, "%m-%d %H:%M")


def clean_text(value: Any, fallback: str = "") -> str:
    """把任意值转成去掉首尾空白的字符串，空值时用 fallback。"""
    text = str(value or "").strip()
    return text or fallback


def truncate(text: str, limit: int) -> str:
    """按字符数截断文本，超长时补省略号。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


# ----------------------------------------------------------------------
# 聊天记录清洗
# ----------------------------------------------------------------------
# AstrBot 的会话历史是「原始 LLM 上下文」，里面混着一堆只给模型看的东西：
# 系统提示块、引用消息标记、图片的本地绝对路径、甚至模型的思维链。
# 直接拿来画卡片会变成满屏乱码，所以先在这里洗干净。

_SYSTEM_BLOCK_RE = re.compile(
    r"<\s*system_reminder\s*>.*?<\s*/\s*system_reminder\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SYSTEM_OPEN_RE = re.compile(r"<\s*/?\s*system_reminder\s*>.*", re.IGNORECASE | re.DOTALL)
_QUOTE_BLOCK_RE = re.compile(
    r"<\s*Quoted\s+Message\s*>.*?<\s*/\s*Quoted\s+Message\s*>",
    re.IGNORECASE | re.DOTALL,
)
_QUOTE_TAG_RE = re.compile(r"<\s*/?\s*Quoted\s+Message\s*>", re.IGNORECASE)
_IMAGE_PATH_RE = re.compile(
    r"(Image|Video|Audio|File)\s+Attachment[^\n]*?:\s*path\s*\S+",
    re.IGNORECASE,
)
_ABS_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}\.(?:jpg|jpeg|png|gif|webp|bmp|mp4|silk|amr|wav|mp3)", re.IGNORECASE)
_META_LINE_RE = re.compile(
    r"^\s*(User ID|Nickname|Group name|Group ID|Current datetime|Weekday|Sender|Platform)\s*[:：].*$",
    re.IGNORECASE | re.MULTILINE,
)
_TEXT_FIELD_RE = re.compile(r"['\"]text['\"]\s*:\s*['\"](.*?)['\"]\s*[,}]", re.DOTALL)
_THINK_TYPES = {"think", "thinking", "reasoning", "redacted_thinking", "tool_use", "tool_result"}
_MEDIA_HINTS = {
    "image": "[图片]",
    "image_url": "[图片]",
    "input_image": "[图片]",
    "audio": "[语音]",
    "input_audio": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{2,}")


def _parts_to_text(text: str) -> str | None:
    """把「多模态内容块」的字符串形态还原成人话。

    LLM 上下文里的一条消息可能是 `[{'type': 'think', ...}, {'type': 'text', ...}]`
    这样的结构，被直接转成字符串后就成了卡片上的乱码。这里解析它，
    丢掉思维链和工具调用，只留下真正说出口的文字。
    解析不了就返回 None，交给调用方走正则兜底。
    """
    stripped = text.strip()
    if not stripped.startswith("[{") and not stripped.startswith("{'"):
        return None
    try:
        data = ast.literal_eval(stripped)
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    pieces: list[str] = []
    for part in data:
        if isinstance(part, str):
            pieces.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = clean_text(part.get("type")).lower()
        if kind in _THINK_TYPES:
            continue
        if kind in _MEDIA_HINTS:
            pieces.append(_MEDIA_HINTS[kind])
            continue
        value = part.get("text") or part.get("content") or part.get("data")
        if isinstance(value, str) and value.strip():
            pieces.append(value.strip())
    return " ".join(pieces).strip()


_ATTACHMENT_PLACEHOLDER = {
    "image": "[图片]",
    "video": "[视频]",
    "audio": "[语音]",
    "file": "[文件]",
}


def _attachment_placeholder(match: Any) -> str:
    """把 "Image Attachment ...: path /root/..." 换成对应的中文占位符。"""
    try:
        kind = clean_text(match.group(1)).lower()
    except Exception:
        return "[附件]"
    return _ATTACHMENT_PLACEHOLDER.get(kind, "[附件]")


def sanitize_history_text(text: Any) -> str:
    """把一条原始上下文内容洗成可以直接给人看的文本。

    依次处理四类噪音：

    1. 多模态内容块（形如 [{'type': 'think', ...}, {'type': 'text', ...}]），
       顺手丢掉思维链和工具调用，只留真正说出口的话；
    2. 只给模型看的系统提示块、引用消息标记；
    3. 图片、语音等附件的本地绝对路径，换成 [图片] 这类占位符；
    4. 注入的环境信息行，以及多余的连续空白。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    parsed = _parts_to_text(raw)
    if parsed is not None:
        raw = parsed
    elif raw.startswith("[{") or raw.startswith("{'"):
        # literal_eval 没啃下来时退一步：用正则把所有 text 字段抠出来。
        found = [item.strip() for item in _TEXT_FIELD_RE.findall(raw)]
        found = [item for item in found if item]
        raw = " ".join(found) if found else "[非文本内容]"

    raw = _SYSTEM_BLOCK_RE.sub("", raw)
    raw = _SYSTEM_OPEN_RE.sub("", raw)
    raw = _QUOTE_BLOCK_RE.sub("[引用]", raw)
    raw = _QUOTE_TAG_RE.sub("", raw)
    raw = _IMAGE_PATH_RE.sub(_attachment_placeholder, raw)
    raw = _ABS_PATH_RE.sub("[图片]", raw)
    raw = _META_LINE_RE.sub("", raw)
    raw = _WS_RE.sub(" ", raw)
    raw = _BLANK_RE.sub("\n", raw)
    return raw.strip()


def split_context_line(line: str) -> tuple[str, str]:
    """把一行会话上下文拆成 (角色, 已清洗的内容)。

    角色取值为 user / assistant / other。
    """
    text = clean_text(line)
    role = "other"
    for prefix, name in (
        ("User: ", "user"),
        ("用户: ", "user"),
        ("Assistant: ", "assistant"),
        ("助手: ", "assistant"),
    ):
        if text.startswith(prefix):
            role = name
            text = text[len(prefix) :]
            break
    return role, sanitize_history_text(text)


def normalize_context_line(line: str) -> str:
    """把一行会话上下文整理成「用户: xxx」这样的中文可读形式。"""
    role, body = split_context_line(line)
    if not body:
        return ""
    if role == "user":
        return "用户: " + body
    if role == "assistant":
        return "助手: " + body
    return body


def message_chain_to_text(chain: Any) -> str:
    """从 MessageChain 里安全地取出纯文本。"""
    if chain is None:
        return ""
    try:
        return str(chain.get_plain_text()).strip()
    except Exception:
        return ""


def severity_text(severity: str) -> str:
    return SEVERITY_TEXT.get(severity, severity or "未知")


def convert_duration(seconds: Any) -> str:
    """把秒数转成中文时长描述，例如 90 秒 -> "1 分钟 30 秒"。"""
    try:
        total = int(seconds or 0)
    except Exception:
        return "一段时间"
    if total <= 0:
        return "0 秒"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if secs and not days and not hours:
        parts.append(f"{secs} 秒")
    return " ".join(parts) or "0 秒"


def render_template(template: str, values: dict[str, str]) -> str:
    """用 str.format 渲染占位符模板，占位符写错时原样返回模板。"""
    try:
        return template.format(**values)
    except Exception:
        return template


def pack_lines(
    lines: list[str],
    *,
    max_chars: int,
    bullet: str = "- ",
    min_line: int = 40,
    max_line: int = 180,
) -> str:
    """把多行文本压进字符预算，返回带前缀的多行字符串。"""
    if not lines:
        return ""
    per_line_limit = max(min_line, min(max_line, max_chars // max(1, len(lines))))
    output: list[str] = []
    used = 0
    for line in lines:
        candidate = f"{bullet}{truncate(line, per_line_limit)}"
        if used + len(candidate) + 1 > max_chars and output:
            break
        output.append(candidate)
        used += len(candidate) + 1
    return "\n".join(output)


def random_suffix(length: int = 6) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))
