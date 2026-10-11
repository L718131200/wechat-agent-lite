# -*- coding: utf-8 -*-
"""监听 + 触发判定 + 回复编排 —— 本项目的业务核心。

引擎 (`wechatauto.db.Listener`) 已经替我们做好了最难的部分：
  * 水位持久化：**回调成功后才推进**，重启不会重复推送也不会丢；
  * **每个会话一条串行工作线程** —— 跨会话并行、**会话内严格保序**。
    这一点很重要：它意味着回调里同步等模型**只会拖慢该会话**，不会阻塞别的会话；
    同时同一个人的连续两条消息天然不会乱序。所以这里不需要自己再造一套队列。
  * `add_all(discover=True)` 自动发现新会话（新建的群也能覆盖）。

真机实测注意点（2026-10-11，微信 4.1.15 + wechatauto-replica 1.2.6.1）：
  * `type` 是**中文**：``文本`` / ``图片`` / ``动画表情`` / ``语音`` ...；
  * 群聊消息的 ``content`` 形如 ``wxid_xxx:\\n正文``，要剥掉这段前缀；
  * ``sender_id == 2`` 表示**自己**发的；
  * `get_sessions()` 的昵称字段是 None，**不能**按昵称匹配会话，
    必须用 `db.search_contact(昵称)` 换成 username。

## 关于群聊图片归属（重要，改过一版）

最初写的是"该群最近 60 秒被 @ 过就认图"，这是个**隐私漏洞**：
A 提问之后，B 在群里发的私人截图会被当成 A 的续图**上传给模型**。

现在改成绑定到 **(群, 发送者)** 的登记制：
  * 只有**同一个发送者**的图片能消费掉这次等待；
  * 额度有界（默认 1 张；消息里明确提到"图/看/截图"等才放宽到 N 张）；
  * **其他成员的消息、机器人自己的回复都不影响它**；
  * **未被请求的图片一律不下载、不上传**，只在内存里留一条消息引用，
    用于支持"先发图，再 @ 机器人问这是什么"这个常见顺序。
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

from .brain import Model, ModelError
from .config import AppConfig, ListenTarget
from .context import ContextStore
from .logger import get_logger
from .ratelimit import RateLimiter
from .sender import Sender
from .vision import EMOJI_TYPES, IMAGE_TYPES, ImageFetcher

log = get_logger("listener")

SELF_SENDER_ID = 2           # 实测：sender_id == 2 即本机账号自己
TEXT_TYPES = {"文本", "text"}

# 群聊内容前缀：wxid_xxx:\n正文
_PREFIX_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]{6,})[:：]\s*\n?", re.S)

# 消息里出现这些词，说明对方"明确在等一张图"，可以放宽接图额度
_IMAGE_HINT = ("图", "照片", "截图", "看看这", "看这", "识图", "画", "拍")


def strip_sender_prefix(content: str) -> str:
    """剥掉群聊内容里的 `wxid_xxx:` 前缀。"""
    if not content:
        return ""
    return _PREFIX_RE.sub("", content, count=1).strip()


@dataclass
class PendingImage:
    """一次「等对方补图」的登记。绑定到 (群, 发送者)，不是整个群。"""
    chat: str
    sender: str
    created: float
    expires: float
    budget: int              # 还能接几张
    reason: str

    def alive(self, now: float) -> bool:
        return now <= self.expires and self.budget > 0


class ImageRequestTracker:
    """谁在等图：把"被 @ 之后的接图权"绑定到 **(群, 发送者)**，而不是整个群。

    为什么必须是 (群, 发送者)：
        A 在群里 @ 机器人提问之后，B 紧接着发的私人截图如果被当成 A 的续图，
        就会被**上传给远程模型** —— 那是数据外传，不只是答错一句话。

    规则：
        * 只有**发起 @ 的那个人**能消费掉这次等待；
        * 额度有界（默认 1 张；消息里明确提到"图/照片/截图"才放宽到 budget 张）；
        * 其他人的消息、机器人自己的回复**都不影响它**；
        * 没被请求的图片只在内存里留一条消息引用（供"先发图再 @ 问"关联），
          **永不下载、永不上传**。

    时间可以注入（now 参数），所以这套规则能用纯逻辑测试覆盖。
    方法单线程调用（引擎保证同一会话串行），故不加锁。
    """

    def __init__(self, ttl: float = 60.0, budget: int = 2):
        self.ttl = float(ttl)
        self.budget = int(budget)
        self._pending: dict[tuple[str, str], PendingImage] = {}
        self._recent: dict[tuple[str, str], tuple[float, dict]] = {}

    def open(self, chat: str, sender: str, text: str = "",
             now: float | None = None) -> PendingImage:
        """登记一次等图。文本里提到"图/看/截图"等才给满额度。"""
        now = time.time() if now is None else now
        budget = self.budget if any(k in (text or "") for k in _IMAGE_HINT) else 1
        p = PendingImage(chat=chat, sender=sender, created=now, expires=now + self.ttl,
                         budget=max(0, budget), reason="@机器人")
        self._pending[(chat, sender)] = p
        return p

    def consume(self, chat: str, sender: str, now: float | None = None) -> str | None:
        """同一个人来的图才消费得掉。返回命中说明，否则 None。"""
        now = time.time() if now is None else now
        key = (chat, sender)
        p = self._pending.get(key)
        if not p:
            return None
        if not p.alive(now):
            self._pending.pop(key, None)
            return None
        p.budget -= 1
        if p.budget <= 0:
            self._pending.pop(key, None)
        return "%s(剩 %d 张)" % (p.reason, max(0, p.budget))

    def note_image(self, chat: str, sender: str, msg: dict,
                   now: float | None = None) -> None:
        """记下"最近一张图"的引用（只有元数据，不读文件）。"""
        self._recent[(chat, sender)] = (time.time() if now is None else now, msg)

    def take_recent(self, chat: str, sender: str, now: float | None = None) -> dict | None:
        """取出并清除该发送者最近的图（用于"先发图，再 @ 问"）。超时则丢弃。"""
        item = self._recent.pop((chat, sender), None)
        if not item:
            return None
        ts, msg = item
        now = time.time() if now is None else now
        return msg if (now - ts) <= self.ttl else None

    def pending_count(self) -> int:
        return len(self._pending)

    def recent_count(self) -> int:
        return len(self._recent)

    def sweep(self, now: float | None = None) -> int:
        """清掉过期登记与过期的"最近图"缓存，返回清理条数。

        7x24 跑下去，这两个 dict 会按 (群, 人) 缓慢增长；必须有人扫。
        """
        now = time.time() if now is None else now
        n = 0
        for k in [k for k, p in self._pending.items() if not p.alive(now)]:
            self._pending.pop(k, None)
            n += 1
        for k, (ts, _msg) in list(self._recent.items()):
            if now - ts > self.ttl:
                self._recent.pop(k, None)
                n += 1
        return n


@dataclass
class ResolvedTarget:
    """配置里的一个监听目标，解析成引擎认识的 username 之后的样子。"""
    target: ListenTarget
    username: str
    is_group: bool
    display: str
    ctx: ContextStore | None = None


class Agent:
    def __init__(self, cfg: AppConfig, db):
        self.cfg = cfg
        self.db = db
        self.model = Model(cfg)
        self.sender = Sender(cfg)
        self.fetcher = ImageFetcher(cfg, db)
        self.targets: dict[str, ResolvedTarget] = {}   # username -> target
        self.system_prompt = cfg.persona_text()
        # 等图登记与"最近一张图"的缓存，全部绑定 (群, 发送者)
        self.tracker = ImageRequestTracker(
            ttl=float(self.cfg.group.at_image_followup_sec),
            budget=int(self.cfg.group.image_followup_budget),
        )
        self._robot_name = ""
        # 限流 / 费用闸门：私聊配成 always 时，这是唯一的刹车
        self.limiter = RateLimiter(self.cfg.limits)

    # ------------------------------------------------------------------ #
    # 会话解析：昵称 -> username
    # ------------------------------------------------------------------ #
    def resolve_targets(self) -> dict[str, ResolvedTarget]:
        out: dict[str, ResolvedTarget] = {}
        for t in self.cfg.listen:
            username = t.chat
            try:
                hits = self.db.search_contact(t.chat)
                if hits:
                    username = hits[0]["username"]
            except Exception as e:
                log.warning("search_contact(%r) 失败：%r", t.chat, e)
            is_group = str(username).endswith("@chatroom")
            if t.type == "group" and not is_group:
                log.warning("配置把 %r 标成群聊，但解析出的 username=%r 不像群；仍按 group 处理",
                            t.chat, username)
            rt = ResolvedTarget(target=t, username=username,
                                is_group=(t.type == "group") or is_group, display=t.chat)
            rt.ctx = ContextStore(
                chat=t.chat, store_dir=self.cfg.context.store_dir,
                max_chars=self.cfg.context.max_chars,
                max_messages=self.cfg.context.max_messages,
            )
            out[username] = rt
            log.info("监听目标：%s -> %s (%s)", t.chat, username, "群" if rt.is_group else "私聊")
        self.targets = out
        return out

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def robot_name(self) -> str:
        if self._robot_name:
            return self._robot_name
        if self.cfg.group.at_name:
            self._robot_name = self.cfg.group.at_name
        else:
            try:
                info = self.db.get_self_info()
                self._robot_name = info.get("nick_name") or info.get("username") or ""
            except Exception as e:
                log.warning("取自己昵称失败：%r", e)
        return self._robot_name

    @staticmethod
    def sender_key(msg: dict) -> str:
        return str(msg.get("sender_username") or msg.get("sender_id") or "?")

    def _member_nick(self, msg: dict) -> str | None:
        """群成员 wxid -> 昵称（at_member 需要昵称）。取不到就返回 None，调用方不 @。"""
        wxid = msg.get("sender_username")
        if not wxid:
            return None
        try:
            return self.db.get_nickname(wxid) or None
        except Exception as e:
            log.debug("取昵称失败 %s: %r", wxid, e)
            return None

    # ------------------------------------------------------------------ #
    # 等图登记（绑定到人）—— 规则全在 ImageRequestTracker 里，这里只是适配
    # ------------------------------------------------------------------ #
    def _open_pending(self, rt: ResolvedTarget, msg: dict, text: str) -> None:
        p = self.tracker.open(rt.username, self.sender_key(msg), text)
        log.info("已为 %s/%s 登记等图（%.0fs 内最多 %d 张）",
                 rt.display, p.sender, self.tracker.ttl, p.budget)

    def _take_recent_image(self, rt: ResolvedTarget, sender: str) -> dict | None:
        return self.tracker.take_recent(rt.username, sender)

    # ------------------------------------------------------------------ #
    # 触发判定
    # ------------------------------------------------------------------ #
    def _at_patterns(self) -> list[re.Pattern]:
        n = re.escape(self.robot_name())
        return [re.compile(r"@%s[\u2005\s]" % n), re.compile(r"@%s$" % n)]

    def _match_at(self, text: str) -> tuple[bool, str]:
        """返回 (是否 @ 了机器人, 去掉 @ 之后的文本)。"""
        robot = self.robot_name()
        if not robot:
            return False, text
        for pat in self._at_patterns():
            if pat.search(text):
                return True, pat.sub("", text).strip()
        return False, text

    # ------------------------------------------------------------------ #
    # 图片到达
    # ------------------------------------------------------------------ #
    def _handle_image(self, rt: ResolvedTarget, msg: dict) -> bool:
        """返回是否应当继续处理这条图片消息。

        * 先无条件把"最近一张图"记在内存里（**不下载、不上传**），供"先图后 @"关联；
        * 只有命中**该发送者**的等图登记时，才真正去下载并识别。
        """
        sender = self.sender_key(msg)
        self.tracker.note_image(rt.username, sender, msg)

        if not rt.is_group:
            if rt.target.mode() == "always":
                return True
            log.debug("私聊图片但回复模式不是 always，忽略")
            return False

        reason = self.tracker.consume(rt.username, sender)
        if reason:
            log.info("群 %s 命中 %s 的等图登记：%s", rt.display, sender, reason)
            return True
        log.debug("群 %s 的图片没有对应的 @ 请求（发送者 %s），不下载、不上传",
                  rt.display, sender)
        return False

    # ------------------------------------------------------------------ #
    # 主回调
    # ------------------------------------------------------------------ #
    def on_message(self, msg: dict, listener=None) -> None:
        try:
            self.tracker.sweep()          # 顺带清理过期状态，防止长期运行缓慢膨胀
            if msg.get("sender_id") == SELF_SENDER_ID:
                return                       # 自己发的，不回
            username = msg.get("username") or ""
            rt = self.targets.get(username)
            if rt is None:
                return                       # 不在配置里

            mtype = str(msg.get("type") or "")
            raw = str(msg.get("content") or "")
            is_image = mtype in IMAGE_TYPES
            is_emoji = mtype in EMOJI_TYPES

            # 消息里可能已经被 @ 过，先把前缀和 @ 都处理干净
            text = strip_sender_prefix(raw)
            at_hit, text_no_at = self._match_at(text)

            # 本次要送去识别的图片消息（可能来自本条，也可能是刚被 @ 关联到的上一条）
            image_msg = None

            # ---------- 图片分支 ----------
            if is_image:
                if not self._handle_image(rt, msg):
                    return
                image_msg = msg
                text = ""                    # 图片没文字，内容交给识别结果
                reason = "图片（命中该发送者的 @ 请求）"
            # ---------- 表情 ----------
            elif is_emoji:
                text = "[对方发来一个表情]"
                reason = "表情"
                if rt.is_group and not at_hit:
                    return
            # ---------- 文本 ----------
            elif mtype in TEXT_TYPES:
                text = text_no_at if at_hit else text
                if not rt.is_group:
                    if rt.target.mode() == "always":
                        reason = "私聊"
                    elif rt.target.mode() == "keyword" and any(
                            k and k in text for k in rt.target.keyword):
                        reason = "私聊关键词"
                    else:
                        return
                else:
                    kw = [k for k in rt.target.keyword if k and k in text]
                    if at_hit:
                        # @ 了：登记等图（支持"先图后 @"时在下面取回）
                        self._open_pending(rt, msg, text)
                        reason = "@机器人"
                        recent = self._take_recent_image(rt, self.sender_key(msg))
                        if recent is not None and any(k in text for k in _IMAGE_HINT):
                            log.info("群 %s：@ 之前刚发过图，直接关联那张图", rt.display)
                            image_msg = recent
                            reason = "@机器人+紧随其后的图"
                    elif kw:
                        reason = "关键词"
                    elif rt.target.mode() == "always":
                        reason = "群聊配置为全回"
                    else:
                        log.debug("群 %s 未命中 @/关键词（发送者 %s）", rt.display, self.sender_key(msg))
                        return
            else:
                if rt.is_group and not at_hit:
                    return
                text = "[%s] %s" % (mtype, text[:80])
                reason = "其它类型"

            log.info("处理消息 chat=%s type=%s 触发=%s", rt.display, mtype, reason)

            # ---------- 图片 -> 视觉识别 ----------
            # 注意：文字如果存在就保留，和识别结果拼在一起给模型 ——
            # 否则"@机器人 这是啥 + 图"会被拆成两次问答，又慢又各说各话。
            if image_msg is not None:
                path = self.fetcher.fetch(image_msg)
                if path:
                    try:
                        desc = self.model.vision(path)
                        note = "[图片识别结果] " + desc
                        text = (text + "\n" + note) if text else note
                        log.info("识图成功：%s", desc[:80])
                    except ModelError as e:
                        log.error("识图失败：%s", e)
                        note = "[对方发来一张图片，但识别失败]"
                        text = (text + "\n" + note) if text else note
                else:
                    note = "[对方发来一张图片，但没能取到图片文件]"
                    text = (text + "\n" + note) if text else note

            if not text:
                text = "[空消息]"

            sender = self.sender_key(msg)
            user_line = "%s: %s" % (sender, text) if rt.is_group else text

            if rt.is_group and self.cfg.group.reply_probability < 100:
                if random.randint(1, 100) > self.cfg.group.reply_probability:
                    log.info("群 %s 未通过回复概率 %d%%", rt.display, self.cfg.group.reply_probability)
                    return

            # 限流闸门放在**调模型之前** —— 被拦下的消息不该产生任何 token 费用
            allowed, why = self.limiter.allow(rt.username, self.sender_key(msg))
            if not allowed:
                log.warning("限流拦下一条（%s）：chat=%s sender=%s", why, rt.display, self.sender_key(msg))
                return

            rt.ctx.add_user(user_line)
            messages = rt.ctx.build_messages(self.system_prompt)
            t0 = time.time()
            try:
                reply = self.model.chat(messages)
            except ModelError as e:
                log.error("模型调用失败：%s", e)
                return
            log.info("模型 %.2fs 回复 %d 字", time.time() - t0, len(reply))

            # 群聊要不要 @ 发送者：at_member 要的是群成员昵称，回调里给的却是 wxid，
            # 换不到昵称就 @ 不出来，所以默认不做（旧包实际也不 @）。
            at_who = None
            if rt.is_group and self.cfg.group.at_reply:
                at_who = self._member_nick(msg)
            sent = self.sender.send(rt.display, reply, at=at_who)
            if sent:
                rt.ctx.add_assistant(reply)
            else:
                # 发送结果不确定时不重试：宁可漏发，也不要重复发。
                log.warning("回复发送未确认成功，未写入上下文，也不自动重发")
        except Exception as e:
            log.exception("处理消息时异常：%r", e)

    # ------------------------------------------------------------------ #
    def start(self):
        from wechatauto.db import Listener
        self.resolve_targets()
        if not self.targets:
            raise RuntimeError("没有任何有效的监听目标，请检查 config.yaml 的 listen 段")
        lst = Listener(self.db, interval=float(self.cfg.listener_interval))
        lst.add_all(self.on_message, discover=True)
        lst.start()
        log.info("监听已启动（数据库增量轮询，间隔 %.1fs），目标 %d 个",
                 self.cfg.listener_interval, len(self.targets))
        return lst
