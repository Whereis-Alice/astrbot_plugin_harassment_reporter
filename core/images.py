"""消息里的图片。

群友说「这张图给狐狸看看」的时候，光把文字带过去是不够的 —— 那张图才是重点。
这个模块负责把「一张图现在在哪儿」收敛成一个统一的小对象：

- 平台发来的图片可能是一个 http 地址、一段 base64，也可能是一个本地文件；
- MessageChain 对这三种情况有三个不同的方法，而 url_image() 收到非 http 地址会直接抛异常；
- 所以先在这里认清类型，再交给 attach_to() 去挂，调用方不用关心这些差别。

取图只看三个地方：用户这条消息本身、用户引用的那条消息、以及（仅在模型明确
要求时）最近的群聊记录。群里图片本来就多，默认不翻历史，免得每次传话都附上
一堆无关的表情包。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from astrbot.api import logger

from .text import clean_text

LOG_PREFIX = "[HarassmentReporter]"

KIND_URL = "url"
KIND_FILE = "file"
KIND_BASE64 = "base64"

_HTTP_PREFIXES = ("http://", "https://")
_BASE64_PREFIX = "base64://"


def _http(value: str) -> str:
    return value if value.startswith(_HTTP_PREFIXES) else ""


def _existing_file(value: str) -> str:
    """确认是一个真实存在的本地文件，顺手支持 file:// 写法。"""
    if not value:
        return ""
    if value.startswith("file://"):
        try:
            path = unquote(urlparse(value).path)
        except Exception:
            return ""
        # Windows 上 file:///C:/x.png 解析出来是 /C:/x.png，前面那个斜杠要去掉。
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        value = path
    try:
        return value if value and os.path.isfile(value) else ""
    except OSError:
        return ""


@dataclass(frozen=True)
class ImageRef:
    """一张待转发的图片，以及它现在在哪儿。"""

    kind: str
    value: str

    @property
    def url(self) -> str:
        """能直接写进 <img src> 的地址。本地文件和 base64 没有，返回空串。"""
        return self.value if self.kind == KIND_URL else ""

    def attach_to(self, chain: Any) -> bool:
        """挂到一条 MessageChain 上。挂不上只返回 False，绝不抛异常。"""
        try:
            if self.kind == KIND_URL:
                chain.url_image(self.value)
            elif self.kind == KIND_BASE64:
                chain.base64_image(self.value)
            else:
                chain.file_image(self.value)
            return True
        except Exception as exc:
            logger.debug("%s 图片挂载失败（%s）：%s", LOG_PREFIX, self.kind, exc)
            return False


def from_component(comp: Any) -> ImageRef | None:
    """把一个 Image 组件认成 ImageRef。认不出来返回 None。

    优先级是「越通用越优先」：http 地址任何平台都能重发，base64 次之，
    本地文件最后 —— 本地路径只有 Bot 自己这台机器认得。
    """
    if comp is None:
        return None
    url = _http(clean_text(getattr(comp, "url", "")))
    if url:
        return ImageRef(KIND_URL, url)

    file = clean_text(getattr(comp, "file", ""))
    url = _http(file)
    if url:
        return ImageRef(KIND_URL, url)
    if file.startswith(_BASE64_PREFIX):
        payload = file[len(_BASE64_PREFIX) :]
        return ImageRef(KIND_BASE64, payload) if payload else None

    for candidate in (clean_text(getattr(comp, "path", "")), file):
        local = _existing_file(candidate)
        if local:
            return ImageRef(KIND_FILE, local)
    return None


def _is_image(comp: Any) -> bool:
    """鸭子类型判断一个组件是不是图片。

    ComponentType 的取值就是类名字符串（Image / Plain / Reply …），
    比对字符串就够了，不必为此 import 整个组件模块。
    """
    return clean_text(getattr(comp, "type", "")) == "Image"


def dedupe(refs: list[ImageRef], *, limit: int) -> list[ImageRef]:
    """按图片地址去重并截到上限。

    同一张图很容易被数到两遍：用户这条消息里有，群历史里也有同一条。
    """
    seen: set[str] = set()
    picked: list[ImageRef] = []
    for ref in refs:
        if not ref or not ref.value or ref.value in seen:
            continue
        seen.add(ref.value)
        picked.append(ref)
        if len(picked) >= max(1, limit):
            break
    return picked


def _chain_of(event: Any) -> list[Any]:
    chain = getattr(getattr(event, "message_obj", None), "message", None)
    return chain if isinstance(chain, list) else []


def from_event(event: Any, *, limit: int) -> list[ImageRef]:
    """用户这条消息里自己带的图。"""
    refs = [ref for comp in _chain_of(event) if _is_image(comp) and (ref := from_component(comp))]
    return dedupe(refs, limit=limit)


def from_reply(event: Any, *, limit: int) -> list[ImageRef]:
    """用户引用（回复）的那条消息里的图。

    「爱丽丝，把楼上这张图发给狐狸」是很自然的说法，所以引用里的图也要算。
    OneBot 适配器在收消息时已经把被引用消息展开进 Reply.chain 了，直接读即可。
    """
    refs: list[ImageRef] = []
    for comp in _chain_of(event):
        if clean_text(getattr(comp, "type", "")) != "Reply":
            continue
        quoted = getattr(comp, "chain", None)
        if not isinstance(quoted, list):
            continue
        for inner in quoted:
            if _is_image(inner):
                ref = from_component(inner)
                if ref:
                    refs.append(ref)
    return dedupe(refs, limit=limit)


def from_urls(urls: list[str], *, limit: int) -> list[ImageRef]:
    """把一串图片地址转成 ImageRef，非 http 的直接丢掉。"""
    refs = [ImageRef(KIND_URL, url) for raw in urls if (url := _http(clean_text(raw)))]
    return dedupe(refs, limit=limit)
