# -*- coding: utf-8 -*-
"""用 Python 设仓库 topics 与 description（PowerShell 的 ConvertTo-Json 会把中文变 ?）。"""
import io
import json
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

tok = io.open(r"E:\dsh\.keys\github-token.txt", encoding="utf-8").read().strip()
H = {"Authorization": "token " + tok, "User-Agent": "wal",
     "Accept": "application/vnd.github+json", "Content-Type": "application/json"}
REPO = "L718131200/wechat-agent-lite"


def api(method, path, body=None):
    req = urllib.request.Request("https://api.github.com" + path,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                                 headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


# 1) description（走 PATCH /repos，中文用 ensure_ascii=False 才不会变问号）
desc = ("微信 AI 机器人的业务层：谁在说、该不该回、回什么、怎么切句。"
        "收发消息与操作微信窗口交给 wechatauto-replica 引擎。PolyForm Noncommercial 1.0.0。")
st, r = api("PATCH", "/repos/" + REPO, {"description": desc})
print("description 更新：%s" % ("成功" if st in (200, 201) else "失败 %s %s" % (st, r)))
if st in (200, 201):
    print("  现在显示为：%s" % r.get("description"))

# 2) topics 有自己的端点：PUT /repos/{owner}/{repo}/topics
topics = ["wechat", "wechat-bot", "automation", "rpa", "uia", "python",
          "llm", "deepseek", "chatbot", "image-recognition",
          "windows", "ai-agent", "rich-media-message", "wechat-automation"]
st2, r2 = api("PUT", "/repos/%s/topics" % REPO, {"names": topics})
if st2 in (200, 201):
    print("topics 已设置：%s" % ", ".join(r2.get("names", [])))
else:
    print("topics 失败 %s %s" % (st2, r2))

# 3) 回读确认
st3, r3 = api("GET", "/repos/" + REPO)
if st3 == 200:
    print("")
    print("回读确认：")
    print("  description: %s" % r3.get("description"))
    print("  topics     : %s" % ", ".join(r3.get("topics") or []))
    print("  可见性     : %s" % ("公开" if not r3.get("private") else "私有"))
