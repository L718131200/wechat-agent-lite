# -*- coding: utf-8 -*-
"""发送：分段控制 + 调用引擎 GUI 层。

旧包实测的教训（wechatbot-new）：一条回复被拆成 7-8 条微信消息，每条间隔
3-5 秒（里面还含发送校验），端到端 28 秒，观感极差。所以这里：
  * max_segments 默认 1（不拆），要拆也有硬上限；
  * verify 默认 False —— 引擎的发送校验很贵，日常不需要；
  * 串行发送（引擎的 UI 操作必须串行，否则会互相抢窗口）。

**关于引擎的两个实测行为（2026-10-11 在真机上量出来的）**：

1. **引擎自带"拟人节流"**（`rhythm.py`）：120 秒内最多写 6 次，超了会强制冷却
   30 秒以上，每次写入前还有 1-4 秒随机等待。这是**防风控设计，不是 bug**，
   但意味着发送频率有天花板 —— 别指望它当广播用。

2. **`click_send()` 的返回值不可靠**：实测它返回 False 时消息其实已经发出去了。
   引擎若不信任这个返回值就可能重发，真机上看到过同一条消息出现两次。
   而窗口被遮挡时情况更糟：输入框探测会连续失败 → 自动重校准 → `find_session`
   一次能耗掉 100 秒以上。**所以发送前先 ensure_visible()** 是必须的：
   它花 1 秒多，但能避免后面那一串灾难。
"""
from __future__ import annotations

import threading
import time

from .config import AppConfig
from .logger import get_logger

log = get_logger("sender")

# 引擎的 UI 操作必须串行；我们自己再加一把锁，避免和监听线程抢窗口
_send_lock = threading.Lock()


def split_text(text: str, max_segments: int, token: str = "\\") -> list[str]:
    """按分隔符拆段，但硬限制段数。

    token 默认反斜杠（人格提示词里约定的写法）。max_segments=1 时永远返回单段。
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_segments <= 1 or not token:
        return [text]
    parts = [p.strip() for p in text.split(token)]
    parts = [p for p in parts if p]
    if not parts:
        return [text]
    if len(parts) <= max_segments:
        return parts
    # 超出上限：前 max_segments-1 段原样，剩下的合并进最后一段（不丢内容）
    head = parts[:max_segments - 1]
    tail = " ".join(parts[max_segments - 1:])
    return head + [tail]


class Sender:
    """把文本发到某个会话。延迟导入引擎，方便离线自测。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._gui = None

    def _ensure_gui(self):
        if self._gui is None:
            from wechatauto.guia import WeChatGUI      # 延迟导入
            self._gui = WeChatGUI()
            log.info("已连接微信 GUI 层")
        return self._gui

    # ------------------------------------------------------------------ #
    def send(self, chat: str, text: str, at: str | None = None) -> bool:
        """发送（必要时 @ 某人）。返回是否成功。"""
        segs = split_text(text, self.cfg.send.max_segments, self.cfg.send.split_token)
        if not segs:
            log.warning("空内容，跳过发送 chat=%s", chat)
            return False
        ok = True
        with _send_lock:
            gui = self._ensure_gui()
            # 先确保微信窗口可见可操作。实测不做这一步时，窗口被遮挡会导致
            # 输入框探测失败 → 自动重校准 → find_session 耗 100 秒以上 → 重试后可能重复发送。
            try:
                gui.ensure_visible()
            except Exception as e:
                log.debug("ensure_visible 失败（继续尝试发送）：%r", e)
            for i, seg in enumerate(segs, 1):
                t0 = time.time()
                try:
                    if at:
                        r = gui.at_member(at, seg, who=chat, verify=False)
                    else:
                        r = gui.send_msg(seg, who=chat, verify=False)
                    ok_one = bool(getattr(r, "is_success", False))
                    log.info("发送 %d/%d -> %s  %.2fs  ok=%s", i, len(segs), chat,
                             time.time() - t0, ok_one)
                    if not ok_one:
                        log.warning("发送未成功：%s", str(r)[:200])
                        ok = False
                except Exception as e:
                    log.error("发送异常 chat=%s: %r", chat, e)
                    ok = False
                if i < len(segs) and self.cfg.send.segment_delay > 0:
                    time.sleep(self.cfg.send.segment_delay)
        return ok
