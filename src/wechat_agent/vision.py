# -*- coding: utf-8 -*-
"""图片：从微信数据库把图取出来 → 交给视觉模型 → 返回能进上下文的文字。

真机实测结论（2026-10-11，微信 4.1.15 + wechatauto-replica 1.2.6.1）：
  * 图片消息在库里 `local_type == 3`，字段含 ``local_id`` / ``shard`` / ``packed_info``；
  * 入口是 ``MediaDownloader(db, save_dir)``，调用
    ``download_image(user, local_id, save_dir=..., tier=..., shard=...)``，返回落盘路径或 None；
  * ``tier`` 的坑（引擎 docstring 自己写的）：``'original'`` 只认"发的时候勾了原图"那一档，
    实测自发图 554 条里只有 19 条有原图、别人发的 3401 条里 611 条有 —— 本机多数图**没有**原图，
    所以日常识图应该用 ``'best'``（原图 > 压缩版 > 缩略图），而不是 ``'original'``；
  * ``shard`` 要把消息行里的分片名原样传回，跨分片同号时才能锁到正确那条。
"""
from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .logger import get_logger

log = get_logger("vision")

# 引擎里表示「图片」的消息类型（实测是中文，不是 'image'）
IMAGE_TYPES = {"图片", "image"}
EMOJI_TYPES = {"动画表情", "表情", "emotion"}


class ImageFetcher:
    """把图片消息落盘。延迟导入引擎，方便离线自测。"""

    def __init__(self, cfg: AppConfig, db):
        self.cfg = cfg
        self.db = db
        self.save_dir = Path(cfg.vision.save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._dl = None

    def _ensure(self):
        if self._dl is None:
            from wechatauto.media import MediaDownloader
            self._dl = MediaDownloader(self.db, save_dir=str(self.save_dir))
            log.info("MediaDownloader 就绪，落地目录 %s", self.save_dir)
        return self._dl

    # ------------------------------------------------------------------ #
    def fetch(self, msg: dict) -> str | None:
        """下载一条图片消息，返回本地路径；失败返回 None。"""
        user = msg.get("username") or msg.get("chat")
        local_id = msg.get("local_id")
        if not user or local_id is None:
            log.warning("图片消息缺 username/local_id：%s", {k: msg.get(k) for k in ("username", "local_id", "type")})
            return None
        try:
            path = self._ensure().download_image(
                user, int(local_id),
                save_dir=str(self.save_dir),
                tier="best",                      # 有什么给什么，不要死等原图
                shard=msg.get("shard"),
            )
        except Exception as e:
            log.error("图片下载异常 user=%s local_id=%s: %r", user, local_id, e)
            return None
        if not path:
            log.warning("图片下载返回空 user=%s local_id=%s（本机可能只有缩略图或尚未落盘）", user, local_id)
            return None
        if not Path(path).exists():
            log.warning("图片路径不存在：%s", path)
            return None
        log.info("图片已落盘 %s (%.1f KB)", Path(path).name, Path(path).stat().st_size / 1024)
        return str(path)
