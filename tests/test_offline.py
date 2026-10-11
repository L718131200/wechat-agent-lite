# -*- coding: utf-8 -*-
"""不依赖微信的离线自测：验证配置、分段、上下文裁剪这三块纯逻辑。

设计取向：把"能在没有微信的机器上跑"的部分都测出来，真机相关的留在实跑阶段验。
    python -m pytest tests/ -q
或者不装 pytest，直接：
    python tests/test_offline.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows 控制台默认按 GBK 解码，直接 print 中文会变乱码（旧包就是被这个坑过）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_agent.config import AppConfig, ConfigError, LLMConfig, load_config  # noqa: E402
from wechat_agent.context import ContextStore                             # noqa: E402
from wechat_agent.listener import strip_sender_prefix                     # noqa: E402
from wechat_agent.sender import split_text                                # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAILS.append(name)


# --------------------------------------------------------------------------- #
def test_strip_prefix():
    print("\n== 群聊内容前缀剥离 ==")
    check("剥 wxid 前缀", strip_sender_prefix("wxid_abc123:\n独眼鱼啊") == "独眼鱼啊")
    check("无前缀不动", strip_sender_prefix("是白饭还是鱼鱼") == "是白饭还是鱼鱼")
    check("空串安全", strip_sender_prefix("") == "")


def test_split():
    print("\n== 分段控制 ==")
    check("max=1 不拆", split_text("你好\\再见", 1) == ["你好\\再见"])
    check("max=2 拆两段", split_text("你好\\再见", 2) == ["你好", "再见"])
    check("超限合并到末段", split_text("a\\b\\c\\d", 2) == ["a", "b c d"])
    check("空文本", split_text("", 3) == [])
    check("全是分隔符", split_text("\\\\", 3) == ["\\\\"])


def test_context():
    print("\n== 上下文裁剪 ==")
    with tempfile.TemporaryDirectory() as d:
        c = ContextStore("测试群", d, max_chars=200, max_messages=50)
        for i in range(20):
            c.add_user("用户消息%02d" % i)
            c.add_assistant("回复%02d" % i)
        msgs = c.build_messages("系统提示")
        check("首条是 system", msgs[0]["role"] == "system")
        check("裁剪后条数受控", len(msgs) <= 22, "实际 %d" % len(msgs))
        body = "".join(m["content"] for m in msgs)
        check("保留的是最新内容", "用户消息19" in body)
        check("最旧的被裁掉", "用户消息00" not in body)
        # 落盘后能读回
        c2 = ContextStore("测试群", d, max_chars=200, max_messages=50)
        check("落盘可读回", len(c2.turns) == len(c.turns), "%d vs %d" % (len(c2.turns), len(c.turns)))
        # 上限调大后，历史仍在（因为落盘保存的是完整历史）
        msgs2 = c2.build_messages("系统提示")
        check("完整历史未丢", c2.stats()["turns"] == 40)


def test_image_tracker():
    """群聊等图归属 —— 这条路径一旦写错就是"把别人的图片传给远程模型"，必须测。"""
    print("\n== 群聊等图归属（隐私关键路径）==")
    from wechat_agent.listener import ImageRequestTracker

    T0 = 1000.0

    # 1) A @ 之后 B 发图：绝不能被吃掉
    tr = ImageRequestTracker(ttl=60, budget=2)
    tr.open("群", "A", "帮我看看这张图", now=T0)
    check("B 的图不命中", tr.consume("群", "B", now=T0 + 5) is None)
    check("B 发图后 A 的登记仍在", tr.pending_count() == 1)
    check("A 自己的图命中", tr.consume("群", "A", now=T0 + 6) is not None)

    # 2) 额度：提到"图"才给满，普通提问只留 1 张
    tr2 = ImageRequestTracker(ttl=60, budget=3)
    check("提到图 -> 满额度", tr2.open("群", "A", "看这几张图", now=T0).budget == 3)
    check("没提图 -> 只 1 张", tr2.open("群", "B", "在吗", now=T0).budget == 1)

    # 3) 额度耗尽后不再接
    tr3 = ImageRequestTracker(ttl=60, budget=1)
    tr3.open("群", "A", "看图", now=T0)
    check("第一张命中", tr3.consume("群", "A", now=T0 + 1) is not None)
    check("第二张不再命中", tr3.consume("群", "A", now=T0 + 2) is None)

    # 4) 超时
    tr4 = ImageRequestTracker(ttl=30, budget=2)
    tr4.open("群", "A", "看图", now=T0)
    check("超时后不命中", tr4.consume("群", "A", now=T0 + 31) is None)
    check("超时后登记被清", tr4.pending_count() == 0)

    # 5) 先发图、再 @（常见顺序）
    tr5 = ImageRequestTracker(ttl=60, budget=2)
    tr5.note_image("群", "A", {"local_id": 1}, now=T0)
    got = tr5.take_recent("群", "A", now=T0 + 10)
    check("先图后 @ 能取回", got is not None and got.get("local_id") == 1)
    check("取回后即清除", tr5.take_recent("群", "A", now=T0 + 11) is None)
    tr5.note_image("群", "A", {"local_id": 2}, now=T0)
    check("超时的最近图丢弃", tr5.take_recent("群", "A", now=T0 + 61) is None)

    # 6) 跨群隔离
    tr6 = ImageRequestTracker(ttl=60, budget=2)
    tr6.open("群1", "A", "看图", now=T0)
    check("另一个群不命中", tr6.consume("群2", "A", now=T0 + 1) is None)


def test_config():
    print("\n== 配置加载与校验 ==")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "prompts").mkdir()
        (d / "prompts" / "p.md").write_text("人格", encoding="utf-8")
        good = d / "config.yaml"
        good.write_text(
            "llm:\n  api_key: sk-test\n  model: deepseek-chat\n"
            "listen:\n  - chat: 某人\n    type: private\n"
            "persona:\n  prompt_file: ./prompts/p.md\n", encoding="utf-8")
        cfg = load_config(good)
        check("加载成功", isinstance(cfg, AppConfig))
        check("默认 max_segments=1", cfg.send.max_segments == 1)
        check("私聊默认 always", cfg.listen[0].mode() == "always")
        check("人格可读", cfg.persona_text() == "人格")

        # 群聊默认转成 at
        g = d / "g.yaml"
        g.write_text("llm:\n  api_key: sk-t\nlisten:\n  - chat: 群\n    type: group\n"
                     "persona:\n  prompt_file: ./prompts/p.md\n", encoding="utf-8")
        check("群聊默认按 @", load_config(g).listen[0].mode() == "at")

        # 缺 key
        bad = d / "bad.yaml"
        bad.write_text("listen:\n  - chat: x\n", encoding="utf-8")
        try:
            load_config(bad)
            check("缺密钥应报错", False)
        except ConfigError as e:
            check("缺密钥应报错", "密钥" in str(e), str(e))

        # 错字段名
        typo = d / "typo.yaml"
        typo.write_text("llm:\n  api_key: sk-t\n  modell: x\n"
                        "listen:\n  - chat: x\n", encoding="utf-8")
        try:
            load_config(typo)
            check("错字段名应报错", False)
        except ConfigError as e:
            check("错字段名应报错", "不认识的字段" in str(e), str(e))

        # listen 为空
        empty = d / "empty.yaml"
        empty.write_text("llm:\n  api_key: sk-t\nlisten: []\n", encoding="utf-8")
        try:
            load_config(empty)
            check("空 listen 应报错", False)
        except ConfigError as e:
            check("空 listen 应报错", "listen" in str(e), str(e))


def main() -> int:
    print("wechat-agent-lite 离线自测")
    test_strip_prefix()
    test_split()
    test_context()
    test_image_tracker()
    test_config()
    print("\n" + "=" * 50)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
