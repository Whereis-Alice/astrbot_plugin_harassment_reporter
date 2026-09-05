from __future__ import annotations

import random
import string
from datetime import datetime
from typing import Any

SEVERITY_TEXT = {"low": "低", "medium": "中", "high": "高"}
URGENCY_TEXT = {"low": "不急", "medium": "一般", "high": "紧急"}
CATEGORY_TEXT = {
    "bug": "疑似故障",
    "feature_request": "功能建议",
    "complaint": "体验吐槽",
    "question": "使用疑问",
    "other": "其他",
}


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


def normalize_context_line(line: str) -> str:
    """把 AstrBot 可读上下文里的英文角色前缀改成中文。"""
    text = clean_text(line)
    if text.startswith("User: "):
        return "用户: " + text[6:]
    if text.startswith("Assistant: "):
        return "助手: " + text[11:]
    return text


def split_context_line(line: str) -> tuple[str, str]:
    """把一行上下文拆成 (角色, 内容)，角色为 user / assistant / other。"""
    text = clean_text(line)
    if text.startswith("User: "):
        return "user", text[6:].strip()
    if text.startswith("用户: "):
        return "user", text[4:].strip()
    if text.startswith("Assistant: "):
        return "assistant", text[11:].strip()
    if text.startswith("助手: "):
        return "assistant", text[4:].strip()
    return "other", text


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


def urgency_text(urgency: str) -> str:
    return URGENCY_TEXT.get(urgency, urgency or "一般")


def category_text(category: str) -> str:
    return CATEGORY_TEXT.get(category, category or "其他")


def make_ticket_id(prefix: str = "FB") -> str:
    """生成便于口头转述的短工单号，例如 FB-7K3Q。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    body = "".join(random.choice(alphabet) for _ in range(4))
    return f"{prefix}-{body}"


def convert_duration(seconds: Any) -> str:
    """把秒数转成中文时长描述。"""
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
    return "".join(parts) or "0 秒"


def render_template(template: str, values: dict[str, str]) -> str:
    """用 str.format 渲染占位符模板，占位符写错时原样返回。"""
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
