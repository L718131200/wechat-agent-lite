# -*- coding: utf-8 -*-
"""日志：UTF-8 + 轮转。

存在的理由：旧包 wechatbot-new 把日志写成 GBK，排查问题时满屏乱码，
连"这条错误到底说了什么"都要靠猜（实测踩过）。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "wechat_agent_lite"

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger。名字会自动挂在 wechat_agent_lite 下。"""
    base = logging.getLogger(LOGGER_NAME)
    if name:
        return base.getChild(name)
    return base


def setup_logger(
    level: str = "INFO",
    log_file: str | Path | None = None,
    encoding: str = "utf-8",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """初始化根 logger（幂等：重复调用不会叠 handler）。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # 控制台：Windows 下强制 UTF-8，否则中文会变问号
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(p), maxBytes=max_bytes, backupCount=backup_count,
            encoding=encoding,   # ← 关键：显式 UTF-8，不跟随系统 locale
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger
