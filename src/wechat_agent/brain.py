# -*- coding: utf-8 -*-
"""模型调用：对话 + 视觉，走 OpenAI 兼容的 /chat/completions。

为什么不引 openai SDK：
  * 旧包 wechatbot-new 把 openai 钉死在 ==1.84.0，升级别的库就容易冲突；
  * 我们只需要一个 POST，用 requests 直发，行为完全可控、可打日志、可计时。

实测踩过的坑（写在这里免得后人再踩）：
  * **不要用「推理模式」的模型名做识图**。实测 deepseek-flash 会把 max_tokens
    全部烧在 reasoning_tokens 上，content 返回空字符串，而且 HTTP 200 不报错。
    同样请求换成 deepseek-chat 就正常。
  * 图片走 base64 data-URI，请求体会膨胀约 1/3，大图要限流。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path

import requests

from .config import AppConfig
from .logger import get_logger

log = get_logger("brain")


class ModelError(Exception):
    """模型调用失败（网络/鉴权/返回结构不对），带人能看懂的原因。"""


class Model:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def chat(self, messages: list[dict], *, temperature: float | None = None,
             max_tokens: int | None = None) -> str:
        llm = self.cfg.llm
        key = llm.resolved_key()
        if not key:
            raise ModelError("没有可用的对话模型密钥")
        payload = {
            "model": llm.model,
            "messages": messages,
            "temperature": llm.temperature if temperature is None else temperature,
            "max_tokens": llm.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if llm.disable_thinking:
            # DeepSeek 官方接口用这个关思考模式（能显著降延迟）；
            # 其它网关可能不认，所以默认不发，由配置显式开启。
            payload["thinking"] = {"type": "disabled"}
        url = llm.base_url.rstrip("/") + "/chat/completions"
        t0 = time.time()
        try:
            r = requests.post(url, headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            }, data=json.dumps(payload).encode("utf-8"), timeout=llm.timeout)
        except Exception as e:
            raise ModelError("请求失败 %s: %r" % (url, e)) from e
        dt = time.time() - t0
        if r.status_code != 200:
            raise ModelError("HTTP %d: %s" % (r.status_code, r.text[:300]))
        try:
            data = r.json()
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
        except Exception as e:
            raise ModelError("返回结构不对: %r / %s" % (e, r.text[:200])) from e

        usage = data.get("usage") or {}
        finish = data["choices"][0].get("finish_reason")
        log.info("chat 完成 %.2fs model=%s tokens=%s finish=%s",
                 dt, data.get("model"), usage.get("total_tokens"), finish)
        if not text:
            # 这种情况多半是"思考 token 吃光了输出预算"，不是网络问题。
            # 把 finish_reason 和 reasoning 长度一起报出来，方便定位。
            reasoning = (msg.get("reasoning_content") or "")
            details = usage.get("completion_tokens_details") or {}
            raise ModelError(
                "模型返回空内容（finish_reason=%s, reasoning_tokens=%s, reasoning 长度=%d）。"
                "若是思考模式模型，请换非思考模型、关掉 thinking，或调大 max_tokens"
                % (finish, details.get("reasoning_tokens"), len(reasoning)))
        return text

    # ------------------------------------------------------------------ #
    # 视觉
    # ------------------------------------------------------------------ #
    def vision(self, image_path: str | Path, prompt: str | None = None) -> str:
        v = self.cfg.vision
        if not v.enabled:
            raise ModelError("vision.enabled = false")
        p = Path(image_path)
        if not p.exists():
            raise ModelError("图片不存在: %s" % p)
        size_mb = p.stat().st_size / 1024 / 1024
        if size_mb > v.max_image_mb:
            raise ModelError("图片 %.1fMB 超过上限 %.1fMB" % (size_mb, v.max_image_mb))

        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode()
        key = v.resolved_key(self.cfg.llm)
        url = v.resolved_base_url(self.cfg.llm).rstrip("/") + "/chat/completions"
        payload = {
            "model": v.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or v.prompt},
                    {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                ],
            }],
            "temperature": v.temperature,
            "max_tokens": v.max_tokens,
            "stream": False,
        }
        t0 = time.time()
        try:
            r = requests.post(url, headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            }, data=json.dumps(payload).encode("utf-8"), timeout=self.cfg.llm.timeout)
        except Exception as e:
            raise ModelError("视觉请求失败: %r" % (e,)) from e
        dt = time.time() - t0
        if r.status_code != 200:
            raise ModelError("视觉 HTTP %d: %s" % (r.status_code, r.text[:300]))
        try:
            data = r.json()
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
        except Exception as e:
            raise ModelError("视觉返回结构不对: %r" % (e,)) from e
        log.info("vision 完成 %.2fs model=%s 图=%.1fKB", dt, data.get("model"), size_mb * 1024)
        if not text:
            raise ModelError("视觉模型返回空内容（多半是推理模式吃光了 max_tokens）")
        return text
