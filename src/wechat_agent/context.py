# -*- coding: utf-8 -*-
"""上下文管理：按会话存历史，送模型前再裁剪。

旧包 wechatbot-new 把历史**写死** 2500 字符（CONTEXT_MAX_CHARS），聊十来句就
开始忘前文，这是"显得傻"的头号原因。这里：
  * 两个上限（字符数 / 条数）都可配；
  * **落盘保存完整历史**，只在拼提示词时裁剪 —— 这样即使调小上限，
    历史也不会丢，之后调大就能恢复；
  * 存储用 JSON（一个会话一个文件），人可读、可手工修。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .logger import get_logger

log = get_logger("context")


@dataclass
class Turn:
    role: str          # "user" | "assistant"
    content: str
    ts: float = 0.0

    def to_message(self) -> dict:
        return {"role": self.role, "content": self.content}


class ContextStore:
    """一个会话的历史。线程安全靠调用方串行化（本项目里由单条工作线程处理）。"""

    def __init__(self, chat: str, store_dir: str | Path,
                 max_chars: int = 12000, max_messages: int = 40):
        self.chat = chat
        self.max_chars = max_chars
        self.max_messages = max_messages
        self.dir = Path(store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # 会话名可能有中文/斜杠等，用短哈希做文件名更安全，同时留个人可读前缀
        safe = "".join(c for c in chat if c.isalnum() or c in "-_")[:24] or "chat"
        digest = hashlib.sha1(chat.encode("utf-8")).hexdigest()[:8]
        self.path = self.dir / ("%s_%s.json" % (safe, digest))
        self.turns: list[Turn] = []
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.turns = [Turn(**t) for t in data.get("turns", [])]
        except Exception as e:
            log.warning("读取上下文失败（忽略并从空开始）%s: %r", self.path, e)
            self.turns = []

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            payload = {"chat": self.chat, "updated": time.time(),
                       "turns": [asdict(t) for t in self.turns]}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)      # 原子替换，避免写一半崩掉
        except Exception as e:
            log.warning("保存上下文失败 %s: %r", self.path, e)

    # ------------------------------------------------------------------ #
    def add_user(self, text: str) -> None:
        self.turns.append(Turn("user", text, time.time()))
        self._save()

    def add_assistant(self, text: str) -> None:
        self.turns.append(Turn("assistant", text, time.time()))
        self._save()

    def reset(self) -> None:
        self.turns = []
        self._save()

    # ------------------------------------------------------------------ #
    def build_messages(self, system_prompt: str) -> list[dict]:
        """拼出送给模型的消息列表：system + 裁剪后的历史。

        裁剪从**最新往旧**回填，保证最近的话一定在；超限即止。
        """
        kept: list[Turn] = []
        total = 0
        for t in reversed(self.turns[-self.max_messages:]):
            cost = len(t.content) + 8
            if kept and total + cost > self.max_chars:
                break
            kept.append(t)
            total += cost
        kept.reverse()
        msgs = [{"role": "system", "content": system_prompt}]
        msgs.extend(t.to_message() for t in kept)
        return msgs

    def stats(self) -> dict:
        return {
            "chat": self.chat,
            "turns": len(self.turns),
            "chars": sum(len(t.content) for t in self.turns),
            "max_chars": self.max_chars,
            "max_messages": self.max_messages,
        }
