# -*- coding: utf-8 -*-
"""限流与费用闸门。

为什么需要：私聊配成 ``always`` 之后，**任何能给这个号发消息的人都在花你的 API 额度**。
没有这道闸门，一个刷屏的人就能把当天的钱烧完，而你没有刹车。

四道闸（都可以单独关掉，0 表示不限）：

1. **单用户冷却**：同一个人两条回复之间至少隔 N 秒 —— 防连发刷屏；
2. **单用户每分钟上限**：防一个人连续追问把队列占满；
3. **单会话每分钟上限**：群聊保护 —— 防一群人同时 @ 把整群刷爆；
4. **每日回复总数**：这是真正的**费用闸门**，跨天自动重置。

纯内存、纯逻辑、时间可注入，所以能用测试把边界钉死（见 tests/test_offline.py）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class LimitConfig:
    enabled: bool = True
    per_user_per_minute: int = 6       # 0 = 不限
    per_chat_per_minute: int = 20      # 0 = 不限
    daily_replies: int = 500           # 0 = 不限
    cooldown_seconds: float = 2.0      # 0 = 不冷却


class RateLimiter:
    def __init__(self, cfg: LimitConfig | None = None):
        self.cfg = cfg or LimitConfig()
        self._user_hits: dict[str, list[float]] = {}
        self._chat_hits: dict[str, list[float]] = {}
        self._last_reply: dict[str, float] = {}
        self._day = ""
        self._day_count = 0
        self._skipped = 0

    # ------------------------------------------------------------------ #
    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._day:
            self._day = day
            self._day_count = 0

    def allow(self, chat: str, sender: str,
              now: float | None = None) -> tuple[bool, str]:
        """能不能回。返回 (是否放行, 被拦的原因)。放行时会记账。"""
        if not self.cfg.enabled:
            return True, ""
        now = time.time() if now is None else now
        self._roll_day(now)

        # 4) 每日费用闸门
        if self.cfg.daily_replies > 0 and self._day_count >= self.cfg.daily_replies:
            self._skipped += 1
            return False, "已达当日回复上限 %d 条（费用闸门）" % self.cfg.daily_replies

        # 1) 同一用户的冷却
        last = self._last_reply.get(sender, 0.0)
        if self.cfg.cooldown_seconds > 0 and last and (now - last) < self.cfg.cooldown_seconds:
            self._skipped += 1
            return False, "冷却中（距上次 %.1fs，要求 %.1fs）" % (
                now - last, self.cfg.cooldown_seconds)

        # 2) 单用户每分钟
        uh = [t for t in self._user_hits.get(sender, []) if now - t < 60]
        if self.cfg.per_user_per_minute > 0 and len(uh) >= self.cfg.per_user_per_minute:
            self._user_hits[sender] = uh
            self._skipped += 1
            return False, "该用户每分钟上限 %d 条" % self.cfg.per_user_per_minute

        # 3) 单会话每分钟
        ch = [t for t in self._chat_hits.get(chat, []) if now - t < 60]
        if self.cfg.per_chat_per_minute > 0 and len(ch) >= self.cfg.per_chat_per_minute:
            self._chat_hits[chat] = ch
            self._skipped += 1
            return False, "该会话每分钟上限 %d 条" % self.cfg.per_chat_per_minute

        # 放行并记账
        uh.append(now)
        ch.append(now)
        self._user_hits[sender] = uh
        self._chat_hits[chat] = ch
        self._last_reply[sender] = now
        self._day_count += 1
        return True, ""

    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        """给日志/自检用。注意别把它塞进每轮对话里。"""
        return {
            "day": self._day,
            "replied_today": self._day_count,
            "daily_limit": self.cfg.daily_replies,
            "skipped": self._skipped,
            "users_seen": len(self._user_hits),
        }
