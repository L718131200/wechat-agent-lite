# -*- coding: utf-8 -*-
"""把本地完整的提交历史用 REST API 推到远端（不经过 git push）。

背景：首次发布是用 Git Data API 推的"当前快照"，所以远端只有 1 个提交，
看起来像一次性生成的。而完整历史对开源项目有价值（能看出迭代过程），
且 SSH 通道要等用户加公钥才能用 —— 所以这里用 API 把 10 个提交原样重放。

关键点：
  * 用 `git show <sha>:<path>` 只读地取每个提交下的文件内容，**绝不 checkout**，
    所以不会动工作区。
  * blob 按内容 sha1 缓存：多数文件在多个提交间没变，能省掉大量上传。
  * 强制 core.autocrlf=false，避免 Windows 上行尾被改写导致内容与本地不一致。
"""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

TOKEN_FILE = r"E:\dsh\.keys\github-token.txt"
REPO_DIR = r"E:\dsh\wechat-agent-lite"
OWNER, NAME = "L718131200", "wechat-agent-lite"
GIT = ["git", "-c", "core.autocrlf=false"]

tok = io.open(TOKEN_FILE, encoding="utf-8").read().strip()
H = {"Authorization": "token " + tok, "User-Agent": "wal-history",
     "Accept": "application/vnd.github+json", "Content-Type": "application/json"}


def api(method, path, body=None, timeout=90):
    req = urllib.request.Request("https://api.github.com" + path,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def git(*args):
    out = subprocess.run(GIT + list(args), cwd=REPO_DIR, capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("git %s 失败: %s" % (" ".join(args),
                                               (out.stderr or b"").decode("utf-8", "replace")[:300]))
    return out.stdout


print("=== 用 API 重放完整提交历史 %s ===" % time.strftime("%H:%M:%S"))

# 1) 取本地历史（从旧到新）
raw = git("log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e").decode("utf-8", "replace")
commits = []
for chunk in raw.split("\x1e"):
    chunk = chunk.strip("\n\ufeff")
    if not chunk.strip():
        continue
    parts = chunk.split("\x1f")
    if len(parts) >= 2:
        commits.append({"sha": parts[0].strip(),
                        "subject": parts[1].strip(),
                        "body": (parts[2].strip() if len(parts) > 2 else "")})
print("本地提交 %d 个" % len(commits))

# 2) 远端当前 HEAD（作为第一个提交的父）
st, ref = api("GET", "/repos/%s/%s/git/ref/heads/main" % (OWNER, NAME))
if st != 200:
    print("!! 取远端 ref 失败 %s %s" % (st, ref))
    raise SystemExit(1)
parent = ref["object"]["sha"]
print("远端当前 HEAD = %s" % parent[:12])

blob_cache = {}      # "path:sha1" -> github blob sha；按内容去重
blob_uploaded = 0

for i, c in enumerate(commits, 1):
    files = git("ls-tree", "-r", "--name-only", c["sha"]).decode("utf-8", "replace").splitlines()
    files = [f.strip() for f in files if f.strip()]
    tree_items = []
    for rel in files:
        content = git("show", "%s:%s" % (c["sha"], rel))       # 只读，不碰工作区
        digest = hashlib.sha1(content).hexdigest()
        key = rel + ":" + digest
        bsha = blob_cache.get(key)
        if bsha is None:
            st, blob = api("POST", "/repos/%s/%s/git/blobs" % (OWNER, NAME), {
                "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
            if st not in (200, 201):
                print("!! blob 失败 %s %s -> %s" % (rel, st, blob))
                raise SystemExit(2)
            bsha = blob["sha"]
            blob_cache[key] = bsha
            blob_uploaded += 1
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": bsha})

    st, tree = api("POST", "/repos/%s/%s/git/trees" % (OWNER, NAME), {"tree": tree_items})
    if st not in (200, 201):
        print("!! tree 失败 %s %s" % (st, tree))
        raise SystemExit(3)

    msg = c["subject"] + (("\n\n" + c["body"]) if c["body"] else "")
    st, cm = api("POST", "/repos/%s/%s/git/commits" % (OWNER, NAME),
                 {"message": msg, "tree": tree["sha"], "parents": [parent]})
    if st not in (200, 201):
        print("!! commit 失败 %s %s" % (st, cm))
        raise SystemExit(4)
    parent = cm["sha"]
    print("  [%2d/%d] %-52s %s  (%d 文件)" % (
        i, len(commits), c["subject"][:52], cm["sha"][:10], len(files)))

# 3) 把 main 指到最新
st, r = api("PATCH", "/repos/%s/%s/git/refs/heads/main" % (OWNER, NAME),
            {"sha": parent, "force": True})
if st not in (200, 201):
    print("!! 更新 main 失败 %s %s" % (st, r))
    raise SystemExit(5)

print("")
print("=== 完成 ===")
print("重放提交 %d 个，实际新建 blob %d 个（其余按内容命中缓存）" % (len(commits), blob_uploaded))
print("远端 main 现在指向 %s" % parent[:12])
