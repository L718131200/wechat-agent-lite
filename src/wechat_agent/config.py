# -*- coding: utf-8 -*-
"""配置：YAML 只读加载 + 环境变量覆盖 + 启动期校验。

针对旧包的教训（wechatbot-new 的网页控制台点一次"保存"就会整体重写 config.py，
曾经把 LISTEN_LIST 清空）：
  * 本模块**只读**配置文件，程序任何路径都不会回写；
  * 字段缺失或类型不对时，启动就报清楚是哪个字段，而不是跑到一半才炸；
  * 密钥可以放环境变量，避免明文进 git。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .ratelimit import LimitConfig

ENV_LLM_KEY = "WAL_LLM_KEY"
ENV_VISION_KEY = "WAL_VISION_KEY"

ReplyMode = Literal["always", "at", "keyword"]


class ConfigError(Exception):
    """配置有问题时抛出，附带人能看懂的原因。"""


# --------------------------------------------------------------------------- #
# 子配置
# --------------------------------------------------------------------------- #
@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.8
    max_tokens: int = 1024
    timeout: int = 60
    # 关掉"思考模式"以降低延迟。DeepSeek 官方接口支持 thinking 参数；
    # 第三方网关不一定认识它，所以默认**不发送**，确认支持后再打开。
    disable_thinking: bool = False

    def resolved_key(self) -> str:
        return self.api_key or os.environ.get(ENV_LLM_KEY, "")


@dataclass
class VisionConfig:
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int = 512
    prompt: str = "用中文简要描述这张图片的内容，一两句话即可。"
    max_image_mb: float = 8.0
    # 小于这个体积的图片按表情/图标跳过（群里斗图不该拿去烧 token）。0 = 不过滤
    min_image_kb: float = 20.0
    # 视觉接口单独设超时：多模态推理明显比文本慢，复用文本超时会误杀
    timeout: int = 90
    # 下载图片的落地目录
    save_dir: str = "./data/media"
    # 本地图片保留小时数（0 = 不清理）。长期运行必须清，否则磁盘会被图片吃满
    keep_hours: float = 24.0

    def resolved_base_url(self, llm: LLMConfig) -> str:
        return self.base_url or llm.base_url

    def resolved_key(self, llm: LLMConfig) -> str:
        return self.api_key or os.environ.get(ENV_VISION_KEY, "") or llm.resolved_key()


@dataclass
class ListenTarget:
    chat: str
    type: Literal["private", "group"] = "private"
    reply: ReplyMode = "always"
    keyword: list[str] = field(default_factory=list)

    def mode(self) -> ReplyMode:
        """群聊默认按 @ 回复；私聊默认全回。"""
        if self.type == "group" and self.reply == "always":
            return "at"
        return self.reply


@dataclass
class GroupConfig:
    at_name: str = ""
    # 图片消息本身没有文字，永远不可能带 @。所以约定：
    # 群里被 @ 之后的 N 秒内，**同一个人**单独发来的图片，按"续接"处理。
    # 默认 30 秒：窗口越长，越容易把"下一个话题的图"错认成这次请求的续图。
    at_image_followup_sec: int = 30
    reply_probability: int = 100
    # 群聊回复时是否 @ 发送者。默认关：at_member 需要的是群成员**昵称**，
    # 而回调给的是 wxid，换不到昵称就 @ 不出来（旧包实际也不 @）。
    at_reply: bool = False
    # 一次 @ 之后最多接几张图。消息里明确提到"图/照片/截图/看这"时才用满这个额度，
    # 普通提问只留 1 张的余地 —— 防止"被 @ 一次就把后面所有图都吃进来"。
    image_followup_budget: int = 2


@dataclass
class ContextConfig:
    max_chars: int = 12000
    max_messages: int = 40
    store_dir: str = "./data/context"


@dataclass
class SendConfig:
    max_segments: int = 1
    segment_delay: float = 0.6
    split_token: str = "\\"


@dataclass
class PersonaConfig:
    prompt_file: str = "./prompts/default.md"


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = "./data/agent.log"
    encoding: str = "utf-8"


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    listen: list[ListenTarget] = field(default_factory=list)
    group: GroupConfig = field(default_factory=GroupConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    send: SendConfig = field(default_factory=SendConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    log: LogConfig = field(default_factory=LogConfig)
    limits: LimitConfig = field(default_factory=LimitConfig)
    # 引擎相关
    listener_interval: float = 1.0
    # 配置来源，便于日志里说明
    source_path: str = ""

    # ------------------------------------------------------------------ #
    def target_of(self, chat: str) -> ListenTarget | None:
        for t in self.listen:
            if t.chat == chat:
                return t
        return None

    def persona_text(self) -> str:
        p = Path(self.persona.prompt_file)
        if not p.is_absolute():
            p = Path(self.source_path).parent / p if self.source_path else p
        if not p.exists():
            raise ConfigError("人格提示词文件不存在：%s" % p)
        return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _need_type(value: Any, cls: type, where: str):
    if not isinstance(value, cls):
        raise ConfigError("%s 应该是 %s，实际是 %s" % (where, cls.__name__, type(value).__name__))
    return value


def _build(cls, data: dict | None, where: str):
    data = data or {}
    _need_type(data, dict, where)
    allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError("%s 里有不认识的字段：%s（检查拼写）" % (where, ", ".join(sorted(unknown))))
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError("%s 构造失败：%s" % (where, e)) from e


def load_config(path: str | Path) -> AppConfig:
    """读取 YAML 配置。这是唯一入口，且不会写回任何文件。"""
    p = Path(path)
    if not p.exists():
        raise ConfigError("配置文件不存在：%s" % p)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError("YAML 解析失败：%s" % e) from e
    _need_type(raw, dict, "顶层")

    cfg = AppConfig(
        llm=_build(LLMConfig, raw.get("llm"), "llm"),
        vision=_build(VisionConfig, raw.get("vision"), "vision"),
        group=_build(GroupConfig, raw.get("group"), "group"),
        context=_build(ContextConfig, raw.get("context"), "context"),
        send=_build(SendConfig, raw.get("send"), "send"),
        persona=_build(PersonaConfig, raw.get("persona"), "persona"),
        log=_build(LogConfig, raw.get("log"), "log"),
        limits=_build(LimitConfig, raw.get("limits"), "limits"),
        source_path=str(p.resolve()),
    )

    # listen 是列表，单独处理
    listens = raw.get("listen") or []
    _need_type(listens, list, "listen")
    if not listens:
        raise ConfigError("listen 是空的：至少要有一个人/群，否则机器人不会理任何人")
    for i, item in enumerate(listens):
        cfg.listen.append(_build(ListenTarget, item, "listen[%d]" % i))

    # listener_interval 顶格
    if "listener_interval" in raw:
        cfg.listener_interval = float(raw["listener_interval"])

    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    if not cfg.llm.resolved_key():
        raise ConfigError(
            "没有可用的对话模型密钥：请在 config.yaml 的 llm.api_key 填，"
            "或设环境变量 %s" % ENV_LLM_KEY)
    if cfg.vision.enabled and not cfg.vision.resolved_key(cfg.llm):
        raise ConfigError("vision.enabled=true 但没有可用密钥（vision.api_key / %s / llm.api_key）" % ENV_VISION_KEY)
    if cfg.send.max_segments < 1:
        raise ConfigError("send.max_segments 至少为 1")
    if cfg.context.max_chars < 500:
        raise ConfigError("context.max_chars 太小（<500），模型会没有上下文可用")
    for t in cfg.listen:
        if not t.chat:
            raise ConfigError("listen 里有空的 chat 名")
        if t.type not in ("private", "group"):
            raise ConfigError("listen[%s].type 只能是 private 或 group" % t.chat)
    if cfg.group.reply_probability < 0 or cfg.group.reply_probability > 100:
        raise ConfigError("group.reply_probability 应在 0-100")
