# -*- coding: utf-8 -*-
"""把引擎给的原始消息，翻译成"能读、能进上下文"的东西。

真机实测（2026-10-11，254 条历史消息）里 `type` 只有这几种：
    文本 204 / 文件/链接/卡片 24 / 动画表情 13 / 图片 10 / 系统消息 3

两个必须处理的坑：

1. **`文件/链接/卡片` 的 content 是一整段 XML**（实测是"拍一拍"，
   ``<appmsg>...<title>"射电望远镜" 拍了拍 "蓝色大肥鱼"</title>...``）。
   如果直接把它当文本喂给模型，既浪费 token 又读不懂。应该抠出 ``<title>``。

2. **`系统消息` 必须忽略**（改群名、邀请入群之类），它不是"谁在说话"。

另外注意：实测有几类的 ``sender_username`` 是**空字符串**（卡片、系统消息），
所以取发送者标识时要有回退。
"""
from __future__ import annotations

import re

# 引擎给的中文 type（实测取值，不要凭空加）
TYPE_TEXT = "文本"
TYPE_IMAGE = "图片"
TYPE_EMOJI = "动画表情"
TYPE_CARD = "文件/链接/卡片"
TYPE_SYSTEM = "系统消息"

TEXT_TYPES = {TYPE_TEXT, "text"}
IMAGE_TYPES = {TYPE_IMAGE, "image"}
EMOJI_TYPES = {TYPE_EMOJI, "表情", "emotion"}
CARD_TYPES = {TYPE_CARD, "card", "link", "file"}
SYSTEM_TYPES = {TYPE_SYSTEM, "system"}

# 群聊内容前缀：wxid_xxx:\n正文
_PREFIX_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]{6,})[:：]\s*\n?", re.S)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)

_XML_ENTITIES = (
    ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
    ("&quot;", '"'), ("&apos;", "'"), ("&#39;", "'"),
)


def strip_sender_prefix(content: str) -> str:
    """剥掉群聊内容里的 `wxid_xxx:` 前缀。"""
    if not content:
        return ""
    return _PREFIX_RE.sub("", content, count=1).strip()


def unescape_xml(text: str) -> str:
    for a, b in _XML_ENTITIES:
        text = text.replace(a, b)
    return text


def extract_title(xml: str) -> str:
    """从卡片/链接/文件的 XML 里抠出可读标题；抠不到返回空串。"""
    if not xml:
        return ""
    m = _TITLE_RE.search(xml)
    return unescape_xml(m.group(1)).strip() if m else ""


def is_poke(xml: str) -> bool:
    """是不是"拍一拍"。实测它的 appmsg type 是 62，但标题文本更可靠。"""
    return "拍了拍" in (xml or "")


def classify(mtype: str, content: str) -> tuple[str, str, bool]:
    """把原始消息翻译成 (类别, 可读文本, 是否应当忽略)。

    类别取值：text / image / emoji / card / system / other
    """
    raw = str(content or "")
    t = str(mtype or "")

    if t in SYSTEM_TYPES:
        return "system", "", True                 # 改群名之类，不是谁在说话

    if t in IMAGE_TYPES:
        return "image", "", False                 # 正文交给视觉识别填

    if t in EMOJI_TYPES:
        return "emoji", "[对方发来一个表情]", False

    if t in TEXT_TYPES:
        return "text", strip_sender_prefix(raw), False

    if t in CARD_TYPES:
        title = extract_title(raw)
        body = strip_sender_prefix(raw) if not raw.lstrip().startswith("<") else ""
        if title:
            if is_poke(raw):
                return "card", "[拍一拍] %s" % title, False
            return "card", "[卡片/链接] %s" % title, False
        # 抠不出标题：绝不把整段 XML 塞给模型，只留一句说明
        return "card", "[对方发来一个卡片/链接%s]" % (("：" + body[:60]) if body else ""), False

    # 未知类型：给一句可读说明，同样不塞原始体
    short = strip_sender_prefix(raw)[:80]
    return "other", "[%s]%s" % (t or "未知类型", (" " + short) if short and not short.lstrip().startswith("<") else ""), False
