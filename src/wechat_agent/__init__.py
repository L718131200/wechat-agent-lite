# -*- coding: utf-8 -*-
"""wechat-agent-lite —— 微信 AI 机器人的业务层。

只做业务：监听、触发判定、上下文、模型调用、图片识别、分段发送。
收发和 UI 操作交给引擎 `wechatauto-replica`（Apache-2.0），本包不重写那部分。
"""
from __future__ import annotations

__version__ = "0.1.0"

from .config import AppConfig, ConfigError, load_config
from .logger import get_logger, setup_logger

__all__ = ["AppConfig", "ConfigError", "load_config", "get_logger", "setup_logger", "__version__"]
