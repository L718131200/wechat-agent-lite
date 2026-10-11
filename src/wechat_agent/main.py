# -*- coding: utf-8 -*-
"""入口。

用法：
    python -m wechat_agent -c config.yaml            # 正常跑
    python -m wechat_agent -c config.yaml --check    # 只自检，不发任何消息

--check 会依次验证：配置文件 → 日志 → 微信数据库能否连上 → 监听目标能否解析 →
模型接口能否调通。第一次部署建议先跑 --check，确认没问题再正式启动。
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .brain import Model, ModelError
from .config import ConfigError, load_config
from .logger import get_logger, setup_logger


def _banner():
    print("=" * 66)
    print(" wechat-agent-lite  v%s   微信 AI 机器人业务层" % __version__)
    print(" 引擎：wechatauto-replica（Apache-2.0）  本包：PolyForm Noncommercial 1.0.0")
    print("=" * 66)


def run_check(cfg, log) -> int:
    """自检：不发任何消息，只验证环境是否就绪。"""
    print("\n[1/5] 配置文件")
    print("      路径：%s" % cfg.source_path)
    print("      监听目标：%d 个" % len(cfg.listen))
    for t in cfg.listen:
        print("        - %-16s type=%-7s reply=%s" % (t.chat, t.type, t.mode()))
    print("      上下文上限：%d 字符 / %d 条" % (cfg.context.max_chars, cfg.context.max_messages))
    print("      分段上限：%d 段" % cfg.send.max_segments)
    lim = cfg.limits
    if lim.enabled:
        print("      限流：每用户 %d/分，每会话 %d/分，每日 %d 条，冷却 %.1fs"
              % (lim.per_user_per_minute, lim.per_chat_per_minute,
                 lim.daily_replies, lim.cooldown_seconds))
    else:
        print("      限流：**已关闭** —— 私聊 always 模式下会持续消耗 API 额度")
    print("      模型：%s @ %s" % (cfg.llm.model, cfg.llm.base_url))
    print("      视觉：%s（%s）" % (cfg.vision.model, "开" if cfg.vision.enabled else "关"))

    print("\n[2/5] 人格提示词")
    try:
        p = cfg.persona_text()
        print("      OK，%d 字" % len(p))
    except ConfigError as e:
        print("      失败：%s" % e)
        return 2

    print("\n[3/5] 微信数据库")
    try:
        from wechatauto.db import WeChatDB
        db = WeChatDB()
        info = db.get_self_info()
        print("      账号：%s (%s)" % (info.get("nick_name"), info.get("username")))
        sessions = db.get_sessions(limit=500)
        print("      可见会话：%d 个" % len(sessions))
    except Exception as e:
        print("      失败：%r" % (e,))
        print("      提示：微信 PC 版要处于登录状态，且本程序与微信在同一台机器上。")
        return 3

    print("\n[4/5] 监听目标解析（昵称 → username）")
    try:
        from .listener import Agent
        agent = Agent(cfg, db)
        resolved = agent.resolve_targets()
        if not resolved:
            print("      没有任何目标解析成功")
            return 4
        for u, rt in resolved.items():
            print("      %-16s -> %-28s %s" % (rt.display, u, "群" if rt.is_group else "私聊"))
    except Exception as e:
        print("      失败：%r" % (e,))
        return 4

    print("\n[5/5] 模型接口")
    try:
        m = Model(cfg)
        t0 = time.time()
        reply = m.chat([{"role": "user", "content": "只回复两个字：测试"}])
        print("      对话 OK（%.2fs）：%s" % (time.time() - t0, reply[:40]))
    except ModelError as e:
        print("      对话失败：%s" % e)
        return 5

    if cfg.vision.enabled:
        print("      视觉接口：跳过（需要真实图片才能验，正式跑以后看日志）")

    print("\n自检通过。可以去掉 --check 正式启动了。")
    return 0


def run_vision_test(cfg) -> int:
    """识图链路自测：拿数据库里**真实的历史图片**跑一次「下载 + 识别」。

    不解发任何消息 —— 所以可以在机器人正常运行时放心跑。
    这是唯一能不发消息就验证 MediaDownloader 与视觉接口的办法。
    """
    print("\n=== 识图链路自测（用数据库里的历史图片，不发任何消息）===")
    try:
        from wechatauto.db import WeChatDB
    except Exception as e:
        print("  连不上引擎：%r" % (e,))
        return 3
    from .brain import Model, ModelError
    from .vision import ImageFetcher

    db = WeChatDB()
    fetcher = ImageFetcher(cfg, db)
    model = Model(cfg)

    sessions = db.get_sessions(limit=500)
    print("  可见会话 %d 个，逐个找最近的图片…" % len(sessions))
    found = None
    for s in sessions:
        u = s.get("username")
        if not u:
            continue
        try:
            rows = db.get_image_rows(u, limit=3)
        except Exception:
            continue
        if rows:
            found = (u, rows[0])
            break
    if not found:
        print("  数据库里没找到任何图片行，无法验证（先让人发张图再来）。")
        return 0

    user, row = found
    import datetime as _dt
    ts = row.get("create_time") or 0
    when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
    print("  取样：会话=%s  local_id=%s  type=%s  时间=%s"
          % (user, row.get("local_id"), row.get("type"), when))

    msg = {
        "username": user,
        "local_id": row.get("local_id"),
        "type": row.get("type"),
        "shard": row.get("shard"),
    }
    t0 = time.time()
    path = fetcher.fetch(msg)
    t1 = time.time()
    if not path:
        print("  下载失败（%.2fs）—— 本机可能只有缩略图，或图还没落盘" % (t1 - t0))
        return 6
    print("  下载 OK（%.2fs）：%s" % (t1 - t0, path))

    try:
        desc = model.vision(path)
    except ModelError as e:
        print("  识别失败（%.2fs）：%s" % (time.time() - t1, e))
        return 7
    print("  识别 OK（%.2fs）" % (time.time() - t1))
    print("  识别结果：%s" % desc[:300])
    print("\n识图链路（下载 + 识别）通过。")
    return 0


class _DrySender:
    """干跑用假发送器：只记录，绝不碰微信窗口。

    存在的意义：可以在**不打扰正在运行的其它机器人**的前提下，
    把整条业务链路（触发判定 → 上下文 → 模型 → 回复文本）跑一遍。
    """

    def __init__(self):
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, chat: str, text: str, at: str | None = None) -> bool:
        self.sent.append((chat, text, at))
        return True


def run_simulate(cfg, chat: str, text: str | None, image_local_id: int | None) -> int:
    """干跑：用一条构造消息走完整业务链路，但**不真的发微信**。

    能验证：目标解析、触发判定（@ / 关键词 / 等图登记）、上下文拼装、
    模型调用、图片下载 + 视觉识别。**不能**验证引擎的回调本身和 GUI 发送
    ——那两件是引擎的职责，需要真机实跑。
    """
    print("\n=== 干跑（不会发送任何微信消息）===")
    try:
        from wechatauto.db import WeChatDB
    except Exception as e:
        print("  连不上引擎：%r" % (e,))
        return 3

    from .listener import Agent

    db = WeChatDB()
    agent = Agent(cfg, db)
    agent.resolve_targets()

    # 找到目标会话的 username（配置里写的是昵称）
    if chat in agent.targets:
        username = chat
    else:
        hits = db.search_contact(chat)
        username = hits[0]["username"] if hits else chat
    rt = agent.targets.get(username)
    if rt is None:
        print("  会话 %r 解析成 %r，但它不在 listen 配置里。当前目标：" % (chat, username))
        for u, t in agent.targets.items():
            print("      %s -> %s" % (t.display, u))
        return 4

    print("  目标会话：%s (%s, %s)" % (rt.display, rt.username, "群" if rt.is_group else "私聊"))

    # 用数据库里真实的最近一条消息当模板，保证字段形状与线上一致
    tpl = {}
    try:
        rows = db.get_messages(rt.username, limit=1)
        tpl = rows[0] if rows else {}
    except Exception as e:
        print("  取模板消息失败（忽略）：%r" % e)

    now = int(time.time())
    if image_local_id is not None:
        msg = {
            "username": rt.username, "type": "图片", "content": "",
            "sender_id": 1, "sender_username": "wxid_simulated_sender",
            "local_id": int(image_local_id), "shard": tpl.get("shard"),
            "create_time": now,
        }
        print("  模拟消息：图片 local_id=%s" % image_local_id)
    else:
        msg = {
            "username": rt.username, "type": "文本", "content": text or "",
            "sender_id": 1, "sender_username": "wxid_simulated_sender",
            "local_id": 0, "shard": tpl.get("shard"),
            "create_time": now,
        }
        print("  模拟消息：文本 %r" % (text or ""))

    dry = _DrySender()
    agent.sender = dry          # 换掉发送器：只记录，不动窗口

    t0 = time.time()
    agent.on_message(msg, None)
    dt = time.time() - t0

    print("\n  ---- 结果 ----")
    print("  总耗时：%.2fs" % dt)
    if dry.sent:
        for i, (c, t, at) in enumerate(dry.sent, 1):
            print("  将发送 %d/%d -> %s%s：" % (i, len(dry.sent), c, (" @ " + at) if at else ""))
            print("      " + t.replace("\n", "\n      "))
    else:
        print("  没有产生回复（被触发判定或限流拦下 —— 看上面的日志行）")
    print("\n  注意：以上内容**没有真的发出去**，只是干跑。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wechat_agent", description="微信 AI 机器人业务层")
    ap.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    ap.add_argument("--check", action="store_true", help="只自检，不发任何消息")
    ap.add_argument("--test-vision", action="store_true",
                    help="拿数据库里最近一张真实图片跑一次「下载+识别」（不发消息）")
    ap.add_argument("--simulate", metavar="会话名",
                    help="干跑：把构造消息喂给业务链路，但**不真的发微信**（不停其它机器人也能用）")
    ap.add_argument("--text", default="你好", help="配合 --simulate：模拟收到的文本")
    ap.add_argument("--image-local-id", type=int, default=None,
                    help="配合 --simulate：改成模拟收到一张图片（传数据库里的 local_id）")
    ap.add_argument("--version", action="version", version="wechat-agent-lite %s" % __version__)
    args = ap.parse_args(argv)

    _banner()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        ex = cfg_path.parent / "config.example.yaml"
        print("找不到配置文件：%s" % cfg_path)
        if ex.exists():
            print("可以复制它当模板：cp %s %s" % (ex, cfg_path))
        return 1

    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        print("配置有问题：%s" % e)
        return 1

    log = setup_logger(cfg.log.level, cfg.log.file, cfg.log.encoding)
    log.info("wechat-agent-lite v%s 启动，配置 %s", __version__, cfg_path)

    if args.check:
        return run_check(cfg, log)
    if args.test_vision:
        return run_vision_test(cfg)
    if args.simulate:
        return run_simulate(cfg, args.simulate, args.text, args.image_local_id)

    # ---- 正式运行 ----
    try:
        from wechatauto.db import WeChatDB
        db = WeChatDB()
    except Exception as e:
        log.error("连不上微信数据库：%r", e)
        log.error("确认微信 PC 版已登录，且本程序和微信在同一台机器上。")
        return 3

    from .listener import Agent
    agent = Agent(cfg, db)
    try:
        lst = agent.start()
    except Exception as e:
        log.error("启动监听失败：%r", e)
        return 4

    stop = {"flag": False}

    def _on_signal(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    log.info("已就绪，按 Ctrl+C 退出。")
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            lst.stop()
        except Exception:
            pass
        log.info("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
